"""탭 판정과 세 경로의 필터(단위, DB 없이: 그룹 판정은 가짜 store). 계약: contracts/auth.md "접근 제어"."""
from __future__ import annotations

import pytest
import yaml

from dashkit.auth import access, admin_routes
from dashkit.auth.routes import Principal

from conftest import ADMIN
from test_reload import write_embed
from test_reload import api, root, state_dir  # noqa: F401

USER = Principal(id="00000000-0000-4000-8000-000000000002", email="user@example.com", role="user")
OPERATOR = Principal(id="00000000-0000-4000-8000-000000000003", email="op@example.com", role="operator")


@pytest.fixture
def grants(monkeypatch):
    """{(user_id, project): {탭 키}}. store.user_tab_keys를 대신한다."""
    table: dict[tuple[str, str], set[str]] = {}
    monkeypatch.setattr(access.store, "user_tab_keys", lambda uid, project: set(table.get((uid, project), set())))
    return table


def tab_keys(body):
    return [t["key"] for t in body["tabs"]]


def test_operators_see_every_tab_without_db(api, monkeypatch):
    monkeypatch.setattr(access.store, "user_tab_keys", lambda *_: pytest.fail("운용자는 그룹을 읽지 않는다"))
    project = api.ctx.current().project
    all_keys = [t["key"] for t in project.dashboard["tabs"]]
    assert tab_keys(api.get_app(user=ADMIN)) == all_keys
    assert tab_keys(api.get_app(user=OPERATOR)) == all_keys
    assert api.get_app(user=OPERATOR)["user"] == {"email": "op@example.com", "role": "operator"}


def test_user_sees_union_of_group_tabs_in_config_order(api, grants):
    project = api.ctx.current().project
    keys = [t["key"] for t in project.dashboard["tabs"]]
    assert len(keys) >= 2
    assert tab_keys(api.get_app(user=USER)) == []
    body = api.get_app(user=USER)
    assert body["applied"] is True                       # 남은 탭이 없으면 적용할 것도 없다
    grants[(USER.id, project.name)] = {keys[-1], keys[0], "no_such_tab"}      # 없는 탭은 무시
    assert tab_keys(api.get_app(user=USER)) == [keys[0], keys[-1]]
    grants[(USER.id, "other-project")] = set(keys)       # 다른 프로젝트의 권한은 쓰지 않는다
    grants[(USER.id, project.name)] = set()
    assert tab_keys(api.get_app(user=USER)) == []


def test_guest_token_and_data_follow_allowed_tabs(api, grants, root, state_dir, monkeypatch):
    monkeypatch.setenv("GUEST_TOKEN_JWT_SECRET", "s" * 40)
    monkeypatch.setenv("GUEST_TOKEN_JWT_AUDIENCE", "aud")
    write_embed(state_dir, "basic")
    project = api.ctx.current().project
    tabs = project.dashboard["tabs"]
    sup = next(t for t in tabs if any(s["type"] == "superset" for s in t["sections"]))
    wid = next(t for t in tabs if any(s.get("dataset") for s in t["sections"]))
    dashboard = next(s["dashboard"] for s in sup["sections"] if s["type"] == "superset")
    dataset = next(s["dataset"] for s in wid["sections"] if s.get("dataset"))
    assert sup["key"] != wid["key"]

    with pytest.raises(api.ApiError) as exc:
        api.post_guest_token(api.GuestTokenRequest(dashboard=dashboard), user=USER)
    assert (exc.value.status, exc.value.detail) == (403, "forbidden")
    with pytest.raises(api.ApiError) as exc:
        api.get_data(dataset, user=USER)
    assert (exc.value.status, exc.value.detail) == (404, "unknown_dataset")
    with pytest.raises(api.ApiError) as exc:                         # 설정에 없는 키는 지금처럼 404
        api.post_guest_token(api.GuestTokenRequest(dashboard="nope"), user=USER)
    assert exc.value.status == 404

    grants[(USER.id, project.name)] = {sup["key"]}
    assert api.post_guest_token(api.GuestTokenRequest(dashboard=dashboard), user=USER)["token"]
    with pytest.raises(api.ApiError):
        api.get_data(dataset, user=USER)
    assert access.allowed_datasets(USER, project) == set()

    grants[(USER.id, project.name)] = {wid["key"]}
    assert access.allowed_datasets(USER, project) == {dataset}
    with pytest.raises(api.ApiError) as exc:
        api.post_guest_token(api.GuestTokenRequest(dashboard=dashboard), user=USER)
    assert exc.value.status == 403
    assert api.post_guest_token(api.GuestTokenRequest(dashboard=dashboard), user=OPERATOR)["token"]


def test_same_dashboard_in_two_tabs_is_allowed_by_either(root):
    """같은 Superset 대시보드를 두 탭이 쓰면 한 탭만 허용돼도 그 대시보드의 토큰이 나간다(4-2 알려진 한계)."""
    from dashkit import config

    path = root / "basic" / "dashboard.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    sup = next(t for t in doc["tabs"] if any(s["type"] == "superset" for s in t["sections"]))
    doc["tabs"].append({**sup, "key": "copy", "label": "사본"})
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    project = config.load_project("basic", root)
    assert config.superset_dashboards(project, {"copy"}) == config.superset_dashboards(project, {sup["key"]})


def test_operator_can_only_manage_user_accounts():
    users = [{"role": "user"}]
    admin_routes.ensure_can_manage(OPERATOR, users)
    admin_routes.ensure_can_manage(ADMIN, users + [{"role": "admin"}, {"role": "operator"}])
    for target in ({"role": "admin"}, {"role": "operator"}):
        with pytest.raises(admin_routes.ApiError) as exc:
            admin_routes.ensure_can_manage(OPERATOR, users + [target])
        assert (exc.value.status, exc.value.detail) == (403, "forbidden")


def test_page_sorts_and_slices():
    items = [{"n": "b", "c": 2}, {"n": "a", "c": None}, {"n": "c", "c": 1}]
    keys = {"n": lambda i: i["n"], "c": lambda i: i["c"]}
    assert [i["n"] for i in admin_routes.page(items, 1, 50, None, keys, "n")["items"]] == ["a", "b", "c"]
    assert [i["n"] for i in admin_routes.page(items, 1, 50, "-n", keys, "n")["items"]] == ["c", "b", "a"]
    assert [i["n"] for i in admin_routes.page(items, 1, 50, "c", keys, "n")["items"]] == ["c", "b", "a"]   # None은 뒤로
    r = admin_routes.page(items, 2, 2, "n", keys, "n")
    assert r == {"items": [{"n": "c", "c": 1}], "total": 3}
    with pytest.raises(admin_routes.ApiError):
        admin_routes.page(items, 1, 50, "password_hash", keys, "n")
