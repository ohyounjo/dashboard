"""사용자 운용 API와 내 정보(통합). 계약: contracts/auth.md "운용 API — 사용자", "내 정보".

설계서 13절 4단계 완료 조건: 3절의 권한 표가 테스트로 고정된다(operator가 admin을 건드리지 못함, 마지막 admin 보호).
"""
from __future__ import annotations

import re
import secrets

import pytest

from dashkit import db

from conftest import (SESSION_COOKIE, WEB_URL, admin_cookie, api, auth_post, delete_user, http_full, login, make_user,
                      random_email)

pytestmark = pytest.mark.integration

PW = "a-good-password-1"


def sql(query, params=()):
    with db.auth_conn(autocommit=True) as conn:
        cur = conn.execute(query, params)
        return cur.fetchall() if cur.description else None


def app_status(cookie):
    return http_full("GET", f"{WEB_URL}/api/app", headers={"Cookie": f"{SESSION_COOKIE}={cookie}"})[0]


def outbox(email):
    code, body = api("GET", "/api/admin/outbox?size=200")
    return [m for m in body["items"] if m["to_email"] == email]


def token_of(mail):
    return re.search(r"#token=([A-Za-z0-9_-]+)$", mail["link"]).group(1)


@pytest.fixture
def accounts():
    """역할별 계정 만들기 도우미. 끝나면 모두 지운다. make(role, status) → {id, email, cookie?}"""
    made = []

    def make(role="user", status="active", password=PW, log_in=True):
        email = random_email(f"u-{role}")
        user = make_user(email, password if status != "invited" else None, role=role, status=status)
        made.append(user.id)
        out = {"id": user.id, "email": email}
        if log_in and status == "active":
            out["cookie"] = login(email, password)
        return out

    yield make
    for uid in made:
        if sql("SELECT 1 FROM dashkit_auth.users WHERE id = %s", (uid,)):
            delete_user(uid)


def as_(person, method, path, body=None):
    return api(method, path, body, cookie=person["cookie"])


# ---------------------------------------------------------------- 3절 권한 표

def test_operator_cannot_touch_operators_or_admins(accounts):
    op = accounts("operator")
    for target in (accounts("admin", log_in=False), accounts("operator", log_in=False)):
        path = f"/api/admin/users/{target['id']}"
        for method, sub, body in (("PATCH", "", {"status": "suspended"}), ("PATCH", "", {"group_ids": []}),
                                  ("DELETE", "", None), ("POST", "/reset-password", {"method": "temporary"}),
                                  ("POST", "/reset-password", {"method": "mail"}), ("POST", "/unlock", {}),
                                  ("POST", "/logout-all", {})):
            code, resp = as_(op, method, path + sub, body)
            assert (code, resp["detail"]) == (403, "forbidden"), (method, sub)
        code, _ = as_(op, "GET", path)                                     # 보기는 된다
        assert code == 200
    (n,), = sql("SELECT count(*) FROM dashkit_auth.users WHERE role IN ('admin','operator') AND status = 'suspended' AND email LIKE 'u-%%'")
    assert n == 0                                                          # 아무것도 바뀌지 않았다
    # 등급 변경과 user 아닌 등급의 초대는 admin만
    user = accounts("user", log_in=False)
    code, resp = as_(op, "PATCH", f"/api/admin/users/{user['id']}", {"role": "operator"})
    assert (code, resp["detail"]) == (403, "forbidden")
    for role in ("operator", "admin"):
        code, resp = as_(op, "POST", "/api/admin/users", {"email": random_email("x"), "role": role})
        assert (code, resp["detail"]) == (403, "forbidden")
    code, resp = as_(op, "POST", "/api/admin/users/bulk-groups", {"user_ids": [user["id"], accounts("admin", log_in=False)["id"]], "add": []})
    assert (code, resp["detail"]) == (403, "forbidden")


def test_operator_can_manage_users(accounts):
    op = accounts("operator")
    user = accounts("user")
    path = f"/api/admin/users/{user['id']}"
    assert as_(op, "PATCH", path, {"status": "suspended"})[0] == 200
    assert app_status(user["cookie"]) == 401                               # 정지하면 세션이 끊긴다
    code, body, _ = auth_post("/api/auth/login", {"email": user["email"], "password": PW})
    assert (code, body["detail"]) == (403, "account_suspended")
    assert as_(op, "PATCH", path, {"status": "active"})[0] == 200
    assert login(user["email"], PW)                                        # 해제하면 비밀번호 그대로
    code, r = as_(op, "POST", f"{path}/reset-password", {"method": "temporary"})
    assert code == 200 and len(r["password"]) == 16
    assert as_(op, "DELETE", path)[0] == 204


def test_admin_manages_operators_and_roles_kill_sessions_and_tokens(accounts):
    target = accounts("user")
    # user일 때 받은 재설정 링크(operator도 메일함에서 볼 수 있었다)
    auth_post("/api/auth/password/forgot", {"email": target["email"]})
    old_link = outbox(target["email"])[0]
    code, d = api("PATCH", f"/api/admin/users/{target['id']}", {"role": "operator"})
    assert code == 200 and d["role"] == "operator"
    assert app_status(target["cookie"]) == 401                             # 등급 변경 때 세션을 지운다
    code, body, _ = auth_post("/api/auth/password/reset", {"token": token_of(old_link), "password": "new-good-password-9"})
    assert (code, body["detail"]) == (410, "token_expired")               # 이전에 발급된 토큰 거절
    op_cookie = login(target["email"], PW)
    assert app_status(op_cookie) == 200
    code, d = api("PATCH", f"/api/admin/users/{target['id']}", {"role": "admin"})
    assert d["role"] == "admin"
    code, body = api("PATCH", f"/api/admin/users/{target['id']}", {"role": "boss"})
    assert code in (409, 422)
    rows = sql("SELECT detail FROM dashkit_auth.audit_log WHERE action = 'user.role' AND target_id = %s ORDER BY id", (target["id"],))
    assert [r[0] for r in rows] == [{"from": "user", "to": "operator"}, {"from": "operator", "to": "admin"}]


def test_self_and_last_admin_are_protected(accounts):
    a, b = accounts("admin"), accounts("admin")
    attempts = (("PATCH", {"status": "suspended"}), ("PATCH", {"role": "operator"}), ("DELETE", None))
    for method, body in attempts:                                          # 활성 admin이 여럿이면 self_target
        code, resp = as_(a, method, f"/api/admin/users/{a['id']}", body)
        assert (code, resp["detail"]) == (409, "self_target"), (method, body)
    # 다른 admin은 서로 정지·해제할 수 있다
    assert as_(a, "PATCH", f"/api/admin/users/{b['id']}", {"status": "suspended"})[0] == 200
    assert as_(a, "PATCH", f"/api/admin/users/{b['id']}", {"status": "active"})[0] == 200
    # a만 활성 admin으로 남긴다(다른 활성 admin은 DB에서 잠시 정지 → 끝나면 되돌린다)
    others = [str(r[0]) for r in sql("SELECT id FROM dashkit_auth.users WHERE role = 'admin' AND status = 'active' AND id <> %s", (a["id"],))]
    try:
        sql("UPDATE dashkit_auth.users SET status = 'suspended' WHERE id = ANY(%s::uuid[])", (others,))
        for method, body in attempts:
            code, resp = as_(a, method, f"/api/admin/users/{a['id']}", body)
            assert (code, resp["detail"]) == (409, "last_admin"), (method, body)
        (role, status), = sql("SELECT role, status FROM dashkit_auth.users WHERE id = %s", (a["id"],))
        assert (role, status) == ("admin", "active")
        # 그룹 바꾸기처럼 admin을 없애지 않는 변경은 된다
        assert as_(a, "PATCH", f"/api/admin/users/{a['id']}", {"group_ids": []})[0] == 200
    finally:
        sql("UPDATE dashkit_auth.users SET status = 'active' WHERE id = ANY(%s::uuid[])", (others,))


def test_last_admin_check_is_atomic(accounts):
    """두 admin이 동시에 서로를 정지해도 활성 admin이 0명이 되지 않는다(활성 admin 행을 잠그고 다시 본다)."""
    import threading

    from dashkit.auth import store

    a, b = accounts("admin", log_in=False), accounts("admin", log_in=False)
    others = [str(r[0]) for r in sql("SELECT id FROM dashkit_auth.users WHERE role = 'admin' AND status = 'active' AND id NOT IN (%s, %s)", (a["id"], b["id"]))]
    results: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def suspend(name, target):
        barrier.wait()
        try:
            store.set_status(target, "suspended", guard_admin=True)
            results[name] = "ok"
        except store.LastAdmin:
            results[name] = "last_admin"

    try:
        sql("UPDATE dashkit_auth.users SET status = 'suspended' WHERE id = ANY(%s::uuid[])", (others,))
        threads = [threading.Thread(target=suspend, args=("a→b", b["id"])), threading.Thread(target=suspend, args=("b→a", a["id"]))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results.values()) == ["last_admin", "ok"]
        (n,), = sql("SELECT count(*) FROM dashkit_auth.users WHERE role = 'admin' AND status = 'active' AND id IN (%s, %s)", (a["id"], b["id"]))
        assert n == 1
    finally:
        sql("UPDATE dashkit_auth.users SET status = 'active' WHERE id = ANY(%s::uuid[])", (others + [a["id"], b["id"]],))


def test_invite_is_created_in_one_transaction():
    """초대 계정·그룹·토큰은 한 트랜잭션이다. 같은 이메일이 있으면 아무것도 만들지 않는다."""
    from dashkit.auth import sessions, store

    email = random_email("inv-atomic")
    code, u = api("POST", "/api/admin/users", {"email": email})
    try:
        (n_tokens,), = sql("SELECT count(*) FROM dashkit_auth.tokens WHERE user_id = %s AND purpose = 'invite'", (u["id"],))
        assert n_tokens == 1
        with pytest.raises(store.Conflict):
            store.create_invited_user(email, "user", u["id"], [], sessions.token_hash(sessions.new_token()), store.now())
        (n_tokens,), = sql("SELECT count(*) FROM dashkit_auth.tokens WHERE user_id = %s", (u["id"],))
        assert n_tokens == 1
    finally:
        delete_user(u["id"])


# ---------------------------------------------------------------- 초대·일괄 초대

def test_invite_and_accept(accounts):
    code, g = api("POST", "/api/admin/groups", {"name": f"inv-{secrets.token_hex(3)}"})
    code2, dg = api("POST", "/api/admin/groups", {"name": f"inv-default-{secrets.token_hex(3)}", "is_default": True})
    try:
        email = random_email("invitee")
        code, u = api("POST", "/api/admin/users", {"email": email.upper(), "role": "user", "group_ids": [g["id"]]})
        assert code == 201 and u["status"] == "invited" and u["source"] == "invite" and u["email"] == email
        assert u["created_by_email"].startswith("test-admin-") and [x["id"] for x in u["groups"]] == [g["id"]]
        code, body = api("POST", "/api/admin/users", {"email": email})
        assert (code, body["detail"]) == (409, "user_exists")
        code, body = api("POST", "/api/admin/users", {"email": "x@not-allowed.example"})
        assert (code, body["detail"]) == (422, "email_domain_not_allowed")
        code, body = api("POST", "/api/admin/users", {"email": random_email("x"), "group_ids": ["00000000-0000-4000-8000-00000000dead"]})
        assert (code, body["detail"]) == (404, "unknown_group")
        first = outbox(email)[0]
        assert first["kind"] == "invite" and "/reset.html#token=" in first["link"]
        assert api("POST", f"/api/admin/users/{u['id']}/resend-invite", {})[0] == 204
        second = outbox(email)[0]
        assert auth_post("/api/auth/password/reset", {"token": token_of(first), "password": PW})[0] == 410
        assert auth_post("/api/auth/password/reset", {"token": token_of(second), "password": PW})[0] == 204
        code, d = api("GET", f"/api/admin/users/{u['id']}")
        assert d["status"] == "active" and {x["id"] for x in d["groups"]} == {g["id"], dg["id"]}   # 기본 그룹도
        assert api("POST", f"/api/admin/users/{u['id']}/resend-invite", {})[0] == 409
        assert login(email, PW)
        api("DELETE", f"/api/admin/users/{u['id']}")
    finally:
        api("DELETE", f"/api/admin/groups/{g['id']}")
        api("DELETE", f"/api/admin/groups/{dg['id']}")


def test_bulk_invite():
    existing = make_user(random_email("bulk-exists"), PW)
    new1, new2 = random_email("bulk-a"), random_email("bulk-b")
    try:
        code, r = api("POST", "/api/admin/users/bulk-invite",
                      {"emails": [new1, new1.upper(), existing.email, "not-an-email", "x@not-allowed.example", new2]})
        assert code == 200
        assert [(x["email"], x["result"]) for x in r["results"]] == [
            (new1, "created"), (existing.email, "exists"), ("not-an-email", "invalid_email"),
            ("x@not-allowed.example", "email_domain_not_allowed"), (new2, "created")]
        for e in (new1, new2):
            (role, status), = sql("SELECT role, status FROM dashkit_auth.users WHERE email = %s", (e,))
            assert (role, status) == ("user", "invited") and outbox(e)[0]["kind"] == "invite"
        code, body = api("POST", "/api/admin/users/bulk-invite", {"emails": [f"b{i}@example.com" for i in range(201)]})
        assert (code, body["detail"]) == (422, "too_many_emails")
    finally:
        delete_user(existing.id)
        for e in (new1, new2):
            rows = sql("SELECT id FROM dashkit_auth.users WHERE email = %s", (e,))
            if rows:
                delete_user(str(rows[0][0]))


# ---------------------------------------------------------------- 리셋 두 방식, 강제 변경, 내 정보

def test_temporary_password_forces_change(accounts):
    user = accounts("user")
    other_session = login(user["email"], PW)
    code, r = api("POST", f"/api/admin/users/{user['id']}/reset-password", {"method": "temporary"})
    temp = r["password"]
    assert code == 200 and len(temp) == 16
    assert app_status(user["cookie"]) == 401 and app_status(other_session) == 401
    text = repr(sql("SELECT detail FROM dashkit_auth.audit_log WHERE target_id = %s", (user["id"],)))
    assert temp not in text and "temporary" in text                         # 임시 비밀번호는 감사 로그에 없다

    code, body, _ = auth_post("/api/auth/login", {"email": user["email"], "password": temp})
    assert code == 200 and body["must_change_password"] is True
    cookie = login(user["email"], temp)
    s2 = login(user["email"], temp)
    assert app_status(cookie) == 403                                         # password_change_required
    code, me = api("GET", "/api/me", cookie=cookie)
    assert code == 200 and me["must_change_password"] is True and me["email"] == user["email"]
    code, body = api("POST", "/api/me/password", {"current": "wrong-one-123", "new": "brand-new-pass-7"}, cookie=cookie)
    assert (code, body["detail"]) == (401, "invalid_credentials")
    code, body = api("POST", "/api/me/password", {"current": temp, "new": temp}, cookie=cookie)
    assert (code, body["detail"], body["reason"]) == (422, "weak_password", "same_as_current")
    code, body = api("POST", "/api/me/password", {"current": temp, "new": "1234567890"}, cookie=cookie)
    assert (code, body["reason"]) == (422, "too_common")
    assert api("POST", "/api/me/password", {"current": temp, "new": "brand-new-pass-7"}, cookie=cookie)[0] == 204
    assert app_status(cookie) == 200                                         # 지금 세션은 남는다
    assert app_status(s2) == 401                                             # 나머지 세션은 지운다
    assert login(user["email"], "brand-new-pass-7")


def test_reset_mail_unlock_logout_all(accounts):
    user = accounts("user")
    assert api("POST", f"/api/admin/users/{user['id']}/reset-password", {"method": "mail"})[0] == 204
    assert app_status(user["cookie"]) == 401
    assert outbox(user["email"])[0]["kind"] == "reset_password"
    code, body = api("POST", f"/api/admin/users/{user['id']}/reset-password", {"method": "sms"})
    assert code == 422
    # 잠금 해제
    for _ in range(5):
        auth_post("/api/auth/login", {"email": user["email"], "password": "wrong-password-x"})
    code, d = api("GET", f"/api/admin/users/{user['id']}")
    assert d["locked"] is True and d["locked_until"]
    assert api("POST", f"/api/admin/users/{user['id']}/unlock", {})[0] == 204
    s1 = login(user["email"], PW)
    s2 = login(user["email"], PW)
    assert api("POST", f"/api/admin/users/{user['id']}/logout-all", {})[0] == 204
    assert app_status(s1) == 401 and app_status(s2) == 401
    actions = [r[0] for r in sql("SELECT action FROM dashkit_auth.audit_log WHERE target_id = %s AND action LIKE 'user.%%' ORDER BY id", (user["id"],))]
    assert actions == ["user.reset_password", "user.unlock", "user.logout_all"]


def test_invited_and_pending_cannot_be_suspended_or_get_reset(accounts):
    inv = accounts("user", status="invited", log_in=False)
    code, body = api("PATCH", f"/api/admin/users/{inv['id']}", {"status": "suspended"})
    assert (code, body["detail"]) == (409, "invalid_status")
    for method in ("mail", "temporary"):
        code, body = api("POST", f"/api/admin/users/{inv['id']}/reset-password", {"method": method})
        assert (code, body["detail"]) == (409, "invalid_status")
    code, body = api("PATCH", f"/api/admin/users/{inv['id']}", {"status": "pending"})
    assert code == 409


def test_me():
    code, me = api("GET", "/api/me")
    assert code == 200 and me["role"] == "admin" and set(me) == {"email", "role", "created_at", "last_login_at", "groups", "must_change_password"}
    code, body = api("GET", "/api/me", auth=False)
    assert (code, body["detail"]) == (401, "unauthenticated")


# ---------------------------------------------------------------- 목록·상세·삭제·일괄 그룹

def test_list_detail_and_sorting(accounts):
    u = accounts("user")
    code, body = api("GET", "/api/admin/users?size=200&sort=-login_count")
    assert code == 200 and body["total"] >= 2
    counts = [x["login_count"] for x in body["items"]]
    assert counts == sorted(counts, reverse=True)
    item = next(x for x in body["items"] if x["id"] == u["id"])
    assert set(item) == {"id", "email", "role", "status", "locked", "groups", "created_at", "login_count", "last_login_at", "source"}
    assert item["login_count"] == 1 and item["last_login_at"]
    code, d = api("GET", f"/api/admin/users/{u['id']}")
    assert {"created_by_email", "password_changed_at", "failed_count", "locked_until", "must_change_password"} <= set(d)
    assert "password_hash" not in d
    for bad in ("00000000-0000-4000-8000-00000000beef", "nope"):
        assert api("GET", f"/api/admin/users/{bad}")[1]["detail"] == "unknown_user"
    code, body = as_(u, "GET", "/api/admin/users")
    assert (code, body["detail"]) == (403, "forbidden")


def test_delete_user(accounts):
    u = accounts("user")
    auth_post("/api/auth/password/forgot", {"email": u["email"]})
    assert api("DELETE", f"/api/admin/users/{u['id']}")[0] == 204
    assert not sql("SELECT 1 FROM dashkit_auth.users WHERE id = %s", (u["id"],))
    assert outbox(u["email"]) == [] and app_status(u["cookie"]) == 401
    (detail, label), = sql("SELECT detail, target_label FROM dashkit_auth.audit_log WHERE action = 'user.delete' AND target_id = %s", (u["id"],))
    assert label == u["email"]
    # 같은 이메일로 다시 가입할 수 있다(새 계정)
    assert auth_post("/api/auth/signup", {"email": u["email"], "password": PW})[0] == 202
    rows = sql("SELECT id, status FROM dashkit_auth.users WHERE email = %s", (u["email"],))
    assert rows and str(rows[0][0]) != u["id"] and rows[0][1] == "pending"
    delete_user(str(rows[0][0]))


def test_bulk_groups(accounts):
    a, b = accounts("user", log_in=False), accounts("user", log_in=False)
    code, g = api("POST", "/api/admin/groups", {"name": f"bulk-{secrets.token_hex(3)}"})
    try:
        assert api("POST", "/api/admin/users/bulk-groups", {"user_ids": [a["id"], b["id"]], "add": [g["id"]]})[0] == 204
        code, d = api("GET", f"/api/admin/groups/{g['id']}")
        assert {m["id"] for m in d["members"]} == {a["id"], b["id"]}
        assert api("POST", "/api/admin/users/bulk-groups", {"user_ids": [a["id"]], "remove": [g["id"]]})[0] == 204
        code, d = api("GET", f"/api/admin/groups/{g['id']}")
        assert {m["id"] for m in d["members"]} == {b["id"]}
        code, body = api("POST", "/api/admin/users/bulk-groups", {"user_ids": [a["id"], "00000000-0000-4000-8000-00000000beef"], "add": [g["id"]]})
        assert (code, body["detail"]) == (404, "unknown_user")
        code, d = api("PATCH", f"/api/admin/users/{a['id']}", {"group_ids": [g["id"]]})
        assert [x["id"] for x in d["groups"]] == [g["id"]]
        code, d = api("PATCH", f"/api/admin/users/{a['id']}", {"group_ids": []})
        assert d["groups"] == []
    finally:
        api("DELETE", f"/api/admin/groups/{g['id']}")


def test_admin_cookie_still_valid():
    """이 파일의 테스트가 테스트용 admin 세션을 끊지 않았다(다음 파일이 그대로 쓴다)."""
    assert app_status(admin_cookie()) == 200
