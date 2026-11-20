"""접속 통계, 감사 로그, 보존 기간(통합). 계약: contracts/auth.md "접속 통계", "감사 로그", "보존 기간".

설계서 13절 5단계 완료 조건: 통계 화면에 탭별·사용자별 수치가 나오고 9-2의 모든 동작이 감사 로그에 남는다.
"""
from __future__ import annotations

import re
import secrets
from datetime import date, timedelta

import pytest

from dashkit import db
from dashkit.auth import store
from dashkit.config import load_project

from conftest import PROJECTS, api, auth_post, delete_user, live_project, login, make_user, random_email

pytestmark = pytest.mark.integration

PW = "a-good-password-1"


def sql(query, params=()):
    with db.auth_conn(autocommit=True) as conn:
        cur = conn.execute(query, params)
        return cur.fetchall() if cur.description else None


@pytest.fixture(scope="module")
def project():
    return load_project(live_project(), PROJECTS)


@pytest.fixture
def viewer(project):
    """첫 두 탭을 볼 수 있는 user 계정(그룹 하나). (계정, 쿠키, 그룹 id)"""
    keys = [t["key"] for t in project.dashboard["tabs"]]
    email = random_email("viewer")
    user = make_user(email, PW)
    code, g = api("POST", "/api/admin/groups", {"name": f"stats-{secrets.token_hex(3)}"})
    api("PUT", f"/api/admin/groups/{g['id']}/tabs", {"project": project.name, "tab_keys": keys[:2]})
    api("PUT", f"/api/admin/groups/{g['id']}/members", {"add": [email]})
    yield user, login(email, PW), g["id"]
    api("DELETE", f"/api/admin/groups/{g['id']}")
    if sql("SELECT 1 FROM dashkit_auth.users WHERE id = %s", (user.id,)):
        delete_user(user.id)


def views_of(user_id):
    return sql("SELECT tab_key FROM dashkit_auth.view_events WHERE user_id = %s ORDER BY id", (user_id,))


# ---------------------------------------------------------------- 조회 기록

def test_views_are_recorded_once_per_half_hour_for_allowed_tabs(project, viewer):
    user, cookie, _ = viewer
    keys = [t["key"] for t in project.dashboard["tabs"]]
    assert api("POST", "/api/views", {"tab": keys[0]}, cookie=cookie)[0] == 204
    assert api("POST", "/api/views", {"tab": keys[0]}, cookie=cookie)[0] == 204       # 30분 안: 세지 않는다
    assert api("POST", "/api/views", {"tab": keys[1]}, cookie=cookie)[0] == 204
    assert [r[0] for r in views_of(user.id)] == [keys[0], keys[1]]
    code, body = api("POST", "/api/views", {"tab": keys[-1]}, cookie=cookie)            # 볼 수 없는 탭
    assert (code, body["detail"]) == (403, "forbidden")
    code, body = api("POST", "/api/views", {"tab": "no_such_tab"}, cookie=cookie)
    assert code == 403
    assert api("POST", "/api/views", {"tab": keys[0]}, auth=False)[0] == 401
    sql("UPDATE dashkit_auth.view_events SET viewed_at = viewed_at - interval '31 minutes' WHERE user_id = %s", (user.id,))
    api("POST", "/api/views", {"tab": keys[0]}, cookie=cookie)
    assert [r[0] for r in views_of(user.id)] == [keys[0], keys[1], keys[0]]


def test_concurrent_views_count_once(project, viewer):
    """같은 사용자·탭의 동시 요청도 30분에 한 번만 센다(사용자·탭별 트랜잭션 잠금)."""
    import threading

    user, cookie, _ = viewer
    key = project.dashboard["tabs"][1]["key"]
    threads = [threading.Thread(target=api, args=("POST", "/api/views", {"tab": key}), kwargs={"cookie": cookie}) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert [r[0] for r in views_of(user.id)] == [key]


def test_stats_by_tab_and_user(project, viewer):
    user, cookie, _ = viewer
    keys = [t["key"] for t in project.dashboard["tabs"]]
    api("POST", "/api/views", {"tab": keys[0]}, cookie=cookie)
    api("POST", "/api/views", {"tab": keys[0]})                                          # admin도 센다
    # 40일 전 기록 하나(30일 기간에는 빠지고 90일에는 들어간다)
    sql("INSERT INTO dashkit_auth.view_events (user_id, project, tab_key, viewed_at) VALUES (%s, %s, %s, now() - interval '40 days')",
        (user.id, project.name, keys[1]))

    code, body = api("GET", "/api/admin/stats/tabs?days=30&size=200")
    assert code == 200 and body["project"] == project.name and body["days"] == 30
    by = {t["key"]: t for t in body["items"]}
    assert [t["key"] for t in body["items"]] == keys                                     # 설정 순서
    assert by[keys[0]]["views"] >= 2 and by[keys[0]]["viewers"] >= 2 and by[keys[0]]["last_viewed_at"]
    before = by[keys[1]]["views"]
    code, body90 = api("GET", "/api/admin/stats/tabs?days=90&size=200")
    assert {t["key"]: t for t in body90["items"]}[keys[1]]["views"] == before + 1
    never = [t for t in body["items"] if t["views"] == 0]
    assert all(t["never"] for t in never) and all(not t["never"] for t in body["items"] if t["views"])
    code, body = api("GET", "/api/admin/stats/tabs?days=14")
    assert (code, body["detail"]) == (422, "invalid_request")

    code, body = api("GET", "/api/admin/stats/users?days=30&size=200")
    me = next(u for u in body["items"] if u["id"] == user.id)
    assert me["views"] == 1 and me["inactive"] is False and me["email"] == user.email
    views = [u["views"] for u in body["items"]]
    assert views == sorted(views, reverse=True)                                          # 기본 정렬: 조회 수
    sql("UPDATE dashkit_auth.users SET last_login_at = now() - interval '31 days' WHERE id = %s", (user.id,))
    code, body = api("GET", "/api/admin/stats/users?days=30&size=200")
    assert next(u for u in body["items"] if u["id"] == user.id)["inactive"] is True
    code, body = api("GET", "/api/admin/stats/users", cookie=cookie)
    assert (code, body["detail"]) == (403, "forbidden")


def test_deleted_user_views_stay_in_tab_counts(project, viewer):
    user, cookie, _ = viewer
    key = project.dashboard["tabs"][0]["key"]
    api("POST", "/api/views", {"tab": key}, cookie=cookie)
    before = {t["key"]: t["views"] for t in api("GET", "/api/admin/stats/tabs?days=7")[1]["items"]}[key]
    assert api("DELETE", f"/api/admin/users/{user.id}")[0] == 204
    after = {t["key"]: t["views"] for t in api("GET", "/api/admin/stats/tabs?days=7")[1]["items"]}[key]
    assert after == before
    assert sql("SELECT count(*) FROM dashkit_auth.view_events WHERE user_id IS NULL AND tab_key = %s", (key,))[0][0] >= 1


# ---------------------------------------------------------------- 감사 로그

def test_every_audited_action_is_recorded(project):
    """9-2의 동작을 하나씩 해 보고 그 기록이 모두 남는지 본다."""
    tag = secrets.token_hex(3)
    admin_email = random_email(f"aud-{tag}-admin")
    admin = make_user(admin_email, PW, role="admin")
    cookie = login(admin_email, PW)

    def as_admin(method, path, body=None):
        code, resp = api(method, path, body, cookie=cookie)
        assert code < 400, (method, path, code, resp)
        return resp

    def mail_token(email):
        code, body = api("GET", "/api/admin/outbox?size=200")
        link = next(m["link"] for m in body["items"] if m["to_email"] == email and m["link"])
        return re.search(r"#token=([A-Za-z0-9_-]+)$", link).group(1)

    created: list[str] = [admin.id]
    try:
        # 로그인 실패·잠금, 로그아웃
        locked = random_email(f"aud-{tag}-locked")
        created.append(make_user(locked, PW).id)
        for _ in range(5):
            auth_post("/api/auth/login", {"email": locked, "password": "wrong-password-x"})
        s = login(admin_email, PW)
        api("POST", "/api/auth/logout", {}, cookie=s)
        # 가입, 메일 인증, 비밀번호 찾기·재설정, 비밀번호 변경
        signup = random_email(f"aud-{tag}-signup")
        auth_post("/api/auth/signup", {"email": signup, "password": PW})
        auth_post("/api/auth/verify", {"token": mail_token(signup)})
        auth_post("/api/auth/password/forgot", {"email": signup})
        auth_post("/api/auth/password/reset", {"token": mail_token(signup), "password": "reset-good-password-2"})
        signup_cookie = login(signup, "reset-good-password-2")
        assert api("POST", "/api/me/password", {"current": "reset-good-password-2", "new": "changed-good-password-3"}, cookie=signup_cookie)[0] == 204
        created.append(str(sql("SELECT id FROM dashkit_auth.users WHERE email = %s", (signup,))[0][0]))
        # 운용자의 계정 생성·초대·리셋·정지·해제·등급 변경·잠금 해제·삭제
        invited = as_admin("POST", "/api/admin/users", {"email": random_email(f"aud-{tag}-inv")})
        created.append(invited["id"])
        as_admin("POST", f"/api/admin/users/{invited['id']}/resend-invite", {})
        auth_post("/api/auth/password/reset", {"token": mail_token(invited["email"]), "password": PW})
        uid = invited["id"]
        as_admin("POST", f"/api/admin/users/{uid}/reset-password", {"method": "temporary"})
        as_admin("PATCH", f"/api/admin/users/{uid}", {"status": "suspended"})
        as_admin("PATCH", f"/api/admin/users/{uid}", {"status": "active"})
        as_admin("PATCH", f"/api/admin/users/{uid}", {"role": "operator"})
        as_admin("POST", f"/api/admin/users/{created[1]}/unlock", {})
        as_admin("POST", f"/api/admin/users/{uid}/logout-all", {})
        # 그룹 생성·수정·허용 탭·구성원·삭제
        g = as_admin("POST", "/api/admin/groups", {"name": f"aud-{tag}"})
        as_admin("PATCH", f"/api/admin/groups/{g['id']}", {"description": "x"})
        as_admin("PUT", f"/api/admin/groups/{g['id']}/tabs", {"project": project.name, "tab_keys": [project.dashboard["tabs"][0]["key"]]})
        as_admin("PUT", f"/api/admin/groups/{g['id']}/members", {"add": [signup]})
        as_admin("PATCH", f"/api/admin/users/{created[2]}", {"group_ids": []})
        as_admin("DELETE", f"/api/admin/groups/{g['id']}")
        as_admin("DELETE", f"/api/admin/users/{uid}")
        created.remove(uid)

        rows = sql("SELECT DISTINCT action FROM dashkit_auth.audit_log WHERE actor_email LIKE %s OR target_label LIKE %s",
                   (f"aud-{tag}-%", f"aud-{tag}%"))
        actions = {r[0] for r in rows}
        expected = {
            "auth.login", "auth.login_failed", "auth.locked", "auth.logout", "auth.signup", "auth.verify_email",
            "auth.password_reset_requested", "auth.password_reset", "auth.password_changed", "auth.invite_accepted",
            "user.create", "user.invite_resent", "user.reset_password", "user.suspend", "user.unsuspend", "user.role",
            "user.unlock", "user.logout_all", "user.delete", "user.groups",
            "group.create", "group.update", "group.tabs", "group.members", "group.delete",
        }
        assert expected <= actions, sorted(expected - actions)
        # 감사 로그 어디에도 비밀번호·토큰·해시가 없다
        text = repr(sql("SELECT detail FROM dashkit_auth.audit_log WHERE actor_email LIKE %s OR target_label LIKE %s",
                        (f"aud-{tag}-%", f"aud-{tag}%")))
        for secret in (PW, "reset-good-password-2", "changed-good-password-3", "$argon2"):
            assert secret not in text
    finally:
        for u in created:
            if sql("SELECT 1 FROM dashkit_auth.users WHERE id = %s", (u,)):
                delete_user(u)


def test_audit_api_filters_and_is_read_only():
    tag = secrets.token_hex(3)
    code, g = api("POST", "/api/admin/groups", {"name": f"auditq-{tag}"})
    api("DELETE", f"/api/admin/groups/{g['id']}")
    code, body = api("GET", "/api/admin/audit?action=group.create&size=200")
    assert code == 200 and body["items"] and {e["action"] for e in body["items"]} == {"group.create"}
    assert any(e["target_label"] == f"auditq-{tag}" for e in body["items"])
    assert "group.delete" in body["actions"]
    code, body = api("GET", "/api/admin/audit?action=group.&size=200")
    assert {e["action"] for e in body["items"]} <= {"group.create", "group.update", "group.delete", "group.tabs", "group.members"}
    ats = [e["at"] for e in body["items"]]
    assert ats == sorted(ats, reverse=True)                                              # 최근 순
    today = date.today()
    code, body = api("GET", f"/api/admin/audit?from={today - timedelta(days=1)}&to={today + timedelta(days=1)}&size=1")
    assert code == 200 and body["total"] >= 2 and len(body["items"]) == 1
    code, body = api("GET", f"/api/admin/audit?from={today + timedelta(days=2)}")
    assert body["total"] == 0
    code, body = api("GET", "/api/admin/audit?from=yesterday")
    assert (code, body["detail"]) == (422, "invalid_request")
    for method in ("DELETE", "PUT", "PATCH", "POST"):
        assert api(method, "/api/admin/audit", {})[0] == 405


# ---------------------------------------------------------------- 보존 기간

def test_retention_cleanup(project):
    user = make_user(random_email("ret"), PW)
    try:
        sql("""INSERT INTO dashkit_auth.view_events (user_id, project, tab_key, viewed_at) VALUES
               (%s, %s, 'old', now() - interval '181 days'), (%s, %s, 'new', now() - interval '179 days')""",
            (user.id, project.name, user.id, project.name))
        sql("""INSERT INTO dashkit_auth.audit_log (action, actor_email, at) VALUES
               ('test.old', %s, now() - interval '366 days'), ('test.new', %s, now() - interval '364 days')""",
            (user.email, user.email))
        removed = store.cleanup_history(180, 365)
        assert removed["view_events"] >= 1 and removed["audit_log"] >= 1
        assert [r[0] for r in views_of(user.id)] == ["new"]
        assert [r[0] for r in sql("SELECT action FROM dashkit_auth.audit_log WHERE actor_email = %s", (user.email,))] == ["test.new"]
    finally:
        delete_user(user.id)
        sql("DELETE FROM dashkit_auth.audit_log WHERE action LIKE 'test.%%'")
