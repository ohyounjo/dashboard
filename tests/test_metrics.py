"""지표 정합성(devtracker). Superset 차트 데이터 API(POST /api/v1/chart/data)로 조회한다.

기대값의 출처: reference/core.test.js (열린 이슈 16, Critical 미해결 3, 가중 진척률 55, D-29, 🆕 D2·D3·D5·D6·D7).
"""
from __future__ import annotations

import pytest

from dashkit import config
from dashkit.config import load_project

from conftest import PROJECTS, live_project

PROJECT = "devtracker"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(live_project() != PROJECT, reason="devtracker가 적용된 상태에서만 확인한다"),
]


@pytest.fixture(scope="module")
def project():
    return load_project(PROJECT, PROJECTS)


def dataset_id(superset, key: str) -> int:
    oid = superset.find_id("dataset", config.object_uuid(PROJECT, "dataset", key))
    assert oid is not None, f"Superset에 데이터셋 {key}가 없음(make apply 필요)"
    return oid


def chart_data(superset, ds_id: int, *, metrics=(), columns=(), filters=(), orderby=(), slice_id=None) -> list[dict]:
    body = {
        "datasource": {"id": ds_id, "type": "table"},
        "force": True,
        "queries": [{
            "metrics": list(metrics), "columns": list(columns), "filters": list(filters),
            "orderby": list(orderby), "row_limit": 1000, "extras": {},
        }],
        "result_format": "json", "result_type": "full",
    }
    if slice_id:
        body["form_data"] = {"slice_id": slice_id}
    r = superset.post("/api/v1/chart/data", json=body)
    assert r.status_code == 200, r.text[:500]
    return r.json()["result"][0]["data"]


def chart_metric_value(superset, project, chart_key: str):
    """차트 설정 그대로(데이터셋 + 지표)를 차트 데이터 API로 조회한다."""
    chart = next(c for _, c in config.charts(project) if c["key"] == chart_key)
    chart_id = superset.find_id("chart", config.object_uuid(PROJECT, "chart", chart_key))
    assert chart_id is not None
    params = superset.get(f"/api/v1/chart/{chart_id}").json()["result"]["params"]
    import json as _json
    metric = _json.loads(params)["metric"]
    rows = chart_data(superset, dataset_id(superset, chart["dataset"]), metrics=[metric], slice_id=chart_id)
    assert len(rows) == 1
    return next(iter(rows[0].values()))


def test_weighted_progress_55(superset, project):
    assert chart_metric_value(superset, project, "kpi_progress") == 55


def test_days_to_next_due_29(superset, project):
    assert chart_metric_value(superset, project, "kpi_dday") == 29


def test_open_issues_16(superset):
    rows = chart_data(superset, dataset_id(superset, "issues"), metrics=["open_issues"])
    assert rows[0]["open_issues"] == 16


def test_critical_open_3(superset):
    rows = chart_data(superset, dataset_id(superset, "issues"), metrics=["critical_open"])
    assert rows[0]["critical_open"] == 3


def test_snapshot_trend_last_week_matches(superset):
    rows = chart_data(superset, dataset_id(superset, "kpi_snapshots"),
                      columns=["week_start", "open_issues", "critical_open", "progress"],
                      orderby=[["week_start", False]])
    last = rows[0]
    assert (last["open_issues"], last["critical_open"], last["progress"]) == (16, 3, 55)


def test_new_docs(superset):
    rows = chart_data(superset, dataset_id(superset, "docs"), columns=["id"],
                      filters=[{"col": "is_new", "op": "==", "val": True}], orderby=[["id", True]])
    assert [r["id"] for r in rows] == ["D2", "D3", "D5", "D6", "D7"]
