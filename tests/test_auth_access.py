"""그룹과 탭 단위 접근 제어(통합). 계약: contracts/auth.md "접근 제어", "운용 API — 그룹". 설계서 13절 2단계 완료 조건:
user 계정이 허용된 탭만 보고, 다른 탭의 guest token·데이터 요청이 403·404.
"""
from __future__ import annotations

import secrets
import uuid

import pytest

from dashkit import config, db
from dashkit.config import load_project

from conftest import PROJECTS, admin_cookie, api, delete_user, http_full, login, make_user, random_email, WEB_URL

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def project():
    return load_project(live_project_name(), PROJECTS)


def live_project_name() -> str:
    from conftest import live_project
    return live_project()


@pytest.fixture(scope="module")
def tabs(project):
    """(superset 섹션만 있는 탭, 위젯 데이터셋이 있는 탭, 그 데이터셋, 그 대시보드)"""
    sup = next(t for t in project.dashboard["tabs"]
               if any(s["type"] == "superset" for s in t["sections"]) and not config.widget_datasets(project, {t["key"]}))
    wid = next(t for t in project.dashboard["tabs"] if config.widget_datasets(project, {t["key"]}) and t["key"] != sup["key"])
    dataset = sorted(config.widget_datasets(project, {wid["key"]}))[0]
    dashboard = next(s["dashboard"] for s in sup["sections"] if s["type"] == "superset")
    others = config.superset_dashboards(project) - config.superset_dashboards(project, {sup["key"]})
    return sup["key"], wid["key"], dataset, dashboard, sorted(others)


@pytest.fixture
def people():
    """일반사용자·운용진·다른 admin 계정과 그 세션 쿠키."""
    made = []

    def person(role):
        email, pw = random_email(f"acc-{role}"), secrets.token_urlsafe(14)
        user = make_user(email, pw, role=role)
        made.append(user.id)
        return {"id": user.id, "email": email, "cookie": login(email, pw)}

    out = {"user": person("user"), "operator": person("operator"), "admin2": person("admin")}
    yield out
    for uid in made:
        delete_user(uid)


@pytest.fixture
def groups():
    """테스트가 만든 그룹 id 목록(끝나면 지운다)."""
    made: list[str] = []
    yield made
    for gid in made:
        api("DELETE", f"/api/admin/groups/{gid}")


def new_group(groups, cookie=None, **body):
    name = body.pop("name", f"test-group-{secrets.token_hex(3)}")
    code, g = api("POST", "/api/admin/groups", {"name": name, **body}, cookie=cookie)
    assert code == 201, g
    groups.append(g["id"])
    return g


def as_user(person, method, path, body=None):
    return api(method, path, body, cookie=person["cookie"])


def auth_rows(query, params=()):
    with db.auth_conn(autocommit=True) as conn:
        return conn.execute(query, params).fetchall()


# ---------------------------------------------------------------- 완료 조건

def test_user_sees_only_allowed_tabs(project, tabs, people, groups):
    sup_key, wid_key, dataset, dashboard, other_dashboards = tabs
    user = people["user"]

    # 그룹이 없으면 탭이 없다
    code, app = as_user(user, "GET", "/api/app")
    assert code == 200 and app["tabs"] == [] and app["user"]["role"] == "user"
    code, body = as_user(user, "POST", "/api/guest-token", {"dashboard": dashboard})
    assert (code, body["detail"]) == (403, "forbidden")
    code, body = as_user(user, "GET", f"/api/data/{dataset}")
    assert (code, body["detail"]) == (404, "unknown_dataset")

    g = new_group(groups)
    code, _ = api("PUT", f"/api/admin/groups/{g['id']}/tabs", {"project": project.name, "tab_keys": [sup_key]})
    assert code == 200
    code, r = api("PUT", f"/api/admin/groups/{g['id']}/members", {"add": [user["email"].upper()]})
    assert code == 200 and r == {"not_found": []}

    code, app = as_user(user, "GET", "/api/app")
    assert [t["key"] for t in app["tabs"]] == [sup_key] and app["applied"] is True
    code, body = as_user(user, "POST", "/api/guest-token", {"dashboard": dashboard})
    assert code == 200 and body["token"]
    for other in other_dashboards:                                     # 다른 탭의 대시보드
        code, body = as_user(user, "POST", "/api/guest-token", {"dashboard": other})
        assert (code, body["detail"]) == (403, "forbidden"), other
    code, body = as_user(user, "GET", f"/api/data/{dataset}")           # 다른 탭의 데이터
    assert (code, body["detail"]) == (404, "unknown_dataset")

    # 두 번째 그룹이 위젯 탭을 허용하면 합집합
    g2 = new_group(groups)
    api("PUT", f"/api/admin/groups/{g2['id']}/tabs", {"project": project.name, "tab_keys": [wid_key]})
    api("PUT", f"/api/admin/groups/{g2['id']}/members", {"add": [user["id"]]})
    code, app = as_user(user, "GET", "/api/app")
    expected = [t["key"] for t in project.dashboard["tabs"] if t["key"] in (sup_key, wid_key)]
    assert [t["key"] for t in app["tabs"]] == expected
    code, body = as_user(user, "GET", f"/api/data/{dataset}")
    assert code == 200 and body["rows"]

    # 구성원에서 빼면 다음 요청부터 반영된다
    api("PUT", f"/api/admin/groups/{g['id']}/members", {"remove": [user["id"]]})
    code, app = as_user(user, "GET", "/api/app")
    assert [t["key"] for t in app["tabs"]] == [wid_key]
    code, _ = as_user(user, "POST", "/api/guest-token", {"dashboard": dashboard})
    assert code == 403

    # 그룹을 지우면 연결만 지워지고 계정은 남는다
    assert api("DELETE", f"/api/admin/groups/{g2['id']}")[0] == 204
    groups.remove(g2["id"])
    code, app = as_user(user, "GET", "/api/app")
    assert code == 200 and app["tabs"] == []


def test_operators_see_every_tab(project, people):
    for who in ("operator", "admin2"):
        code, app = as_user(people[who], "GET", "/api/app")
        assert [t["key"] for t in app["tabs"]] == [t["key"] for t in project.dashboard["tabs"]], who


# ---------------------------------------------------------------- 그룹 API 권한과 오류

def test_admin_api_is_for_operators_only(people, groups):
    code, body = as_user(people["user"], "GET", "/api/admin/groups")
    assert (code, body["detail"]) == (403, "forbidden")
    code, body = as_user(people["user"], "POST", "/api/admin/groups", {"name": "x"})
    assert (code, body["detail"]) == (403, "forbidden")
    code, body = api("GET", "/api/admin/groups", auth=False)
    assert (code, body["detail"]) == (401, "unauthenticated")
    g = new_group(groups, cookie=people["operator"]["cookie"])           # operator도 그룹을 만든다
    code, body = as_user(people["operator"], "GET", f"/api/admin/groups/{g['id']}")
    assert code == 200 and body["name"] == g["name"]


def test_operator_cannot_put_operators_or_admins_in_groups(people, groups):
    g = new_group(groups)
    op = people["operator"]
    for target in (people["admin2"], op):
        code, body = as_user(op, "PUT", f"/api/admin/groups/{g['id']}/members",
                             {"add": [people["user"]["email"], target["email"]]})
        assert (code, body["detail"]) == (403, "forbidden")
    assert api("GET", f"/api/admin/groups/{g['id']}")[1]["members"] == []      # 아무것도 바뀌지 않았다
    code, _ = as_user(op, "PUT", f"/api/admin/groups/{g['id']}/members", {"add": [people["user"]["email"]]})
    assert code == 200
    code, _ = api("PUT", f"/api/admin/groups/{g['id']}/members", {"add": [people["admin2"]["email"]]})   # admin은 된다
    assert code == 200
    code, body = as_user(op, "PUT", f"/api/admin/groups/{g['id']}/members", {"remove": [people["admin2"]["id"]]})
    assert code == 403


def test_group_errors(project, groups):
    g = new_group(groups)
    code, body = api("POST", "/api/admin/groups", {"name": "  " + g["name"] + " "})
    assert (code, body["detail"]) == (409, "group_exists")
    other = new_group(groups)
    code, body = api("PATCH", f"/api/admin/groups/{other['id']}", {"name": g["name"]})
    assert (code, body["detail"]) == (409, "group_exists")
    for bad in (str(uuid.uuid4()), "not-a-uuid"):
        code, body = api("GET", f"/api/admin/groups/{bad}")
        assert (code, body["detail"]) == (404, "unknown_group")
        code, body = api("PUT", f"/api/admin/groups/{bad}/tabs", {"project": project.name, "tab_keys": []})
        assert code == 404
    code, body = api("PUT", f"/api/admin/groups/{g['id']}/tabs", {"project": project.name, "tab_keys": ["no_such_tab"]})
    assert (code, body["detail"]) == (422, "unknown_tab")
    code, body = api("PUT", f"/api/admin/groups/{g['id']}/tabs", {"project": "zz-gone", "tab_keys": ["x"]})
    assert (code, body["detail"]) == (422, "unknown_tab")
    code, _ = api("PUT", f"/api/admin/groups/{g['id']}/tabs", {"project": "zz-gone", "tab_keys": []})
    assert code == 200
    code, body = api("POST", "/api/admin/groups", {"name": " "})
    assert (code, body["detail"]) == (422, "invalid_request")
    code, r = api("PUT", f"/api/admin/groups/{g['id']}/members", {"add": ["nobody-x@example.com", "not-an-id"], "remove": [str(uuid.uuid4())]})
    assert code == 200 and len(r["not_found"]) == 3
    code, body = api("GET", "/api/admin/groups?sort=password_hash")
    assert (code, body["detail"]) == (422, "invalid_request")
    code, _, _ = http_full("POST", f"{WEB_URL}/api/admin/groups", raw=b"name=x",
                           headers={"Content-Type": "application/x-www-form-urlencoded",
                                    "Cookie": f"dashkit_session={admin_cookie()}"})
    assert code == 415


def test_list_sort_and_counts(project, people, groups):
    sup_key, wid_key = [t["key"] for t in project.dashboard["tabs"]][:2]
    small = new_group(groups, is_default=True)
    big = new_group(groups)
    api("PUT", f"/api/admin/groups/{big['id']}/tabs", {"project": project.name, "tab_keys": [sup_key, wid_key]})
    api("PUT", f"/api/admin/groups/{big['id']}/members", {"add": [people["user"]["email"], people["admin2"]["email"]]})
    code, body = api("GET", "/api/admin/groups?sort=-member_count&size=200")
    assert code == 200 and body["total"] >= 2
    mine = [g for g in body["items"] if g["id"] in (small["id"], big["id"])]
    assert [g["id"] for g in mine] == [big["id"], small["id"]]
    assert mine[0]["member_count"] == 2 and mine[0]["tab_count"] == 2 and mine[1]["is_default"] is True
    code, body = api("GET", "/api/admin/groups?size=1&page=1")
    assert len(body["items"]) == 1


def test_missing_and_other_project_tabs_are_shown_but_ignored(project, people, groups):
    sup_key = project.dashboard["tabs"][0]["key"]
    g = new_group(groups)
    api("PUT", f"/api/admin/groups/{g['id']}/members", {"add": [people["user"]["email"]]})
    api("PUT", f"/api/admin/groups/{g['id']}/tabs", {"project": project.name, "tab_keys": [sup_key]})
    # 설정에서 없어진 탭과 다른 프로젝트의 탭(지금 설정 파일을 고치는 대신 DB에 직접 넣는다)
    with db.auth_conn(autocommit=True) as conn:
        conn.execute("INSERT INTO dashkit_auth.group_tabs (group_id, project, tab_key) VALUES (%s, %s, 'gone_tab'), (%s, 'zz-other', 'x')",
                     (g["id"], project.name, g["id"]))
    code, detail = api("GET", f"/api/admin/groups/{g['id']}")
    entries = {(t["project"], t["tab_key"]): t for t in detail["tabs"]}
    assert entries[(project.name, sup_key)]["exists"] is True and entries[(project.name, sup_key)]["current"] is True
    assert entries[(project.name, "gone_tab")]["exists"] is False
    assert entries[("zz-other", "x")]["current"] is False and entries[("zz-other", "x")]["exists"] is False
    code, t = api("GET", "/api/admin/tabs")
    assert [x["key"] for x in t["tabs"]] == [x["key"] for x in project.dashboard["tabs"]] and t["project"] == project.name
    refs = {(o["project"], o["tab_key"]) for o in t["others"]}
    assert {(project.name, "gone_tab"), ("zz-other", "x")} <= refs and (project.name, sup_key) not in refs
    code, app = as_user(people["user"], "GET", "/api/app")
    assert [x["key"] for x in app["tabs"]] == [sup_key]                  # 없는 탭은 판정에서 무시
    # 허용 탭을 다시 저장해도 없는 탭 기록은 남는다(설정에 돌아오면 다시 쓰인다)
    api("PUT", f"/api/admin/groups/{g['id']}/tabs", {"project": project.name, "tab_keys": []})
    api("PUT", f"/api/admin/groups/{g['id']}/tabs", {"project": "zz-other", "tab_keys": []})
    refs = lambda: {(x["project"], x["tab_key"]) for x in api("GET", f"/api/admin/groups/{g['id']}")[1]["tabs"]}
    assert refs() == {(project.name, "gone_tab"), ("zz-other", "x")}
    # 없는 탭은 하나씩 뺀다: 같은 프로젝트의 다른 없는 탭은 그대로
    with db.auth_conn(autocommit=True) as conn:
        conn.execute("INSERT INTO dashkit_auth.group_tabs (group_id, project, tab_key) VALUES (%s, %s, 'gone_two')",
                     (g["id"], project.name))
    code, detail = api("DELETE", f"/api/admin/groups/{g['id']}/tabs/{project.name}/gone_tab")
    assert code == 200 and refs() == {(project.name, "gone_two"), ("zz-other", "x")}
    assert api("DELETE", f"/api/admin/groups/{g['id']}/tabs/zz-other/x")[0] == 200
    assert api("DELETE", f"/api/admin/groups/{g['id']}/tabs/zz-other/x")[0] == 200       # 없는 기록: 아무것도 안 함
    assert refs() == {(project.name, "gone_two")}
    code, body = as_user(people["operator"], "DELETE", f"/api/admin/groups/{g['id']}/tabs/{project.name}/gone_two")
    assert code == 200 and refs() == set()
    code, body = as_user(people["user"], "DELETE", f"/api/admin/groups/{g['id']}/tabs/{project.name}/{sup_key}")
    assert (code, body["detail"]) == (403, "forbidden")


def test_tab_replacement_locks_the_group_row(project, groups):
    """허용 탭 저장은 부모 groups 행을 먼저 잠근다(기존 탭이 없는 그룹도 동시 저장이 직렬화된다)."""
    import threading

    from dashkit.auth import store

    g = new_group(groups)
    key = project.dashboard["tabs"][0]["key"]
    valid = {t["key"] for t in project.dashboard["tabs"]}
    done = threading.Event()
    with db.auth_conn() as conn:                                        # 트랜잭션 안에서 그룹 행을 잡고 있는다
        conn.execute("SELECT 1 FROM dashkit_auth.groups WHERE id = %s FOR UPDATE", (g["id"],))
        t = threading.Thread(target=lambda: (store.set_group_tabs(g["id"], project.name, [key], valid), done.set()))
        t.start()
        assert not done.wait(1.5)                                       # 잠금이 풀릴 때까지 기다린다
        conn.rollback()
    assert done.wait(10)
    t.join()
    assert [x["tab_key"] for x in api("GET", f"/api/admin/groups/{g['id']}")[1]["tabs"]] == [key]


def test_group_changes_are_audited(project, people, groups):
    g = new_group(groups, description="설명")
    gid = g["id"]
    api("PATCH", f"/api/admin/groups/{gid}", {"description": "바꾼 설명", "is_default": True})
    api("PUT", f"/api/admin/groups/{gid}/tabs", {"project": project.name, "tab_keys": [project.dashboard["tabs"][0]["key"]]})
    api("PUT", f"/api/admin/groups/{gid}/members", {"add": [people["user"]["email"]]})
    assert api("DELETE", f"/api/admin/groups/{gid}")[0] == 204
    groups.remove(gid)
    rows = auth_rows("SELECT action, actor_email, target_label, detail FROM dashkit_auth.audit_log WHERE target_id = %s ORDER BY id", (gid,))
    assert [r[0] for r in rows] == ["group.create", "group.update", "group.tabs", "group.members", "group.delete"]
    assert all(r[1].startswith("test-admin-") and r[2] == g["name"] for r in rows)
    assert rows[1][3] == {"description": {"from": "설명", "to": "바꾼 설명"}, "is_default": {"from": False, "to": True}}
    assert rows[3][3]["added"] == [people["user"]["email"]]
    # 계정은 남는다
    assert auth_rows("SELECT 1 FROM dashkit_auth.users WHERE id = %s", (people["user"]["id"],))
