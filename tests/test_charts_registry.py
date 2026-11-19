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


def test_generated_docs_are_current():
    assert generated.check() == [], "생성 구간이 최신이 아님: make gen"
