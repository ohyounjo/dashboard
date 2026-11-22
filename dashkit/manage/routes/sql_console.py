"""읽기 전용 SQL 콘솔 경로(POST /api/manage/sql, MANAGE_FEATURES=sql). contracts/manage.md."""
from __future__ import annotations

import time
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from ... import db, paging
from ...api.data import _TYPE_BY_OID
from ...api.errors import ApiError
from ...auth import store as auth_store
from ...auth.routes import Principal, client_ip, require_operator
from ...features import has_manage_feature
from ...ingest import IngestError
from .. import context
from ..sql_guard import prepare_sql
from .rows import _to_json
from .tables import _json, _project

router = APIRouter()

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_MAX_OFFSET = 100_000


def _int_param(value: Any, name: str, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise IngestError(422, "invalid_query", f"{name}은 정수여야 합니다")
    if not (minimum <= value <= maximum):
        raise IngestError(422, "invalid_query", f"{name}은 {minimum}..{maximum} 범위여야 합니다")
    return value


def _audit_query(
    principal: Principal,
    request: Request,
    project_name: str,
    sql_text: str,
    *,
    rows: int,
    elapsed_ms: int,
    has_more: bool | None = None,
    limit: int | None = None,
    offset: int | None = None,
    error: str | None = None,
) -> None:
    detail: dict[str, Any] = {
        "project": project_name,
        "sql": sql_text[:2000],
        "rows": rows,
        "elapsed_ms": elapsed_ms,
    }
    if error is not None:
        detail["error"] = error
    else:
        detail["has_more"] = has_more
        detail["limit"] = limit
        detail["offset"] = offset
    try:
        auth_store.record_audit(
            "data.query.run",
            actor_id=principal.id,
            actor_email=principal.email,
            target_type="sql",
            target_id=None,
            target_label=project_name,
            detail=detail,
            ip=client_ip(request),
        )
    except Exception:
        pass


def _execute_sql(guarded_sql: str, limit: int, offset: int) -> dict[str, Any]:
    timeout = max(int(context.limits().statement_timeout_ms), 1)
    lock_timeout = min(timeout, 5000)
    opts = (
        f"-c default_transaction_read_only=on "
        f"-c statement_timeout={timeout} "
        f"-c lock_timeout={lock_timeout} "
        f"-c search_path=raw,dash,dashkit_meta"
    )
    try:
        conn = db.inspect_conn(application_name="dashkit-manage-sql", options=opts)
    except (psycopg.Error, RuntimeError):
        raise IngestError(503, "db_unavailable", "조회용 DB 계정으로 접속하지 못했습니다(make data-init)") from None

    try:
        with conn.cursor() as cur:
            cur.execute(guarded_sql, (limit + 1, offset))
            desc = cur.description or []
            columns = [{"name": d.name, "type": _TYPE_BY_OID.get(d.type_code, "text")} for d in desc]
            fetched = cur.fetchmany(limit + 1)
    except psycopg.errors.QueryCanceled:
        raise IngestError(504, "query_timeout", "쿼리가 시간 제한을 넘었습니다. 조건을 좁혀 주세요") from None
    except psycopg.errors.InsufficientPrivilege:
        raise IngestError(403, "forbidden", "접근 권한이 없는 스키마·테이블 또는 열입니다") from None
    except psycopg.errors.ReadOnlySqlTransaction:
        raise IngestError(422, "invalid_query", "읽기 전용 모드에서는 데이터를 변경할 수 없습니다") from None
    except psycopg.Error as exc:
        reason = exc.diag.message_primary or type(exc).__name__
        raise IngestError(422, "invalid_query", f"쿼리를 실행할 수 없습니다: {reason}") from None
    finally:
        conn.close()

    has_more = len(fetched) > limit
    sliced = fetched[:limit]
    rows: list[list[Any]] = []
    truncated_cells: list[list[int]] = []
    for row_idx, raw_row in enumerate(sliced):
        out_row: list[Any] = []
        for col_idx, (val, col) in enumerate(zip(raw_row, columns)):
            cell = _to_json(val, col["type"])
            if isinstance(cell, str) and len(cell) > paging.CELL_CHARS:
                cell = cell[: paging.CELL_CHARS]
                truncated_cells.append([row_idx, col_idx])
            out_row.append(cell)
        rows.append(out_row)

    return {
        "columns": columns,
        "rows": rows,
        "has_more": has_more,
        "truncated_cells": truncated_cells,
    }


@router.post("/sql")
async def post_sql(request: Request, principal: Principal = Depends(require_operator)) -> dict[str, Any]:
    if not has_manage_feature("sql"):
        raise ApiError(404, "not_found", "비활성화된 기능입니다")
    project = _project()
    body = await _json(request)
    raw_sql = body.get("sql")
    limit = _int_param(body.get("limit"), "limit", default=_DEFAULT_LIMIT, minimum=1, maximum=_MAX_LIMIT)
    offset = _int_param(body.get("offset"), "offset", default=0, minimum=0, maximum=_MAX_OFFSET)

    started = time.monotonic()
    sql_for_audit = raw_sql.strip() if isinstance(raw_sql, str) else ""
    try:
        guarded_sql, _cleaned = prepare_sql(raw_sql)
        result = await run_in_threadpool(_execute_sql, guarded_sql, limit, offset)
    except IngestError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if sql_for_audit:
            _audit_query(principal, request, project.name, sql_for_audit, rows=0, elapsed_ms=elapsed_ms, error=exc.detail)
        raise

    elapsed_ms = int((time.monotonic() - started) * 1000)
    _audit_query(
        principal,
        request,
        project.name,
        sql_for_audit,
        rows=len(result["rows"]),
        elapsed_ms=elapsed_ms,
        has_more=result["has_more"],
        limit=limit,
        offset=offset,
    )
    return {**result, "elapsed_ms": elapsed_ms}
