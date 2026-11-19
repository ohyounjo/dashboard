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
