"""설정 검사(dashkit/config.py). DB 없이 돈다."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from dashkit import config
from dashkit.config import ConfigError, load_project

FIXTURES = Path(__file__).parent / "fixtures"
PROJECTS = Path(__file__).resolve().parent.parent / "projects"


def make_project(tmp_path: Path, mutate_dash=None, mutate_schema=None, name="basic") -> Path:
    dst = tmp_path / name
    shutil.copytree(FIXTURES / "basic", dst)
    for fname, fn in (("dashboard.yaml", mutate_dash), ("schema.yaml", mutate_schema)):
        if fn:
            p = dst / fname
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
            fn(doc)
            p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return tmp_path


_counter = iter(range(10**6))


def errors_of(tmp_path, **kw) -> list[str]:
    base = tmp_path / f"case{next(_counter)}"
    with pytest.raises(ConfigError) as exc:
        load_project("basic", make_project(base, **kw))
    return exc.value.errors


# ---------------------------------------------------------------- 통과

def test_fixture_loads(tmp_path):
    p = load_project("basic", make_project(tmp_path))
    assert p.schema["tables"]["things"]["primary_key"] == ["id"]
    assert p.schema["tables"]["things"]["columns"]["id"]["required"] is True  # PK는 required
    assert config.table_order(p) == ["owners", "things"]
    assert config.widget_datasets(p) == {"things"}
    sec = p.dashboard["tabs"][0]["sections"][0]
    assert sec["title"] is None and sec["height"] is None  # 선택 필드는 None으로 채움


@pytest.mark.parametrize("name", sorted(d.name for d in PROJECTS.iterdir() if (d / "dashboard.yaml").exists())
                         if PROJECTS.exists() else [])
def test_example_projects_pass_check(name):
    load_project(name, PROJECTS)


def test_example_projects_exist():
    names = {d.name for d in PROJECTS.iterdir()} if PROJECTS.exists() else set()
    assert {"devtracker", "sales-sample"} <= names


# ---------------------------------------------------------------- 요구된 3가지 오류

def test_unknown_dataset_reference(tmp_path):
    def m(d):
        d["dashboards"]["main"]["charts"][0]["dataset"] = "nope"
    errs = errors_of(tmp_path, mutate_dash=m)
    assert any("charts[0].dataset" in e and "'nope'" in e for e in errs), errs


def test_layout_chart_key_not_in_charts(tmp_path):
    def m(d):
        d["dashboards"]["main"]["layout"][1][0] = "ghost"
    errs = errors_of(tmp_path, mutate_dash=m)
    assert any("layout[1][0]" in e and "'ghost'" in e for e in errs), errs
    assert any("'c_table'가 layout에 배치되지 않음" in e for e in errs), errs


def test_filter_column_not_in_view(tmp_path):
    def m(d):
        d["dashboards"]["main"]["filters"][0]["column"] = "no_such_col"
    p = load_project("basic", make_project(tmp_path, mutate_dash=m))  # sql 데이터셋은 정적으로 통과
    view_cols = {"things": {"id", "owner_id", "phase", "score", "begins", "ends", "owner_label"},
                 "owners": {"id", "label"}}
    errs = config.check_view_columns(p, view_cols)
    assert errs == ["dashboard.yaml:dashboards.main.filters[0].column: 데이터셋 'things' 뷰에 열 'no_such_col'가 없음"]


def test_filter_column_missing_on_table_dataset_is_static(tmp_path):
    def m(d):
        d["dashboards"]["main"]["filters"][0].update(dataset="owners", column="no_such_col")
    errs = errors_of(tmp_path, mutate_dash=m)
    assert any("filters[0].column" in e and "no_such_col" in e for e in errs), errs


# ---------------------------------------------------------------- 그 밖의 참조 검사

def test_unknown_metric_name(tmp_path):
    def m(d):
        d["dashboards"]["main"]["charts"][0]["metric"] = "missing_metric"
    assert any("지표 'missing_metric'" in e for e in errors_of(tmp_path, mutate_dash=m))


def test_duplicate_chart_key(tmp_path):
    def m(d):
        d["dashboards"]["main"]["charts"][1]["key"] = "c_total"
    assert any("중복" in e for e in errors_of(tmp_path, mutate_dash=m))


def test_section_refs(tmp_path):
    def m(d):
        d["tabs"][0]["sections"][0]["dashboard"] = "nope"
        d["tabs"][1]["sections"][1]["dataset"] = "nope2"
    errs = errors_of(tmp_path, mutate_dash=m)
    assert any("sections[0].dashboard" in e for e in errs)
    assert any("sections[1].dataset" in e for e in errs)


def test_row_too_wide(tmp_path):
    def m(d):
        d["dashboards"]["main"]["layout"][0][1]["width"] = 12
    assert any("12칸" in e for e in errors_of(tmp_path, mutate_dash=m))


def test_reserved_word_column(tmp_path):
    def m(s):
        s["tables"]["things"]["columns"]["end"] = {"type": "date"}
    assert any("'end'은 SQL 예약어" in e for e in errors_of(tmp_path, mutate_schema=m))


def test_ref_cycle_and_bad_ref(tmp_path):
    def m(s):
        s["tables"]["owners"]["columns"]["thing_id"] = {"type": "text", "ref": "things.id"}
    assert any("순환" in e for e in errors_of(tmp_path, mutate_schema=m))

    def m2(s):
        s["tables"]["things"]["columns"]["owner_id"]["ref"] = "owners.label"
    assert any("단일 열 primary_key가 아님" in e for e in errors_of(tmp_path, mutate_schema=m2))


def test_json_schema_errors_have_paths(tmp_path):
    def m(d):
        d["tabs"][0]["icon"] = "rocket"
        d["dashboards"]["main"]["charts"][1].pop("x")
    errs = errors_of(tmp_path, mutate_dash=m)
    assert any(e.startswith("dashboard.yaml:tabs[0].icon:") for e in errs), errs
    assert any(e.startswith("dashboard.yaml:dashboards.main.charts[1]") for e in errs), errs


def test_all_errors_collected(tmp_path):
    def m(d):
        d["dashboards"]["main"]["charts"][0]["dataset"] = "nope"
        d["dashboards"]["main"]["layout"][1][0] = "ghost"
        d["tabs"][0]["sections"][0]["dashboard"] = "nope"
    assert len(errors_of(tmp_path, mutate_dash=m)) >= 3


# ---------------------------------------------------------------- 한 쿼리 안 열·지표 라벨 중복(superset-notes.md 14절)
# 픽스처의 c_line(charts[1]) 자리를 아래 차트로 바꾼다. 데이터셋 things의 지표 total_score와 alt_score는 표시 이름이 같다.

def _replace_line_chart(spec: dict, labels: dict | None = None):
    def m(d):
        ds = d["datasets"]["things"]
        ds["metrics"]["alt_score"] = "MAX(score)"
        ds["labels"] = labels if labels is not None else {"total_score": "점수", "alt_score": "점수"}
        d["dashboards"]["main"]["charts"][1] = {"key": "c_line", "title": "추이", "dataset": "things", **spec}
    return m


S = lambda sql, label: {"sql": sql, "label": label}   # noqa: E731


@pytest.mark.parametrize("spec, path, label", [
    # x축 유형: x와 groupby가 같은 열, groupby 여럿 가운데 하나만 같음, groupby에 같은 열 두 번(합쳐지지 않는다)
    ({"viz": "line", "x": "begins", "time_grain": "P1M", "groupby": ["begins"], "metrics": ["total_score"]}, "groupby[0]", "begins"),
    ({"viz": "bar", "x": "phase", "groupby": ["id", "phase"], "metrics": ["total_score"]}, "groupby[1]", "phase"),
    ({"viz": "area", "x": "phase", "groupby": ["id", "id"], "metrics": ["total_score"]}, "groupby[1]", "id"),
    ({"viz": "waterfall", "x": "phase", "groupby": ["phase"], "metric": "total_score"}, "groupby[0]", "phase"),
    # 지표 라벨이 같은 쿼리의 열 이름과 같음(adhoc 라벨)
    ({"viz": "bar", "x": "phase", "metrics": [S("COUNT(*)", "phase")]}, "metrics[0]", "phase"),
    ({"viz": "pie", "groupby": ["phase"], "metric": S("COUNT(*)", "phase")}, "metric", "phase"),
    ({"viz": "big_number", "time_column": "begins", "metric": S("COUNT(*)", "begins")}, "time_column", "begins"),
    # 이름이 다른 두 지표의 표시 이름(datasets.labels)이 같음
    ({"viz": "radar", "groupby": ["phase"], "metrics": ["total_score", "alt_score"]}, "metrics[1]", "점수"),
    ({"viz": "bar", "x": "phase", "metrics": [S("SUM(score)", "점수"), "total_score"]}, "metrics[1]", "점수"),
    # 서로 다른 열 필드에 같은 열
    ({"viz": "heatmap", "x": "phase", "y": "phase", "metric": "total_score"}, "y", "phase"),
    ({"viz": "sankey", "source": "phase", "target": "phase", "metric": "total_score"}, "target", "phase"),
    ({"viz": "bubble", "entity": "id", "series": "id", "x": "total_score", "y": S("MAX(score)", "m"), "size": S("COUNT(*)", "n")},
     "series", "id"),
    ({"viz": "histogram", "column": "score", "groupby": ["score"]}, "groupby[0]", "score"),
    ({"viz": "box_plot", "groupby": ["phase"], "columns": ["phase"], "metrics": ["total_score"]}, "columns[0]", "phase"),
    # 혼합: 묶음마다 따로 본다(둘째 묶음 안에서 겹침)
    ({"viz": "mixed", "x": "phase", "metrics": ["total_score"], "metrics_b": [S("COUNT(*)", "phase")]}, "metrics_b[0]", "phase"),
    # params가 축약 필드의 파라미터를 덮어씀
    ({"viz": "bar", "x": "phase", "metrics": ["total_score"], "params": {"groupby": ["phase"]}}, "params.groupby[0]", "phase"),
    # 원본 방식: viz_type이 레지스트리 유형(hook 없음)과 같으면 그 유형의 파라미터 이름으로 읽는다
    ({"viz_type": "echarts_timeseries_bar", "params": {"x_axis": "phase", "groupby": ["phase"], "metrics": ["total_score"]}},
     "params.groupby[0]", "phase"),
    ({"viz_type": "pie", "params": {"groupby": ["phase"], "metric": {"expressionType": "SQL", "sqlExpression": "COUNT(*)",
                                                                       "label": "phase"}}}, "params.metric", "phase"),
])
def test_duplicate_query_labels_are_reported(tmp_path, spec, path, label):
    errs = errors_of(tmp_path, mutate_dash=_replace_line_chart(spec))
    hits = [e for e in errs if e.startswith(f"dashboard.yaml:dashboards.main.charts[1].{path}: ")]
    assert len(hits) == 1, errs
    assert f"라벨 '{label}'가 한 쿼리에서 겹침" in hits[0] and '"Duplicate column/metric labels"' in hits[0]
    assert len(errs) == 1, errs


def test_duplicate_label_message_names_display_label_source(tmp_path):
    errs = errors_of(tmp_path, mutate_dash=_replace_line_chart(
        {"viz": "bar", "x": "phase", "metrics": ["total_score", "alt_score"]}))
    assert errs == [
        "dashboard.yaml:dashboards.main.charts[1].metrics[1]: 열·지표 라벨 '점수'가 한 쿼리에서 겹침(metrics[0], metrics[1]). "
        "Superset이 차트 자리에 \"Duplicate column/metric labels\" 오류를 냄. "
        "지표 'total_score', 지표 'alt_score'의 라벨은 데이터셋 labels의 표시 이름임"
    ]


@pytest.mark.parametrize("spec", [
    {"viz": "bar", "x": "phase", "groupby": ["id"], "metrics": ["total_score"]},
    # 같은 저장 지표 또는 동일 정의의 adhoc 지표 반복은 허용한다
    {"viz": "bar", "x": "phase", "metrics": ["total_score", "total_score"]},
    {"viz": "line", "x": "begins", "metrics": [S("COUNT(*)", "n"), S("COUNT(*)", "n")]},
    {"viz": "line", "x": "begins", "metrics": [S("COUNT(*)", "n")], "params": {"metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "n", "optionName": "a"}, {"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "n", "optionName": "b"}]}},
    # 합치는 유형(query.merge_columns): 같은 열 두 번, 행·열에 같은 열
    {"viz": "pie", "groupby": ["phase", "phase"], "metric": "total_score"},
    {"viz": "pivot_table", "rows": ["phase"], "columns": ["phase"], "metrics": ["total_score"]},
    {"viz": "table", "columns": ["id", "id"]},
    {"viz": "table", "groupby": ["phase", "phase"], "metrics": ["total_score", "total_score"]},
    # 정렬(order_by)은 라벨이 아니다
    {"viz": "table", "columns": ["id", "phase"], "order_by": [["id", "asc"], ["id", "desc"]]},
    # 혼합: 두 묶음은 따로 간다
    {"viz": "mixed", "x": "phase", "groupby": ["id"], "groupby_b": ["id"], "metrics": ["total_score"],
     "metrics_b": ["total_score"]},
    # 열 쪽은 표시 이름을 쓰지 않는다: 열 표시 이름이 지표 표시 이름과 같아도 됨
    {"viz": "bar", "x": "phase", "metrics": ["total_score"], "_labels": {"phase": "점수", "total_score": "점수"}},
    # 판단할 수 없는 원본 차트(레지스트리에 없는 viz_type, hook 유형)는 보지 않는다
    {"viz_type": "graph_chart", "params": {"source": "phase", "target": "phase", "metric": "total_score"}},
    {"viz_type": "table", "params": {"query_mode": "raw", "all_columns": ["phase"],
                                     "metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "phase"}]}},
])
def test_query_labels_that_superset_accepts(tmp_path, spec):
    spec = dict(spec)
    labels = spec.pop("_labels", None)
    load_project("basic", make_project(tmp_path, mutate_dash=_replace_line_chart(spec, labels)))


@pytest.mark.parametrize("spec, path", [
    ({"viz": "bar", "x": "phase", "metrics": [S("COUNT(*)", "n"), S("SUM(score)", "n")]}, "metrics[1]"),
    ({"viz": "mixed", "x": "phase", "metrics": ["total_score"], "metrics_b": [S("COUNT(*)", "n"), S("SUM(score)", "n")]}, "metrics_b[1]"),
    ({"viz": "mixed", "x": "phase", "metrics": [S("COUNT(*)", "n"), S("SUM(score)", "n")], "metrics_b": ["total_score"]}, "metrics[1]"),
    ({"viz": "bar", "x": "phase", "metrics": ["total_score"], "params": {"metrics": ["total_score", {"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "total_score"}]}}, "params.metrics[1]"),
    ({"viz_type": "table", "params": {"query_mode": "aggregate", "groupby": ["phase"], "metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "phase"}]}}, "params.metrics[0]"),
    ({"viz": "table", "columns": ["id"], "params": {"query_mode": "aggregate", "groupby": ["phase"], "metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "phase"}]}}, "params.metrics[0]"),
])
def test_query_collision_reports_yaml_path(tmp_path, spec, path):
    errs = errors_of(tmp_path, mutate_dash=_replace_line_chart(spec))
    assert any(e.startswith(f"dashboard.yaml:dashboards.main.charts[1].{path}:") for e in errs), errs


@pytest.mark.parametrize("viz_type, params, path", [
    ("graph_chart", {"source": "phase", "target": "id", "metric": {"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "phase"}}, "params.metric"),
    ("tree_chart", {"id": "phase", "parent": "id", "name": "score", "metric": {"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "phase"}}, "params.metric"),
])
def test_native_chart_collision(tmp_path, viz_type, params, path):
    errs = errors_of(tmp_path, mutate_dash=_replace_line_chart({"viz_type": viz_type, "params": params}))
    assert any(path in e and "Duplicate column/metric labels" in e for e in errs), errs


@pytest.mark.parametrize("metrics", [
    ["total_score", {"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "total_score"}],
    [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "total_score"}, "total_score"],
])
def test_stored_and_adhoc_metric_conflict_in_either_order(tmp_path, metrics):
    spec = {"viz_type": "echarts_timeseries_bar", "params": {"x_axis": "phase", "metrics": metrics}}
    errs = errors_of(tmp_path, mutate_dash=_replace_line_chart(spec))
    assert len(errs) == 1 and "params.metrics[1]" in errs[0] and "첫 지표만" in errs[0], errs


def test_table_percent_metric_collision_and_omitted_mode(tmp_path):
    sql = lambda expression: {"expressionType": "SQL", "sqlExpression": expression, "label": "n"}
    spec = {"viz_type": "table", "params": {"groupby": ["id"], "all_columns": ["phase"], "metrics": [sql("COUNT(*)")], "percent_metrics": [sql("SUM(score)")]}}
    errs = errors_of(tmp_path, mutate_dash=_replace_line_chart(spec))
    assert any("params.percent_metrics[0]" in e and "params.metrics[0]" in e for e in errs), errs


def test_metric_removal_message_names_both_yaml_paths(tmp_path):
    errs = errors_of(tmp_path, mutate_dash=_replace_line_chart(
        {"viz": "bar", "x": "phase", "metrics": [S("COUNT(*)", "n"), S("SUM(score)", "n")]}))
    assert errs == ["dashboard.yaml:dashboards.main.charts[1].metrics[1]: 지표 라벨 'n'로 서로 다른 지표가 겹침(metrics[0], metrics[1]). Superset이 첫 지표만 보내므로 각 지표에 고유한 라벨을 쓰세요."]


@pytest.mark.parametrize("spec", [
    {"viz": "bar", "x": "phase", "metrics": [S("COUNT(*)", "n"), S("SUM(score)", "n")], "params": {"metrics": ["total_score"]}},
    {"viz": "mixed", "x": "phase", "metrics": [S("COUNT(*)", "n")], "metrics_b": [S("SUM(score)", "n")]},
    {"viz_type": "table", "params": {"query_mode": "raw", "all_columns": ["phase"], "groupby": ["id"], "metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "phase"}]}},
    {"viz_type": "table", "params": {"query_mode": "unknown", "groupby": ["phase"], "metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "phase"}]}},
    {"viz_type": "table", "params": {"query_mode": "aggregate", "groupby": [{"sqlExpression": "phase", "label": "phase"}], "metrics": [{"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "phase"}]}},
])
def test_query_override_and_separate_queries_are_allowed(tmp_path, spec):
    load_project("basic", make_project(tmp_path, mutate_dash=_replace_line_chart(spec)))


def test_shorthand_override_to_native_uses_params(tmp_path):
    spec = {"viz": "sankey", "source": "phase", "target": "id", "metric": "total_score", "params": {"viz_type": "graph_chart", "metric": {"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "phase"}}}
    errs = errors_of(tmp_path, mutate_dash=_replace_line_chart(spec))
    assert any("params.metric" in e and "Duplicate column/metric labels" in e for e in errs), errs


def test_overridden_viz_type_uses_final_query(tmp_path):
    spec = {"viz_type": "unknown", "params": {"viz_type": "pie", "groupby": ["phase"], "metric": {"expressionType": "SQL", "sqlExpression": "COUNT(*)", "label": "phase"}}}
    errs = errors_of(tmp_path, mutate_dash=_replace_line_chart(spec))
    assert any("params.metric" in e and "Duplicate column/metric labels" in e for e in errs), errs


# ---------------------------------------------------------------- 도구 함수

def test_object_uuid_is_deterministic():
    a = config.object_uuid("p", "chart", "k")
    assert a == config.object_uuid("p", "chart", "k")
    assert a != config.object_uuid("q", "chart", "k")
    assert config.object_uuid("devtracker", "chart", "kpi_progress") == "1a48d16a-b967-5f07-b247-5f1864bafae2"


def test_layout_rows_widths(tmp_path):
    p = load_project("basic", make_project(tmp_path))
    rows = config.layout_rows(p.dashboard["dashboards"]["main"])
    assert [(i["chart"], i["width"]) for i in rows[0]] == [("c_total", 4), ("c_line", 8)]
    assert [i["width"] for i in rows[1]] == [6, 6]
    assert all(i["height"] == 50 for r in rows for i in r)


def test_referenced_columns(tmp_path):
    p = load_project("basic", make_project(tmp_path))
    refs = config.referenced_columns(p)
    assert set(refs["things"]) == {"begins", "id", "phase", "owner_label", "ends", "score"}
    assert "owners" not in refs  # viz_type 원본 차트의 params는 검사하지 않음


def test_resolve_as_of(tmp_path):
    p = load_project("basic", make_project(tmp_path))
    assert config.resolve_as_of(p) == date(2026, 1, 15)

    def m(d):
        d["app"]["as_of"] = "today"
    p2 = load_project("basic", make_project(tmp_path / "x", mutate_dash=m))
    assert config.resolve_as_of(p2, today=date(2030, 5, 1)) == date(2030, 5, 1)
