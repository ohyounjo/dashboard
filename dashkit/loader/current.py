"""지금 DB에 적재된 상태를 읽는다. `dashkit plan`(dashkit/plan.py)의 "읽어 오는 부분"이다. 아무것도 바꾸지 않는다.

**dash_reader 계정만 쓴다**(읽기 경로). raw 스키마는 읽지 않는다. 지난 적재의 내용은 로더가 남긴 적재 기록
(`dashkit_meta`, loader/record.py)으로 안다. owner 계정의 환경변수가 없어도 동작한다.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

import psycopg
from psycopg import sql

from .. import db
from . import record

DASH = "dash"


class CurrentError(Exception):
    """지금 상태를 읽지 못함(접속 실패, 시간 초과)."""


@dataclass
class LoadRecord:
    """지난 적재의 기록. snapshot은 record.snapshot의 형식, fingerprints는 테이블 → [(row_key, row_hash)]."""
    snapshot: dict[str, Any]
    fingerprints: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    @property
    def project(self) -> str | None:
        return self.snapshot.get("project")


def pg_message(exc: BaseException) -> str:
    diag = getattr(exc, "diag", None)
    msg = (diag.message_primary if diag and diag.message_primary else str(exc)).strip()
    return msg or type(exc).__name__


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


def read_view_columns(conn: psycopg.Connection, names: Sequence[str] | None = None) -> dict[str, list[tuple[str, str]]]:
    """dash 스키마의 뷰 → [(열, 타입)](ordinal 순서). 타입 표기는 sync.read_view_columns와 같다."""
    query = ("SELECT table_name, column_name, data_type, udt_name FROM information_schema.columns "
             "WHERE table_schema = %s")
    params: list[Any] = [DASH]
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
        raise CurrentError(f"뷰 열 정보를 읽지 못함: {pg_message(exc)}") from None
    return out


def reader_view_columns(dbname: str | None = None) -> dict[str, list[tuple[str, str]]]:
    with reader(dbname) as conn:
        return read_view_columns(conn)


def read_load_record(conn: psycopg.Connection, tables: Sequence[str] | None = None) -> LoadRecord | None:
    """지난 적재 기록. 없으면 None(아직 적재한 적이 없거나, 기록을 남기기 전의 dashkit으로 적재한 DB).

    tables가 있으면 그 테이블들의 지문만 읽는다(Studio가 테이블 하나만 볼 때).
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
                out = LoadRecord(row[0])
                stmt = sql.SQL("SELECT table_name, row_key, row_hash FROM {}.{}").format(
                    meta, sql.Identifier(record.FINGERPRINT_TABLE))
                if tables is not None:
                    cur.execute(stmt + sql.SQL(" WHERE table_name = ANY(%s)"), (list(tables),))
                else:
                    cur.execute(stmt)
                for table, key, digest in cur:
                    out.fingerprints.setdefault(table, []).append((key, digest))
                return out
    except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName, psycopg.errors.InsufficientPrivilege):
        return None
    except psycopg.Error as exc:
        raise CurrentError(f"적재 기록을 읽지 못함: {pg_message(exc)}") from None
