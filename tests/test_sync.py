"""sync(dashkit.sync). 운영 객체와 섞이지 않도록 임시 프로젝트 이름과 임시 STATE_DIR을 쓴다.

임시 프로젝트는 현재 적용된 프로젝트의 설정을 복사한 것이다(뷰는 이미 DB에 있으므로 적재하지 않는다).
UUID가 프로젝트 이름에서 나오므로 운영 객체와 겹치지 않는다. 끝나면 만든 객체를 모두 지운다.
"""
from __future__ import annotations

import json

import pytest
import yaml

from dashkit import config
from dashkit.config import load_project
from dashkit.sync import sync

from conftest import copy_project, live_project

TEMP = "zz-synctest"
pytestmark = pytest.mark.integration
KINDS_DELETE_ORDER = ("dashboard", "chart", "dataset", "database")


def _objects(superset, project) -> dict[str, dict[str, int | None]]:
    out = {}
    for kind, uuids in _expected_uuids(project).items():
        out[kind] = {u: superset.find_id(kind, u) for u in uuids}
    return out


def _expected_uuids(project) -> dict[str, list[str]]:
    p = project.name
    return {
        "database": [config.object_uuid(p, "database", "main")],
        "dataset": [config.object_uuid(p, "dataset", k) for k in project.dashboard["datasets"]],
        "chart": [config.object_uuid(p, "chart", c["key"]) for _, c in config.charts(project)],
        "dashboard": [config.object_uuid(p, "dashboard", k) for k in project.dashboard["dashboards"]],
    }


def _totals(superset) -> dict[str, int]:
    return {k: superset.get(f"/api/v1/{k}/?q=(page_size:1)").json()["count"] for k in KINDS_DELETE_ORDER}


@pytest.fixture(scope="module")
def env(tmp_path_factory, superset):
    root = copy_project(live_project(), tmp_path_factory.mktemp("projects"), TEMP)
    state = tmp_path_factory.mktemp("state")
    build = tmp_path_factory.mktemp("build")
    yield root, state, build
    # 정리: 이 테스트가 만든 객체를 모두 지운다
    managed = json.loads((state / "managed.json").read_text()) if (state / "managed.json").exists() else {"objects": {}}
    for kind in KINDS_DELETE_ORDER:
        for u in managed["objects"].get(kind, []):
            oid = superset.find_id(kind, u)
            if oid is not None:
                superset.delete_object(kind, oid)


def _sync(root, state, build):
    project = load_project(TEMP, root)
    return project, sync(project, state_dir=state, build_dir=build, log=lambda m: None)


def test_sync_twice_is_idempotent(env, superset):
    root, state, build = env
    totals0 = _totals(superset)
    project, r1 = _sync(root, state, build)
    objs1 = _objects(superset, project)
    embed1 = json.loads((state / "embed.json").read_text())
    totals1 = _totals(superset)

    _, r2 = _sync(root, state, build)
    objs2 = _objects(superset, project)
    embed2 = json.loads((state / "embed.json").read_text())
    totals2 = _totals(superset)

    assert r1 == r2
    assert objs1 == objs2
    assert embed1["dashboards"] == embed2["dashboards"]
    assert totals1 == totals2
    expected = _expected_uuids(project)
    assert {k: totals1[k] - totals0[k] for k in totals1} == {k: len(v) for k, v in expected.items()}


def test_all_chart_keys_created(env, superset):
    root, state, build = env
    project = load_project(TEMP, root)
    missing = [c["key"] for _, c in config.charts(project)
               if superset.find_id("chart", config.object_uuid(TEMP, "chart", c["key"])) is None]
    assert missing == []
    embed = json.loads((state / "embed.json").read_text())
    assert set(embed["dashboards"]) == set(project.dashboard["dashboards"])


def test_removed_chart_is_pruned(env, superset):
    root, state, build = env
    path = root / TEMP / "dashboard.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    # 차트가 둘 이상인 대시보드에서 마지막 차트 하나를 지운다(배치에서도)
    dkey, d = next((k, v) for k, v in doc["dashboards"].items() if len(v["charts"]) > 1)
    victim = d["charts"].pop()["key"]
    new_layout = []
    for row in d["layout"]:
        row = [i for i in row if (i if isinstance(i, str) else i["chart"]) != victim]
        if row:
            new_layout.append(row)
    d["layout"] = new_layout
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")

    victim_uuid = config.object_uuid(TEMP, "chart", victim)
    assert superset.find_id("chart", victim_uuid) is not None
    project, result = _sync(root, state, build)
    assert superset.find_id("chart", victim_uuid) is None
    assert victim_uuid not in result["chart"]
    others = [config.object_uuid(TEMP, "chart", c["key"]) for _, c in config.charts(project)]
    assert all(superset.find_id("chart", u) is not None for u in others)
