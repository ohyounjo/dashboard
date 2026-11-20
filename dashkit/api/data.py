"""/api/data 조회: dash.<데이터셋> 뷰를 읽어 열 타입과 JSON 값으로 바꾼다."""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import psycopg
from psycopg import sql

from .. import db

# Postgres 타입 OID → 계약의 6개 타입. 목록에 없는 타입(enum, uuid 등)은 text.
_TYPE_BY_OID = {
    16: "boolean",
    20: "integer", 21: "integer", 23: "integer", 26: "integer",
    700: "number", 701: "number", 1700: "number", 790: "number",
    1082: "date",
    1114: "timestamp", 1184: "timestamp",
}


def max_rows() -> int:
    try:
        n = int(os.environ.get("DATA_MAX_ROWS", "5000"))
    except ValueError:
        n = 5000
    return max(n, 0)


def connect() -> psycopg.Connection:
    timeout_ms = int(os.environ.get("DATA_STATEMENT_TIMEOUT_MS", "10000"))
    return db.reader_conn(
        autocommit=True,
        application_name="dashkit-api",
        options=f"-c statement_timeout={timeout_ms} -c default_transaction_read_only=on",
    )


def _value(v: Any, kind: str) -> Any:
    if v is None:
        return None
    if kind == "date" and isinstance(v, date):
        return v.isoformat()
    if kind == "timestamp" and isinstance(v, datetime):
        return v.isoformat()
    if kind == "number":
        if isinstance(v, Decimal):
            if not v.is_finite():
                return None
            return int(v) if v == v.to_integral_value() and abs(v) < 2**53 else float(v)
        if isinstance(v, float) and not math.isfinite(v):
            return None
        return v
    if kind in ("integer", "boolean"):
        return v
    # text: JSON에 바로 넣을 수 없는 값은 문자열로
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    return str(v)


def fetch(dataset: str) -> dict:
    """dataset은 호출 전에 허용 목록과 대조된 이름이어야 한다."""
    limit = max_rows()
    query = sql.SQL("SELECT * FROM {}.{} LIMIT {}").format(
        sql.Identifier("dash"), sql.Identifier(dataset), sql.Literal(limit + 1)
    )
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query)
        desc = cur.description or []
        raw_rows = cur.fetchall()
    columns = [{"name": d.name, "type": _TYPE_BY_OID.get(d.type_code, "text")} for d in desc]
    truncated = len(raw_rows) > limit
    raw_rows = raw_rows[:limit]
    rows = [
        {c["name"]: _value(v, c["type"]) for c, v in zip(columns, r)}
        for r in raw_rows
    ]
    return {"columns": columns, "rows": rows, "truncated": truncated}


def ping() -> bool:
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False


def fetch_page(dataset: str, project: Any, limit: int, cursor: str | None) -> dict:
    """한 페이지(limit 1~200, cursor). dataset은 허용 목록과 대조된 이름. 행을 가리킬 키가 뷰에 있으면 키 방식,
    없으면 건너뛰기 방식(dashkit/paging.py). 응답은 {columns, rows, next_cursor, changed, truncated: false}."""
    from .. import paging

    ds = project.dashboard["datasets"].get(dataset, {})
    q = paging.parse_query({"limit": limit, "cursor": cursor}, [])
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = 'dash' "
                    "AND table_name = %s ORDER BY ordinal_position", (dataset,))
        found = cur.fetchall()
        if not found:
            raise psycopg.errors.UndefinedTable(f"dash.{dataset}")
        columns = [paging.Column(c, _VIEW_TYPES.get(t, "text")) for c, t in found]
        names = {c.name for c in columns}
        table = ds.get("table")
        pk = list(project.schema["tables"][table]["primary_key"]) if table else []
        key_columns = pk if table and all(k in names for k in pk) else None
        # 데이터 판: 다시 만든 시각과 배치 기록(추가 적재가 줄을 더한다). dash_reader가 읽을 수 있는 적재 기록이다
        cur.execute("SELECT loaded_at FROM dashkit_meta.load_snapshot WHERE id = 1")
        row = cur.fetchone()
        try:
            cur.execute("SELECT table_name || '/' || file, sha256 FROM dashkit_meta.batch")
            batches = dict(cur.fetchall())
        except psycopg.errors.UndefinedTable:
            conn.rollback()
            batches = {}
        version = paging.data_version(row[0] if row else None, batches)
        page = paging.fetch_page(cur, sql.SQL("{}.{}").format(sql.Identifier("dash"), sql.Identifier(dataset)),
                                 columns, q, key_columns=key_columns, version=version, total_rows=None,
                                 to_json=_value)
    out = page.public()
    return {"columns": out["columns"], "rows": out["rows"], "next_cursor": out["next_cursor"],
            "changed": out["changed"], "truncated": False}


_VIEW_TYPES = {"bigint": "integer", "integer": "integer", "smallint": "integer", "numeric": "number",
               "double precision": "number", "real": "number", "boolean": "boolean", "date": "date",
               "timestamp with time zone": "timestamp", "timestamp without time zone": "timestamp"}
