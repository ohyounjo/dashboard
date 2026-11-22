"""지금 DB에 적재된 상태를 읽는다. `dashkit plan`(dashkit/plan.py)의 "읽어 오는 부분"이다. 아무것도 바꾸지 않는다.

읽기 전용 계정(`dash_inspect` 우선, 미설정 시 `dash_reader` 폴백)만 쓴다. owner 계정의 환경변수는 쓰지 않는다.
지난 적재의 내용은 로더가 남긴 적재 기록(`dashkit_meta`, loader/record.py)으로 안다.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

import psycopg
from psycopg import pq, sql

from .. import db
from . import record

RAW = "raw"
DASH = "dash"
DEFAULT_PLAN_STATEMENT_TIMEOUT_MS = 15000


class CurrentError(Exception):
    """지금 상태를 읽지 못함(접속 실패, 시간 초과)."""


class DescribeError(Exception):
    """SQL 뷰 쿼리를 실행 없이 분석하지 못함(문법 오류, 매개변수, 중복 열 등)."""


@dataclass
class LoadRecord:
    """지난 적재의 기록. snapshot은 record.snapshot의 형식, fingerprints는 테이블 → [(row_key, row_hash)]."""
    snapshot: dict[str, Any]
    fingerprints: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    # 테이블 → {파일: (sha256, rows, inserted)}. 배치 기록(dashkit_meta.batch)이 없던 dashkit으로 적재한 DB면 빈 dict.
    # 추가 적재도 이 표에 줄을 더하므로, inserted의 합이 지금 테이블의 행 수다(스냅샷의 rows는 마지막 load 때의 값)
    batches: dict[str, dict[str, tuple[str, int, int]]] = field(default_factory=dict)
    fingerprints_readable: bool = True

    @property
    def project(self) -> str | None:
        return self.snapshot.get("project")


def pg_message(exc: BaseException) -> str:
    diag = getattr(exc, "diag", None)
    msg = (diag.message_primary if diag and diag.message_primary else str(exc)).strip()
    return msg or type(exc).__name__


def _statement_timeout_ms() -> int:
    raw = os.environ.get("PLAN_STATEMENT_TIMEOUT_MS")
    if not raw:
        return DEFAULT_PLAN_STATEMENT_TIMEOUT_MS
    try:
        val = int(raw)
        return val if val > 0 else DEFAULT_PLAN_STATEMENT_TIMEOUT_MS
    except ValueError:
        return DEFAULT_PLAN_STATEMENT_TIMEOUT_MS


@contextmanager
def reader(dbname: str | None = None) -> Iterator[psycopg.Connection]:
    """dash_reader의 읽기 전용 연결. 끝나면 롤백한다."""
    try:
        conn = db.reader_conn(dbname, application_name="dashkit-plan",
                              options="-c default_transaction_read_only=on")
    except (psycopg.Error, RuntimeError) as exc:
        raise CurrentError(f"DB 접속 실패(읽기 계정): {pg_message(exc)}") from None
    try:
        yield conn
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


@contextmanager
def inspector(dbname: str | None = None) -> Iterator[psycopg.Connection]:
    """dash_inspect의 읽기 전용 연결(REPEATABLE READ, statement_timeout). 끝나면 롤백한다."""
    timeout = _statement_timeout_ms()
    try:
        conn = db.inspect_conn(
            dbname,
            application_name="dashkit-plan",
            options=f"-c default_transaction_read_only=on -c statement_timeout={timeout}",
        )
        conn.read_only = True
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    except (psycopg.Error, RuntimeError) as exc:
        raise CurrentError(f"DB 접속 실패(운용 조회 계정): {pg_message(exc)}") from None
    try:
        yield conn
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def _read_schema_columns(conn: psycopg.Connection, schema: str, names: Sequence[str] | None,
                         error_prefix: str) -> dict[str, list[tuple[str, str]]]:
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
        raise CurrentError(f"{error_prefix}: {pg_message(exc)}") from None
    return out


def read_view_columns(conn: psycopg.Connection, names: Sequence[str] | None = None) -> dict[str, list[tuple[str, str]]]:
    """dash 스키마의 뷰 → [(열, 타입)](ordinal 순서). 타입 표기는 sync.read_view_columns와 같다."""
    return _read_schema_columns(conn, DASH, names, "뷰 열 정보를 읽지 못함")


def read_raw_columns(conn: psycopg.Connection, names: Sequence[str] | None = None) -> dict[str, list[tuple[str, str]]]:
    """raw 스키마의 테이블 → [(열, 타입)](ordinal 순서). dash_inspect 연결에서 사용한다."""
    return _read_schema_columns(conn, RAW, names, "raw 열 정보를 읽지 못함")


def _pg_res_message(res: pq.PGresult) -> str:
    msg = res.error_field(pq.DiagnosticField.MESSAGE_PRIMARY)
    if msg:
        return msg.decode("utf-8", errors="replace").strip()
    raw = res.error_message
    if raw:
        return raw.decode("utf-8", errors="replace").strip()
    return "알 수 없는 PostgreSQL 오류"


def describe_query(conn: psycopg.Connection, query: str) -> list[tuple[str, str]]:
    """SQL 쿼리를 실행하지 않고 PQprepare/PQdescribePrepared로 출력 열 [(이름, data_type)]을 구한다."""
    sql_text = query.strip().rstrip(";").strip()
    if not sql_text:
        raise DescribeError("쿼리가 비어 있음")
    try:
        with conn.transaction():
            prep = conn.pgconn.prepare(b"", sql_text.encode("utf-8"))
            if prep.status != pq.ExecStatus.COMMAND_OK:
                raise DescribeError(_pg_res_message(prep))
            desc = conn.pgconn.describe_prepared(b"")
            if desc.status != pq.ExecStatus.COMMAND_OK:
                raise DescribeError(_pg_res_message(desc))
            if desc.nparams > 0:
                raise DescribeError("쿼리에 매개변수($1 등)를 쓸 수 없음")
            if desc.nfields == 0:
                raise DescribeError("쿼리가 열을 돌려주지 않음")
            fields = [(desc.fname(i).decode("utf-8"), desc.ftype(i)) for i in range(desc.nfields)]
            seen: set[str] = set()
            dups: list[str] = []
            for name, _oid in fields:
                if name in seen and name not in dups:
                    dups.append(name)
                seen.add(name)
            if dups:
                raise DescribeError(f"출력 열 이름이 중복됨: {', '.join(dups)}")
            oids = sorted({oid for _name, oid in fields})
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT t.oid, "
                    "CASE "
                    "  WHEN t.oid = 705 THEN 'text' "
                    "  WHEN t.typcategory = 'A' OR (t.typelem <> 0 AND t.typlen = -1) "
                    "       OR t.typtype IN ('d', 'e', 'c') OR n.nspname <> 'pg_catalog' "
                    "  THEN t.typname "
                    "  ELSE format_type(t.oid, NULL) "
                    "END "
                    "FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
                    "WHERE t.oid = ANY(%s)",
                    (oids,),
                )
                type_map = {int(oid): str(tname) for oid, tname in cur.fetchall()}
            return [(name, type_map.get(oid, "unknown")) for name, oid in fields]
    except DescribeError:
        raise
    except psycopg.Error as exc:
        raise DescribeError(pg_message(exc)) from None


def reader_view_columns(dbname: str | None = None) -> dict[str, list[tuple[str, str]]]:
    with reader(dbname) as conn:
        return read_view_columns(conn)


def read_load_record(conn: psycopg.Connection, tables: Sequence[str] | None = None,
                     *, include_fingerprints: bool = True) -> LoadRecord | None:
    """지난 적재 기록. 없으면 None(아직 적재한 적이 없거나, 기록을 남기기 전의 dashkit으로 적재한 DB).

    tables가 있으면 그 테이블들의 지문만 읽는다(Studio가 테이블 하나만 볼 때).
    tables=[]이거나 include_fingerprints=False면 row_fingerprint를 조회하지 않는다.
    row_fingerprint 조회에 권한이 없으면(InsufficientPrivilege) fingerprints_readable=False로 돌려준다.
    """
    meta = sql.Identifier(record.META)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SELECT snapshot FROM {}.{} WHERE id = 1").format(
                    meta, sql.Identifier(record.SNAPSHOT_TABLE)))
                row = cur.fetchone()
                if row is None or not isinstance(row[0], dict):
                    return None
                out = LoadRecord(row[0], fingerprints_readable=bool(include_fingerprints))
        if include_fingerprints and (tables is None or len(tables) > 0):
            try:
                with conn.transaction():
                    with conn.cursor() as cur:
                        stmt = sql.SQL("SELECT table_name, row_key, row_hash FROM {}.{}").format(
                            meta, sql.Identifier(record.FINGERPRINT_TABLE))
                        if tables is not None:
                            cur.execute(stmt + sql.SQL(" WHERE table_name = ANY(%s)"), (list(tables),))
                        else:
                            cur.execute(stmt)
                        for table, key, digest in cur:
                            out.fingerprints.setdefault(table, []).append((key, digest))
            except psycopg.errors.InsufficientPrivilege:
                out.fingerprints_readable = False
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    stmt = sql.SQL("SELECT table_name, file, sha256, rows, inserted FROM {}.{}").format(
                        meta, sql.Identifier(record.BATCH_TABLE))
                    if tables is not None:
                        cur.execute(stmt + sql.SQL(" WHERE table_name = ANY(%s)"), (list(tables),))
                    else:
                        cur.execute(stmt)
                    for table, file, digest, rows, inserted in cur:
                        out.batches.setdefault(table, {})[file] = (digest, int(rows), int(inserted))
        except (psycopg.errors.UndefinedTable, psycopg.errors.InsufficientPrivilege):
            pass
        return out
    except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName, psycopg.errors.InsufficientPrivilege):
        return None
    except psycopg.Error as exc:
        raise CurrentError(f"적재 기록을 읽지 못함: {pg_message(exc)}") from None
