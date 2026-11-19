"""plan(dashkit.plan.make_plan)을 실제 DB와 함께 확인한다. 순수 함수의 검사는 tests/test_plan_diff.py.

- 숫자 검사: 임시 DB에 예시를 적재하고, 임시 폴더의 프로젝트 사본을 고친 뒤 plan의 숫자를 본다
  (tests/test_loader.py의 임시 DB, tests/test_sync.py의 임시 프로젝트 방식). 지난 번들은 sync가 쓰는 함수
  (build_bundle, write_build)로 임시 폴더에 만든다. Superset에는 닿지 않는다.
- 현재 스택 검사: 방금 적용된 프로젝트(env PROJECT)에 plan을 돌려 "변경 없음"이고 아무것도 바뀌지 않았는지 본다.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
import yaml
from psycopg import sql

from dashkit import db, plan
from dashkit.__main__ import main
from dashkit.config import load_project
from dashkit.loader import current, load
from dashkit.sync.bundle import build_bundle, write_build, zip_bundle

from conftest import PROJECTS, broken_csv_copy, copy_project, live_project, read_csv, write_csv

PROJECT = "devtracker"
EXAMPLES = ("devtracker", "sales-sample", "chart-gallery")
pytestmark = pytest.mark.integration


# ---------------------------------------------------------------- 도구

def _create_db() -> str:
    name = f"dash_plan_{uuid.uuid4().hex[:8]}"
    with db.owner_conn(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return name


def _drop_db(name: str) -> None:
    with db.owner_conn(autocommit=True) as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


@pytest.fixture(scope="module")
def empty_db():
    name = _create_db()
    yield name
    _drop_db(name)


@pytest.fixture(scope="module")
def applied(tmp_path_factory):
    """임시 DB에 devtracker를 적재하고, 그 뷰 열로 만든 번들을 임시 빌드 폴더에 둔다(적용 직후와 같은 상태)."""
    name = _create_db()
    build = tmp_path_factory.mktemp("build")
    project = load_project(PROJECT, PROJECTS)
    load(project, dbname=name)
    files = build_bundle(project, current.reader_view_columns(name))
    write_build(files, zip_bundle(files), build / PROJECT)
    yield name, build
    _drop_db(name)


def db_fingerprint(dbname: str | None) -> dict:
    """DB의 내용과 구조. plan 전후에 같아야 한다."""
    out: dict = {}
    with db.owner_conn(dbname) as conn:
        out["relations"] = conn.execute(
            "SELECT n.nspname, c.relname, c.relkind, c.oid::int FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname IN ('raw', 'dash') OR n.nspname LIKE 'pg_temp%' ORDER BY 1, 2").fetchall()
        out["columns"] = conn.execute(
            "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema IN ('raw', 'dash') ORDER BY 1, 2, ordinal_position").fetchall()
        out["prepared"] = conn.execute("SELECT count(*) FROM pg_prepared_xacts").fetchone()[0]
        for (table,) in conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'raw' ORDER BY 1").fetchall():
            rows = conn.execute(sql.SQL("SELECT t::text FROM raw.{} t ORDER BY 1").format(sql.Identifier(table))).fetchall()
            out[f"raw.{table}"] = (len(rows), hashlib.sha256(repr(rows).encode()).hexdigest())
    return out


def tree_fingerprint(path: Path) -> dict:
    """폴더 안 모든 파일의 내용과 수정 시각."""
    if not path.exists():
        return {}
    return {str(f.relative_to(path)): (hashlib.sha256(f.read_bytes()).hexdigest(), f.stat().st_mtime_ns)
            for f in sorted(path.rglob("*")) if f.is_file()}


def by(items: list[dict], key: str) -> dict[str, dict]:
    return {i[key]: i for i in items}


def edit_yaml(path: Path, fn) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(doc)
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


def assert_nothing_to_do(result: dict) -> None:
    assert result["changed"] is False, plan.format_plan(result)
    assert all(t["status"] == "unchanged" for t in result["data"]), result["data"]
    assert all(v["status"] == "unchanged" for v in result["views"]), result["views"]
    s = result["superset"]
    assert s["first"] is False and s["database"] == "unchanged" and s["other"] == [] and s["assumed_columns"] == []
    assert s["replaces"] is None
    for kind in ("datasets", "charts", "dashboards"):
        assert (s[kind]["added"], s[kind]["changed"], s[kind]["removed"], s[kind]["unknown"]) == ([], [], [], []), s[kind]


# ---------------------------------------------------------------- 임시 DB: 숫자

def test_right_after_apply_there_is_nothing_to_do(applied):
    dbname, build = applied
    project = load_project(PROJECT, PROJECTS)
    result = plan.make_plan(project, dbname=dbname, build_dir=build, state_dir=build)
    assert_nothing_to_do(result)
    assert {t["table"]: t["rows"] for t in result["data"]} == {
        t: len(read_csv(project.data_dir / f"{t}.csv")[1]) for t in project.schema["tables"]}
    assert plan.format_plan(result).splitlines()[-1] == "→ 바뀌는 것이 없음"


def test_counts_after_editing_a_copy(applied, tmp_path):
    """CSV 한 행 추가·한 행 수정, 차트 하나 추가 → 데이터 +1 ~1, 차트 +1, 그 대시보드는 배치 변경."""
    dbname, build = applied
    root = copy_project(PROJECT, tmp_path)
    path = root / PROJECT / "data" / "members.csv"
    header, rows = read_csv(path)
    rows[1]["name"] = rows[1]["name"] + " (수정)"
    rows.append({**rows[0], "id": "m990", "name": "새 사람"})
    write_csv(path, header, rows)

    added: dict[str, str] = {}

    def add_chart(doc):
        dkey, d = next(iter(doc["dashboards"].items()))
        d["charts"].append({**d["charts"][0], "key": "zz_plan_extra", "title": "plan 검사용 차트"})
        d["layout"].append(["zz_plan_extra"])
        added["dashboard"] = dkey

    edit_yaml(root / PROJECT / "dashboard.yaml", add_chart)
    before = db_fingerprint(dbname), tree_fingerprint(build)

    result = plan.make_plan(load_project(PROJECT, root), dbname=dbname, build_dir=build, state_dir=build)

    tables = by(result["data"], "table")
    m = tables.pop("members")
    assert (m["status"], m["added"], m["removed"], m["changed"]) == ("changed", 1, 0, 1)
    assert m["unchanged"] == len(rows) - 2
    assert m["keys"] == {"added": ["m990"], "removed": [], "changed": [rows[1]["id"]]}
    assert all(t["status"] == "unchanged" for t in tables.values()), tables
    assert all(v["status"] == "unchanged" for v in result["views"]), result["views"]
    s = result["superset"]
    assert (s["charts"]["added"], s["charts"]["changed"], s["charts"]["removed"]) == (["zz_plan_extra"], [], [])
    assert s["dashboards"]["changed"] == [{"key": added["dashboard"], "detail": "배치 변경"}]
    assert s["dashboards"]["added"] == [] and s["datasets"]["changed"] == []
    assert result["changed"] is True
    text = plan.format_plan(result)
    assert "members  +1행 −0행 ~1행" in text and "차트 +1 (zz_plan_extra)" in text
    assert f"대시보드 ~1 ({added['dashboard']}: 배치 변경)" in text
    assert (db_fingerprint(dbname), tree_fingerprint(build)) == before


def test_removed_rows_and_removed_chart(applied, tmp_path):
    dbname, build = applied
    root = copy_project(PROJECT, tmp_path)
    path = root / PROJECT / "data" / "kpi_snapshots.csv"
    header, rows = read_csv(path)
    write_csv(path, header, rows[:-2])
    result = plan.make_plan(load_project(PROJECT, root), dbname=dbname, build_dir=build, state_dir=build)
    k = by(result["data"], "table")["kpi_snapshots"]
    assert (k["added"], k["removed"], k["changed"], k["unchanged"]) == (0, 2, 0, len(rows) - 2)


def test_sql_view_column_added_is_found_without_running_anything(applied, tmp_path):
    """`sql:` 데이터셋에 열을 더하면 적용 전에 뷰와 데이터셋의 열 추가로 나온다(쿼리는 실행하지 않고 Describe만 한다)."""
    dbname, build = applied
    root = copy_project(PROJECT, tmp_path)

    def widen(doc):
        ds = doc["datasets"]["docs"]
        ds["sql"] = "SELECT q.*, (dash.as_of() - DATE '2026-01-01') AS zz_age_days FROM (" + ds["sql"].strip().rstrip(";") + ") q"

    edit_yaml(root / PROJECT / "dashboard.yaml", widen)
    before = db_fingerprint(dbname)
    result = plan.make_plan(load_project(PROJECT, root), dbname=dbname, build_dir=build, state_dir=build)
    views = by(result["views"], "dataset")
    assert views["docs"]["status"] == "changed"
    assert views["docs"]["columns"] == {"added": ["zz_age_days"], "removed": [], "retyped": [], "reordered": False}
    assert all(v["status"] == "unchanged" for k, v in views.items() if k != "docs")
    assert result["superset"]["datasets"]["changed"] == [{"key": "docs", "detail": "열 추가: zz_age_days"}]
    assert "dash.docs  열 추가: zz_age_days" in plan.format_plan(result)
    assert db_fingerprint(dbname) == before


def test_schema_column_added_makes_dependent_sql_views_unknown(applied, tmp_path):
    """raw 테이블의 구조가 바뀌면 그 테이블 이름이 나오는 `sql:` 뷰는 "적용해야 알 수 있음"이다."""
    dbname, build = applied
    root = copy_project(PROJECT, tmp_path)

    def add_column(doc):
        doc["tables"]["members"]["columns"]["zz_team"] = {"type": "text"}

    edit_yaml(root / PROJECT / "schema.yaml", add_column)
    path = root / PROJECT / "data" / "members.csv"
    header, rows = read_csv(path)
    write_csv(path, header + ["zz_team"], [{**r, "zz_team": "a"} for r in rows])
    project = load_project(PROJECT, root)
    result = plan.make_plan(project, dbname=dbname, build_dir=build, state_dir=build)

    m = by(result["data"], "table")["members"]
    assert m["status"] == "changed" and m["columns"]["added"] == ["zz_team"]
    assert (m["added"], m["removed"], m["changed"]) == (0, 0, 0)        # 값 비교는 양쪽에 있는 열만
    views = by(result["views"], "dataset")
    naming = {k for k, ds in project.dashboard["datasets"].items() if "sql" in ds and "members" in ds["sql"]}
    assert naming, "예시에 members를 읽는 sql 데이터셋이 있어야 함"
    for key, v in views.items():
        if key in naming:
            assert v["status"] == "unknown" and "raw.members" in v["reason"], v
        else:
            assert v["status"] == "unchanged", v
    # 열을 모르는 데이터셋은 지금 뷰의 열로 번들을 만들고 그 사실을 알린다
    assert sorted(result["superset"]["assumed_columns"]) == sorted(naming)
    assert "적용해야 알 수 있음" in plan.format_plan(result)


def test_sql_error_is_reported_as_unknown_with_the_reason(applied, tmp_path):
    dbname, build = applied
    root = copy_project(PROJECT, tmp_path)

    def typo(doc):
        doc["datasets"]["docs"]["sql"] = "SELECT no_such_column_zz FROM raw.docs"

    edit_yaml(root / PROJECT / "dashboard.yaml", lambda doc: None)   # 형식만 다시 쓴 것은 변경이 아니다
    assert_nothing_to_do(plan.make_plan(load_project(PROJECT, root), dbname=dbname, build_dir=build, state_dir=build))
    # 차트·필터가 가리키는 열은 정적 검사에서 걸리지 않게 그대로 두고 쿼리만 깨뜨린다(sql: 데이터셋의 열은 적재 때 확인한다)
    edit_yaml(root / PROJECT / "dashboard.yaml", typo)
    result = plan.make_plan(load_project(PROJECT, root), dbname=dbname, build_dir=build, state_dir=build)
    docs = by(result["views"], "dataset")["docs"]
    assert docs["status"] == "unknown" and "no_such_column_zz" in docs["reason"]
    # 실패한 Describe가 같은 트랜잭션의 나머지 읽기를 망치지 않는다
    assert all(v["status"] == "unchanged" for k, v in by(result["views"], "dataset").items() if k != "docs")


def test_broken_csv_stops_plan_with_validation_errors(applied, tmp_path):
    dbname, build = applied
    root = broken_csv_copy(tmp_path)
    with pytest.raises(plan.PlanError) as exc:
        plan.make_plan(load_project(PROJECT, root), dbname=dbname, build_dir=build, state_dir=build)
    assert len(exc.value.errors) == 5 and all(e.startswith("data/") for e in exc.value.errors)


def test_first_apply_on_an_empty_db(empty_db, tmp_path):
    project = load_project(PROJECT, PROJECTS)
    before = db_fingerprint(empty_db)
    result = plan.make_plan(project, dbname=empty_db, build_dir=tmp_path / "no-build", state_dir=tmp_path)
    assert result["changed"] is True
    assert all(t["status"] == "new" and t["added"] == t["rows"] and t["removed"] == 0 for t in result["data"])
    views = by(result["views"], "dataset")
    for key, ds in project.dashboard["datasets"].items():
        assert views[key]["status"] == ("new" if "table" in ds else "unknown")
    s = result["superset"]
    assert s["first"] is True
    assert len(s["dashboards"]["added"]) == len(project.dashboard["dashboards"])
    unknown = {u["key"] for u in s["datasets"]["unknown"]}
    assert unknown == {k for k, ds in project.dashboard["datasets"].items() if "sql" in ds}
    assert "처음 적용" in plan.format_plan(result)
    assert db_fingerprint(empty_db) == before == db_fingerprint(empty_db)
    assert not (tmp_path / "no-build").exists()


@pytest.mark.parametrize("name", EXAMPLES)
def test_predicted_columns_equal_real_view_columns(name):
    """세 예시 모두: 적재 직후에는 미리 구한 뷰 열이 실제 뷰 열과 같다(이름·타입·순서. schema의 type 7종을 다 지난다)."""
    dbname = _create_db()
    try:
        project = load_project(name, PROJECTS)
        load(project, dbname=dbname)
        views_now = current.reader_view_columns(dbname)
        with current.readonly_owner(dbname) as conn:
            raw_now = current.read_columns(conn, current.RAW)
            predicted = plan.predict_view_columns(project, raw_now, views_now, lambda q: current.describe_query(conn, q))
        assert {k: p["columns"] for k, p in predicted.items()} == {k: views_now[k] for k in project.dashboard["datasets"]}
        assert {t: plan.table_columns(spec) for t, spec in project.schema["tables"].items()} == raw_now
    finally:
        _drop_db(dbname)


# ---------------------------------------------------------------- 읽기 전용 연결

def test_owner_connection_for_plan_cannot_write(applied):
    dbname, _build = applied
    with current.readonly_owner(dbname) as conn:
        assert conn.execute("SELECT count(*) FROM raw.members").fetchone()[0] > 0     # raw는 읽힌다
        for stmt in (
            "INSERT INTO raw.members (id, name, role) VALUES ('m991', 'x', 'x')",
            "DELETE FROM raw.members",
            "CREATE TABLE raw.t_probe (x int)",
            "CREATE VIEW dash.v_probe AS SELECT 1 AS x",
            "DROP SCHEMA dash CASCADE",
            "CREATE TEMP TABLE t_probe (x int)",
        ):
            with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                with conn.transaction():
                    conn.execute(stmt)
    # 트랜잭션을 끝내고 새로 시작해도 세션 기본값이 읽기 전용이다
    with current.readonly_owner(dbname) as conn:
        conn.rollback()
        conn.read_only = None
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM raw.members")


def test_describe_never_executes_the_query(applied):
    dbname, _build = applied
    with current.readonly_owner(dbname) as conn:
        cols = current.describe_query(conn, "SELECT id, 1::numeric AS n, now() AS at, pg_sleep(3600) AS never FROM raw.members")
        assert cols == [("id", "text"), ("n", "numeric"), ("at", "timestamp with time zone"), ("never", "void")]
        with pytest.raises(current.DescribeError):
            current.describe_query(conn, "SELECT 1; DELETE FROM raw.members")            # 문장 하나만 받는다
        with pytest.raises(current.DescribeError):
            current.describe_query(conn, "SELECT * FROM raw.no_such_table")
        assert conn.execute("SELECT count(*) FROM raw.members").fetchone()[0] > 0       # 실패 뒤에도 연결을 쓸 수 있다
        assert conn.execute("SELECT count(*) FROM pg_prepared_statements").fetchone()[0] == 0


# ---------------------------------------------------------------- 현재 스택(방금 적용된 프로젝트)

def _state_dir() -> Path:
    return Path(os.environ.get("STATE_DIR") or "/app/.state")


def _build_dir() -> Path:
    return Path(os.environ.get("BUILD_DIR") or "/app/.build")


def _superset_objects(superset) -> dict:
    out = {}
    for kind in ("dashboard", "chart", "dataset", "database"):
        body = superset.get(f"/api/v1/{kind}/?q=(page_size:1000)").json()
        rows = [{k: r.get(k) for k in ("id", "uuid", "changed_on_utc", "changed_on")} for r in body["result"]]
        out[kind] = (body["count"], sorted(json.dumps(r, sort_keys=True, default=str) for r in rows))
    return out


def test_live_plan_reports_no_change_and_changes_nothing(superset, capsys):
    """make apply 직후의 스택: plan은 "변경 없음"이고, raw 행·뷰·Superset 객체·상태 파일·빌드 폴더가 그대로다."""
    project = load_project(live_project(), PROJECTS)
    before = (db_fingerprint(None), _superset_objects(superset), tree_fingerprint(_state_dir()), tree_fingerprint(_build_dir()))
    assert before[2], "상태 파일이 있어야 함(make apply 뒤에 실행)"

    result = plan.make_plan(project)
    assert_nothing_to_do(result)

    assert main(["plan"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("데이터   변경 없음") and out.rstrip().splitlines()[-1] == "→ 바뀌는 것이 없음"
    assert main(["plan", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] is False

    after = (db_fingerprint(None), _superset_objects(superset), tree_fingerprint(_state_dir()), tree_fingerprint(_build_dir()))
    assert after == before


def test_live_plan_with_pending_changes_still_changes_nothing(superset, tmp_path, monkeypatch, capsys):
    """바뀔 것이 있는 설정으로 돌려도(같은 이름의 프로젝트 사본) plan은 읽기만 한다."""
    name = live_project()
    root = copy_project(name, tmp_path)

    def retitle(doc):
        dkey, d = next(iter(doc["dashboards"].items()))
        d["title"] = d["title"] + " (plan 검사)"
        d["charts"].append({**d["charts"][0], "key": "zz_plan_extra", "title": "plan 검사용 차트"})
        d["layout"].append(["zz_plan_extra"])

    edit_yaml(root / name / "dashboard.yaml", retitle)
    tables = load_project(name, PROJECTS).schema["tables"]
    referenced = {spec["ref"].split(".")[0] for t in tables.values() for spec in t["columns"].values() if "ref" in spec}
    table = next(t for t in tables if t not in referenced)     # 아무도 참조하지 않는 테이블에서 마지막 행을 뺀다
    path = root / name / "data" / f"{table}.csv"
    header, rows = read_csv(path)
    write_csv(path, header, rows[:-1])

    before = (db_fingerprint(None), _superset_objects(superset), tree_fingerprint(_state_dir()), tree_fingerprint(_build_dir()))
    monkeypatch.setenv("PROJECTS_DIR", str(root))
    assert main(["plan"]) == 0
    out = capsys.readouterr().out
    assert "차트 +1 (zz_plan_extra)" in out and "제목 변경, 배치 변경" in out
    assert f"{table}  +0행 −1행 ~0행" in out
    after = (db_fingerprint(None), _superset_objects(superset), tree_fingerprint(_state_dir()), tree_fingerprint(_build_dir()))
    assert after == before


def test_switching_project_ignores_the_stale_bundle(applied, tmp_path):
    """상태 파일이 다른 프로젝트의 것이면 이 프로젝트의 지난 번들이 남아 있어도 전부 새로 만드는 것으로 본다."""
    dbname, build = applied
    (tmp_path / "managed.json").write_text(json.dumps(
        {"version": 1, "project": "zz-other", "objects": {"chart": ["a", "b"], "dashboard": ["c"]}}), encoding="utf-8")
    project = load_project(PROJECT, PROJECTS)
    result = plan.make_plan(project, dbname=dbname, build_dir=build, state_dir=tmp_path)
    s = result["superset"]
    assert result["changed"] is True and s["first"] is True
    assert s["replaces"] == {"project": "zz-other", "objects": 3}
    assert sorted(s["dashboards"]["added"]) == sorted(project.dashboard["dashboards"])
    text = plan.format_plan(result)
    assert "프로젝트 전환" in text and "지금 적용된 프로젝트 'zz-other'의 객체 3개 삭제" in text
