"""plan의 비교 기준(.build/<프로젝트>/applied/)은 import에 성공한 번들만 담는다. Superset 없이 가짜 클라이언트로 본다."""
from __future__ import annotations

import pytest

from dashkit import plan
from dashkit.config import load_project
from dashkit.sync import SyncError, sync
from dashkit.sync.bundle import APPLIED_ROOT, BUNDLE_ROOT, build_bundle

import golden
from conftest import PROJECTS

NAME = "devtracker"


class FakeClient:
    base_url = "http://fake"

    def __init__(self, fail_import: bool):
        self.fail_import = fail_import
        self.imported = False

    def wait_ready(self, timeout: int = 0) -> None:
        pass

    def login(self) -> None:
        pass

    def import_bundle(self, zip_bytes: bytes, passwords: dict) -> None:
        if self.fail_import:
            raise SyncError("asset 번들 가져오기 실패(시험)")
        self.imported = True


class StopAfterImport(Exception):
    pass


class FakeClientStopsAfterImport(FakeClient):
    def find_id(self, kind: str, uuid: str):
        raise StopAfterImport()


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("DATA_DB_READER_PASSWORD", "x")


def test_failed_import_does_not_become_the_baseline(tmp_path, env):
    project = load_project(NAME, PROJECTS)
    build, state = tmp_path / "build", tmp_path / "state"
    with pytest.raises(SyncError):
        sync(project, state_dir=state, build_dir=build, client=FakeClient(fail_import=True),
             view_columns=golden.read_columns(NAME), log=lambda m: None)
    # 이번에 만든 번들은 원인 확인용으로 남지만, 적용된 번들 기록은 생기지 않는다
    assert (build / NAME / BUNDLE_ROOT).is_dir()
    assert not (build / NAME / APPLIED_ROOT).exists()
    assert plan.read_bundle_dir(build / NAME / APPLIED_ROOT) is None


def test_successful_import_writes_the_baseline(tmp_path, env):
    project = load_project(NAME, PROJECTS)
    build, state = tmp_path / "build", tmp_path / "state"
    client = FakeClientStopsAfterImport(fail_import=False)
    with pytest.raises(StopAfterImport):
        sync(project, state_dir=state, build_dir=build, client=client,
             view_columns=golden.read_columns(NAME), log=lambda m: None)
    assert client.imported
    applied = plan.read_bundle_dir(build / NAME / APPLIED_ROOT)
    built = build_bundle(project, golden.read_columns(NAME))
    assert applied is not None and set(applied) == set(built)
    assert {k: v for k, v in applied.items() if k != "metadata.yaml"} == {k: v for k, v in built.items() if k != "metadata.yaml"}


def test_failed_import_keeps_the_previous_baseline(tmp_path, env):
    project = load_project(NAME, PROJECTS)
    build, state = tmp_path / "build", tmp_path / "state"
    with pytest.raises(StopAfterImport):
        sync(project, state_dir=state, build_dir=build, client=FakeClientStopsAfterImport(fail_import=False),
             view_columns=golden.read_columns(NAME), log=lambda m: None)
    marker = build / NAME / APPLIED_ROOT / "charts" / "zz_previous.yaml"
    marker.write_text("previous: true\n", encoding="utf-8")
    with pytest.raises(SyncError):
        sync(project, state_dir=state, build_dir=build, client=FakeClient(fail_import=True),
             view_columns=golden.read_columns(NAME), log=lambda m: None)
    assert marker.is_file(), "실패한 sync가 지난 성공 기록을 건드리면 안 됨"


def test_plan_sees_objects_a_failed_prune_left_behind(tmp_path):
    """prune에 실패한 객체는 managed.json에 남는다. 번들 비교에는 안 나오므로 plan이 따로 센다(리뷰 지적 회귀)."""
    import json

    from dashkit.config import object_uuid
    from dashkit.sync.bundle import managed_objects

    project = load_project(NAME, PROJECTS)
    current = managed_objects(project)
    state = tmp_path / "state"
    state.mkdir()

    def write(doc: dict) -> None:
        (state / "managed.json").write_text(json.dumps(doc), encoding="utf-8")

    assert plan.read_pending_prune(state, project) == 0            # 상태 파일 없음
    write({"version": 1, "project": NAME, "objects": current})
    assert plan.read_pending_prune(state, project) == 0            # 지난 sync가 깨끗이 끝남
    leftover = {**current, "chart": current["chart"] + [object_uuid(NAME, "chart", "zz_removed")],
                "dashboard": current["dashboard"] + [object_uuid(NAME, "dashboard", "zz_gone")]}
    write({"version": 1, "project": NAME, "objects": leftover})
    assert plan.read_pending_prune(state, project) == 2            # 삭제에 실패한 두 객체
    write({"version": 1, "project": "zz-other", "objects": leftover})
    assert plan.read_pending_prune(state, project) == 0            # 다른 프로젝트의 기록은 replaces가 맡는다
    write({"broken": True})
    assert plan.read_pending_prune(state, project) == 0
