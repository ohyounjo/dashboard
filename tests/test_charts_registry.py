"""차트 유형 레지스트리(dashkit/charts/). DB 없이 돈다."""
from __future__ import annotations

import inspect

import pytest
import yaml
from jsonschema import Draft202012Validator

from dashkit import charts, config, generated
from dashkit.sync import bundle


def write_type(tmp_path, name, doc):
    (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    return tmp_path


def test_every_type_file_is_registered_and_valid():
    types = charts.registry()
    assert {p.stem for p in charts.CHARTS_DIR.glob("*.yaml")} == set(types)
    assert {"big_number_total", "big_number", "line", "bar", "pie", "table"} <= set(types)
    schema = config.load_schema("dashboard.schema.json")
    Draft202012Validator.check_schema(schema)
    assert schema["$defs"]["chart"]["properties"]["viz"]["enum"] == list(types)
    for viz in types:
        assert f"v_{viz}" in schema["$defs"]


def test_chart_params_has_no_per_type_branch():
    """8절 1단계 완료 조건: chart_params에 유형별 분기가 없다."""
    source = inspect.getsource(bundle.chart_params) + inspect.getsource(charts.build_params)
    # 중복 라벨 검사도 유형 이름이 아니라 kind와 유형 파일의 query 선언으로 판단한다
    for fn in (charts.label_conflicts, charts._label_values, charts._raw_types, charts._parse_query):
        source += inspect.getsource(fn)
    for viz in charts.registry():
        assert f'"{viz}"' not in source and f"'{viz}'" not in source


def test_new_type_needs_only_one_file(tmp_path):
    """유형 파일 하나로 스키마, params, 열·지표 참조가 모두 나온다."""
    types = charts.load_registry(write_type(tmp_path, "zz_sample", {
        "viz": "zz_sample", "viz_type": "zz_native", "label": "견본",
        "fields": {
            "groupby": {"kind": "columns", "required": True, "min_items": 1},
            "metric": {"kind": "metric", "required": True},
            "mode": {"kind": "enum", "values": ["a", "b"], "param": "zz_mode", "default": "a"},
            "format": {"kind": "d3format", "param": "number_format"},
        },
        "defaults": {"sort_by_metric": True},
    }))
    t = types["zz_sample"]
    assert t.schema()["required"] == ["groupby", "metric"]
    assert t.schema()["properties"]["mode"] == {"enum": ["a", "b"]}
    schema = charts.extend_schema(config.load_schema("dashboard.schema.json"), types)
    Draft202012Validator.check_schema(schema)
    assert "zz_sample" in schema["$defs"]["chart"]["properties"]["viz"]["enum"]
    assert "| `zz_sample` | 견본 | `zz_native` |" in charts.shorthand_table(types)


@pytest.mark.parametrize("doc, reason", [
    ({"viz": "zz_bad", "viz_type": "x", "label": "x", "fields": {"a": {"kind": "nope"}}}, "kind"),
    ({"viz": "zz_bad", "viz_type": "x", "label": "x", "fields": {"title": {"kind": "string"}}}, "공통 필드"),
    ({"viz": "zz_bad", "viz_type": "x", "label": "x", "fields": {"a": {"kind": "string", "oops": 1}}}, "알 수 없는 키"),
    ({"viz": "zz_bad", "viz_type": "x", "label": "x", "fields": {"a": {"kind": "enum"}}}, "values"),
    ({"viz": "zz_other", "viz_type": "x", "label": "x"}, "파일 이름"),
    ({"viz": "zz_bad", "label": "x"}, "viz_type"),
    ({"viz": "zz_bad", "viz_type": "x", "label": "x", "hook": "dashkit.charts.hooks:missing"}, "hook"),
    ({"viz": "zz_bad", "viz_type": "x", "label": "x", "query": {"oops": 1}}, "query: 알 수 없는 키"),
    ({"viz": "zz_bad", "viz_type": "x", "label": "x", "query": {"merge_columns": "yes"}}, "merge_columns"),
    ({"viz": "zz_bad", "viz_type": "x", "label": "x", "fields": {"a": {"kind": "columns"}}, "query": {"groups": []}},
     "query.groups"),
    ({"viz": "zz_bad", "viz_type": "x", "label": "x", "fields": {"a": {"kind": "string"}}, "query": {"groups": [["a"]]}},
     "'a'은 kind가"),
    ({"viz": "zz_bad", "viz_type": "x", "label": "x", "query": {"groups": [["missing"]]}}, "'missing'은 kind가"),
])
def test_bad_type_file_is_rejected(tmp_path, doc, reason):
    with pytest.raises(charts.RegistryError) as exc:
        charts.load_registry(write_type(tmp_path, "zz_bad", doc))
    assert reason in str(exc.value)


def test_param_order_and_defaults():
    p = charts.build_params({"viz": "big_number", "metric": "m", "time_column": "d", "format": ".0f", "where": "a > 1"})
    assert list(p) == ["viz_type", "metric", "x_axis", "time_grain_sqla", "compare_lag", "show_trend_line",
                       "aggregation", "y_axis_format", "adhoc_filters"]
    assert p["time_grain_sqla"] == "P1D" and p["compare_lag"] == 1
    p = charts.build_params({"viz": "bar", "x": "c", "metrics": ["m", {"sql": "SUM(a)", "label": "합"}], "horizontal": True})
    assert p["orientation"] == "horizontal" and p["groupby"] == []
    assert p["metrics"][1] == {"expressionType": "SQL", "sqlExpression": "SUM(a)", "label": "합"}
    assert "time_grain_sqla" not in p


def test_refs_follow_kinds():
    chart = {"viz": "table", "groupby": ["a"], "metrics": ["m", {"sql": "SUM(b)", "label": "합"}],
             "order_by": [["합", "desc"], ["a", "asc"], ["m", "asc"]]}
    assert charts.metric_refs(chart) == [(["metrics", 0], "m")]
    assert charts.column_refs(chart) == [(["groupby", 0], "a"), (["order_by", 1, 0], "a")]
    chart = {"viz": "big_number", "metric": "m", "time_column": "d"}
    assert charts.metric_refs(chart) == [(["metric"], "m")]
    assert charts.column_refs(chart) == [(["time_column"], "d")]


def test_query_declaration():
    """query 선언: 생략하면 라벨 kind(column·columns·metric·metrics) 필드 전부가 쿼리 하나, groups로 나눌 수 있다."""
    types = charts.registry()
    assert types["bar"].query_groups == (("x", "metrics", "groupby"),) and not types["bar"].merge_columns
    assert types["table"].query_groups == (("columns", "groupby", "metrics"),) and types["table"].merge_columns
    assert types["mixed"].query_groups == (("x", "groupby", "metrics"), ("x", "groupby_b", "metrics_b"))
    assert types["big_number_total"].query_groups == (("metric",),)
    for t in types.values():
        assert t.query_groups, t.viz


def test_label_conflicts_follow_declaration(tmp_path, monkeypatch):
    """새 유형 파일의 query 선언만으로 검사가 따라온다(유형 이름을 보지 않는다)."""
    doc = {"viz": "zz_two", "viz_type": "zz_native", "label": "견본",
           "fields": {"a": {"kind": "column"}, "b": {"kind": "columns"}, "m": {"kind": "metric"}, "n": {"kind": "metric"}}}
    # 모든 라벨이 c: a, b[0], b[1], 지표 m(adhoc 라벨 c), 지표 n(표시 이름 c)
    chart = {"viz": "zz_two", "a": "c", "b": ["c", "c"], "m": {"sql": "1", "label": "c"}, "n": "k"}

    def conflicts(query, target=chart):
        types = charts.load_registry(write_type(tmp_path, "zz_two", {**doc, **({"query": query} if query else {})}))
        monkeypatch.setattr(charts, "registry", lambda: types)
        return [path for path, _reason in charts.label_conflicts(target, {"k": "c"})]

    assert conflicts(None) == [["n"]]                                    # 쿼리 하나: 마지막 자리 하나로 보고
    assert conflicts({"groups": [["a", "m"], ["b", "n"]]}) == [["m"], ["n"]]   # 쿼리마다 따로
    assert conflicts({"merge_columns": True}) == [["n"]]                 # b[1]은 합쳐져도 a·m·n이 겹침
    assert conflicts({"merge_columns": True}, {"viz": "zz_two", "b": ["c", "c"]}) == []
    assert conflicts(None, {"viz": "zz_two", "b": ["c", "c"]}) == [["b", 1]]


def test_generated_docs_are_current():
    assert generated.check() == [], "생성 구간이 최신이 아님: make gen"
