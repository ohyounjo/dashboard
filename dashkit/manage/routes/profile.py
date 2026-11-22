"""테이블 열 통계 조회(MANAGE_FEATURES=profile, contracts/manage.md).

읽기 경로라 dash_inspect만 사용한다(읽기 전용 트랜잭션, MANAGE_STATEMENT_TIMEOUT_MS).
sensitive: true 열은 집계 SQL에서 제외하고 통계 필드를 null로 반환한다.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from psycopg import sql
from starlette.concurrency import run_in_threadpool

from ... import paging
from ...auth.routes import Principal, require_operator
from ...features import has_manage_feature
from ...ingest import IngestError
from . import rows
from .tables import _project, _table

router = APIRouter()


def _truncate_stat(value: Any, col_type: str) -> Any:
    if col_type in ("text", "enum") and isinstance(value, str) and len(value) > paging.CELL_CHARS:
        return value[:paging.CELL_CHARS]
    return value


@router.get("/tables/{table}/profile")
async def get_table_profile(table: str, principal: Principal = Depends(require_operator)) -> dict[str, Any]:
    if not has_manage_feature("profile"):
        raise IngestError(404, "not_found", "비활성화된 기능입니다")
    project = _project()
    name = _table(project, table)
    spec = project.schema["tables"][name]

    exprs: list[sql.Composable] = [sql.SQL("COUNT(*)")]
    col_items = list(spec["columns"].items())
    for col, col_spec in col_items:
        if col_spec.get("sensitive"):
            continue
        ident = sql.Identifier(col)
        exprs.append(sql.SQL("COUNT({})").format(ident))
        exprs.append(sql.SQL("COUNT(DISTINCT {})").format(ident))
        if col_spec["type"] != "boolean":
            exprs.append(sql.SQL("MIN({})").format(ident))
            exprs.append(sql.SQL("MAX({})").format(ident))

    if len(exprs) == 1:
        stmt = sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
            sql.Identifier("dashkit_meta"),
            sql.Identifier(paging.mask_view_name("raw", name)),
        )
    else:
        stmt = sql.SQL("SELECT {} FROM {}.{}").format(
            sql.SQL(", ").join(exprs),
            sql.Identifier("raw"),
            sql.Identifier(name),
        )

    def work(cur) -> dict[str, Any]:
        cur.execute(stmt)
        row = cur.fetchone()
        total_rows = int(row[0] or 0) if row is not None else 0
        idx = 1
        columns_out: list[dict[str, Any]] = []
        for col, col_spec in col_items:
            col_type = col_spec["type"]
            if col_spec.get("sensitive"):
                columns_out.append({
                    "name": col,
                    "type": col_type,
                    "sensitive": True,
                    "null_count": None,
                    "null_ratio": None,
                    "distinct_count": None,
                    "min": None,
                    "max": None,
                })
                continue
            non_null = int(row[idx] or 0)
            distinct_count = int(row[idx + 1] or 0)
            idx += 2
            if col_type == "boolean":
                min_val, max_val = None, None
            else:
                raw_min, raw_max = row[idx], row[idx + 1]
                idx += 2
                paging_type = rows._SCHEMA_TYPES.get(col_type, col_type)
                min_val = _truncate_stat(rows._to_json(raw_min, paging_type), col_type)
                max_val = _truncate_stat(rows._to_json(raw_max, paging_type), col_type)
            if total_rows == 0:
                null_count = 0
                null_ratio = None
            else:
                null_count = total_rows - non_null
                null_ratio = round(null_count / total_rows, 6)
            columns_out.append({
                "name": col,
                "type": col_type,
                "sensitive": False,
                "null_count": null_count,
                "null_ratio": null_ratio,
                "distinct_count": distinct_count,
                "min": min_val,
                "max": max_val,
            })
        return {"table": name, "total_rows": total_rows, "columns": columns_out}

    return await run_in_threadpool(rows._run, work)
