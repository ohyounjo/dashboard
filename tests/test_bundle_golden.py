"""번들 스냅샷(보강설계서 3-1). 고정된 뷰 열 정보와 시각으로 만든 번들이 tests/golden/과 한 바이트도 다르지 않아야 한다.

의도한 변경이면 `make golden`으로 갱신하고 diff를 PR에서 검토한다.
"""
from __future__ import annotations

import pytest

from dashkit.config import load_project
from dashkit.sync import read_view_columns

import golden
from conftest import PROJECTS, live_project


@pytest.mark.parametrize("name", golden.GOLDEN_PROJECTS)
def test_bundle_matches_golden(name):
    built = golden.build(name)
    expected = golden.read_golden(name)
    assert sorted(built) == sorted(expected), "번들 파일 목록이 스냅샷과 다름(make golden)"
    diff = [path for path in built if built[path] != expected[path]]
    assert diff == [], "스냅샷과 내용이 다른 파일(make golden 후 diff 검토): " + ", ".join(diff)


@pytest.mark.parametrize("name", golden.GOLDEN_PROJECTS)
def test_bundle_is_deterministic(name):
    assert golden.build(name) == golden.build(name)


@pytest.mark.parametrize("name", golden.GOLDEN_PROJECTS)
def test_view_columns_cover_datasets(name):
    project = load_project(name, PROJECTS)
    assert set(golden.read_columns(name)) == set(project.dashboard["datasets"])


@pytest.mark.integration
def test_golden_view_columns_match_db():
    """스냅샷에 쓰는 뷰 열 정보가 실제 DB의 뷰와 같은지(현재 적용된 프로젝트)."""
    name = live_project()
    if name not in golden.GOLDEN_PROJECTS:
        pytest.skip("스냅샷 대상 프로젝트가 아님")
    actual = read_view_columns(load_project(name, PROJECTS))
    assert {k: [tuple(c) for c in v] for k, v in actual.items()} == golden.read_columns(name), \
        "뷰 열이 바뀜: PROJECT=<이름> make golden COLUMNS=1"
