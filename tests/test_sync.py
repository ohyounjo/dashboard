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


def _without(row: list, victim: str) -> list:
    return [i for i in row if (i if isinstance(i, str) else i.get("chart")) != victim]


def _drop_one_chart(root) -> str:
    """임시 프로젝트 설정에서 차트 하나를 지우고(배치와 필터 범위에서도) 그 차트의 UUID를 돌려준다."""
    path = root / TEMP / "dashboard.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    # 차트가 둘 이상인 대시보드에서 마지막 차트 하나를 지운다
    dkey, d = next((k, v) for k, v in doc["dashboards"].items() if len(v["charts"]) > 1)
    victim = d["charts"].pop()["key"]
    new_layout = []
    for entry in d["layout"]:
        if isinstance(entry, dict):   # 대시보드 안 탭
            for tab in entry["tabs"]:
                tab["rows"] = [r for r in (_without(row, victim) for row in tab["rows"]) if r]
            entry["tabs"] = [t for t in entry["tabs"] if t["rows"]]
            if entry["tabs"]:
                new_layout.append(entry)
        elif _without(entry, victim):
            new_layout.append(_without(entry, victim))
    d["layout"] = new_layout
    for flt in d.get("filters") or []:
        if "charts" in flt:
            flt["charts"] = [k for k in flt["charts"] if k != victim] or [d["charts"][0]["key"]]
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return config.object_uuid(TEMP, "chart", victim)


def test_removed_chart_is_pruned(env, superset):
    root, state, build = env
    victim_uuid = _drop_one_chart(root)
    assert superset.find_id("chart", victim_uuid) is not None
    project, result = _sync(root, state, build)
    assert superset.find_id("chart", victim_uuid) is None
    assert victim_uuid not in result["chart"]
    others = [config.object_uuid(TEMP, "chart", c["key"]) for _, c in config.charts(project)]
    assert all(superset.find_id("chart", u) is not None for u in others)


# ---------------------------------------------------------------- 표식(보강설계서 3-5, D3)

def _marks(superset, project) -> dict[str, set]:
    """종류 → 그 종류 객체들의 is_managed_externally 값 집합(한 건 조회)."""
    out: dict[str, set] = {}
    for kind, uuids in _expected_uuids(project).items():
        for u in uuids:
            obj = superset.get_object(kind, superset.find_id(kind, u))
            assert obj is not None and "is_managed_externally" in obj, f"{kind} {u}"
            out.setdefault(kind, set()).add(obj["is_managed_externally"])
    return out


def _make_chart_by_hand(superset, project) -> int:
    """Superset 화면에서 만든 것과 같은 차트(표식 없음, UUID 버전 4)를 임시 프로젝트의 데이터셋 위에 만든다."""
    dataset_id = superset.find_id("dataset", _expected_uuids(project)["dataset"][0])
    body = superset.expect(superset.post("/api/v1/chart/", json={
        "slice_name": f"{TEMP} 손으로 만든 차트", "viz_type": "table", "datasource_id": dataset_id,
        "datasource_type": "table", "params": "{}",
    }), "차트 생성")
    return int(body["id"])


def test_objects_carry_the_mark(env, superset):
    """import가 표식을 저장한다(네 종류 모두). 번들의 표식 키는 export로는 검증되지 않아 여기서 본다."""
    root, state, build = env
    project, _ = _sync(root, state, build)
    assert _marks(superset, project) == {k: {True} for k in KINDS_DELETE_ORDER}


def test_prune_without_state_file(env, superset):
    """.state/managed.json이 없어도 이전 객체가 지워진다. 표식 없는 객체와 다른 프로젝트의 객체는 그대로다."""
    root, state, build = env
    project, _ = _sync(root, state, build)
    by_hand = _make_chart_by_hand(superset, project)
    try:
        victim_uuid = _drop_one_chart(root)
        assert superset.find_id("chart", victim_uuid) is not None
        (state / "managed.json").unlink()
        totals_before = _totals(superset)

        project, result = _sync(root, state, build)

        assert superset.find_id("chart", victim_uuid) is None
        assert victim_uuid not in result["chart"]
        assert superset.get_object("chart", by_hand) is not None, "표식 없는 차트는 지우지 않는다"
        totals_after = _totals(superset)
        assert {k: totals_before[k] - totals_after[k] for k in totals_after} == {
            "dashboard": 0, "chart": 1, "dataset": 0, "database": 0}, "지운 것은 그 차트 하나뿐이어야 함"
        assert all(i is not None for objs in _objects(superset, project).values() for i in objs.values())
        assert json.loads((state / "managed.json").read_text())["objects"] == result
    finally:
        superset.delete_object("chart", by_hand)


def test_unlocked_sync_keeps_identifying_objects(env, superset, monkeypatch):
    """SYNC_LOCK_UI=0: 화면 편집 차단만 꺼지고(is_managed_externally false), 상태 파일 없는 prune은 계속 된다."""
    root, state, build = env
    monkeypatch.setenv("SYNC_LOCK_UI", "0")
    project, _ = _sync(root, state, build)
    assert _marks(superset, project) == {k: {False} for k in KINDS_DELETE_ORDER}

    by_hand = _make_chart_by_hand(superset, project)
    try:
        victim_uuid = _drop_one_chart(root)
        (state / "managed.json").unlink()
        project, result = _sync(root, state, build)
        assert superset.find_id("chart", victim_uuid) is None
        assert superset.get_object("chart", by_hand) is not None
    finally:
        superset.delete_object("chart", by_hand)

    # 다시 잠그면 표식이 돌아온다
    monkeypatch.delenv("SYNC_LOCK_UI")
    project, _ = _sync(root, state, build)
    assert _marks(superset, project) == {k: {True} for k in KINDS_DELETE_ORDER}
