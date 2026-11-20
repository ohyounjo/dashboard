"""행 조회의 페이지 나눔(설계서 8-1, 8-2, contracts/manage.md "행 조회"). 관리 서비스의 행 조회와 backend의 `/api/data`가
함께 쓴다. DB 접속은 부른 쪽이 준다(관리 서비스는 dash_inspect, backend는 dash_reader).

- **필터·정렬·열은 목록과 대조한 뒤에만** SQL에 들어간다(식별자는 psycopg.sql.Identifier, 값은 전부 매개변수).
  사용자 입력이 SQL 글에 들어가는 경로가 없다.
- 한 번에 돌려주는 행은 기본 50, 최대 200(넘으면 422 invalid_query).
- **키 방식 커서**: 행을 가리킬 고유한 키(기본 키 열 전부)가 결과에 있을 때. 순서는 (요청한 정렬 열, 기본 키)이고 다음 페이지는
  "마지막 행 다음"을 키 조건으로 고른다. 깊은 페이지도 속도가 같다. 기본 키로 정렬하면 첫 페이지 때 있던 행은 겹치거나 빠지지 않는다.
- **건너뛰기 방식 커서**: 그 밖(sql: 데이터셋, 기본 키 열이 빠진 뷰). 순서는 요청한 정렬 열 뒤에 나머지 모든 열. 깊이 100,000행까지.
- 커서에는 첫 페이지 때의 **데이터 판**이 들어 있다. 다음 페이지에서 판이 다르면 응답의 changed가 true다.
- 커서는 서버만 읽는 값이다(서명한 JSON). 어느 방식이든 요청·응답의 모양은 같다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from psycopg import sql

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_OFFSET = 100_000
COUNT_CAP = 100_000
CELL_CHARS = 2000                   # 목록에서 칸 하나를 자르는 길이(전체 값은 한 행 읽기로)
ROW_BYTES = 2 * 1024 * 1024         # 한 행 읽기의 응답 한도(넘는 칸만 자른다)

OPS = ("eq", "ne", "lt", "le", "gt", "ge", "in", "contains", "starts_with", "is_null", "not_null")
_ORDERED = ("integer", "number", "date", "timestamp", "text")     # lt·le·gt·ge를 받는 유형
_TEXTUAL = ("text",)                                               # contains·starts_with를 받는 유형

# 커서 서명 키. 프로세스마다 새로 만든다(관리 서비스·backend가 다시 뜨면 커서는 처음부터 다시 받는다)
_KEY = os.environ.get("PAGING_CURSOR_KEY", "").encode() or secrets.token_bytes(32)


class QueryError(Exception):
    """요청을 받을 수 없음. detail: invalid_filter | invalid_query."""

    def __init__(self, detail: str, message: str):
        self.detail = detail
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Column:
    name: str
    type: str          # text | integer | number | boolean | date | timestamp (enum은 text로 본다)


@dataclass
class Query:
    filters: list[dict[str, Any]] = field(default_factory=list)
    sort: list[tuple[str, str]] = field(default_factory=list)       # [(열, asc|desc)]
    columns: list[str] | None = None
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None
    count: bool = False
    key: dict[str, Any] | None = None


def parse_query(body: Mapping[str, Any], columns: Sequence[Column]) -> Query:
    """요청 본문 → Query. 열 이름·연산·유형·한도를 목록과 대조한다. 틀리면 QueryError."""
    names = {c.name: c for c in columns}
    q = Query()
    limit = body.get("limit", DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise QueryError("invalid_query", f"limit은 1~{MAX_LIMIT}의 정수입니다")
    q.limit = limit
    filters = body.get("filters") or []
    if not isinstance(filters, list):
        raise QueryError("invalid_filter", "filters는 목록입니다")
    for i, f in enumerate(filters):
        if not isinstance(f, dict) or f.get("column") not in names or f.get("op") not in OPS:
            raise QueryError("invalid_filter", f"filters[{i}]: 열 또는 연산이 올바르지 않습니다")
        col = names[f["column"]]
        q.filters.append({"column": col.name, "op": f["op"], "value": _filter_value(col, f["op"], f.get("value"), i)})
    sort = body.get("sort") or []
    if not isinstance(sort, list):
        raise QueryError("invalid_query", "sort는 목록입니다")
    seen: set[str] = set()
    for i, s in enumerate(sort):
        if not isinstance(s, dict) or s.get("column") not in names or s.get("dir", "asc") not in ("asc", "desc") \
                or s["column"] in seen:
            raise QueryError("invalid_query", f"sort[{i}]: 열 또는 방향이 올바르지 않습니다")
        seen.add(s["column"])
        q.sort.append((names[s["column"]].name, s.get("dir", "asc")))
    cols = body.get("columns")
    if cols is not None:
        if not isinstance(cols, list) or not cols or any(c not in names for c in cols) or len(set(cols)) != len(cols):
            raise QueryError("invalid_query", "columns는 있는 열 이름의 목록입니다")
        q.columns = [names[c].name for c in cols]
    cursor = body.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise QueryError("invalid_query", "cursor는 글입니다")
    q.cursor = cursor
    q.count = body.get("count") is True
    key = body.get("key")
    if key is not None:
        if not isinstance(key, dict) or not key or any(k not in names for k in key):
            raise QueryError("invalid_query", "key는 {기본 키 열: 값}입니다")
        q.key = {names[k].name: _scalar(names[k], v, f"key.{k}") for k, v in key.items()}
    return q


def _scalar(col: Column, value: Any, where: str) -> Any:
    """필터 값 하나를 열 유형의 파이썬 값으로. 틀리면 QueryError(invalid_filter)."""
    if value is None:
        raise QueryError("invalid_filter", f"{where}: 값이 필요합니다")
    try:
        if col.type == "integer":
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ValueError
            return int(value)
        if col.type == "number":
            if isinstance(value, bool):
                raise ValueError
            d = Decimal(str(value))
            if not d.is_finite():
                raise ValueError
            return d
        if col.type == "boolean":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in ("true", "false"):
                return value.lower() == "true"
            raise ValueError
        if col.type == "date":
            return date.fromisoformat(str(value))
        if col.type == "timestamp":
            return datetime.fromisoformat(str(value))
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise ValueError
        return str(value)
    except (ValueError, ArithmeticError):
        raise QueryError("invalid_filter", f"{where}: '{value}'는 {col.type} 값이 아닙니다") from None


def _filter_value(col: Column, op: str, value: Any, i: int) -> Any:
    where = f"filters[{i}]"
    if op in ("is_null", "not_null"):
        return None
    if op in ("lt", "le", "gt", "ge") and col.type not in _ORDERED:
        raise QueryError("invalid_filter", f"{where}: {col.type} 열에는 {op}를 쓸 수 없습니다")
    if op in ("contains", "starts_with") and col.type not in _TEXTUAL:
        raise QueryError("invalid_filter", f"{where}: {col.type} 열에는 {op}를 쓸 수 없습니다")
    if op == "in":
        if not isinstance(value, list) or not value or len(value) > 1000:
            raise QueryError("invalid_filter", f"{where}: in의 값은 1~1000개의 목록입니다")
        return [_scalar(col, v, where) for v in value]
    return _scalar(col, value, where)


# ---------------------------------------------------------------- SQL 조각

def where_clause(filters: Sequence[Mapping[str, Any]]) -> tuple[sql.Composable, list[Any]]:
    parts: list[sql.Composable] = []
    params: list[Any] = []
    for f in filters:
        c = sql.Identifier(f["column"])
        op, v = f["op"], f["value"]
        if op == "is_null":
            parts.append(sql.SQL("{} IS NULL").format(c))
        elif op == "not_null":
            parts.append(sql.SQL("{} IS NOT NULL").format(c))
        elif op == "in":
            parts.append(sql.SQL("{} = ANY(%s)").format(c))
            params.append(list(v))
        elif op == "contains":
            parts.append(sql.SQL("strpos({}, %s) > 0").format(c))
            params.append(v)
        elif op == "starts_with":
            parts.append(sql.SQL("starts_with({}, %s)").format(c))
            params.append(v)
        else:
            sym = {"eq": "=", "ne": "<>", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}[op]
            parts.append(sql.SQL("{} " + sym + " %s").format(c))
            params.append(v)
    if not parts:
        return sql.SQL("TRUE"), params
    return sql.SQL(" AND ").join(parts), params


def _order(keys: Sequence[tuple[str, str]]) -> sql.Composable:
    return sql.SQL(", ").join(
        sql.SQL("{} {} NULLS LAST").format(sql.Identifier(c), sql.SQL("DESC" if d == "desc" else "ASC")) for c, d in keys)


def _after(keys: Sequence[tuple[str, str]], last: Sequence[Any]) -> tuple[sql.Composable, list[Any]]:
    """(keys 순서, NULLS LAST)에서 last 값 다음의 행을 고르는 조건. 마지막 키(기본 키)는 NULL이 없다."""
    (c, d), rest = keys[0], keys[1:]
    col = sql.Identifier(c)
    v = last[0]
    cmp = sql.SQL("<" if d == "desc" else ">")
    if not rest:
        return sql.SQL("{} {} %s").format(col, cmp), [v]
    inner, params = _after(rest, last[1:])
    if v is None:          # NULL 무리(맨 뒤) 안에서만 다음
        return sql.SQL("({} IS NULL AND ({}))").format(col, inner), params
    return (sql.SQL("({c} {cmp} %s OR {c} IS NULL OR ({c} = %s AND ({inner})))").format(
        c=col, cmp=cmp, inner=inner), [v, v, *params])


# ---------------------------------------------------------------- 커서

def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return {"$t": "dt" if isinstance(v, datetime) else "d", "v": v.isoformat()}
    if isinstance(v, Decimal):
        return {"$t": "n", "v": str(v)}
    return v


def _from_json(v: Any) -> Any:
    if isinstance(v, dict) and "$t" in v:
        t, s = v["$t"], v["v"]
        return datetime.fromisoformat(s) if t == "dt" else date.fromisoformat(s) if t == "d" else Decimal(s)
    return v


def encode_cursor(doc: Mapping[str, Any]) -> str:
    body = json.dumps(doc, separators=(",", ":"), default=str).encode()
    mac = hmac.new(_KEY, body, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(mac + body).decode().rstrip("=")


def decode_cursor(token: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        mac, body = raw[:16], raw[16:]
        if not hmac.compare_digest(mac, hmac.new(_KEY, body, hashlib.sha256).digest()[:16]):
            raise ValueError
        doc = json.loads(body)
        if not isinstance(doc, dict):
            raise ValueError
        return doc
    except (ValueError, TypeError):
        raise QueryError("invalid_query", "cursor가 올바르지 않습니다(처음부터 다시 조회하세요)") from None


# ---------------------------------------------------------------- 한 페이지

@dataclass
class Page:
    columns: list[Column]
    rows: list[dict[str, Any]]
    next_cursor: str | None
    total: int | str | None
    changed: bool
    truncated_cells: list[list[Any]]

    def public(self) -> dict[str, Any]:
        return {"columns": [{"name": c.name, "type": c.type} for c in self.columns], "rows": self.rows,
                "next_cursor": self.next_cursor, "total": self.total, "changed": self.changed,
                "truncated_cells": self.truncated_cells}


def fetch_page(cur: Any, relation: sql.Composable, columns: Sequence[Column], q: Query, *,
               key_columns: Sequence[str] | None, version: str, total_rows: int | None,
               to_json: Any = None) -> Page:
    """한 페이지를 읽는다. relation은 목록과 대조한 이름으로 만든 식별자(예: raw.<t>, dash.<d>)다.

    key_columns가 있으면(고유한 키가 결과에 있음) 키 방식, 없으면 건너뛰기 방식. total_rows는 필터가 없을 때 쓸 전체 행 수
    (배치 기록에서 구한 값. 모르면 None). to_json(값, 유형)은 값을 JSON으로 바꾸는 함수(/api/data와 같은 형식).
    """
    to_json = to_json or (lambda v, _t: v)
    by = {c.name: c for c in columns}
    shown = q.columns or [c.name for c in columns]
    where, params = where_clause(q.filters)
    mode = "key" if key_columns else "offset"
    sort = list(q.sort) or ([(k, "asc") for k in key_columns] if key_columns else [])
    if mode == "key":
        keys = sort + [(k, "asc") for k in key_columns if k not in {c for c, _ in sort}]   # type: ignore[union-attr]
        # 다음 페이지 조건에 쓸 값(정렬 열·기본 키)도 읽는다. 응답에는 요청한 열만 싣는다
        select_cols = shown + [c for c, _d in keys if c not in shown]
    else:
        keys = sort + [(c.name, "asc") for c in columns if c.name not in {s for s, _ in sort}]
        select_cols = list(shown)
    changed = False
    offset = 0
    after: tuple[sql.Composable, list[Any]] | None = None
    if q.cursor:
        doc = decode_cursor(q.cursor)
        if doc.get("mode") != mode or doc.get("keys") != [list(k) for k in keys] or doc.get("where") != _fingerprint(q):
            raise QueryError("invalid_query", "cursor가 이 조회의 것이 아닙니다(처음부터 다시 조회하세요)")
        changed = doc.get("version") != version
        if mode == "key":
            after = _after(keys, [_from_json(v) for v in doc["last"]])
        else:
            offset = int(doc["offset"])
    if offset > MAX_OFFSET:
        raise QueryError("invalid_query", f"이 조회는 {MAX_OFFSET}행 뒤로 넘길 수 없습니다. 조건을 좁혀 주세요")
    cond, cparams = (sql.SQL("({}) AND ({})").format(where, after[0]), params + after[1]) if after else (where, params)
    stmt = sql.SQL("SELECT {} FROM {} WHERE {} ORDER BY {} LIMIT %s OFFSET %s").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in select_cols), relation, cond, _order(keys))
    cur.execute(stmt, [*cparams, q.limit + 1, offset])
    raw = cur.fetchall()
    more = len(raw) > q.limit
    raw = raw[:q.limit]
    rows: list[dict[str, Any]] = []
    truncated: list[list[Any]] = []
    for i, r in enumerate(raw):
        row: dict[str, Any] = {}
        for c, v in zip(select_cols, r):
            if c not in shown:
                continue
            value = to_json(v, by[c].type)
            if isinstance(value, str) and len(value) > CELL_CHARS:
                value = value[:CELL_CHARS]
                truncated.append([i, c])
            row[c] = value
        rows.append(row)
    nxt = None
    if more:
        doc = {"mode": mode, "keys": [list(k) for k in keys], "where": _fingerprint(q), "version": version}
        if mode == "key":
            last = raw[-1]
            idx = {c: j for j, c in enumerate(select_cols)}
            doc["last"] = [_jsonable(last[idx[c]]) for c, _d in keys]
        else:
            doc["offset"] = offset + q.limit
        nxt = encode_cursor(doc)
    total: int | str | None = None
    if q.count:
        cur.execute(sql.SQL("SELECT count(*) FROM (SELECT 1 FROM {} WHERE {} LIMIT %s) s").format(relation, where),
                    [*params, COUNT_CAP + 1])
        n = int(cur.fetchone()[0])
        total = f"{COUNT_CAP}+" if n > COUNT_CAP else n
    elif not q.filters and total_rows is not None:
        total = total_rows
    return Page([by[c] for c in shown], rows, nxt, total, changed, truncated)


def _fingerprint(q: Query) -> str:
    """같은 조회인지(필터·열)의 지문. 다른 조회의 커서를 섞어 쓰지 않게."""
    text = json.dumps({"f": q.filters, "c": q.columns}, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def fetch_row(cur: Any, relation: sql.Composable, columns: Sequence[Column], key: Mapping[str, Any],
              to_json: Any = None) -> dict[str, Any] | None:
    """기본 키로 한 행을 자르지 않고 읽는다. 응답 한도(ROW_BYTES)를 넘으면 큰 칸부터 잘라 truncated_cells에 적는다."""
    to_json = to_json or (lambda v, _t: v)
    cond = sql.SQL(" AND ").join(sql.SQL("{} = %s").format(sql.Identifier(k)) for k in key)
    cur.execute(sql.SQL("SELECT {} FROM {} WHERE {} LIMIT 2").format(
        sql.SQL(", ").join(sql.Identifier(c.name) for c in columns), relation, cond), list(key.values()))
    found = cur.fetchall()
    if not found:
        return None
    row = {c.name: to_json(v, c.type) for c, v in zip(columns, found[0])}
    truncated: list[str] = []
    size = len(json.dumps(row, ensure_ascii=False, default=str).encode())
    for name in sorted((c.name for c in columns if isinstance(row[c.name], str)), key=lambda n: -len(row[n])):
        if size <= ROW_BYTES:
            break
        cut = len(row[name].encode()) - CELL_CHARS
        row[name] = row[name][:CELL_CHARS]
        truncated.append(name)
        size -= max(cut, 0)
    return {"row": row, "truncated_cells": truncated}


def data_version(loaded_at: Any, batches: Mapping[str, Any]) -> str:
    """데이터 판: (마지막으로 다시 만든 시각, 배치 기록의 이름·해시 목록을 합친 해시). batches는 {파일: sha256}."""
    h = hashlib.sha256(str(loaded_at).encode())
    for name in sorted(batches):
        h.update(f"\0{name}\0{batches[name]}".encode())
    return h.hexdigest()[:24]
