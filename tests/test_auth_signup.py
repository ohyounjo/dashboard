"""가입·메일 인증·비밀번호 찾기·재설정, 테스트 메일함, 기본 그룹, 잠금, 요청 제한(통합).
계약: contracts/auth.md "계정 흐름", "메일과 테스트 모드", "잠금과 요청 제한".

설계서 13절 3단계 완료 조건: 테스트 메일함의 링크만으로 가입 → 인증 → 로그인 → 찾기 → 재설정을 끝까지 한다.
"""
from __future__ import annotations

import io
import os
import re
import secrets
from contextlib import redirect_stdout

import pytest

from dashkit import db
from dashkit.auth import cli, store

from conftest import (SESSION_COOKIE, WEB_URL, api, auth_post, delete_user, http_full, login, make_user, random_email,
                      session_cookie)

pytestmark = pytest.mark.integration

PASSWORD = "first-good-password-1"
NEW_PASSWORD = "second-good-password-2"


def sql(query, params=()):
    with db.auth_conn(autocommit=True) as conn:
        cur = conn.execute(query, params)
        return cur.fetchall() if cur.description else None


def user_id(email):
    rows = sql("SELECT id FROM dashkit_auth.users WHERE email = %s", (email,))
    return str(rows[0][0]) if rows else None


@pytest.fixture
def cleanup():
    """테스트가 만든 계정 주소(끝나면 지운다)."""
    emails: list[str] = []
    yield emails
    for e in emails:
        uid = user_id(e)
        if uid:
            delete_user(uid)


def outbox(email, cookie=None):
    """그 주소 앞으로 간 테스트 메일(최근 순). cookie가 없으면 테스트용 admin으로 본다."""
    code, body = api("GET", "/api/admin/outbox?size=200", cookie=cookie)
    assert code == 200, body
    return [m for m in body["items"] if m["to_email"] == email]


def token_of(mail):
    m = re.search(r"#token=([A-Za-z0-9_-]+)$", mail["link"])
    assert m, mail["link"]
    return m.group(1)


def get_app(cookie):
    return http_full("GET", f"{WEB_URL}/api/app", headers={"Cookie": f"{SESSION_COOKIE}={cookie}"})


# ---------------------------------------------------------------- 완료 조건: 메일함 링크만으로 끝까지

def test_signup_verify_login_forgot_reset_with_outbox_links(cleanup):
    email = random_email("flow")
    cleanup.append(email)

    code, body, _ = auth_post("/api/auth/signup", {"email": email.upper(), "password": PASSWORD})
    assert (code, body) == (202, {"status": "accepted"})
    code, body, _ = auth_post("/api/auth/login", {"email": email, "password": PASSWORD})
    assert (code, body["detail"]) == (403, "email_not_verified")            # 인증 전에는 로그인 불가

    (mail,) = outbox(email)
    assert mail["kind"] == "verify_email" and mail["role"] == "user"
    assert mail["link"].startswith(os.environ["WEB_PUBLIC_URL"].rstrip("/") + "/verify.html#token=")
    assert auth_post("/api/auth/verify", {"token": token_of(mail)})[0] == 204
    (status, verified), = sql("SELECT status, email_verified_at FROM dashkit_auth.users WHERE email = %s", (email,))
    assert status == "active" and verified is not None

    old_session = login(email, PASSWORD)
    assert get_app(old_session)[0] == 200

    assert auth_post("/api/auth/password/forgot", {"email": email})[0] == 202
    reset = [m for m in outbox(email) if m["kind"] == "reset_password"][0]
    assert "/reset.html#token=" in reset["link"]
    assert auth_post("/api/auth/password/reset", {"token": token_of(reset), "password": NEW_PASSWORD})[0] == 204

    assert get_app(old_session)[0] == 401                                    # 재설정하면 모든 세션이 끊긴다
    code, body, _ = auth_post("/api/auth/login", {"email": email, "password": PASSWORD})
    assert (code, body["detail"]) == (401, "invalid_credentials")
    assert get_app(login(email, NEW_PASSWORD))[0] == 200
    actions = [r[0] for r in sql("SELECT action FROM dashkit_auth.audit_log WHERE actor_email = %s ORDER BY id", (email,))]
    for a in ("auth.signup", "auth.verify_email", "auth.login", "auth.password_reset_requested", "auth.password_reset"):
        assert a in actions, a


# ---------------------------------------------------------------- 계정 존재를 알 수 없음

def test_signup_and_forgot_do_not_reveal_accounts(cleanup):
    active, pending, suspended, invited, new = (random_email(p) for p in ("ex-active", "ex-pending", "ex-susp", "ex-inv", "ex-new"))
    cleanup += [active, pending, suspended, invited, new]
    make_user(active, PASSWORD)
    make_user(suspended, PASSWORD, status="suspended")
    make_user(invited, None, status="invited")
    auth_post("/api/auth/signup", {"email": pending, "password": PASSWORD})

    signups = {e: auth_post("/api/auth/signup", {"email": e, "password": NEW_PASSWORD})[:2] for e in (active, pending, suspended, invited, new)}
    assert set(map(repr, signups.values())) == {repr((202, {"status": "accepted"}))}
    forgots = {e: auth_post("/api/auth/password/forgot", {"email": e})[:2] for e in (active, pending, suspended, invited, random_email("nobody"))}
    assert set(map(repr, forgots.values())) == {repr((202, {"status": "accepted"}))}
    resends = {e: auth_post("/api/auth/verify/resend", {"email": e})[:2] for e in (active, pending, random_email("nobody"))}
    assert set(map(repr, resends.values())) == {repr((202, {"status": "accepted"}))}

    kinds = {e: [m["kind"] for m in outbox(e)] for e in (active, pending, suspended, invited, new)}
    assert kinds[active] == ["reset_password", "already_registered"]          # 이미 있는 주소: 메일로만 알린다
    assert kinds[suspended] == ["already_registered"]                         # 정지된 계정은 찾기 메일을 받지 않는다
    assert kinds[invited] == ["already_registered"]
    assert kinds[new] == ["verify_email"]
    assert kinds[pending] == ["verify_email", "verify_email", "verify_email"]  # 가입, 다시 가입, 재발송
    # 인증 전 계정에 다시 가입하면 새 비밀번호가 되고 이전 인증 링크는 무효다
    newest, older = outbox(pending)[0], outbox(pending)[-1]
    assert auth_post("/api/auth/verify", {"token": token_of(older)})[0] == 410
    assert auth_post("/api/auth/verify", {"token": token_of(newest)})[0] == 204
    assert login(pending, NEW_PASSWORD)
    # 이미 있는 계정의 비밀번호는 가입 요청으로 바뀌지 않는다
    assert login(active, PASSWORD)


def test_signup_validation(cleanup):
    other = "someone@" + "not-allowed.example"
    code, body, _ = auth_post("/api/auth/signup", {"email": other, "password": PASSWORD})
    assert (code, body["detail"]) == (422, "email_domain_not_allowed")
    code, body, _ = auth_post("/api/auth/signup", {"email": "not-an-email", "password": PASSWORD})
    assert (code, body["detail"]) == (422, "invalid_email")
    email = random_email("weakpw")
    cleanup.append(email)
    local = email.split("@")[0]
    for pw, reason in (("short", "too_short"), (local.upper(), "same_as_email"), ("1234567890", "too_common"),
                       ("x" * 129, "too_long")):
        code, body, _ = auth_post("/api/auth/signup", {"email": email, "password": pw})
        assert (code, body["detail"], body["reason"]) == (422, "weak_password", reason), pw
    assert user_id(email) is None                                              # 아무것도 만들지 않았다


# ---------------------------------------------------------------- 토큰

def test_tokens_are_single_use_expire_and_replace_older(cleanup):
    email = random_email("tok")
    cleanup.append(email)
    make_user(email, PASSWORD)
    auth_post("/api/auth/password/forgot", {"email": email})
    first = token_of(outbox(email)[0])
    auth_post("/api/auth/password/forgot", {"email": email})
    second = token_of(outbox(email)[0])
    assert first != second
    code, body, _ = auth_post("/api/auth/password/reset", {"token": first, "password": NEW_PASSWORD})
    assert (code, body["detail"]) == (410, "token_expired")                    # 새로 요청하면 이전 링크는 무효
    # 정책 위반은 토큰을 쓰지 않는다
    code, body, _ = auth_post("/api/auth/password/reset", {"token": second, "password": "short"})
    assert (code, body["detail"]) == (422, "weak_password")
    assert auth_post("/api/auth/password/reset", {"token": second, "password": NEW_PASSWORD})[0] == 204
    code, body, _ = auth_post("/api/auth/password/reset", {"token": second, "password": "third-good-password-3"})
    assert (code, body["detail"]) == (410, "token_expired")                    # 재사용 거절
    # 만료
    auth_post("/api/auth/password/forgot", {"email": email})
    third = token_of(outbox(email)[0])
    sql("UPDATE dashkit_auth.tokens SET expires_at = now() - interval '1 second' WHERE user_id = %s", (user_id(email),))
    assert auth_post("/api/auth/password/reset", {"token": third, "password": "third-good-password-3"})[0] == 410
    for bad in ("", "x", "a" * 43):
        assert auth_post("/api/auth/verify", {"token": bad})[0] == 410
    # DB에는 토큰 원문이 없다
    assert not sql("SELECT 1 FROM dashkit_auth.tokens WHERE token_hash = convert_to(%s, 'UTF8')", (third,))
    assert login(email, NEW_PASSWORD)


def test_reset_token_is_rejected_for_suspended_account(cleanup):
    email = random_email("tok-susp")
    cleanup.append(email)
    make_user(email, PASSWORD)
    auth_post("/api/auth/password/forgot", {"email": email})
    token = token_of(outbox(email)[0])
    sql("UPDATE dashkit_auth.users SET status = 'suspended' WHERE email = %s", (email,))
    assert auth_post("/api/auth/password/reset", {"token": token, "password": NEW_PASSWORD})[0] == 410


def test_token_issue_is_serialized_per_user(cleanup):
    """같은 사용자의 토큰 발급은 사용자 행을 먼저 잠근다(동시에 찾기를 요청해도 유효한 링크는 하나)."""
    import threading
    from datetime import timedelta

    from dashkit.auth import sessions

    email = random_email("tok-lock")
    cleanup.append(email)
    uid = make_user(email, PASSWORD).id
    done = threading.Event()
    with db.auth_conn() as conn:                                        # 다른 트랜잭션이 사용자 행을 잡고 있다
        conn.execute("SELECT 1 FROM dashkit_auth.users WHERE id = %s FOR UPDATE", (uid,))
        t = threading.Thread(target=lambda: (store.create_token(uid, "reset_password", sessions.token_hash(sessions.new_token()),
                                                                 store.now() + timedelta(minutes=30)), done.set()))
        t.start()
        assert not done.wait(1.5)
        conn.rollback()
    assert done.wait(10)
    t.join()
    # 동시에 여러 번 요청해도 남는 재설정 토큰은 하나다
    threads = [threading.Thread(target=auth_post, args=("/api/auth/password/forgot", {"email": email})) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sql("SELECT count(*) FROM dashkit_auth.tokens WHERE user_id = %s AND purpose = 'reset_password'", (uid,))[0][0] == 1


def test_reset_is_all_or_nothing(cleanup):
    """토큰 소비·새 비밀번호·활성화·세션 삭제는 한 트랜잭션이다. 중간에 실패하면 아무것도 바뀌지 않는다."""
    from datetime import timedelta

    from dashkit.auth import passwords, sessions

    email = random_email("atomic")
    cleanup.append(email)
    uid = make_user(email, PASSWORD).id                                  # active 계정에 초대 토큰(활성화 단계에서 실패)
    session = login(email, PASSWORD)
    token = sessions.new_token()
    store.create_token(uid, "invite", sessions.token_hash(token), store.now() + timedelta(hours=1))
    (token_id,), = sql("SELECT id FROM dashkit_auth.tokens WHERE user_id = %s", (uid,))
    assert store.use_reset_token(str(token_id), uid, "invite", passwords.hash_password(NEW_PASSWORD)) is False
    assert sql("SELECT 1 FROM dashkit_auth.tokens WHERE id = %s", (token_id,))           # 토큰은 그대로
    assert login(email, PASSWORD) and get_app(session)[0] == 200                         # 비밀번호·세션도 그대로
    assert store.use_verify_token(str(token_id), uid) is False                           # 용도가 다른 토큰은 못 쓴다


# ---------------------------------------------------------------- 기본 그룹

def test_new_accounts_join_default_groups_when_they_become_active(cleanup):
    code, default = api("POST", "/api/admin/groups", {"name": f"default-{secrets.token_hex(3)}", "is_default": True})
    code2, plain = api("POST", "/api/admin/groups", {"name": f"plain-{secrets.token_hex(3)}"})
    assert code == code2 == 201
    try:
        email = random_email("deflt")
        cleanup.append(email)
        auth_post("/api/auth/signup", {"email": email, "password": PASSWORD})
        uid = user_id(email)
        assert not sql("SELECT 1 FROM dashkit_auth.group_members WHERE user_id = %s", (uid,))   # 인증 전에는 넣지 않는다
        auth_post("/api/auth/verify", {"token": token_of(outbox(email)[0])})
        groups = {str(r[0]) for r in sql("SELECT group_id FROM dashkit_auth.group_members WHERE user_id = %s", (uid,))}
        assert groups == {default["id"]}
        # 운용자가 빼면 다시 넣지 않는다(비밀번호 재설정 등으로 다시 넣지 않음)
        api("PUT", f"/api/admin/groups/{default['id']}/members", {"remove": [uid]})
        auth_post("/api/auth/password/forgot", {"email": email})
        auth_post("/api/auth/password/reset", {"token": token_of(outbox(email)[0]), "password": NEW_PASSWORD})
        assert not sql("SELECT 1 FROM dashkit_auth.group_members WHERE user_id = %s", (uid,))
    finally:
        api("DELETE", f"/api/admin/groups/{default['id']}")
        api("DELETE", f"/api/admin/groups/{plain['id']}")


# ---------------------------------------------------------------- 테스트 메일함의 등급 제한

def test_operator_cannot_see_mail_for_admins_or_operators(cleanup):
    op_email, admin_email, user_email = random_email("ob-op"), random_email("ob-admin"), random_email("ob-user")
    cleanup += [op_email, admin_email, user_email]
    make_user(op_email, PASSWORD, role="operator")
    make_user(admin_email, PASSWORD, role="admin")
    make_user(user_email, PASSWORD)
    op_cookie = login(op_email, PASSWORD)
    # operator가 로그인 없이 admin·operator(자기 자신 포함)·user 주소로 비밀번호 찾기를 요청한다
    for e in (admin_email, op_email, user_email):
        assert auth_post("/api/auth/password/forgot", {"email": e})[0] == 202

    code, body = api("GET", "/api/admin/outbox?size=200", cookie=op_cookie)
    seen = {m["to_email"] for m in body["items"]}
    assert user_email in seen and admin_email not in seen and op_email not in seen
    assert {m["role"] for m in body["items"]} == {"user"}
    assert all(m["link"] is None or "token=" in m["link"] for m in body["items"])
    # admin은 전부 본다
    assert {m["kind"] for m in outbox(admin_email)} == {"reset_password"} and outbox(op_email)

    # 판정은 읽을 때의 등급: user가 operator로 바뀌면 그 계정 앞의 옛 메일도 operator에게 보이지 않는다
    sql("UPDATE dashkit_auth.users SET role = 'operator' WHERE email = %s", (user_email,))
    code, body = api("GET", "/api/admin/outbox?size=200", cookie=op_cookie)
    assert user_email not in {m["to_email"] for m in body["items"]}
    # 일반사용자는 메일함을 볼 수 없다
    plain = random_email("ob-plain")
    cleanup.append(plain)
    make_user(plain, PASSWORD)
    code, body = api("GET", "/api/admin/outbox", cookie=login(plain, PASSWORD))
    assert (code, body["detail"]) == (403, "forbidden")


def test_cli_outbox_shows_everything():
    email = random_email("ob-cli")
    user = make_user(email, PASSWORD, role="admin")
    try:
        auth_post("/api/auth/password/forgot", {"email": email})
        out = io.StringIO()
        with redirect_stdout(out):
            assert cli.run(["outbox", "--limit", "200"]) == 0
        text = out.getvalue()
        assert email in text and "reset_password" in text and "/reset.html#token=" in text
    finally:
        delete_user(user.id)


# ---------------------------------------------------------------- 잠금과 요청 제한

def test_five_failures_lock_the_account_for_fifteen_minutes(cleanup):
    email = random_email("lock")
    cleanup.append(email)
    make_user(email, PASSWORD)
    codes = [auth_post("/api/auth/login", {"email": email, "password": "wrong-password-x"})[0] for _ in range(5)]
    assert codes == [401, 401, 401, 401, 423]
    code, body, h = auth_post("/api/auth/login", {"email": email, "password": PASSWORD})
    assert (code, body["detail"]) == (423, "account_locked") and session_cookie(h) is None   # 맞는 비밀번호도 거절
    (until, minutes), = sql("SELECT locked_until, extract(epoch FROM locked_until - now()) / 60 FROM dashkit_auth.users WHERE email = %s", (email,))
    assert 14 < float(minutes) <= 15
    actions = [r[0] for r in sql("SELECT action FROM dashkit_auth.audit_log WHERE actor_email = %s ORDER BY id", (email,))]
    assert "auth.locked" in actions
    # 재설정하면 잠금이 풀린다
    auth_post("/api/auth/password/forgot", {"email": email})
    assert auth_post("/api/auth/password/reset", {"token": token_of(outbox(email)[0]), "password": NEW_PASSWORD})[0] == 204
    assert login(email, NEW_PASSWORD)
    # 시간이 지나면(잠금 끝 시각이 지나면) 다시 로그인할 수 있다
    for _ in range(5):
        auth_post("/api/auth/login", {"email": email, "password": "wrong-password-x"})
    sql("UPDATE dashkit_auth.users SET locked_until = now() - interval '1 second' WHERE email = %s", (email,))
    assert login(email, NEW_PASSWORD)
    (failed,), = sql("SELECT failed_count FROM dashkit_auth.users WHERE email = %s", (email,))
    assert failed == 0


def test_missing_accounts_lock_the_same_way():
    email = random_email("ghost")
    codes = [auth_post("/api/auth/login", {"email": email, "password": "wrong-password-x"})[0] for _ in range(6)]
    assert codes == [401, 401, 401, 401, 423, 423]


def test_auth_posts_are_rate_limited_per_ip():
    ip = "10.250.{}.{}".format(*secrets.token_bytes(2))
    codes = []
    for _ in range(21):
        code, body, headers = auth_post("/api/auth/verify/resend", {"email": random_email("rl")}, ip=ip)
        codes.append(code)
    assert codes[:20] == [202] * 20 and codes[20] == 429
    assert body["detail"] == "rate_limited" and int(headers["Retry-After"]) >= 1
    assert auth_post("/api/auth/verify/resend", {"email": random_email("rl")})[0] == 202   # 다른 IP는 그대로
    assert http_full("GET", f"{WEB_URL}/api/auth/config")[0] == 200                           # GET은 세지 않는다


# ---------------------------------------------------------------- 보존 기간

def test_cleanup_removes_old_pending_accounts_and_mail(cleanup):
    old, fresh = random_email("old-pending"), random_email("fresh-pending")
    cleanup += [old, fresh]
    for e in (old, fresh):
        auth_post("/api/auth/signup", {"email": e, "password": PASSWORD})
    sql("UPDATE dashkit_auth.users SET created_at = now() - interval '8 days' WHERE email = %s", (old,))
    sql("UPDATE dashkit_auth.outbox SET created_at = now() - interval '8 days' WHERE to_email = %s", (fresh,))
    removed = store.cleanup(7, 7)
    assert removed["pending_users"] >= 1 and removed["outbox"] >= 1
    assert user_id(old) is None and user_id(fresh) is not None
    assert outbox(fresh) == []
