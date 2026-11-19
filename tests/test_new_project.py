"""make new로 만든 초안이 실제로 적용되는지(보강설계서 8절 4단계 완료 조건).

make test의 4단계가 예시 CSV 폴더(projects/sales-sample/data)에서 projects/zz-newtest를 만들고 apply한 뒤 이 파일을 돌린다.
평소에는 건너뛴다.
"""
from __future__ import annotations

from typing import Any

import pytest

from dashkit import charts, config
from dashkit.config import load_project

from conftest import PROJECTS, api, live_project

PROJECT = "zz-newtest"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(live_project() != PROJECT, reason="make test의 초안 생성 단계에서만 실행"),
]


@pytest.fixture(scope="module")
def project():
    return load_project(PROJECT, PROJECTS)


def test_draft_is_applied(project):
    code, app = api("GET", "/api/app")
    assert code == 200 and app["project"] == PROJECT and app["applied"] is True
    assert [t["key"] for t in app["tabs"]] == [t["key"] for t in project.dashboard["tabs"]]
    for tab in app["tabs"]:
        for sec in tab["sections"]:
            if sec["type"] == "superset":
                assert sec["embed_id"], sec


def test_draft_widget_data(project):
    datasets = config.widget_datasets(project)
    assert datasets, "예시 CSV에서는 위젯 섹션이 하나 이상 나와야 함"
    for ds in datasets:
        code, body = api("GET", f"/api/data/{ds}")
        assert code == 200 and body["rows"], ds


def test_draft_charts_return_rows(project, superset):
    all_charts = [c for _d, c in config.charts(project)]
    assert all_charts
    for chart in all_charts:
        columns = [col for _path, col in charts.column_refs(chart)]
        metrics: list[Any] = []
        for f in charts.registry()[chart["viz"]].fields:
            value = chart.get(f.name)
            if f.kind == "metric" and value is not None:
                metrics.append(charts.metric_param(value))
            elif f.kind == "metrics" and value:
                metrics += [charts.metric_param(m) for m in value]
        chart_id = superset.find_id("chart", config.object_uuid(PROJECT, "chart", chart["key"]))
        ds_id = superset.find_id("dataset", config.object_uuid(PROJECT, "dataset", chart["dataset"]))
        assert chart_id is not None and ds_id is not None, chart["key"]
        r = superset.post("/api/v1/chart/data", json={
            "datasource": {"id": ds_id, "type": "table"}, "force": True,
            "queries": [{"metrics": metrics, "columns": list(dict.fromkeys(columns)), "filters": [], "orderby": [],
                         "row_limit": 1000, "extras": {}}],
            "form_data": {"slice_id": chart_id}, "result_format": "json", "result_type": "full",
        })
        assert r.status_code == 200, f"{chart['key']}: {r.status_code} {r.text[:300]}"
        assert r.json()["result"][0]["data"], f"{chart['key']}: 행이 없음"
