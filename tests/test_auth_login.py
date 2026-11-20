"""로그인·로그아웃·세션·요청 보호(통합). 계약: contracts/auth.md. 설계서 13절 1단계 완료 조건.

- 로그인 없이 /api/app·/api/data·/api/guest-token이 401
- admin으로 로그인해 기존 웹앱(/api/app)을 그대로 본다
- 실패 응답으로 계정 존재 여부를 구분할 수 없다
"""
from __future__ import annotations

import hashlib
import os
import secrets
import statistics
import time

import pytest
from argon2 import PasswordHasher

from dashkit import config, db
from dashkit.config import load_project

from conftest import (PROJECTS, SESSION_COOKIE, WEB_URL, api, delete_user, http_full, live_project, login,
                      make_user, random_email, session_cookie)

pytestmark = pytest.mark.integration

ORIGIN = (os.environ.get("WEB_ORIGINS") or "http://localhost:8080").split(",")[0].strip()


@pytest.fixture
def account():
    """활성 user 계정 하나. (이메일, 비밀번호, id)"""
    email, password = random_email("login"), secrets.token_urlsafe(16)
    user = make_user(email, password)
    yield email, password, user.id
    delete_user(user.id)


def auth_sql(query: str, params=()):
    with db.auth_conn(autocommit=True) as conn:
        cur = conn.execute(query, params)
        return cur.fetchall() if cur.description else None


def post_login(email: str, password: str, headers=None):
    return http_full("POST", f"{WEB_URL}/api/auth/login", {"email": email, "password": password}, headers)


def with_cookie(cookie: str) -> dict:
    return {"Cookie": f"{SESSION_COOKIE}={cookie}"}


# ---------------------------------------------------------------- 로그인 필요

def test_dashboard_api_requires_login():
    project = load_project(live_project(), PROJECTS)
    dataset = sorted(config.widget_datasets(project))[0]
    dashboard = next(iter(project.dashboard["dashboards"]))
    for method, path, body in (("GET", "/api/app", None), ("GET", f"/api/data/{dataset}", None),
                               ("POST", "/api/guest-token", {"dashboard": dashboard})):
        code, resp = api(method, path, body, auth=False)
        assert (code, resp["detail"]) == (401, "unauthenticated"), path
        code, resp = api(method, path, body, cookie="x" * 43)            # 없는 세션
        assert (code, resp["detail"]) == (401, "unauthenticated"), path
    code, body = api("GET", "/api/health", auth=False)                    # healthcheck는 인증 없음
    assert code == 200 and body["status"] == "ok"


def test_admin_sees_the_whole_app():
    project = load_project(live_project(), PROJECTS)
    code, app = api("GET", "/api/app")
    assert code == 200
    assert [t["key"] for t in app["tabs"]] == [t["key"] for t in project.dashboard["tabs"]]
    assert app["user"]["role"] == "admin" and app["user"]["email"].startswith("test-admin-")
    assert app["applied"] is True


def test_auth_config():
    code, body = api("GET", "/api/auth/config", auth=False)
    assert code == 200
    domains = [d.strip().lower() for d in os.environ["AUTH_EMAIL_DOMAINS"].split(",") if d.strip()]
    assert body == {"email_domains": domains, "mail_mode": "test", "password_min": 10}


# ---------------------------------------------------------------- 로그인·로그아웃

def test_login_sets_cookie_and_logout_ends_session(account):
    email, password, user_id = account
    code, body, headers = post_login(email.upper(), password)            # 이메일은 대소문자 무시
    assert code == 200 and body == {"email": email, "role": "user", "must_change_password": False}
    raw = next(c for c in headers.get_all("Set-Cookie") if c.startswith(SESSION_COOKIE + "="))
    attrs = {a.strip().split("=")[0].lower() for a in raw.split(";")[1:]}
    assert {"httponly", "path", "samesite", "max-age"} <= attrs and "secure" not in attrs
    assert "samesite=lax" in raw.lower() and "path=/" in raw.lower()
    token = session_cookie(headers)
    assert len(token) >= 43

    # DB에는 토큰의 SHA-256만 있다
    rows = auth_sql("SELECT token_hash, ip FROM dashkit_auth.sessions WHERE user_id = %s", (user_id,))
    assert len(rows) == 1 and bytes(rows[0][0]) == hashlib.sha256(token.encode()).digest() and rows[0][1] is not None
    assert not auth_sql("SELECT 1 FROM dashkit_auth.sessions WHERE token_hash = %s", (token.encode(),))
    (count, last), = auth_sql("SELECT login_count, last_login_at FROM dashkit_auth.users WHERE id = %s", (user_id,))
    assert count == 1 and last is not None

    code, app, _ = http_full("GET", f"{WEB_URL}/api/app", headers=with_cookie(token))
    assert code == 200 and app["user"] == {"email": email, "role": "user"}

    code, _, headers = http_full("POST", f"{WEB_URL}/api/auth/logout", {}, with_cookie(token))
    assert code == 204
    cleared = next(c for c in headers.get_all("Set-Cookie") if c.startswith(SESSION_COOKIE + "="))
    assert "max-age=0" in cleared.lower() or "expires=thu, 01 jan 1970" in cleared.lower()
    assert not auth_sql("SELECT 1 FROM dashkit_auth.sessions WHERE user_id = %s", (user_id,))
    code, body, _ = http_full("GET", f"{WEB_URL}/api/app", headers=with_cookie(token))
    assert (code, body["detail"]) == (401, "unauthenticated")
    code, _, _ = http_full("POST", f"{WEB_URL}/api/auth/logout", {})     # 세션 없이도 204
    assert code == 204


def test_failed_login_does_not_reveal_accounts(account):
    email, password, user_id = account
    invited = make_user(random_email("invited"), None, status="invited")
    try:
        cases = {
            "wrong password": (email, password + "x"),
            "no such account": (random_email("nobody"), password),
            "invited, no password": (invited.email, password),
            "malformed email": ("not-an-email", password),
        }
        bodies = {}
        for name, (e, p) in cases.items():
            code, body, headers = post_login(e, p)
            assert code == 401, name
            assert session_cookie(headers) is None, name
            bodies[name] = body
        assert len({repr(b) for b in bodies.values()}) == 1
        assert bodies["wrong password"]["detail"] == "invalid_credentials"
        # 실패한 로그인은 접속 횟수를 올리지 않는다
        assert auth_sql("SELECT login_count FROM dashkit_auth.users WHERE id = %s", (user_id,))[0][0] == 0
    finally:
        delete_user(invited.id)


def test_missing_account_takes_as_long_as_wrong_password(account):
    """없는 계정에도 해시 검증을 한 번 수행해 응답 시간을 맞춘다(5-2)."""
    email, password, _ = account

    def timed(e):
        t = time.perf_counter()
        code, _, _ = post_login(e, password + "x")
        assert code == 401
        return time.perf_counter() - t

    existing = statistics.median(timed(email) for _ in range(5))
    missing = statistics.median(timed(random_email("nobody")) for _ in range(5))
    assert missing > existing * 0.5, (existing, missing)


def test_status_is_told_only_after_correct_password():
    pending = make_user(random_email("pending"), "pending-password-1", status="pending")
    suspended = make_user(random_email("suspended"), "suspended-password-1", status="suspended")
    try:
        code, body, h = post_login(pending.email, "pending-password-1")
        assert (code, body["detail"]) == (403, "email_not_verified") and session_cookie(h) is None
        code, body, _ = post_login(pending.email, "wrong-password-1")
        assert (code, body["detail"]) == (401, "invalid_credentials")
        code, body, h = post_login(suspended.email, "suspended-password-1")
        assert (code, body["detail"]) == (403, "account_suspended") and session_cookie(h) is None
        code, body, _ = post_login(suspended.email, "wrong-password-1")
        assert (code, body["detail"]) == (401, "invalid_credentials")
    finally:
        delete_user(pending.id)
        delete_user(suspended.id)


def test_old_hash_parameters_are_upgraded_on_login(account):
    email, password, user_id = account
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(password)
    auth_sql("UPDATE dashkit_auth.users SET password_hash = %s WHERE id = %s", (weak, user_id))
    login(email, password)
    (stored,), = auth_sql("SELECT password_hash FROM dashkit_auth.users WHERE id = %s", (user_id,))
    assert stored != weak and stored.startswith("$argon2id$") and PasswordHasher().verify(stored, password)


# ---------------------------------------------------------------- 세션 만료·상태 변경

def test_idle_and_max_lifetime_expire_sessions(account):
    email, password, user_id = account
    idle = int(os.environ.get("AUTH_SESSION_IDLE_MINUTES") or 480)

    token = login(email, password)
    auth_sql("UPDATE dashkit_auth.sessions SET last_seen_at = now() - make_interval(mins => %s) WHERE user_id = %s",
             (idle + 1, user_id))
    code, body, _ = http_full("GET", f"{WEB_URL}/api/app", headers=with_cookie(token))
    assert (code, body["detail"]) == (401, "unauthenticated")
    assert not auth_sql("SELECT 1 FROM dashkit_auth.sessions WHERE user_id = %s", (user_id,))   # 만료된 행은 지운다

    token = login(email, password)
    auth_sql("UPDATE dashkit_auth.sessions SET expires_at = now() - interval '1 second' WHERE user_id = %s", (user_id,))
    code, _, _ = http_full("GET", f"{WEB_URL}/api/app", headers=with_cookie(token))
    assert code == 401


def test_last_seen_is_updated_at_most_once_a_minute(account):
    email, password, user_id = account
    token = login(email, password)

    def last_seen():
        return auth_sql("SELECT last_seen_at FROM dashkit_auth.sessions WHERE user_id = %s", (user_id,))[0][0]

    auth_sql("UPDATE dashkit_auth.sessions SET last_seen_at = now() - interval '20 seconds' WHERE user_id = %s", (user_id,))
    before = last_seen()
    assert http_full("GET", f"{WEB_URL}/api/app", headers=with_cookie(token))[0] == 200
    assert last_seen() == before
    auth_sql("UPDATE dashkit_auth.sessions SET last_seen_at = now() - interval '2 minutes' WHERE user_id = %s", (user_id,))
    before = last_seen()
    assert http_full("GET", f"{WEB_URL}/api/app", headers=with_cookie(token))[0] == 200
    assert last_seen() > before


def test_suspending_an_account_ends_its_sessions_on_next_request(account):
    email, password, user_id = account
    token = login(email, password)
    auth_sql("UPDATE dashkit_auth.users SET status = 'suspended' WHERE id = %s", (user_id,))
    code, _, _ = http_full("GET", f"{WEB_URL}/api/app", headers=with_cookie(token))
    assert code == 401


def test_must_change_password_blocks_other_api(account):
    email, password, user_id = account
    auth_sql("UPDATE dashkit_auth.users SET must_change_password = true WHERE id = %s", (user_id,))
    code, body, headers = post_login(email, password)
    assert code == 200 and body["must_change_password"] is True
    token = session_cookie(headers)
    for method, path, payload in (("GET", "/api/app", None), ("POST", "/api/guest-token", {"dashboard": "x"})):
        code, body, _ = http_full(method, f"{WEB_URL}{path}", payload, with_cookie(token))
        assert (code, body["detail"]) == (403, "password_change_required"), path
    assert http_full("POST", f"{WEB_URL}/api/auth/logout", {}, with_cookie(token))[0] == 204


# ---------------------------------------------------------------- 요청 보호

def test_state_changing_requests_are_guarded(account):
    email, password, _ = account
    body = {"email": email, "password": password}
    code, resp, h = http_full("POST", f"{WEB_URL}/api/auth/login", body, {"Origin": "http://evil.example"})
    assert (code, resp["detail"]) == (403, "forbidden_origin") and session_cookie(h) is None
    code, resp, _ = http_full("POST", f"{WEB_URL}/api/auth/login", body, {"Sec-Fetch-Site": "cross-site"})
    assert (code, resp["detail"]) == (403, "forbidden_origin")
    import json
    code, resp, _ = http_full("POST", f"{WEB_URL}/api/auth/login", raw=json.dumps(body).encode(),
                              headers={"Content-Type": "text/plain"})
    assert (code, resp["detail"]) == (415, "unsupported_media_type")
    code, resp, _ = http_full("POST", f"{WEB_URL}/api/auth/login", raw=b"email=a&password=b",
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert code == 415
    code, resp, h = http_full("POST", f"{WEB_URL}/api/auth/login", body,
                              {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"})
    assert code == 200 and session_cookie(h)


def test_login_page_is_served_with_csp():
    code, body, headers = http_full("GET", f"{WEB_URL}/login.html")
    assert code == 200 and "auth.js" in body
    assert "script-src 'self'" in headers["Content-Security-Policy"]


def test_login_and_logout_are_audited(account):
    email, password, user_id = account
    post_login(email, password + "x")
    post_login(random_email("nobody"), password)
    token = login(email, password)
    http_full("POST", f"{WEB_URL}/api/auth/logout", {}, with_cookie(token))
    rows = auth_sql("""SELECT action, actor_id, actor_email, detail, ip FROM dashkit_auth.audit_log
                        WHERE actor_email = %s ORDER BY id""", (email,))
    assert [r[0] for r in rows] == ["auth.login_failed", "auth.login", "auth.logout"]
    assert rows[0][3] == {"reason": "wrong_password"} and str(rows[0][1]) == user_id and rows[0][4] is not None
    text = repr(auth_sql("SELECT detail FROM dashkit_auth.audit_log WHERE actor_email = %s", (email,)))
    assert password not in text                                          # 비밀번호는 남기지 않는다
