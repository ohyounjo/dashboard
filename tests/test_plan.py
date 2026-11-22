"""plan(dashkit.plan.make_plan)을 실제 DB와 함께 확인한다. 순수 함수의 검사는 tests/test_plan_diff.py.

- 숫자 검사: 임시 DB에 예시를 적재하고, 임시 폴더의 프로젝트 사본을 고친 뒤 plan의 숫자를 본다
  (tests/test_loader.py의 임시 DB, tests/test_sync.py의 임시 프로젝트 방식). 지난 번들은 sync가 쓰는 함수
  (build_bundle, write_build, write_applied)로 임시 폴더에 만든다. Superset에는 닿지 않는다.
- 현재 스택 검사: 방금 적용된 프로젝트(env PROJECT)에 plan을 돌려 "변경 없음"이고 아무것도 바뀌지 않았는지 본다.
- plan은 dash_reader 계정만 쓴다. 이 파일의 owner 연결은 준비(적재)와 확인(전후 비교)에만 쓴다.
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
from dashkit.loader import current, load, record
from dashkit.sync.bundle import build_bundle, write_applied, write_build, zip_bundle

from conftest import PROJECTS, broken_csv_copy, copy_project, live_project, read_csv, write_csv

PROJECT = "devtracker"
EXAMPLES = ("devtracker", "sales-sample", "chart-gallery")
OWNER_VARS = ("DATA_DB_OWNER_USER", "DATA_DB_OWNER_PASSWORD")
pytestmark = pytest.mark.integration


# ---------------------------------------------------------------- 도구

def _create_db() -> str:
    name = f"dash_plan_{uuid.uuid4().hex[:8]}"
    with db.owner_conn(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return name


def _drop_db(name: str) -> None:
    with db.owner_conn(autocommit=True) as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid() "
                     "AND backend_type = 'client backend'", (name,))
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


@pytest.fixture(scope="module")
def empty_db():
    name = _create_db()
    yield name
    _drop_db(name)


@pytest.fixture
def scratch_db():
    name = _create_db()
    yield name
    _drop_db(name)


def _write_bundle(project, dbname: str, build: Path) -> None:
    files = build_bundle(project, current.reader_view_columns(dbname))
    # 성공한 sync가 남기는 것과 같게: 이번 번들(bundle/)과 적용된 번들(applied/, plan의 비교 기준)
    write_build(files, zip_bundle(files), build / project.name)
    write_applied(files, build / project.name)


@pytest.fixture(scope="module")
def applied(tmp_path_factory):
    """임시 DB에 devtracker를 적재하고, 그 뷰 열로 만든 번들을 임시 빌드 폴더에 둔다(적용 직후와 같은 상태)."""
    name = _create_db()
    build = tmp_path_factory.mktemp("build")
    project = load_project(PROJECT, PROJECTS)
    load(project, dbname=name)
    _write_bundle(project, name, build)
    yield name, build
    _drop_db(name)


@pytest.fixture
def no_owner(monkeypatch):
    """owner 계정의 환경변수를 지운다. 이 상태에서 owner 연결을 열려고 하면 RuntimeError다."""
    for var in OWNER_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(db, "owner_conn", _refuse_owner)


def _refuse_owner(*_args, **_kwargs):
    raise AssertionError("plan은 owner 계정으로 접속하면 안 됨")


def db_fingerprint(dbname: str | None) -> dict:
    """DB의 내용과 구조. plan 전후에 같아야 한다(확인용이라 owner로 읽는다)."""
    out: dict = {}
    with db.owner_conn(dbname) as conn:
        out["relations"] = conn.execute(
            "SELECT n.nspname, c.relname, c.relkind, c.oid::int FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname IN ('raw', 'dash', 'dashkit_meta') OR n.nspname LIKE 'pg_temp%' ORDER BY 1, 2").fetchall()
        out["columns"] = conn.execute(
            "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema IN ('raw', 'dash', 'dashkit_meta') ORDER BY 1, 2, ordinal_position").fetchall()
        for schema, table in conn.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema IN ('raw', 'dashkit_meta') ORDER BY 1, 2").fetchall():
            rows = conn.execute(sql.SQL("SELECT t::text FROM {}.{} t ORDER BY 1").format(
                sql.Identifier(schema), sql.Identifier(table))).fetchall()
            out[f"{schema}.{table}"] = (len(rows), hashlib.sha256(repr(rows).encode()).hexdigest())
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

def test_right_after_apply_there_is_nothing_to_do(applied, no_owner):
    dbname, build = applied
    project = load_project(PROJECT, PROJECTS)
    result = plan.make_plan(project, dbname=dbname, build_dir=build, state_dir=build)
    assert_nothing_to_do(result)
    assert result["load_record"] == {"exists": True, "project": PROJECT}
    assert {t["table"]: (t["rows"], t["unchanged"]) for t in result["data"]} == {
        t: (n, n) for t in project.schema["tables"] for n in [len(read_csv(project.data_dir / f"{t}.csv")[1])]}
    assert plan.format_plan(result).splitlines()[-1] == "→ 바뀌는 것이 없음"


def test_plan_runs_without_owner_credentials(applied, monkeypatch):
    """tools 컨테이너에 owner 비밀번호가 없어도 plan이 돈다(db.owner_conn은 건드리지 않고 환경변수만 지운다)."""
    dbname, build = applied
    for var in OWNER_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError):
        db.owner_conn(dbname)                      # 이 상태에서 owner 연결은 열 수 없다
    assert_nothing_to_do(plan.make_plan(load_project(PROJECT, PROJECTS), dbname=dbname, build_dir=build, state_dir=build))


def test_counts_after_editing_a_copy(applied, tmp_path, monkeypatch):
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

    monkeypatch.setattr(db, "owner_conn", _refuse_owner)
    result = plan.make_plan(load_project(PROJECT, root), dbname=dbname, build_dir=build, state_dir=build)
    monkeypatch.undo()

    tables = by(result["data"], "table")
    m = tables.pop("members")
    assert (m["status"], m["added"], m["removed"], m["changed"]) == ("changed", 1, 0, 1)
    assert m["unchanged"] == len(rows) - 2 and m["comparable"] is True
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


def test_removed_rows(applied, tmp_path, no_owner):
    dbname, build = applied
    root = copy_project(PROJECT, tmp_path)
    path = root / PROJECT / "data" / "kpi_snapshots.csv"
    header, rows = read_csv(path)
    write_csv(path, header, rows[:-2])
    result = plan.make_plan(load_project(PROJECT, root), dbname=dbname, build_dir=build, state_dir=build)
    k = by(result["data"], "table")["kpi_snapshots"]
    assert (k["added"], k["removed"], k["changed"], k["unchanged"]) == (0, 2, 0, len(rows) - 2)


def test_same_values_in_another_notation_are_not_changes(applied, tmp_path, no_owner):
    """CSV의 표기만 바꾼 것(숫자 자릿수, 불리언 대소문자, 열 순서, 따옴표)은 변경이 아니다."""
    dbname, build = applied
    root = copy_project(PROJECT, tmp_path)
    project = load_project(PROJECT, PROJECTS)
    touched = 0
    for table, spec in project.schema["tables"].items():
        path = root / PROJECT / "data" / f"{table}.csv"
        header, rows = read_csv(path)
        for row in rows:
            for col, cspec in spec["columns"].items():
                if row[col] == "":
                    continue
                if cspec["type"] == "number" and "." not in row[col]:
                    row[col] += ".00"; touched += 1
                elif cspec["type"] == "boolean":
                    row[col] = row[col].upper(); touched += 1
                elif cspec["type"] == "integer" and not row[col].startswith(("-", "+")):
                    row[col] = "+" + row[col]; touched += 1
        write_csv(path, list(reversed(header)), rows)
    assert touched > 0
    assert_nothing_to_do(plan.make_plan(load_project(PROJECT, root), dbname=dbname, build_dir=build, state_dir=build))


def test_table_dataset_column_added_shows_up_before_apply(applied, tmp_path, no_owner):
    """`table:` 데이터셋의 테이블에 열을 더하면 적용 전에 뷰와 데이터셋의 "열 추가"로 나온다."""
    dbname, build = applied
    root = copy_project(PROJECT, tmp_path)

    def add_column(doc):
        doc["tables"]["kpi_snapshots"]["columns"]["zz_age_days"] = {"type": "integer"}

    edit_yaml(root / PROJECT / "schema.yaml", add_column)
    path = root / PROJECT / "data" / "kpi_snapshots.csv"
    header, rows = read_csv(path)
    write_csv(path, header + ["zz_age_days"], [{**r, "zz_age_days": "3"} for r in rows])
    result = plan.make_plan(load_project(PROJECT, root), dbname=dbname, build_dir=build, state_dir=build)

    k = by(result["data"], "table")["kpi_snapshots"]
    assert k["columns"]["added"] == ["zz_age_days"] and k["comparable"] is False
    assert k["comparison"] == "key_only"
    assert (k["added"], k["removed"], k["matched"], k["changed"]) == (0, 0, len(rows), None)
    views = by(result["views"], "dataset")
    assert views["kpi_snapshots"]["status"] == "changed"
    assert views["kpi_snapshots"]["columns"] == {"added": ["zz_age_days"], "removed": [], "retyped": [], "reordered": False}
    assert result["superset"]["datasets"]["changed"] == [{"key": "kpi_snapshots", "detail": "열 추가: zz_age_days"}]
    assert "dash.kpi_snapshots  열 추가: zz_age_days" in plan.format_plan(result)


def test_changed_sql_dataset_is_predictable_with_inspect_and_unknown_without(applied, tmp_path, no_owner, monkeypatch):
    """`sql:` 데이터셋의 쿼리를 바꾸면 dash_inspect로 새 열을 적용 전에 예측하고, dash_inspect가 없으면 unknown으로 남긴다."""
    dbname, build = applied
    root = copy_project(PROJECT, tmp_path)

    def widen(doc):
        ds = doc["datasets"]["docs"]
        ds["sql"] = "SELECT q.*, 1 AS zz_extra FROM (" + ds["sql"].strip().rstrip(";") + ") q"

    edit_yaml(root / PROJECT / "dashboard.yaml", lambda doc: None)   # 형식만 다시 쓴 것은 변경이 아니다
    assert_nothing_to_do(plan.make_plan(load_project(PROJECT, root), dbname=dbname, build_dir=build, state_dir=build))
    edit_yaml(root / PROJECT / "dashboard.yaml", widen)
    result = plan.make_plan(load_project(PROJECT, root), dbname=dbname, build_dir=build, state_dir=build)
    views = by(result["views"], "dataset")
    assert views["docs"]["status"] == "changed" and views["docs"]["columns"]["added"] == ["zz_extra"]
    assert all(v["status"] == "unchanged" for k, v in views.items() if k != "docs")
    assert result["superset"]["assumed_columns"] == []
    assert any(c["key"] == "docs" and "zz_extra" in c["detail"] for c in result["superset"]["datasets"]["changed"])
    assert result["changed"] is True
    assert "dash.docs  열 추가: zz_extra" in plan.format_plan(result)

    # dash_inspect가 없으면(owner_conn으로 폴백하지 않고) unknown으로 물러난다
    monkeypatch.delenv("DATA_DB_INSPECT_PASSWORD", raising=False)
    fallback = plan.make_plan(load_project(PROJECT, root), dbname=dbname, build_dir=build, state_dir=build)
    fb_views = by(fallback["views"], "dataset")
    assert fb_views["docs"]["status"] == "unknown" and "쿼리가 바뀜" in fb_views["docs"]["reason"]
    assert fallback["superset"]["assumed_columns"] == ["docs"]
    assert "적용해야 알 수 있음" in plan.format_plan(fallback)


def test_schema_column_added_makes_dependent_sql_views_unknown(applied, tmp_path, no_owner):
    """raw 테이블의 구조가 바뀌면 그 테이블을 읽는 `sql:` 뷰는 "적용해야 알 수 있음"이고, 행은 key_only로 맞춘다."""
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
    assert m["comparison"] == "key_only" and m["comparable"] is False
    assert (m["added"], m["removed"], m["matched"], m["changed"]) == (0, 0, len(rows), None)
    views = by(result["views"], "dataset")
    naming = {k for k, ds in project.dashboard["datasets"].items() if "sql" in ds and "members" in ds["sql"]}
    assert naming, "예시에 members를 읽는 sql 데이터셋이 있어야 함"
    for key, v in views.items():
        if key in naming:
            assert v["status"] == "unknown" and "raw.members" in v["reason"], v
        else:
            assert v["status"] == "unchanged", v
    assert sorted(result["superset"]["assumed_columns"]) == sorted(naming)
    assert "적용해야 알 수 있음" in plan.format_plan(result)


def test_broken_csv_stops_plan_with_validation_errors(applied, tmp_path, no_owner):
    dbname, build = applied
    root = broken_csv_copy(tmp_path)
    with pytest.raises(plan.PlanError) as exc:
        plan.make_plan(load_project(PROJECT, root), dbname=dbname, build_dir=build, state_dir=build)
    assert len(exc.value.errors) == 5 and all(e.startswith("data/") for e in exc.value.errors)


def test_switching_project_ignores_the_stale_bundle(applied, tmp_path, no_owner):
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


# ---------------------------------------------------------------- 적재 기록이 없거나 다른 프로젝트의 것

def test_first_apply_on_an_empty_db(empty_db, tmp_path, no_owner):
    project = load_project(PROJECT, PROJECTS)
    result = plan.make_plan(project, dbname=empty_db, build_dir=tmp_path / "no-build", state_dir=tmp_path)
    assert result["changed"] is True and result["load_record"] == {"exists": False, "project": None}
    for t in result["data"]:
        assert t["status"] == "unrecorded" and t["added"] is None and t["removed"] is None
        assert t["rows"] == len(read_csv(project.data_dir / f"{t['table']}.csv")[1])
    views = by(result["views"], "dataset")
    for key, ds in project.dashboard["datasets"].items():
        assert views[key]["status"] == ("new" if "table" in ds else "unknown")
    s = result["superset"]
    assert s["first"] is True
    assert len(s["dashboards"]["added"]) == len(project.dashboard["dashboards"])
    assert {u["key"] for u in s["datasets"]["unknown"]} == {k for k, ds in project.dashboard["datasets"].items() if "sql" in ds}
    text = plan.format_plan(result)
    assert "지난 적재 기록 없음: 비교할 수 없음(한 번 apply하면 생김)" in text and "처음 적용" in text
    assert not (tmp_path / "no-build").exists()


def test_db_loaded_before_records_existed(scratch_db, tmp_path, monkeypatch):
    """적재 기록을 남기기 전의 dashkit으로 적재한 DB: 데이터는 "비교할 수 없음"으로 명시하고, 한 번 적재하면 비교된다."""
    project = load_project(PROJECT, PROJECTS)
    load(project, dbname=scratch_db)
    _write_bundle(project, scratch_db, tmp_path)
    with db.owner_conn(scratch_db) as conn:
        conn.execute("DROP SCHEMA dashkit_meta CASCADE")
    before = db_fingerprint(scratch_db)

    monkeypatch.setattr(db, "owner_conn", _refuse_owner)
    result = plan.make_plan(project, dbname=scratch_db, build_dir=tmp_path, state_dir=tmp_path)
    monkeypatch.undo()
    assert result["changed"] is True and result["load_record"]["exists"] is False
    assert all(t["status"] == "unrecorded" and t["comparable"] is False for t in result["data"])
    views = by(result["views"], "dataset")
    for key, ds in project.dashboard["datasets"].items():
        assert views[key]["status"] == ("unchanged" if "table" in ds else "unknown")
    # 뷰는 있으므로 번들은 지금 뷰의 열로 만들어 비교한다(그 사실을 알린다)
    assert sorted(result["superset"]["assumed_columns"]) == sorted(k for k, ds in project.dashboard["datasets"].items() if "sql" in ds)
    assert result["superset"]["charts"]["changed"] == [] and result["superset"]["charts"]["unknown"] == []
    assert db_fingerprint(scratch_db) == before

    load(project, dbname=scratch_db)                 # 한 번 적재하면 기록이 생긴다
    assert_nothing_to_do(plan.make_plan(project, dbname=scratch_db, build_dir=tmp_path, state_dir=tmp_path))


def test_record_of_another_project_means_full_replacement(scratch_db, tmp_path, monkeypatch):
    other = load_project("sales-sample", PROJECTS)
    load(other, dbname=scratch_db)
    project = load_project(PROJECT, PROJECTS)
    monkeypatch.setattr(db, "owner_conn", _refuse_owner)
    result = plan.make_plan(project, dbname=scratch_db, build_dir=tmp_path, state_dir=tmp_path)
    monkeypatch.undo()
    assert result["load_record"] == {"exists": True, "project": "sales-sample"}
    tables = by(result["data"], "table")
    for name in project.schema["tables"]:
        t = tables[name]
        assert t["added"] == t["rows"] and t["changed"] == 0
        assert t["status"] == "new" or (t["comparable"] is False and "sales-sample" in t["reason"])
    for name in other.schema["tables"]:
        if name not in project.schema["tables"]:
            assert tables[name]["status"] == "removed"
            assert tables[name]["removed"] == len(read_csv(other.data_dir / f"{name}.csv")[1])
    views = by(result["views"], "dataset")
    assert all(views[k]["status"] == "removed" for k in other.dashboard["datasets"] if k not in project.dashboard["datasets"])
    sql_keys = [k for k, ds in project.dashboard["datasets"].items() if "sql" in ds]
    assert sql_keys and all(views[k]["status"] == "unknown" for k in sql_keys)
    assert all(views[k]["reason"] == "지난 적재는 다른 프로젝트 'sales-sample'의 것" for k in sql_keys)
    assert f"dash.{sql_keys[0]}  적용해야 알 수 있음 (지난 적재는 다른 프로젝트 'sales-sample'의 것)" in plan.format_plan(result)
    assert result["superset"]["assumed_columns"] == []


# ---------------------------------------------------------------- 리뷰 회귀(실제 적재와 함께)

def _mini(root: Path, pk: str, columns: list[str], rows: list[dict]) -> Path:
    """표 하나짜리 프로젝트(zz-mini)를 root에 쓴다."""
    base = root / "zz-mini"
    (base / "data").mkdir(parents=True, exist_ok=True)
    (base / "schema.yaml").write_text(yaml.safe_dump(
        {"tables": {"things": {"primary_key": pk, "columns": {c: {"type": "text"} for c in columns}}}}), encoding="utf-8")
    (base / "dashboard.yaml").write_text(yaml.safe_dump({
        "app": {"title": "mini", "as_of": "2026-01-01"},
        "datasets": {"things": {"table": "things"}},
        "dashboards": {"home": {"title": "home", "layout": [["c_rows"]], "charts": [
            {"key": "c_rows", "title": "rows", "viz": "table", "dataset": "things", "columns": [columns[0]]}]}},
        "tabs": [{"key": "home", "label": "home", "icon": "grid", "sections": [{"type": "superset", "dashboard": "home"}]}],
    }, allow_unicode=True), encoding="utf-8")
    write_csv(base / "data" / "things.csv", columns, rows)
    return root


def test_all_columns_renamed_keeps_the_old_row_count(scratch_db, tmp_path, monkeypatch):
    """리뷰 P2: id 열 2행을 적재한 뒤 열 이름을 code로 바꾸고 1행만 두면 +1 −2다(삭제 규모가 0이 되지 않는다)."""
    load(load_project("zz-mini", _mini(tmp_path / "a", "id", ["id"], [{"id": "a"}, {"id": "b"}])), dbname=scratch_db)
    project = load_project("zz-mini", _mini(tmp_path / "b", "code", ["code"], [{"code": "a"}]))
    monkeypatch.setattr(db, "owner_conn", _refuse_owner)
    t, = plan.make_plan(project, dbname=scratch_db, build_dir=tmp_path, state_dir=tmp_path)["data"]
    assert (t["added"], t["removed"], t["changed"], t["comparable"]) == (1, 2, 0, False)
    assert t["columns"]["added"] == ["code"] and t["columns"]["removed"] == ["id"]


def test_changed_primary_key_shows_the_deletion(scratch_db, tmp_path, monkeypatch):
    """리뷰 P2: 기본 키 id로 (a,x),(b,x)를 적재한 뒤 기본 키를 label로 바꾸고 (b,x)만 두면 삭제가 드러난다."""
    cols = ["id", "label"]
    load(load_project("zz-mini", _mini(tmp_path / "a", "id", cols, [{"id": "a", "label": "x"}, {"id": "b", "label": "x"}])),
         dbname=scratch_db)
    project = load_project("zz-mini", _mini(tmp_path / "b", "label", cols, [{"id": "b", "label": "x"}]))
    monkeypatch.setattr(db, "owner_conn", _refuse_owner)
    t, = plan.make_plan(project, dbname=scratch_db, build_dir=tmp_path, state_dir=tmp_path)["data"]
    assert (t["added"], t["removed"], t["changed"], t["comparable"]) == (1, 2, 0, False)
    assert "기본 키" in t["reason"]


# ---------------------------------------------------------------- 적재 기록(dashkit_meta)

@pytest.mark.parametrize("name", EXAMPLES)
def test_load_record_matches_csv_for_every_example(name, scratch_db, tmp_path, monkeypatch):
    """세 예시 모두: 적재 직후 plan이 "변경 없음"이다(기록된 지문 = CSV의 지문, 예측한 뷰 열 = 실제 뷰 열)."""
    project = load_project(name, PROJECTS)
    counts = load(project, dbname=scratch_db)
    _write_bundle(project, scratch_db, tmp_path)
    monkeypatch.setattr(db, "owner_conn", _refuse_owner)
    result = plan.make_plan(project, dbname=scratch_db, build_dir=tmp_path, state_dir=tmp_path)
    assert_nothing_to_do(result)
    assert {t["table"]: t["unchanged"] for t in result["data"]} == counts
    with current.inspector(scratch_db) as conn:
        rec = current.read_load_record(conn, include_fingerprints=True)
        views_now = current.read_view_columns(conn)
    got = json.loads(json.dumps(rec.snapshot))
    sources = {k: d.pop("sources") for k, d in got["datasets"].items()}
    assert got == json.loads(json.dumps(record.snapshot(
        name, project.schema, project.dashboard, counts,
        fingerprint_key_id=record.key_id(record.fingerprint_key()),
    )))
    # 계보(contracts/ingest.md "계보"): 데이터셋마다 읽는 raw 테이블과 열. table: 데이터셋은 그 테이블의 모든 열
    assert set(sources) == set(project.dashboard["datasets"])
    for key, ds in project.dashboard["datasets"].items():
        for t, cols in sources[key].items():
            assert cols == sorted(cols) and set(cols) <= set(project.schema["tables"][t]["columns"])
        if "table" in ds:
            assert sources[key] == {ds["table"]: sorted(project.schema["tables"][ds["table"]]["columns"])}
        else:
            assert sources[key], f"{key}: sql: 데이터셋이 읽는 테이블을 찾지 못함"
    assert {t: len(p) for t, p in rec.fingerprints.items() if p} == {t: n for t, n in counts.items() if n}
    # 기록 어디에도 CSV의 기본 키 원본 값이 없다: 지문 테이블의 두 열은 전부 SHA-256 16진수이고,
    # 스냅샷은 (바로 위에서 본 대로) 이름·타입·행 수·정의 해시뿐이다
    import re
    for table, spec in project.schema["tables"].items():
        _header, rows = read_csv(project.data_dir / f"{table}.csv")
        originals = {tuple(r[c] for c in spec["primary_key"]) for r in rows}
        stored = rec.fingerprints.get(table, [])
        assert all(re.fullmatch(r"[0-9a-f]{64}", k) and re.fullmatch(r"[0-9a-f]{64}", h) for k, h in stored)
        keys = {k for k, _h in stored}
        for original in originals:
            assert not any(v and v in keys for v in original)
            assert json.dumps(list(original), ensure_ascii=False, separators=(",", ":")) not in keys
    with db.inspect_conn(scratch_db) as conn:
        whole = conn.execute(
            "SELECT coalesce(string_agg(f::text, ' '), '') FROM dashkit_meta.row_fingerprint f").fetchone()[0]
    long_keys = {v for t, spec in project.schema["tables"].items()
                 for r in read_csv(project.data_dir / f"{t}.csv")[1] for v in (r[c] for c in spec["primary_key"])
                 if len(v) >= 4 and not re.fullmatch(r"[0-9a-f]+", v)}
    assert long_keys, "원본 값이 없는지 볼 만한(4자 이상, 16진수 꼴이 아닌) 기본 키 값이 예시에 있어야 함"
    assert not [v for v in long_keys if v in whole]
    # table: 데이터셋의 정적 예측이 실제 뷰 열과 같다(이름·타입·순서. schema의 type 7종을 다 지난다)
    for key, ds in project.dashboard["datasets"].items():
        if "table" in ds:
            assert plan.table_columns(project.schema["tables"][ds["table"]]) == views_now[key]


def test_record_stores_keys_and_hashes_only(applied):
    dbname, _build = applied
    with db.inspect_conn(dbname) as conn:
        cols = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'dashkit_meta' "
            "ORDER BY 1, ordinal_position").fetchall()
        # 배치 기록(contracts/ingest.md "배치 기록")은 파일 이름·내용 해시·행 수뿐이다
        assert cols == [("batch", c) for c in ("table_name", "file", "sha256", "added_at", "rows", "inserted",
                                               "updated", "skipped", "unchanged")] + [
                        ("load_snapshot", "id"), ("load_snapshot", "loaded_at"), ("load_snapshot", "snapshot"),
                        ("row_fingerprint", "table_name"), ("row_fingerprint", "row_key"), ("row_fingerprint", "row_hash")]
        name = read_csv(PROJECTS / PROJECT / "data" / "members.csv")[1][0]["name"]
        files = conn.execute("SELECT string_agg(file, ',') FROM dashkit_meta.batch").fetchone()[0]
        assert name not in files
        dump = conn.execute("SELECT string_agg(row_key || row_hash, '') FROM dashkit_meta.row_fingerprint").fetchone()[0]
        snap = conn.execute("SELECT snapshot::text FROM dashkit_meta.load_snapshot").fetchone()[0]
        assert name not in dump and name not in snap
        assert conn.execute("SELECT count(*) FROM dashkit_meta.load_snapshot").fetchone()[0] == 1


def test_reader_can_read_snapshot_and_batch_but_not_fingerprints_or_raw(applied):
    dbname, _build = applied
    with db.reader_conn(dbname, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM dashkit_meta.load_snapshot").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM dashkit_meta.batch").fetchone()[0] >= 1
        for stmt in (
            "SELECT count(*) FROM dashkit_meta.row_fingerprint",                  # dash_reader는 지문을 읽지 못한다
            "SELECT count(*) FROM raw.members",                                   # raw는 여전히 못 읽는다
            "INSERT INTO dashkit_meta.row_fingerprint VALUES ('t', 'k', 'h')",
            "UPDATE dashkit_meta.row_fingerprint SET row_hash = 'x'",
            "DELETE FROM dashkit_meta.row_fingerprint",
            "UPDATE dashkit_meta.load_snapshot SET snapshot = '{}'",
            "TRUNCATE dashkit_meta.load_snapshot",
            "CREATE TABLE dashkit_meta.t_probe (x int)",
            "DROP TABLE dashkit_meta.row_fingerprint",
            "DROP SCHEMA dashkit_meta CASCADE",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt)


def test_failed_load_keeps_the_previous_record(scratch_db, tmp_path):
    project = load_project(PROJECT, PROJECTS)
    load(project, dbname=scratch_db)
    before = db_fingerprint(scratch_db)
    root = copy_project(PROJECT, tmp_path)
    path = root / PROJECT / "data" / "issues.csv"
    header, rows = read_csv(path)
    rows[2]["created"], rows[2]["updated"] = "2026-11-18", "2026-11-01"      # checks 위반 → 적재 롤백
    write_csv(path, header, rows)
    from dashkit.loader import LoadError
    with pytest.raises(LoadError):
        load(load_project(PROJECT, root), dbname=scratch_db)
    assert db_fingerprint(scratch_db) == before


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


def _everything(superset) -> tuple:
    return (db_fingerprint(None), _superset_objects(superset), tree_fingerprint(_state_dir()), tree_fingerprint(_build_dir()))


def test_live_plan_reports_no_change_and_changes_nothing(superset, capsys, monkeypatch):
    """make apply 직후의 스택: plan은 "변경 없음"이고, raw 행·뷰·적재 기록·Superset 객체·상태 파일·빌드 폴더가 그대로다."""
    project = load_project(live_project(), PROJECTS)
    before = _everything(superset)
    assert before[2], "상태 파일이 있어야 함(make apply 뒤에 실행)"

    with monkeypatch.context() as m:
        m.setattr(db, "owner_conn", _refuse_owner)
        for var in OWNER_VARS:
            m.delenv(var, raising=False)
        assert_nothing_to_do(plan.make_plan(project))
        assert main(["plan"]) == 0
        out = capsys.readouterr().out
        assert out.splitlines()[0].startswith("데이터   변경 없음") and out.rstrip().splitlines()[-1] == "→ 바뀌는 것이 없음"
        assert main(["plan", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["changed"] is False

    assert _everything(superset) == before


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

    before = _everything(superset)
    with monkeypatch.context() as m:
        m.setattr(db, "owner_conn", _refuse_owner)
        m.setenv("PROJECTS_DIR", str(root))
        assert main(["plan"]) == 0
    out = capsys.readouterr().out
    assert "차트 +1 (zz_plan_extra)" in out and "제목 변경, 배치 변경" in out
    assert f"{table}  +0행 −1행 ~0행" in out
    assert _everything(superset) == before


def test_describe_query_matches_information_schema_and_rejects_unsafe(scratch_db):
    """describe_query가 실제 CREATE VIEW + information_schema와 1:1 일치하며 실행 없이 안전하게 거부한다."""
    import time
    from dashkit.loader.current import DescribeError

    project = load_project(PROJECT, PROJECTS)
    load(project, dbname=scratch_db)

    query = (
        "SELECT 'lit' AS lit_col, m.*, "
        "1::integer AS int4_col, 1::bigint AS int8_col, 1.5::numeric AS num_col, "
        "true AS bool_col, current_date AS date_col, now() AS ts_col, "
        "(current_date - DATE '2026-01-01')::integer AS diff_col, "
        "interval '1 day' AS iv_col, ARRAY['a', 'b']::text[] AS arr_col, "
        "dash.as_of() AS as_of_col "
        "FROM raw.members m"
    )
    with current.inspector(scratch_db) as conn:
        predicted = current.describe_query(conn, query)
        # pg_sleep은 실행되지 않고 즉시 분석된다
        t0 = time.monotonic()
        sleep_cols = current.describe_query(conn, "SELECT pg_sleep(10) AS s")
        assert time.monotonic() - t0 < 2.0 and len(sleep_cols) == 1

        # 위험·오류 쿼리는 모두 DescribeError로 거부된다
        for bad in (
            "SELECT * FROM raw.no_such_table",
            "SELECT 1 AS x, 2 AS x",
            "SELECT $1::text AS p",
            "SELECT 1; SELECT 2",
            "SELECT * FROM raw.members WHERE id = $1",
        ):
            with pytest.raises(DescribeError):
                current.describe_query(conn, bad)

    # 실제 CREATE VIEW의 information_schema 결과와 (이름, 타입) 1:1 일치 확인
    with db.owner_conn(scratch_db) as conn:
        try:
            conn.execute(f"CREATE OR REPLACE VIEW dash.zz_probe_view AS {query}")
            actual = current.read_view_columns(conn, ["zz_probe_view"])["zz_probe_view"]
        finally:
            conn.rollback()
    assert predicted == actual


def test_e2e_new_sql_dataset_and_key_only_plan(applied, tmp_path, no_owner):
    """새 sql: 데이터셋 열 예측과 비키 열 추가 시 key_only 요약을 E2E로 검증한다."""
    dbname, build = applied
    root = copy_project(PROJECT, tmp_path)

    def add_sql_ds(doc):
        doc["datasets"]["member_issues"] = {
            "sql": "SELECT m.id AS member_id, i.id AS issue_id FROM raw.members m JOIN raw.issues i ON i.assignee = m.id"
        }

    def add_col(doc):
        doc["tables"]["members"]["columns"]["zz_team"] = {"type": "text"}

    edit_yaml(root / PROJECT / "dashboard.yaml", add_sql_ds)
    # 1) raw 구조가 그대로일 때 새 sql: 데이터셋의 열을 적용 전에 예측한다
    pred_only = plan.make_plan(load_project(PROJECT, root), dbname=dbname, build_dir=build, state_dir=build)
    views_only = by(pred_only["views"], "dataset")
    assert views_only["member_issues"]["status"] == "new"
    assert views_only["member_issues"]["columns"]["added"] == ["member_id", "issue_id"]

    # 2) raw.members에 비키 열을 추가하면 행은 key_only로 요약되고, members를 읽는 새 sql: 뷰는 unknown으로 물러난다
    edit_yaml(root / PROJECT / "schema.yaml", add_col)
    path = root / PROJECT / "data" / "members.csv"
    header, rows = read_csv(path)
    new_row = {**rows[0], "id": "m999", "zz_team": "core"}
    write_csv(path, header + ["zz_team"], [{**r, "zz_team": "a"} for r in rows] + [new_row])

    result = plan.make_plan(load_project(PROJECT, root), dbname=dbname, build_dir=build, state_dir=build)
    views = by(result["views"], "dataset")
    assert views["member_issues"]["status"] == "unknown" and "raw.members" in views["member_issues"]["reason"]
    m = by(result["data"], "table")["members"]
    assert m["comparison"] == "key_only"
    assert (m["added"], m["removed"], m["matched"], m["changed"]) == (1, 0, len(rows), None)


def test_legacy_snapshot_and_key_rotation_recovery(scratch_db, tmp_path, monkeypatch):
    """구버전 스냅샷(fingerprint 없음)과 키 회전 시 오탐 +N -N 없이 unrecorded 안내 후 1회 load로 복구한다."""
    project = load_project(PROJECT, PROJECTS)
    load(project, dbname=scratch_db)

    # 1. 구버전 스냅샷 시뮬레이션: snapshot JSONB에서 fingerprint 키 제거
    with db.owner_conn(scratch_db) as conn:
        with conn.transaction():
            conn.execute("UPDATE dashkit_meta.load_snapshot SET snapshot = snapshot - 'fingerprint'")
    legacy_plan = plan.make_plan(project, dbname=scratch_db, build_dir=tmp_path / "b", state_dir=tmp_path / "s")
    for entry in legacy_plan["data"]:
        if entry.get("mode") != "append":
            assert entry["comparison"] == "unrecorded" and entry["added"] is None and "구버전" in entry["reason"]
    load(project, dbname=scratch_db)
    recovered = plan.make_plan(project, dbname=scratch_db, build_dir=tmp_path / "b", state_dir=tmp_path / "s")
    assert all(t["status"] == "unchanged" for t in recovered["data"])

    # 2. 키 회전: 다른 PLAN_FINGERPRINT_KEY 설정 후 plan → unrecorded → 1회 load → 복구
    monkeypatch.setenv("PLAN_FINGERPRINT_KEY", "rotated-new-fingerprint-key-32-bytes!")
    rotated_plan = plan.make_plan(project, dbname=scratch_db, build_dir=tmp_path / "b", state_dir=tmp_path / "s")
    for entry in rotated_plan["data"]:
        if entry.get("mode") != "append":
            assert entry["comparison"] == "unrecorded" and entry["added"] is None and "달라" in entry["reason"]
    load(project, dbname=scratch_db)
    after_load = plan.make_plan(project, dbname=scratch_db, build_dir=tmp_path / "b", state_dir=tmp_path / "s")
    assert all(t["status"] == "unchanged" for t in after_load["data"])
