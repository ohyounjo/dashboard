"""CSV → raw 테이블 → dash 뷰 (reference/plan.md 6-2절 2번, 누적 테이블은 reference/data-design.md 4·5절).

1단계(DB 접속 전): replace 테이블의 CSV를 schema.yaml 규칙으로 검증한다. 오류가 하나라도 있으면 DB를 건드리지 않는다.
2단계: 데이터 DB의 쓰기 잠금(dashkit/project/locks.py)을 잡고, 이 프로젝트 폴더의 끊긴 폴더 작업을 정리한다
        (dashkit/ingest/recover.py, load 방식). 누적(append) 테이블의 배치 파일 이름을 확인한다.
3단계: 트랜잭션 하나에서 raw·dash 스키마를 통째로 다시 만든다. 어느 단계든 실패하면 롤백한다.
        replace 테이블은 지금처럼 CSV를 그대로 넣고, append 테이블은 빈 테이블에 배치를 번호순으로 하나씩
        추가 적재와 같은 함수(dashkit/ingest/apply.py의 stage_batch → apply_batch)로 넣는다(5-7).
        같은 트랜잭션에서 적재 기록(dashkit_meta: 행 지문, 배치 기록, 적재 스냅샷, loader/record.py)도 다시 만든다.
        `dashkit plan`이 dash_reader 계정만으로 원본 파일과 지난 적재를 비교하는 데 쓴다.
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
LOCK_WAIT_MS = 10_000          # 쓰기 잠금을 기다리는 시간(CLI·Studio의 apply). 관리 서비스는 MANAGE_LOCK_WAIT_MS
MAX_KEYS_SHOWN = 20


def table_mode(table: dict) -> str:
    return table.get("mode") or "replace"


def inspect_role() -> str:
    """운용자 조회용 읽기 계정(dash_inspect). 로더가 적재 때마다 raw·dash·dashkit_meta의 SELECT를 준다."""
    return os.environ.get("DATA_DB_INSPECT_USER") or "dash_inspect"

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


def load(project: Project, *, dbname: str | None = None, lock_wait_ms: int = LOCK_WAIT_MS) -> dict[str, int]:
    """원본 파일을 검증하고 적재한다. 테이블 → 적재 행 수(적재 순서). 실패하면 LoadError."""
    from ..ingest import recover as ingest_recover
    from ..project import locks

    order = table_order(project)
    tables = project.schema["tables"]
    replace_order = [t for t in order if table_mode(tables[t]) != "append"]
    data, errors = validate_csv(project.schema, project.data_dir, replace_order)
    if errors:
        raise LoadError(errors)

    as_of_sql = _as_of_body(project)

    try:
        conn = db.owner_conn(dbname)
    except (psycopg.Error, RuntimeError) as exc:
        raise LoadError([f"DB 접속 실패: {_pg_message(exc)}"]) from None
    try:
        try:
            locks.acquire(conn, lock_wait_ms)
        except locks.LockBusy:
            raise LoadError(["다른 쓰기 작업(데이터 추가·적재)이 진행 중이라 쓰기 잠금을 잡지 못함. 끝난 뒤 다시 실행하세요"]) from None
        except psycopg.Error as exc:
            raise LoadError([f"DB 오류: {_pg_message(exc)}"]) from None
        try:
            outcomes = ingest_recover.recover(project, conn, mode="load")
            if any(o.kind == "config" for o in outcomes):
                # 설정 파일이 복구로 바뀌었다(이미 읽은 설정은 낡았다). 다시 읽고 실행해야 한다
                raise LoadError(["끝나지 않은 구조 변경을 정리했습니다(설정 파일이 바뀌었을 수 있음). 다시 실행하세요"])
        except ingest_recover.RecoverError as exc:
            raise LoadError(exc.errors) from None
        counts = _rebuild_on(project, conn, data, order, as_of_sql)
        from ..ingest.compact import clear_merged

        clear_merged(project)      # 합친 뒤의 다시 만들기가 성공했으니 합치기 전의 배치(_merged/)는 지운다(설계서 6-4)
    except _StepError as exc:
        raise LoadError(exc.errors) from None
    except psycopg.Error as exc:
        raise LoadError([f"DB 오류: {_pg_message(exc)}"]) from None
    finally:
        locks.release(conn)
        conn.close()
    return counts


def rebuild_locked(project: Project, conn: psycopg.Connection, *,
                   exclude: frozenset[tuple[str, str]] = frozenset(), include: tuple = ()) -> dict[str, int]:
    """쓰기 잠금을 이미 쥔 접속에서 다시 만든다(되돌리기·되살리기, 복구가 쓴다). 실패하면 LoadError.

    부른 쪽의 트랜잭션 안이면 저장점으로 돌고 커밋은 부른 쪽이 한다. exclude의 (테이블, 배치 이름)은 읽지 않는다
    (그 파일이 처음부터 없었을 때와 같은 결과). include의 원본 파일(batches.SourceFile, 예: 되살릴 `_removed/`의 파일)은
    번호순 자리에 끼워 읽는다. 끊긴 폴더 작업의 복구는 부른 쪽이 먼저 한다.
    """
    order = table_order(project)
    tables = project.schema["tables"]
    data, errors = validate_csv(project.schema, project.data_dir, [t for t in order if table_mode(tables[t]) != "append"])
    if errors:
        raise LoadError(errors)
    try:
        return _rebuild_on(project, conn, data, order, _as_of_body(project), exclude, include)
    except _StepError as exc:
        raise LoadError(exc.errors) from None
    except psycopg.Error as exc:
        raise LoadError([f"DB 오류: {_pg_message(exc)}"]) from None


def _rebuild_on(project: Project, conn: psycopg.Connection, data: dict[str, TableData], order: list[str],
                as_of_sql: str, exclude: frozenset[tuple[str, str]] = frozenset(), include: tuple = ()) -> dict[str, int]:
    try:
        record.fingerprint_key()
    except record.FingerprintKeyError as exc:
        raise LoadError([str(exc)]) from None
    sources, errors = _sources(project, order)
    if errors:
        raise LoadError(errors)
    sources = {t: [f for f in files if (t, f.name) not in exclude] for t, files in sources.items()}
    for extra in include:
        files = sources.setdefault(extra.table, [])
        if any(f.number == extra.number for f in files if f.number is not None):
            raise LoadError([f"data/{extra.table}/{extra.name}:1:-: 배치 번호 {extra.number}가 폴더의 배치와 겹침"])
        files.append(extra)
        files.sort(key=lambda f: -1 if f.number is None else f.number)
    with conn.transaction():
        with conn.cursor() as cur:
            return _rebuild(project, cur, order, data, sources, as_of_sql)


def _sources(project: Project, order: list[str]) -> tuple[dict[str, list], list[str]]:
    """append 테이블의 원본 파일(읽는 순서)과 이름 오류. replace 테이블은 넣지 않는다."""
    from ..ingest import batches

    out: dict[str, list] = {}
    errors: list[str] = []
    for tname in order:
        table = project.schema["tables"][tname]
        if table_mode(table) != "append":
            continue
        files, errs = batches.sources(project.data_dir, tname, "append")
        out[tname] = files
        errors += errs
    return out, errors


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
             data: dict[str, TableData], sources: dict[str, list], as_of_sql: str) -> dict[str, int]:
    from ..ingest import apply as ingest_apply
    from ..ingest import batches, ledger

    tables = project.schema["tables"]
    raw, dash = sql.Identifier(RAW), sql.Identifier(DASH)

    cur.execute(sql.SQL("SET LOCAL lock_timeout = {}").format(sql.Literal(LOCK_TIMEOUT)))
    _step(cur, "스키마 삭제", sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(dash))
    _step(cur, "스키마 삭제", sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(raw))
    _step(cur, "스키마 생성", sql.SQL("CREATE SCHEMA {}").format(raw))

    # 테이블(참조되는 테이블이 먼저)
    for tname in order:
        _step(cur, f"schema.yaml:tables.{tname}: 테이블 생성", _create_table_sql(tname, tables[tname]))

    # 데이터. replace 테이블은 CSV 그대로, append 테이블은 배치를 번호순으로 apply_batch로(추가 적재와 같은 길)
    counts: dict[str, int] = {}
    batch_rows: list[tuple] = []
    errors: list[str] = []
    for tname in order:
        if tname in data:
            td = data[tname]
            stmt = sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
                raw, sql.Identifier(tname), sql.SQL(", ").join(map(sql.Identifier, td.columns)))
            try:
                with cur.copy(stmt) as copy:
                    for row in td.rows:
                        copy.write_row(row)
            except psycopg.Error as exc:
                raise _StepError([f"data/{tname}.csv: 적재 실패: {_pg_message(exc)}"]) from None
            counts[tname] = len(td.rows)
            n = len(td.rows)
            batch_rows.append(ledger.row(tname, f"{tname}.csv",
                                         batches.content_hash(batches.first_file(project.data_dir, tname)),
                                         {"total": n, "inserted": n, "updated": 0, "skipped": 0, "unchanged": 0}))
            continue
        counts[tname] = 0
        failed = False
        for src in sources.get(tname, []):
            label = ingest_apply.source_label(src)
            try:
                staged = ingest_apply.stage_source(cur, project.schema, src)
                try:
                    if not staged.ok:
                        errors += staged.errors
                        failed = True
                        continue
                    if failed or errors:
                        continue        # 앞 배치가 실패했으면 넣지 않고 나머지 파일의 검증 오류만 모은다
                    result = ingest_apply.apply_batch(cur, project.schema, staged)
                finally:
                    ingest_apply.drop_stage(cur, staged)
            except ingest_apply.BatchError as exc:
                errors += [e if e.startswith(("data/", label)) else f"{label}: {e}" for e in (exc.errors or [exc.message])]
                failed = True
                continue
            except psycopg.Error as exc:
                raise _StepError([f"{label}: 적재 실패: {_pg_message(exc)}"]) from None
            counts[tname] += result["inserted"]
            batch_rows.append(ledger.row(tname, src.name, staged.sha256 or "", result))
    if errors:
        raise _StepError(errors)

    # dash 스키마, 기준일 함수
    _step(cur, "스키마 생성", sql.SQL("CREATE SCHEMA {}").format(dash))
    _step(cur, "dash.as_of() 생성", sql.SQL(
        "CREATE FUNCTION {}.as_of() RETURNS date LANGUAGE sql STABLE AS {}"
    ).format(dash, sql.Literal(as_of_sql)))

    # 데이터셋 뷰(설정 순서. 뒤 뷰가 앞 뷰를 쓸 수 있다)
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

    # 계보: 데이터셋 뷰가 읽는 raw 테이블·열(PostgreSQL의 의존 정보. 뷰가 뷰를 쓰면 끝까지 따라간다. 설계서 7-2)
    sources = read_sources(cur)

    # checks(위반 행이 하나라도 있으면 실패). 모든 규칙을 실행해 결과를 모은다. 추가 적재와 같은 함수다
    errors = ingest_apply.run_checks(cur, project.schema, order)
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

    # 읽기 전용 계정 권한. 대시보드용 dash_reader는 dash만, 운용자 조회용 dash_inspect(있으면)는 raw·dash 읽기만
    reader = sql.Identifier(os.environ.get("DATA_DB_READER_USER") or "dash_reader")
    label = "읽기 계정 권한 부여"
    _step(cur, label, sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(raw))
    _step(cur, label, sql.SQL("REVOKE ALL ON FUNCTION {}.as_of() FROM PUBLIC").format(dash))
    _step(cur, label, sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(dash, reader))
    _step(cur, label, sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(dash, reader))
    _step(cur, label, sql.SQL("GRANT EXECUTE ON FUNCTION {}.as_of() TO {}").format(dash, reader))
    readers: list[str] = []
    cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (inspect_role(),))
    if cur.fetchone():
        inspect = sql.Identifier(inspect_role())
        readers.append(inspect_role())
        for schema in (raw, dash):
            _step(cur, label, sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(schema, inspect))
            _step(cur, label, sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(schema, inspect))
        _step(cur, label, sql.SQL("GRANT EXECUTE ON FUNCTION {}.as_of() TO {}").format(dash, inspect))

    # 적재 기록(dash_inspect가 row_fingerprint를 읽고, dash_reader는 load_snapshot·batch만 읽는다).
    # 원본 값은 넣지 않고(PLAN_FINGERPRINT_KEY 기반 HMAC-SHA256), 누적 테이블은 행 지문을 남기지 않는다.
    try:
        fkey = record.fingerprint_key()
    except record.FingerprintKeyError as exc:
        raise _StepError([str(exc)]) from None
    prints = {t: record.fingerprints(tables[t], data[t].rows, table_name=t, key=fkey)
              for t in order if t in data}
    snap = record.snapshot(project.name, project.schema, project.dashboard, counts,
                           sources=sources, fingerprint_key_id=record.key_id(fkey))
    try:
        record.write(cur, snap, prints, os.environ.get("DATA_DB_READER_USER") or "dash_reader", batch_rows, readers)
    except psycopg.Error as exc:
        raise _StepError([f"적재 기록({record.META}) 쓰기: {_pg_message(exc)}"]) from None
    return {t: counts[t] for t in order}


def read_sources(cur: psycopg.Cursor) -> dict[str, dict[str, list[str]]]:
    """dash 뷰 → {raw 테이블: [그 뷰가 읽는 열(이름순)]}. 열 없이 테이블만 읽으면(count(*) 등) 빈 목록이다.
    뷰가 다른 dash 뷰를 읽으면 그 뷰의 원본까지 따라가 합친다. 쿼리 글에서 이름을 찾는 것보다 정확하다."""
    cur.execute(
        "SELECT view_name, table_schema, table_name, column_name FROM information_schema.view_column_usage "
        "WHERE view_schema = %s UNION ALL "
        "SELECT view_name, table_schema, table_name, NULL FROM information_schema.view_table_usage WHERE view_schema = %s",
        (DASH, DASH))
    direct: dict[str, dict[tuple[str, str], set[str]]] = {}
    for view, schema, table, column in cur.fetchall():
        cols = direct.setdefault(view, {}).setdefault((schema, table), set())
        if column:
            cols.add(column)

    def resolve(view: str, seen: frozenset[str]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for (schema, table), cols in direct.get(view, {}).items():
            if schema == RAW:
                out.setdefault(table, set()).update(cols)
            elif schema == DASH and table not in seen:
                for t, c in resolve(table, seen | {table}).items():
                    out.setdefault(t, set()).update(c)
        return out

    return {view: {t: sorted(c) for t, c in sorted(resolve(view, frozenset({view})).items())} for view in direct}


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


__all__ = ["LoadError", "load", "rebuild_locked", "table_mode"]
