"""범용성: PROJECT를 바꿔 apply하면 코드 변경 없이 화면이 바뀌고 이전 프로젝트 객체가 남지 않는다.

`make test`가 PROJECT=<다른 예시>로 apply한 뒤 SWITCH_FROM=<원래 프로젝트>를 주고 이 파일만 실행한다.
평소 pytest 실행에서는 건너뛴다. 끝나면 make test가 원래 프로젝트로 다시 apply한다.
"""
from __future__ import annotations

import os

import pytest

from dashkit import config
from dashkit.config import load_project

from conftest import PROJECTS, api, live_project

SWITCH_FROM = os.environ.get("SWITCH_FROM")
pytestmark = pytest.mark.skipif(not SWITCH_FROM, reason="make test의 전환 단계에서만 실행")


@pytest.fixture(scope="module")
def now():
    return load_project(live_project(), PROJECTS)


@pytest.fixture(scope="module")
def before():
    return load_project(SWITCH_FROM, PROJECTS)


def test_switched_project_differs(now, before):
    assert now.name != before.name


def test_tabs_changed(now, before):
    code, app = api("GET", "/api/app")
    assert code == 200 and app["project"] == now.name and app["applied"] is True
    assert app["app"]["title"] == now.dashboard["app"]["title"]
    keys = [t["key"] for t in app["tabs"]]
    assert keys == [t["key"] for t in now.dashboard["tabs"]]
    assert not set(keys) & {t["key"] for t in before.dashboard["tabs"]}
    types = {s["type"] for t in app["tabs"] for s in t["sections"]}
    assert {"superset", "gantt", "kanban"} <= types


def test_widget_data_changed(now, before):
    for ds in config.widget_datasets(now):
        code, body = api("GET", f"/api/data/{ds}")
        assert code == 200 and body["rows"]
    for ds in config.widget_datasets(before) - config.widget_datasets(now):
        code, _ = api("GET", f"/api/data/{ds}")
        assert code == 404


def test_superset_objects_switched(now, before, superset):
    for kind, keys in (
        ("dataset", list(before.dashboard["datasets"])),
        ("chart", [c["key"] for _, c in config.charts(before)]),
        ("dashboard", list(before.dashboard["dashboards"])),
        ("database", ["main"]),
    ):
        left = [k for k in keys if superset.find_id(kind, config.object_uuid(before.name, kind, k)) is not None]
        assert left == [], f"이전 프로젝트 {kind}가 남음: {left}"
    charts_now = [c["key"] for _, c in config.charts(now)]
    assert all(superset.find_id("chart", config.object_uuid(now.name, "chart", k)) for k in charts_now)
    total = superset.get("/api/v1/chart/?q=(page_size:1)").json()["count"]
    assert total == len(charts_now), "Superset의 차트가 현재 프로젝트 차트와 정확히 같아야 함"


def test_guest_token_for_new_dashboard(now):
    dkey = next(s["dashboard"] for t in now.dashboard["tabs"] for s in t["sections"] if s["type"] == "superset")
    code, body = api("POST", "/api/guest-token", {"dashboard": dkey})
    assert code == 200 and body["token"]
