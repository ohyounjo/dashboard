"""plan의 차이 계산(dashkit/plan.py의 순수 함수). DB 없이 돈다. DB와 함께 보는 검사는 tests/test_plan.py."""
from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from dashkit import plan
from dashkit.config import load_project
from dashkit.sync.bundle import build_bundle

FIXTURES = Path(__file__).parent / "fixtures"

THINGS_NOW = [("id", "text"), ("owner_id", "text"), ("phase", "text"), ("score", "bigint"),
              ("begins", "date"), ("ends", "date")]
OWNERS_NOW = [("id", "text"), ("label", "text")]
THINGS_VIEW = THINGS_NOW + [("owner_label", "text")]


def project_copy(tmp_path: Path, mutate_dash=None, mutate_schema=None):
    root = tmp_path / "p"
    shutil.copytree(FIXTURES / "basic", root / "basic")
    for fname, fn in (("dashboard.yaml", mutate_dash), ("schema.yaml", mutate_schema)):
        if fn:
            p = root / "basic" / fname
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
            fn(doc)
            p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return load_project("basic", root)


# ---------------------------------------------------------------- 행

def test_rows_added_removed_changed():
    cols = ["id", "label", "score"]
    old = [("a", "하나", 1), ("b", "둘", 2), ("c", "셋", 3)]
    new = [["a", "하나", 1], ["b", "둘!", 2], ["d", "넷", 4], ["e", "다섯", 5]]
    d = plan.diff_rows(cols, ["id"], new, cols, old)
    assert (d["added"], d["removed"], d["changed"], d["unchanged"]) == (2, 1, 1, 1)
    assert d["keys"] == {"added": ["d", "e"], "removed": ["c"], "changed": ["b"]}
    assert d["comparable"] is True


def test_rows_identical_is_no_change_even_when_column_order_differs():
    new = [["a", 1, None], ["b", 2, "x"]]
    old = [("x", "b", 2), (None, "a", 1)]
    d = plan.diff_rows(["id", "n", "memo"], ["id"], new, ["memo", "id", "n"], old)
    assert (d["added"], d["removed"], d["changed"], d["unchanged"]) == (0, 0, 0, 2)


def test_rows_composite_primary_key():
    cols = ["region", "day", "amount"]
    old = [("east", date(2026, 1, 1), 10), ("east", date(2026, 1, 2), 20), ("west", date(2026, 1, 1), 30)]
    new = [["east", date(2026, 1, 1), 10], ["east", date(2026, 1, 2), 25], ["west", date(2026, 1, 2), 40]]
    d = plan.diff_rows(cols, ["region", "day"], new, cols, old)
    assert (d["added"], d["removed"], d["changed"], d["unchanged"]) == (1, 1, 1, 1)
    assert d["keys"]["added"] == ["west, 2026-01-02"]
    assert d["keys"]["changed"] == ["east, 2026-01-02"]


def test_rows_compare_values_not_representations():
    """숫자의 자릿수 표기나 시각의 시간대 표기가 달라도 같은 값이면 변경이 아니다."""
    kst = timezone(timedelta(hours=9))
    old = [("a", Decimal("1.50"), datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc))]
    same = [["a", Decimal("1.5"), datetime(2026, 1, 1, 9, 0)]]            # 시간대 없는 시각 = 세션 시간대 기준
    d = plan.diff_rows(["id", "n", "at"], ["id"], same, ["id", "n", "at"], old, tz=kst)
    assert (d["changed"], d["unchanged"]) == (0, 1)
    other = [["a", Decimal("1.5"), datetime(2026, 1, 1, 9, 1)]]
    assert plan.diff_rows(["id", "n", "at"], ["id"], other, ["id", "n", "at"], old, tz=kst)["changed"] == 1


def test_rows_only_common_columns_are_compared():
    """새로 생긴 열은 값 비교에서 빠진다(그 사실은 열 차이가 따로 알린다)."""
    d = plan.diff_rows(["id", "label", "extra"], ["id"], [["a", "하나", "새 값"]], ["id", "label"], [("a", "하나")])
    assert d["compared_columns"] == ["id", "label"]
    assert (d["changed"], d["unchanged"]) == (0, 1)


def test_rows_without_primary_key_column_cannot_be_matched():
    d = plan.diff_rows(["code", "label"], ["code"], [["a", "x"], ["b", "y"]], ["id", "label"], [("1", "x")])
    assert d["comparable"] is False
    assert (d["added"], d["removed"], d["changed"]) == (2, 1, 0)


def test_rows_key_samples_are_capped():
    new = [[i] for i in range(100)]
    d = plan.diff_rows(["id"], ["id"], new, ["id"], [], max_keys=5)
    assert d["added"] == 100 and len(d["keys"]["added"]) == 5


# ---------------------------------------------------------------- 열, 테이블

def test_columns_added_removed_retyped_reordered():
    old = [("id", "text"), ("score", "bigint"), ("memo", "text")]
    d = plan.diff_columns([("id", "text"), ("score", "numeric"), ("age_days", "integer")], old)
    assert d == {"added": ["age_days"], "removed": ["memo"],
                 "retyped": [{"column": "score", "from": "bigint", "to": "numeric"}], "reordered": False}
    assert plan.diff_columns([("score", "bigint"), ("id", "text"), ("memo", "text")], old)["reordered"] is True
    assert plan.diff_columns(old, old) == {"added": [], "removed": [], "retyped": [], "reordered": False}


def test_table_diff_new_table_counts_everything_as_added(tmp_path):
    table = project_copy(tmp_path).schema["tables"]["owners"]
    d = plan.table_diff("owners", table, [["o1", "가"], ["o2", "나"]], None)
    assert (d["status"], d["added"], d["removed"], d["changed"], d["rows"]) == ("new", 2, 0, 0, 2)


def test_table_diff_against_current_table(tmp_path):
    table = project_copy(tmp_path).schema["tables"]["owners"]
    now = SimpleNamespace(columns=OWNERS_NOW, row_columns=["id", "label"], rows=[("o1", "가"), ("o2", "나")])
    assert plan.table_diff("owners", table, [["o1", "가"], ["o2", "나"]], now)["status"] == "unchanged"
    d = plan.table_diff("owners", table, [["o1", "가"], ["o2", "다"], ["o3", "라"]], now)
    assert (d["status"], d["added"], d["removed"], d["changed"]) == ("changed", 1, 0, 1)
    # 열이 사라지는 것만으로도 변경이다
    wider = SimpleNamespace(columns=OWNERS_NOW + [("old_col", "text")], row_columns=["id", "label"],
                            rows=[("o1", "가"), ("o2", "나")])
    d = plan.table_diff("owners", table, [["o1", "가"], ["o2", "나"]], wider)
    assert d["status"] == "changed" and d["columns"]["removed"] == ["old_col"] and d["changed"] == 0


def test_table_columns_follow_schema_types(tmp_path):
    table = project_copy(tmp_path).schema["tables"]["things"]
    assert plan.table_columns(table) == THINGS_NOW


# ---------------------------------------------------------------- 뷰 열

def test_views_unchanged_when_nothing_moves(tmp_path):
    project = project_copy(tmp_path)
    raw = {"things": THINGS_NOW, "owners": OWNERS_NOW}
    views = {"things": THINGS_VIEW, "owners": OWNERS_NOW}
    asked = []
    predicted = plan.predict_view_columns(project, raw, views, lambda q: asked.append(q) or THINGS_VIEW)
    assert len(asked) == 1 and "owner_label" in asked[0]          # sql: 데이터셋만 묻는다
    assert predicted["owners"] == {"columns": OWNERS_NOW, "reason": None}
    assert {v["dataset"]: v["status"] for v in plan.diff_views(predicted, views)} == {
        "things": "unchanged", "owners": "unchanged"}


def test_view_sql_column_added(tmp_path):
    project = project_copy(tmp_path)
    raw = {"things": THINGS_NOW, "owners": OWNERS_NOW}
    views = {"things": THINGS_VIEW, "owners": OWNERS_NOW}
    predicted = plan.predict_view_columns(project, raw, views, lambda q: THINGS_VIEW + [("age_days", "integer")])
    things = next(v for v in plan.diff_views(predicted, views) if v["dataset"] == "things")
    assert things["status"] == "changed" and things["columns"]["added"] == ["age_days"]


def test_view_sql_is_unknown_when_a_table_it_names_changes(tmp_path):
    """쿼리에 이름이 나오는 raw 테이블의 구조가 바뀌면 지금 DB에 묻지 않는다(답이 적용 뒤와 다를 수 있다)."""
    def add_column(schema):
        schema["tables"]["owners"]["columns"]["team"] = {"type": "text"}
    project = project_copy(tmp_path, mutate_schema=add_column)
    raw = {"things": THINGS_NOW, "owners": OWNERS_NOW}
    views = {"things": THINGS_VIEW, "owners": OWNERS_NOW}

    def never(_q):
        raise AssertionError("구조가 바뀐 테이블을 읽는 쿼리는 묻지 않아야 함")

    predicted = plan.predict_view_columns(project, raw, views, never)
    assert predicted["things"]["columns"] is None and "raw.owners" in predicted["things"]["reason"]
    by_key = {v["dataset"]: v for v in plan.diff_views(predicted, views)}
    assert by_key["things"]["status"] == "unknown"
    assert by_key["owners"]["status"] == "changed" and by_key["owners"]["columns"]["added"] == ["team"]


def test_view_sql_is_unknown_when_an_earlier_view_it_names_changes(tmp_path):
    def chain(dash):
        dash["datasets"]["later"] = {"sql": "SELECT id FROM dash.things"}
    project = project_copy(tmp_path, mutate_dash=chain)
    raw = {"things": THINGS_NOW, "owners": OWNERS_NOW}
    views = {"things": THINGS_VIEW, "owners": OWNERS_NOW, "later": [("id", "text")]}
    answers = {True: THINGS_VIEW + [("extra", "text")], False: [("id", "text")]}
    predicted = plan.predict_view_columns(project, raw, views, lambda q: answers["owner_label" in q])
    assert predicted["things"]["columns"][-1] == ("extra", "text")
    assert predicted["later"]["columns"] is None and "dash.things" in predicted["later"]["reason"]


def test_view_unknown_when_describe_fails_or_missing(tmp_path):
    project = project_copy(tmp_path)

    def boom(_q):
        raise RuntimeError('relation "raw.things" does not exist')

    raw = {"things": THINGS_NOW, "owners": OWNERS_NOW}
    p = plan.predict_view_columns(project, raw, {}, boom)
    assert p["things"]["columns"] is None and "does not exist" in p["things"]["reason"]
    p = plan.predict_view_columns(project, raw, {}, None)
    assert p["things"]["columns"] is None and p["owners"]["columns"] == OWNERS_NOW
    views = plan.diff_views(p, {"gone": [("id", "text")]})
    assert {v["dataset"]: v["status"] for v in views} == {"things": "unknown", "owners": "new", "gone": "removed"}


# ---------------------------------------------------------------- 번들

COLUMNS = {"things": THINGS_VIEW, "owners": OWNERS_NOW}


def bundle(project, columns=COLUMNS, ts=None) -> dict[str, str]:
    return build_bundle(project, columns, env={}, timestamp=ts or datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_bundle_same_config_is_no_change_and_timestamp_is_ignored(tmp_path):
    project = project_copy(tmp_path)
    old = bundle(project, ts=datetime(2025, 1, 1, tzinfo=timezone.utc))
    new = bundle(project, ts=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert old["metadata.yaml"] != new["metadata.yaml"]
    d = plan.diff_bundles(new, old)
    assert d["first"] is False and d["database"] == "unchanged" and d["other"] == []
    for kind in ("datasets", "charts", "dashboards"):
        assert (d[kind]["added"], d[kind]["changed"], d[kind]["removed"], d[kind]["unknown"]) == ([], [], [], [])
    assert (d["datasets"]["unchanged"], d["charts"]["unchanged"], d["dashboards"]["unchanged"]) == (2, 4, 1)


def test_bundle_first_apply_lists_everything_as_added(tmp_path):
    d = plan.diff_bundles(bundle(project_copy(tmp_path)), None)
    assert d["first"] is True
    assert d["charts"]["added"] == ["c_line", "c_raw", "c_table", "c_total"]
    assert d["dashboards"]["added"] == ["main"] and d["datasets"]["added"] == ["owners", "things"]


def test_bundle_chart_added_changed_removed_and_layout_summary(tmp_path):
    old = bundle(project_copy(tmp_path / "a"))

    def edit(dash):
        d = dash["dashboards"]["main"]
        d["charts"] = [c for c in d["charts"] if c["key"] != "c_raw"]                       # 삭제
        d["charts"].append({"key": "c_new", "title": "새 차트", "viz": "big_number_total",   # 추가
                            "dataset": "things", "metric": "total_score"})
        next(c for c in d["charts"] if c["key"] == "c_table")["row_limit"] = 9               # 옵션 변경
        next(c for c in d["charts"] if c["key"] == "c_total")["title"] = "총합"              # 제목 변경
        d["layout"] = [["c_total", {"chart": "c_line", "width": 8}], ["c_table", "c_new"]]

    d = plan.diff_bundles(bundle(project_copy(tmp_path / "b", mutate_dash=edit)), old)
    assert d["charts"]["added"] == ["c_new"] and d["charts"]["removed"] == ["c_raw"]
    assert d["charts"]["changed"] == [{"key": "c_table", "detail": "옵션 변경"},
                                      {"key": "c_total", "detail": "제목 변경"}]
    assert d["dashboards"]["changed"] == [{"key": "main", "detail": "배치 변경"}]
    assert d["datasets"]["changed"] == []


def test_bundle_dashboard_filter_and_title_summary(tmp_path):
    old = bundle(project_copy(tmp_path / "a"))

    def edit(dash):
        d = dash["dashboards"]["main"]
        d["title"] = "새 제목"
        d["filters"].append({"key": "f_owner", "label": "담당", "type": "select", "dataset": "things",
                             "column": "owner_label"})
        d["cross_filters"] = True

    d = plan.diff_bundles(bundle(project_copy(tmp_path / "b", mutate_dash=edit)), old)
    assert d["dashboards"]["changed"] == [{"key": "main", "detail": "제목 변경, 필터 변경, 교차 필터 변경"}]
    assert d["charts"]["changed"] == []


def test_bundle_dataset_column_and_metric_summary(tmp_path):
    project = project_copy(tmp_path / "a")
    old = bundle(project)
    wider = {**COLUMNS, "things": THINGS_VIEW + [("age_days", "integer")]}
    d = plan.diff_bundles(bundle(project, wider), old)
    assert d["datasets"]["changed"] == [{"key": "things", "detail": "열 추가: age_days"}]

    def edit(dash):
        dash["datasets"]["things"]["metrics"]["avg_score"] = "AVG(score)"

    d = plan.diff_bundles(bundle(project_copy(tmp_path / "b", mutate_dash=edit)), old)
    assert d["datasets"]["changed"] == [{"key": "things", "detail": "지표 변경"}]


def test_bundle_unknown_files_are_not_reported_as_changes(tmp_path):
    project = project_copy(tmp_path)
    old = bundle(project)
    new = bundle(project, {**COLUMNS, "things": []})
    d = plan.diff_bundles(new, old, {"datasets/dashkit/things.yaml": "이유", "charts/c_line.yaml": "이유"})
    assert d["datasets"]["unknown"] == [{"key": "things", "reason": "이유"}]
    assert d["datasets"]["changed"] == []
    assert [u["key"] for u in d["charts"]["unknown"]] == ["c_line"]


def test_read_bundle_dir_round_trip(tmp_path):
    from dashkit.sync.bundle import write_build, zip_bundle

    files = bundle(project_copy(tmp_path / "p"))
    assert plan.read_bundle_dir(tmp_path / "none") is None
    write_build(files, zip_bundle(files), tmp_path / "build")
    assert plan.read_bundle_dir(tmp_path / "build" / "bundle") == files


def test_other_project_in_state_file_is_reported(tmp_path):
    assert plan.read_other_project(tmp_path, "basic") is None                       # 상태 파일 없음
    (tmp_path / "managed.json").write_text(json.dumps(
        {"version": 1, "project": "basic", "objects": {"chart": ["a"], "dashboard": ["b"]}}), encoding="utf-8")
    assert plan.read_other_project(tmp_path, "basic") is None                       # 같은 프로젝트
    assert plan.read_other_project(tmp_path, "another") == {"project": "basic", "objects": 2}
    (tmp_path / "managed.json").write_text("{깨진", encoding="utf-8")
    assert plan.read_other_project(tmp_path, "another") is None


# ---------------------------------------------------------------- 출력

def _plan(tmp_path, tables, views, superset, changed=True) -> dict:
    return {"project": "basic", "changed": changed, "data": tables, "views": views, "superset": superset}


def test_format_and_json(tmp_path):
    project = project_copy(tmp_path / "a")
    old = bundle(project)

    def edit(dash):
        d = dash["dashboards"]["main"]
        d["charts"].append({"key": "c_new", "title": "새 차트", "viz": "big_number_total",
                            "dataset": "things", "metric": "total_score"})
        d["layout"].append(["c_new"])

    edited = project_copy(tmp_path / "b", mutate_dash=edit)
    wider = {**COLUMNS, "things": THINGS_VIEW + [("age_days", "integer")]}
    now = SimpleNamespace(columns=OWNERS_NOW, row_columns=["id", "label"], rows=[("o1", "가"), ("o2", "나")])
    tables = [
        plan.table_diff("owners", edited.schema["tables"]["owners"], [["o1", "가"], ["o2", "다"], ["o3", "라"]], now),
        plan.table_diff("things", edited.schema["tables"]["things"], [],
                        SimpleNamespace(columns=THINGS_NOW, row_columns=[c for c, _t in THINGS_NOW], rows=[])),
    ]
    predicted = {"things": {"columns": wider["things"], "reason": None},
                 "owners": {"columns": OWNERS_NOW, "reason": None}}
    views = plan.diff_views(predicted, COLUMNS)
    superset = {**plan.diff_bundles(bundle(edited, wider), old), "replaces": {"project": "before", "objects": 7}}
    result = _plan(tmp_path, tables, views, superset)
    json.dumps(result)   # 구조화된 결과는 JSON으로 바꿀 수 있어야 한다
    text = plan.format_plan(result)
    lines = text.splitlines()
    assert lines[0] == "데이터   owners  +1행 −0행 ~1행"
    assert lines[1] == "         변경 없음: things"
    assert lines[2] == "뷰       dash.things  열 추가: age_days"
    assert "Superset 데이터셋 ~1 (things: 열 추가: age_days)" in lines
    assert "         차트 +1 (c_new)" in lines
    assert "         대시보드 ~1 (main: 배치 변경)" in lines
    assert "         지금 적용된 프로젝트 'before'의 객체 7개 삭제" in lines
    assert lines[-1].startswith("→ 바뀌는 것이 있음")


def test_format_no_change_and_first_apply(tmp_path):
    project = project_copy(tmp_path)
    files = bundle(project)
    same = _plan(tmp_path, [], plan.diff_views({k: {"columns": v, "reason": None} for k, v in COLUMNS.items()}, COLUMNS),
                 plan.diff_bundles(files, files), changed=False)
    text = plan.format_plan(same)
    assert "뷰       변경 없음 (뷰 2개)" in text and "Superset 변경 없음" in text
    assert text.splitlines()[-1] == "→ 바뀌는 것이 없음"

    predicted = plan.predict_view_columns(project, {}, {}, None)
    first = _plan(tmp_path, [plan.table_diff("owners", project.schema["tables"]["owners"], [["o1", "가"]], None)],
                  plan.diff_views(predicted, {}), plan.diff_bundles(files, None))
    text = plan.format_plan(first)
    assert "owners  +1행 −0행 ~0행  (새 테이블)" in text
    assert "dash.things  적용해야 알 수 있음 (아직 적재된 적이 없음)" in text
    assert "Superset 처음 적용 (지난 번들이 없음)" in text
