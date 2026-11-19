"""지금 DB에 적재된 상태를 읽는다. `dashkit plan`(dashkit/plan.py)의 "읽어 오는 부분"이다. 아무것도 바꾸지 않는다.

raw 스키마는 owner 계정만 읽을 수 있다(dash_reader에게는 권한이 없고 앞으로도 주지 않는다). 그래서 이 모듈은
로더와 같은 owner 계정을 쓰되, 연결을 읽기 전용으로 연다.

- 세션 기본값 `default_transaction_read_only=on` + 트랜잭션 `READ ONLY`(psycopg `read_only`) + `REPEATABLE READ`
  (모든 테이블을 같은 시점으로 읽는다). 쓰기·DDL은 Postgres가 거부한다.
- 끝나면 항상 롤백한다.
- `sql:` 데이터셋의 결과 열은 쿼리를 **실행하지 않고** 확장 프로토콜의 Parse/Describe로만 알아낸다(describe_query).

이 모듈은 tools 컨테이너(로더, Studio)에서만 쓴다. backend에는 owner 계정이 없다.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import Any, Iterator, Sequence

import psycopg
from psycopg import sql

from .. import db

RAW = "raw"
DASH = "dash"
STATEMENT_TIMEOUT_MS = 60000


class CurrentError(Exception):
    """지금 상태를 읽지 못함(접속 실패, 권한, 시간 초과)."""


class DescribeError(Exception):
    """쿼리의 결과 열을 지금 DB에서 알아낼 수 없음(없는 테이블·열 등). 메시지는 Postgres의 이유다."""


@dataclass
class RawTable:
    """raw.<name>의 지금 내용."""
    name: str
    columns: list[tuple[str, str]]                       # (열 이름, information_schema data_type), ordinal 순서
    row_columns: list[str] = field(default_factory=list)  # rows의 열 순서
    rows: list[tuple] = field(default_factory=list)


def pg_message(exc: BaseException) -> str:
    diag = getattr(exc, "diag", None)
    msg = (diag.message_primary if diag and diag.message_primary else str(exc)).strip()
    return msg or type(exc).__name__


@contextmanager
def readonly_owner(dbname: str | None = None) -> Iterator[psycopg.Connection]:
    """owner 계정의 읽기 전용 연결. 트랜잭션 하나(READ ONLY, REPEATABLE READ)를 열고 끝나면 롤백한다."""
    timeout_ms = int(os.environ.get("PLAN_STATEMENT_TIMEOUT_MS", str(STATEMENT_TIMEOUT_MS)))
    try:
        conn = db.owner_conn(
            dbname,
            application_name="dashkit-plan",
            options=f"-c default_transaction_read_only=on -c statement_timeout={timeout_ms}",
        )
    except (psycopg.Error, RuntimeError) as exc:
        raise CurrentError(f"DB 접속 실패: {pg_message(exc)}") from None
    try:
        conn.read_only = True
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        try:
            yield conn
        finally:
            conn.rollback()
    finally:
        conn.close()


def session_timezone(conn: psycopg.Connection) -> tzinfo:
    """세션 시간대. 시간대 없는 시각을 timestamptz 열에 적재할 때 Postgres가 쓰는 기준이다."""
    return conn.info.timezone


def read_columns(conn: psycopg.Connection, schema: str,
                 names: Sequence[str] | None = None) -> dict[str, list[tuple[str, str]]]:
    """스키마의 테이블·뷰 → [(열, 타입)](ordinal 순서). 타입 표기는 sync.read_view_columns와 같다."""
    query = ("SELECT table_name, column_name, data_type, udt_name FROM information_schema.columns "
             "WHERE table_schema = %s")
    params: list[Any] = [schema]
    if names is not None:
        query += " AND table_name = ANY(%s)"
        params.append(list(names))
    query += " ORDER BY table_name, ordinal_position"
    out: dict[str, list[tuple[str, str]]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            for table, col, data_type, udt in cur.fetchall():
                out.setdefault(table, []).append((col, udt if data_type in ("USER-DEFINED", "ARRAY") else data_type))
    except psycopg.Error as exc:
        raise CurrentError(f"열 정보를 읽지 못함({schema}): {pg_message(exc)}") from None
    return out


def read_raw_table(conn: psycopg.Connection, name: str, columns: list[tuple[str, str]],
                   want: Sequence[str] | None = None) -> RawTable:
    """raw.<name>의 행을 읽는다. want가 있으면 그 가운데 테이블에 있는 열만(want 순서) 읽는다."""
    have = [c for c, _t in columns]
    row_columns = [c for c in want if c in have] if want is not None else have
    table = RawTable(name, list(columns), row_columns)
    if not row_columns:
        return table
    stmt = sql.SQL("SELECT {} FROM {}.{}").format(
        sql.SQL(", ").join(map(sql.Identifier, row_columns)), sql.Identifier(RAW), sql.Identifier(name))
    try:
        with conn.cursor() as cur:
            cur.execute(stmt)
            table.rows = cur.fetchall()
    except psycopg.Error as exc:
        raise CurrentError(f"raw.{name}을 읽지 못함: {pg_message(exc)}") from None
    return table


def reader_view_columns(dbname: str | None = None) -> dict[str, list[tuple[str, str]]]:
    """dash 스키마의 모든 뷰 → [(열, 타입)]. dash_reader 계정으로 읽는다(읽기 전용)."""
    try:
        with db.reader_conn(dbname, application_name="dashkit-plan",
                            options="-c default_transaction_read_only=on") as conn:
            try:
                return read_columns(conn, DASH)
            finally:
                conn.rollback()
    except (psycopg.Error, RuntimeError) as exc:
        raise CurrentError(f"DB 접속 실패(읽기 계정): {pg_message(exc)}") from None


def describe_query(conn: psycopg.Connection, query: str, name: str = "dashkit_plan_q") -> list[tuple[str, str]]:
    """쿼리의 결과 열 [(열, 타입)]을 실행 없이 알아낸다. 알 수 없으면 DescribeError.

    libpq의 prepare(Parse) + describe_prepared(Describe)만 쓴다. Parse는 문장 하나만 받고 아무것도 실행하지 않는다.
    실패하면 세이브포인트로 되돌려 같은 트랜잭션을 계속 쓸 수 있게 한다.
    """
    body = query.strip().rstrip(";").strip()
    pgconn = conn.pgconn
    stmt = name.encode("ascii")
    try:
        with conn.transaction():
            res = pgconn.prepare(stmt, body.encode(conn.info.encoding))
            if res.status != psycopg.pq.ExecStatus.COMMAND_OK:
                raise DescribeError(_result_error(res, conn))
            res = pgconn.describe_prepared(stmt)
            if res.status != psycopg.pq.ExecStatus.COMMAND_OK:
                raise DescribeError(_result_error(res, conn))
            fields = [(res.fname(i).decode(conn.info.encoding), res.ftype(i)) for i in range(res.nfields)]
            with conn.cursor() as cur:
                cur.execute(sql.SQL("DEALLOCATE {}").format(sql.Identifier(name)))
                cur.execute(
                    "SELECT t.oid, format_type(t.oid, NULL), t.typname, n.nspname, t.typtype, t.typelem, t.typlen "
                    "FROM pg_catalog.pg_type t JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace "
                    "WHERE t.oid = ANY(%s)",
                    ([oid for _n, oid in fields],),
                )
                types = {row[0]: _type_name(*row[1:]) for row in cur.fetchall()}
    except psycopg.Error as exc:
        raise DescribeError(pg_message(exc)) from None
    return [(n, types.get(oid, str(oid))) for n, oid in fields]


def _result_error(res: Any, conn: psycopg.Connection) -> str:
    raw = res.error_field(psycopg.pq.DiagnosticField.MESSAGE_PRIMARY) or res.error_message or b""
    return raw.decode(conn.info.encoding, "replace").strip() or "알 수 없는 오류"


def _type_name(formatted: str, typname: str, namespace: str, typtype: str, typelem: int, typlen: int) -> str:
    """information_schema.columns의 data_type 규칙을 따른 표기(배열·사용자 정의 타입은 udt_name)."""
    if typtype == "d" or (typelem != 0 and typlen == -1) or namespace != "pg_catalog":
        return typname
    return formatted
