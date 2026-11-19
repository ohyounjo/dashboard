"""CSV → raw 테이블 → dash 뷰 (reference/plan.md 6-2절 2번).

1단계(DB 접속 전): CSV를 schema.yaml 규칙으로 검증한다. 오류가 하나라도 있으면 DB를 건드리지 않는다.
2단계: 트랜잭션 하나에서 raw·dash 스키마를 통째로 다시 만든다. 어느 단계든 실패하면 롤백한다.
        같은 트랜잭션에서 적재 기록(dashkit_meta: 행 지문과 적재 스냅샷, loader/record.py)도 다시 만든다.
        `dashkit plan`이 dash_reader 계정만으로 CSV와 지난 적재를 비교하는 데 쓴다.
"""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

from .. import db
from ..config import Project, check_view_columns, resolve_as_of, table_order
from . import record
from .validate import TableData, validate_csv

RAW = "raw"
DASH = "dash"
LOCK_TIMEOUT = "10s"
MAX_KEYS_SHOWN = 20

_PG_TYPES = {
    "text": "text",
    "enum": "text",
    "integer": "bigint",
    "number": "numeric",
    "boolean": "boolean",
    "date": "date",
    "timestamp": "timestamptz",
}


class LoadError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


def _pg_message(exc: BaseException) -> str:
    diag = getattr(exc, "diag", None)
    msg = (diag.message_primary if diag and diag.message_primary else str(exc)).strip()
    detail = diag.message_detail if diag else None
    return f"{msg} ({detail})" if detail else msg


def load(project: Project, *, dbname: str | None = None) -> dict[str, int]:
    """CSV를 검증하고 적재한다. 테이블 → 적재 행 수(적재 순서). 실패하면 LoadError."""
    order = table_order(project)
    data, errors = validate_csv(project.schema, project.data_dir, order)
    if errors:
        raise LoadError(errors)

    as_of_sql = _as_of_body(project)

    try:
        conn = db.owner_conn(dbname)
    except (psycopg.Error, RuntimeError) as exc:
        raise LoadError([f"DB 접속 실패: {_pg_message(exc)}"]) from None
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                _rebuild(project, cur, order, data, as_of_sql)
    except _StepError as exc:
        raise LoadError(exc.errors) from None
    except psycopg.Error as exc:
        raise LoadError([f"DB 오류: {_pg_message(exc)}"]) from None
    finally:
        conn.close()
    return {t: len(data[t].rows) for t in order}


class _StepError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


def _as_of_body(project: Project) -> str:
    """dash.as_of() 함수 본문 SQL(문자열 리터럴은 이미 따옴표 처리됨)."""
    if project.dashboard["app"]["as_of"] != "today":
        d = resolve_as_of(project)
        return f"SELECT DATE '{d.isoformat()}'"
    tz = os.environ.get("TZ") or "UTC"
    try:
        ZoneInfo(tz)
    except Exception:
        raise LoadError([f"TZ 환경변수 '{tz}'가 올바른 시간대 이름이 아님"]) from None
    quoted = tz.replace("'", "''")
    return f"SELECT (now() AT TIME ZONE '{quoted}')::date"


def _step(cur: psycopg.Cursor, label: str, query) -> None:
    try:
        cur.execute(query)
    except psycopg.Error as exc:
        raise _StepError([f"{label}: {_pg_message(exc)}"]) from None


def _rebuild(project: Project, cur: psycopg.Cursor, order: list[str],
             data: dict[str, TableData], as_of_sql: str) -> None:
    tables = project.schema["tables"]
    raw, dash = sql.Identifier(RAW), sql.Identifier(DASH)

    cur.execute(sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(LOCK_TIMEOUT)))
    _step(cur, "스키마 삭제", sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(dash))
    _step(cur, "스키마 삭제", sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(raw))
    _step(cur, "스키마 생성", sql.SQL("CREATE SCHEMA {}").format(raw))

    # 테이블(참조되는 테이블이 먼저)
    for tname in order:
        _step(cur, f"schema.yaml:tables.{tname}: 테이블 생성", _create_table_sql(tname, tables[tname]))

    # 데이터
    for tname in order:
        td = data[tname]
        stmt = sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
            raw, sql.Identifier(tname), sql.SQL(", ").join(map(sql.Identifier, td.columns)))
        try:
            with cur.copy(stmt) as copy:
                for row in td.rows:
                    copy.write_row(row)
        except psycopg.Error as exc:
            raise _StepError([f"data/{tname}.csv: 적재 실패: {_pg_message(exc)}"]) from None

    # dash 스키마, 기준일 함수
    _step(cur, "스키마 생성", sql.SQL("CREATE SCHEMA {}").format(dash))
    _step(cur, "dash.as_of() 생성", sql.SQL(
        "CREATE FUNCTION {}.as_of() RETURNS date LANGUAGE sql STABLE AS {}"
    ).format(dash, sql.Literal(as_of_sql)))

    # 데이터셋 뷰(설정 순서. 뒤 뷰가 앞 뷰를 쓸 수 있다)
    errors: list[str] = []
    for key, ds in project.dashboard["datasets"].items():
        if "table" in ds:
            body = sql.SQL("SELECT * FROM {}.{}").format(raw, sql.Identifier(ds["table"]))
        else:
            body = sql.SQL(ds["sql"].strip().rstrip(";").strip())
        stmt = sql.SQL("CREATE VIEW {}.{} AS ").format(dash, sql.Identifier(key)) + body
        try:
            with cur.connection.transaction():
                cur.execute(stmt)
        except psycopg.Error as exc:
            errors.append(f"dashboard.yaml:datasets.{key}: 뷰 생성 실패: {_pg_message(exc)}")
    if errors:
        raise _StepError(errors)

    # checks(위반 행이 하나라도 있으면 실패). 모든 규칙을 실행해 결과를 모은다.
    for tname in order:
        for i, check in enumerate(tables[tname]["checks"]):
            try:
                with cur.connection.transaction():
                    cur.execute(sql.SQL(check["sql"].strip().rstrip(";").strip()))
                    rows = cur.fetchall() if cur.description else []
            except psycopg.Error as exc:
                errors.append(f"schema.yaml:tables.{tname}.checks[{i}] ({check['name']}): SQL 오류: {_pg_message(exc)}")
                continue
            if rows:
                keys = [str(r[0]) for r in rows[:MAX_KEYS_SHOWN]]
                more = f" 외 {len(rows) - MAX_KEYS_SHOWN}건" if len(rows) > MAX_KEYS_SHOWN else ""
                errors.append(f"{check['name']}: 위반 키 {', '.join(keys)}{more}")
    if errors:
        raise _StepError(errors)

    # 설정이 가리키는 열이 실제 뷰에 있는지
    cur.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = %s",
        (DASH,),
    )
    view_columns: dict[str, set[str]] = {}
    for tbl, col in cur.fetchall():
        view_columns.setdefault(tbl, set()).add(col)
    errors = check_view_columns(project, view_columns)
    if errors:
        raise _StepError(errors)

    # 읽기 전용 계정 권한
    reader = sql.Identifier(os.environ.get("DATA_DB_READER_USER") or "dash_reader")
    label = "읽기 계정 권한 부여"
    _step(cur, label, sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(raw))
    _step(cur, label, sql.SQL("REVOKE ALL ON FUNCTION {}.as_of() FROM PUBLIC").format(dash))
    _step(cur, label, sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(dash, reader))
    _step(cur, label, sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(dash, reader))
    _step(cur, label, sql.SQL("GRANT EXECUTE ON FUNCTION {}.as_of() TO {}").format(dash, reader))

    # 적재 기록(plan이 dash_reader로 읽는다). 원본 값은 넣지 않는다(기본 키의 해시와 행 해시뿐)
    prints = {t: record.fingerprints(tables[t], data[t].rows) for t in order}
    snap = record.snapshot(project.name, project.schema, project.dashboard, {t: len(data[t].rows) for t in order})
    try:
        record.write(cur, snap, prints, os.environ.get("DATA_DB_READER_USER") or "dash_reader")
    except psycopg.Error as exc:
        raise _StepError([f"적재 기록({record.META}) 쓰기: {_pg_message(exc)}"]) from None


def _create_table_sql(tname: str, table: dict) -> sql.Composed:
    parts: list[sql.Composable] = []
    for cname, spec in table["columns"].items():
        col = sql.SQL("{} {}").format(sql.Identifier(cname), sql.SQL(_PG_TYPES[spec["type"]]))
        if spec.get("required"):
            col = col + sql.SQL(" NOT NULL")
        parts.append(col)
    parts.append(sql.SQL("PRIMARY KEY ({})").format(
        sql.SQL(", ").join(map(sql.Identifier, table["primary_key"]))))
    for cname, spec in table["columns"].items():
        if "ref" in spec:
            rt, rc = spec["ref"].split(".", 1)
            parts.append(sql.SQL("FOREIGN KEY ({}) REFERENCES {}.{} ({})").format(
                sql.Identifier(cname), sql.Identifier(RAW), sql.Identifier(rt), sql.Identifier(rc)))
    return sql.SQL("CREATE TABLE {}.{} ({})").format(
        sql.Identifier(RAW), sql.Identifier(tname), sql.SQL(", ").join(parts))


__all__ = ["LoadError", "load"]
