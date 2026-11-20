"""배치 기록(`dashkit_meta.batch`)과 적재 스냅샷 읽기·쓰기(contracts/ingest.md "배치 기록").

로더가 load 때 통째로 다시 만들고(loader/record.py의 write), 추가 적재·되돌리기·합치기가 줄을 더하고 지운다.
모두 owner 접속의 트랜잭션 안에서 부른다. 원본 값은 없다(이름, 해시, 행 수).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

import psycopg
from psycopg import sql

from ..loader.record import BATCH_COLUMNS, BATCH_TABLE, META, SNAPSHOT_TABLE
from .batches import stamp_of


@dataclass(frozen=True)
class Recorded:
    table: str
    file: str
    sha256: str
    added_at: datetime | None
    rows: int
    inserted: int
    updated: int
    skipped: int
    unchanged: int


@dataclass(frozen=True)
class Loaded:
    """DB에 적재된 프로젝트(load_snapshot). 아직 적재한 적이 없으면 project가 None."""
    project: str | None
    data_revision: str | None
    loaded_at: datetime | None
    snapshot: Mapping[str, Any]


def loaded(cur: psycopg.Cursor) -> Loaded:
    try:
        with cur.connection.transaction():
            cur.execute(sql.SQL("SELECT loaded_at, snapshot FROM {}.{} WHERE id = 1").format(
                sql.Identifier(META), sql.Identifier(SNAPSHOT_TABLE)))
            row = cur.fetchone()
    except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName):
        return Loaded(None, None, None, {})
    if row is None or not isinstance(row[1], dict):
        return Loaded(None, None, None, {})
    snap = row[1]
    return Loaded(snap.get("project"), snap.get("data_revision"), row[0], snap)


def read(cur: psycopg.Cursor, tables: Iterable[str] | None = None) -> dict[str, dict[str, Recorded]]:
    """{테이블: {파일: 기록}}. 표가 없으면(기록을 남기기 전의 dashkit으로 적재한 DB) 빈 dict."""
    stmt = sql.SQL("SELECT {} FROM {}.{}").format(
        sql.SQL(", ").join(map(sql.Identifier, BATCH_COLUMNS)), sql.Identifier(META), sql.Identifier(BATCH_TABLE))
    params: tuple[Any, ...] = ()
    if tables is not None:
        stmt += sql.SQL(" WHERE table_name = ANY(%s)")
        params = (list(tables),)
    out: dict[str, dict[str, Recorded]] = {}
    try:
        with cur.connection.transaction():
            cur.execute(stmt, params)
            rows = cur.fetchall()
    except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName):
        return out
    for r in rows:
        rec = Recorded(*r)
        out.setdefault(rec.table, {})[rec.file] = rec
    return out


def add(cur: psycopg.Cursor, table: str, file: str, sha256: str, counts: Mapping[str, int]) -> None:
    cur.execute(sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
        sql.Identifier(META), sql.Identifier(BATCH_TABLE), sql.SQL(", ").join(map(sql.Identifier, BATCH_COLUMNS)),
        sql.SQL(", ").join(sql.Placeholder() * len(BATCH_COLUMNS))),
        (table, file, sha256, stamp_of(file), int(counts["total"]), int(counts["inserted"]), int(counts["updated"]),
         int(counts["skipped"]), int(counts["unchanged"])))


def row(table: str, file: str, sha256: str, counts: Mapping[str, int]) -> tuple[Any, ...]:
    """로더가 record.write에 넘기는 배치 기록 한 줄(BATCH_COLUMNS 순서)."""
    return (table, file, sha256, stamp_of(file), int(counts["total"]), int(counts["inserted"]),
            int(counts["updated"]), int(counts["skipped"]), int(counts["unchanged"]))


def remove(cur: psycopg.Cursor, table: str, file: str) -> None:
    cur.execute(sql.SQL("DELETE FROM {}.{} WHERE table_name = %s AND file = %s").format(
        sql.Identifier(META), sql.Identifier(BATCH_TABLE)), (table, file))
