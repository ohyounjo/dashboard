"""테이블 행 조회 결과 CSV 내보내기(MANAGE_FEATURES=export, contracts/manage.md).

읽기 경로라 dash_inspect만 사용한다(읽기 전용 트랜잭션, 서버사이드 커서 단일 쿼리).
sensitive: true 열은 SQL에서 제외해 '***'로 출력하고, 필터·정렬에 민감 열이 포함되면 422로 거절한다.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from psycopg import sql
from starlette.concurrency import run_in_threadpool

from ... import paging
from ...auth import store as auth_store
from ...auth.routes import Principal, client_ip, require_operator
from ...features import has_manage_feature
from ...ingest import IngestError
from .. import context
from . import rows
from .tables import _project, _table

router = APIRouter()
EXPORT_BATCH = 2000


def _parse_json_list(raw: str, detail: str, label: str) -> list[Any] | None:
    if not raw or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise IngestError(422, detail, f"{label} JSON 형식이 올바르지 않습니다") from None
    if not isinstance(parsed, list):
        raise IngestError(422, detail, f"{label}는 JSON 배열이어야 합니다")
    return parsed


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else str(value)
    return str(value)


def _close_quietly(cur: Any, conn: Any) -> None:
    for obj in (cur, conn):
        if obj is not None:
            try:
                obj.close()
            except Exception:
                pass


@router.get("/tables/{table}/export")
async def export_table_rows(
    table: str,
    request: Request,
    format: str = "csv",
    filters: str = "",
    sort: str = "",
    columns: str = "",
    principal: Principal = Depends(require_operator),
) -> StreamingResponse:
    if not has_manage_feature("export"):
        raise IngestError(404, "not_found", "비활성화된 기능입니다")
    if format != "csv":
        raise IngestError(422, "invalid_query", "지원하지 않는 내보내기 형식입니다")

    parsed_filters = _parse_json_list(filters, "invalid_filter", "filters") or []
    parsed_sort = _parse_json_list(sort, "invalid_query", "sort") or []
    parsed_columns = _parse_json_list(columns, "invalid_query", "columns")

    project = _project()
    name = _table(project, table)
    spec = project.schema["tables"][name]
    all_cols = [paging.Column(c, rows._SCHEMA_TYPES.get(s["type"], s["type"])) for c, s in spec["columns"].items()]
    q = rows._parse({"filters": parsed_filters, "sort": parsed_sort, "columns": parsed_columns}, all_cols)

    sensitive_cols = {c for c, s in spec["columns"].items() if s.get("sensitive")}
    if any(f["column"] in sensitive_cols for f in q.filters):
        raise IngestError(422, "invalid_filter", "민감 열은 필터 조건으로 쓸 수 없습니다")
    if any(col in sensitive_cols for col, _dir in q.sort):
        raise IngestError(422, "invalid_query", "민감 열은 정렬 기준으로 쓸 수 없습니다")

    selected_cols = q.columns or [c.name for c in all_cols]
    sql_cols = [c for c in selected_cols if c not in sensitive_cols]
    sql_pos = {c: i for i, c in enumerate(sql_cols)}

    pk = list(spec["primary_key"])
    sort_cols = {c for c, _d in q.sort}
    order_keys = list(q.sort) + [(k, "asc") for k in pk if k not in sort_cols]
    where_sql, where_params = paging.where_clause(q.filters)
    order_sql = paging._order(order_keys)
    max_rows = context.limits().export_max_rows

    select_sql = sql.SQL(", ").join(map(sql.Identifier, sql_cols)) if sql_cols else sql.SQL("1")
    stmt = sql.SQL("SELECT {select} FROM {schema}.{table} WHERE {where} ORDER BY {order} LIMIT %s").format(
        select=select_sql,
        schema=sql.Identifier("raw"),
        table=sql.Identifier(name),
        where=where_sql,
        order=order_sql,
    )
    params = [*where_params, max_rows + 1]

    def _open_stream():
        conn = rows._connect()
        cur = None
        try:
            cur = conn.cursor(name="dashkit_export")
            cur.itersize = EXPORT_BATCH
            cur.execute(stmt, params)
            first = cur.fetchmany(EXPORT_BATCH)
            return conn, cur, first
        except paging.QueryError as exc:
            _close_quietly(cur, conn)
            raise IngestError(422, exc.detail, exc.message) from None
        except psycopg.errors.QueryCanceled:
            _close_quietly(cur, conn)
            raise IngestError(504, "query_timeout", "조회가 시간 제한을 넘었습니다. 조건을 좁혀 주세요") from None
        except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName):
            _close_quietly(cur, conn)
            raise IngestError(409, "rebuild_required", "DB에 이 테이블·뷰가 없습니다. 먼저 적용(apply)하세요") from None
        except psycopg.Error as exc:
            _close_quietly(cur, conn)
            raise IngestError(422, "invalid_query", f"조회할 수 없습니다: {exc.diag.message_primary or type(exc).__name__}") from None
        except Exception:
            _close_quietly(cur, conn)
            raise

    conn, cur, first_batch = await run_in_threadpool(_open_stream)
    ip = client_ip(request)

    def _format_row(db_row: tuple[Any, ...]) -> list[str]:
        return ["***" if c in sensitive_cols else _csv_cell(db_row[sql_pos[c]]) for c in selected_cols]

    def generate():
        exported_rows = 0
        truncated = False
        try:
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\n")
            writer.writerow(selected_cols)
            yield ("\ufeff" + buf.getvalue()).encode("utf-8")

            batch = first_batch
            while batch:
                buf = io.StringIO()
                writer = csv.writer(buf, lineterminator="\n")
                for db_row in batch:
                    if exported_rows >= max_rows:
                        truncated = True
                        break
                    writer.writerow(_format_row(db_row))
                    exported_rows += 1
                chunk = buf.getvalue()
                if chunk:
                    yield chunk.encode("utf-8")
                if truncated:
                    break
                batch = cur.fetchmany(EXPORT_BATCH)
        finally:
            _close_quietly(cur, conn)
            try:
                auth_store.record_audit(
                    "data.export",
                    actor_id=principal.id,
                    actor_email=principal.email,
                    target_type="table",
                    target_id=name,
                    target_label=name,
                    detail={
                        "project": project.name,
                        "table": name,
                        "format": "csv",
                        "columns": selected_cols,
                        "filter_count": len(q.filters),
                        "rows": exported_rows,
                        "truncated": truncated,
                    },
                    ip=ip,
                )
            except Exception:
                pass

    headers = {
        "Content-Disposition": f'attachment; filename="{name}.csv"',
        "Cache-Control": "no-store",
        "X-Export-Max-Rows": str(max_rows),
    }
    return StreamingResponse(generate(), media_type="text/csv; charset=utf-8", headers=headers)
