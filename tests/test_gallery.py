"""차트 갤러리(projects/chart-gallery, 보강설계서 4-5).

- 단위: 갤러리가 레지스트리의 축약 유형 전부와 카탈로그의 원본 유형을 하나씩 담고 있는지.
- 통합(chart-gallery가 적용된 스택): 차트마다 `POST /api/v1/chart/data`가 200이고 행이 있는지.
  축약 차트는 레지스트리의 kind로 열·지표를 모아 쿼리를 만든다. 원본(viz_type) 차트는 params에 열 이름이
  어떤 키로 들어가는지 알 수 없으므로 아래 RAW_QUERY_KEYS에 유형별로 적어 둔다.

이 테스트가 보는 것은 "차트가 참조한 열과 지표로 데이터가 나온다"까지다. 유형별 후처리(상자 수염, 히스토그램 등)와
화면 렌더링은 헤드리스 캡처로 확인했고 결과는 contracts/superset-notes.md 10절에 있다.
"""
from __future__ import annotations

from typing import Any

import pytest

from dashkit import charts, config
from dashkit.config import load_project

from conftest import PROJECTS, live_project

PROJECT = "chart-gallery"
# 갤러리에 원본 방식으로 넣은 유형. 카탈로그의 chord, world_map, country_map은 임베드(guest) 화면에서
# 그려지지 않아 넣지 않았다(contracts/superset-notes.md 10절).
RAW_TYPES = ("graph_chart", "tree_chart", "gantt_chart")
# 원본 유형의 params에서 열 이름이 들어가는 키, 지표가 들어가는 키
RAW_QUERY_KEYS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "graph_chart": (("source", "target"), ("metric",)),
    "tree_chart": (("id", "parent", "name"), ("metric",)),
    "gantt_chart": (("start_time", "end_time", "y_axis", "series"), ()),
}

def _project():
    return load_project(PROJECT, PROJECTS)


def _charts() -> list[dict]:
    return [c for _d, c in config.charts(_project())]


def _unique(items: list[Any]) -> list[Any]:
    out: list[Any] = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


def chart_query(chart: dict) -> tuple[list[str], list[Any]]:
    """차트가 참조한 (열 목록, 지표 목록). 지표는 Superset 형식(이름 또는 adhoc SQL)."""
    if "viz" in chart:
        columns = [col for _path, col in charts.column_refs(chart)]
        metrics: list[Any] = []
        for f in charts.registry()[chart["viz"]].fields:
            value = chart.get(f.name)
            if value is None:
                continue
            if f.kind == "metric":
                metrics.append(charts.metric_param(value))
            elif f.kind == "metrics":
                metrics += [charts.metric_param(m) for m in value]
        return _unique(columns), _unique(metrics)
    viz_type = chart["viz_type"]
    assert viz_type in RAW_QUERY_KEYS, f"원본 유형 '{viz_type}'의 열·지표 키를 RAW_QUERY_KEYS에 적어야 함"
    column_keys, metric_keys = RAW_QUERY_KEYS[viz_type]
    params = chart["params"]
    columns = []
    for k in column_keys:
        value = params.get(k)
        columns += value if isinstance(value, list) else [value] if value else []
    return _unique(columns), _unique([params[k] for k in metric_keys if params.get(k)])


# ---------------------------------------------------------------- 단위

def test_gallery_covers_every_type():
    all_charts = _charts()
    shorthand = {c["viz"] for c in all_charts if "viz" in c}
    assert shorthand == set(charts.registry()), "갤러리에 축약 유형이 하나씩 있어야 함"
    raw = {c["viz_type"] for c in all_charts if "viz_type" in c}
    assert raw == set(RAW_TYPES)
    assert not raw & {t.viz_type for t in charts.registry().values()}, "축약으로 올린 유형은 축약으로 쓴다"


def test_every_gallery_chart_has_a_query():
    for chart in _charts():
        columns, metrics = chart_query(chart)
        assert columns or metrics, chart["key"]


def test_column_refs_follow_kinds_in_config():
    """설정 검사가 유형별 필드 이름이 아니라 kind로 열을 모은다(bubble의 x는 지표, heatmap의 y는 열)."""
    refs = config.referenced_columns(_project())["harvests"]
    by_key = {c["key"]: i for i, c in enumerate(_project().dashboard["dashboards"]["shelf_cross"]["charts"])}
    base = "dashboards.shelf_cross.charts"
    assert f"{base}[{by_key['g_heatmap']}].y" in refs["fruit"]
    assert f"{base}[{by_key['g_bubble']}].entity" in refs["orchard"]
    assert "avg_crate_won" not in refs and "weight_total" not in refs


# ---------------------------------------------------------------- 통합

def _dataset_id(superset, key: str) -> int:
    oid = superset.find_id("dataset", config.object_uuid(PROJECT, "dataset", key))
    assert oid is not None, f"Superset에 데이터셋 {key}가 없음(make apply 필요)"
    return oid


@pytest.mark.integration
@pytest.mark.skipif(live_project() != PROJECT, reason="chart-gallery가 적용된 상태에서만 확인한다")
@pytest.mark.parametrize("chart", _charts(), ids=lambda c: c["key"])
def test_chart_data_has_rows(superset, chart):
    columns, metrics = chart_query(chart)
    chart_id = superset.find_id("chart", config.object_uuid(PROJECT, "chart", chart["key"]))
    assert chart_id is not None, f"Superset에 차트 {chart['key']}가 없음(make apply 필요)"
    query: dict[str, Any] = {"metrics": metrics, "columns": columns, "filters": [], "orderby": [],
                             "row_limit": 1000, "extras": {}}
    if chart.get("where"):
        query["extras"]["where"] = chart["where"]
    r = superset.post("/api/v1/chart/data", json={
        "datasource": {"id": _dataset_id(superset, chart["dataset"]), "type": "table"},
        "force": True, "queries": [query], "form_data": {"slice_id": chart_id},
        "result_format": "json", "result_type": "full",
    })
    assert r.status_code == 200, f"{chart['key']}: {r.status_code} {r.text[:500]}"
    result = r.json()["result"][0]
    assert not result.get("error"), f"{chart['key']}: {result.get('error')}"
    assert len(result["data"]) > 0, f"{chart['key']}: 행이 없음"
