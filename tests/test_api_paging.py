"""/api/data의 선택 매개변수 limit·cursor(contracts/api.md, 설계서 8-5). 안 보내면 지금과 같다."""
from __future__ import annotations

import urllib.parse

import pytest

from conftest import PROJECTS, api, live_project
from dashkit.config import load_project, widget_datasets

pytestmark = pytest.mark.integration


def test_paged_rows_are_the_same_rows_as_one_shot():
    project = load_project(live_project(), PROJECTS)
    names = sorted(widget_datasets(project))
    assert names, "위젯 데이터셋이 있는 예시여야 함"
    for name in names:
        code, whole = api("GET", f"/api/data/{name}")
        assert code == 200 and "next_cursor" not in whole
        rows, cursor, pages = [], None, 0
        while True:
            q = "?limit=3" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
            code, page = api("GET", f"/api/data/{name}{q}")
            assert code == 200, page
            assert len(page["rows"]) <= 3 and page["changed"] is False and page["columns"] == whole["columns"]
            rows += page["rows"]
            pages += 1
            cursor = page["next_cursor"]
            if not cursor:
                break
        key = lambda r: sorted((k, str(v)) for k, v in r.items())   # noqa: E731
        assert sorted(map(key, rows)) == sorted(map(key, whole["rows"])), name
        assert pages == max(1, -(-len(whole["rows"]) // 3))


def test_paged_requests_are_checked():
    name = sorted(widget_datasets(load_project(live_project(), PROJECTS)))[0]
    for q in ("?limit=0", "?limit=201", "?limit=x", "?cursor=garbage"):
        code, body = api("GET", f"/api/data/{name}{q}")
        assert code == 422 and body["detail"] == "invalid_query", (q, body)
