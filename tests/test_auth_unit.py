"""인증의 순수 함수(설정, 비밀번호, 세션 계산, 요청 보호, CLI). DB 없이 돈다. 계약: contracts/auth.md."""
from __future__ import annotations

import io
import re
from datetime import datetime, timedelta, timezone

import pytest

from dashkit.auth import cli, emails, guard, passwords, sessions, store
from dashkit.auth import settings as auth_settings
from dashkit.auth.settings import AuthConfigError

from conftest import ROOT

BASE_ENV = {"AUTH_EMAIL_DOMAINS": "example.com"}


def settings(**env):
    return auth_settings.load({**BASE_ENV, **env})


# ---------------------------------------------------------------- 설정

def test_email_domains_are_required_and_normalized():
    with pytest.raises(AuthConfigError):
        auth_settings.load({})
    with pytest.raises(AuthConfigError):
        auth_settings.load({"AUTH_EMAIL_DOMAINS": " , "})
    with pytest.raises(AuthConfigError):
        auth_settings.load({"AUTH_EMAIL_DOMAINS": "not a domain"})
    cfg = auth_settings.load({"AUTH_EMAIL_DOMAINS": " Example.COM, @corp.example.org,example.com"})
    assert cfg.email_domains == ("example.com", "corp.example.org")
    assert cfg.domain_allowed("a@example.com") and not cfg.domain_allowed("a@evil.example")
    assert not cfg.domain_allowed("a@sub.example.com")


def test_settings_defaults_and_validation():
    cfg = settings()
    assert (cfg.mail_mode, cfg.public_url, cfg.cookie_secure, cfg.idle_minutes, cfg.max_days) == \
        ("test", "http://localhost:8080", False, 480, 7)
    assert {"http://localhost:8080", "http://127.0.0.1:8080"} <= cfg.web_origins
    assert settings(AUTH_COOKIE_SECURE="1").cookie_secure is True
    assert settings(WEB_ORIGINS="https://dash.example.com/", WEB_PUBLIC_URL="https://dash.example.com").web_origins == \
        frozenset({"https://dash.example.com"})
    # 기본 포트를 적어도 브라우저가 보내는 Origin(기본 포트 생략)과 같은 모양이 된다
    cfg = settings(WEB_ORIGINS="https://dash.example.com:443,http://intra.example.com:80/,http://localhost:8080",
                   WEB_PUBLIC_URL="https://dash.example.com:443/")
    assert cfg.web_origins == frozenset({"https://dash.example.com", "http://intra.example.com", "http://localhost:8080"})
    assert guard.check("POST", {"origin": "https://dash.example.com", "content-type": "application/json"}, cfg.web_origins) is None
    assert settings(WEB_ORIGINS="https://dash.example.com:8443").web_origins >= {"https://dash.example.com:8443"}
    for bad in ({"AUTH_MAIL_MODE": "sendmail"}, {"AUTH_COOKIE_SECURE": "maybe"}, {"AUTH_SESSION_MAX_DAYS": "0"},
                {"AUTH_SESSION_IDLE_MINUTES": "x"}, {"WEB_PUBLIC_URL": "ftp://x"}):
        with pytest.raises(AuthConfigError):
            settings(**bad)


# ---------------------------------------------------------------- 이메일·비밀번호

def test_email_normalize_and_validate():
    assert emails.normalize("  Kim.A@Example.COM ") == "kim.a@example.com"
    for ok in ("a@example.com", "first.last+tag@sub.example.co.kr"):
        assert emails.valid(ok), ok
    for bad in ("", "a", "a@", "@example.com", "a@example", "a b@example.com", ".a@example.com", "a..b@example.com",
                "a@-example.com", "x" * 65 + "@example.com", "a@example.com\n"):
        assert not emails.valid(bad), bad


def test_password_hash_is_argon2id_and_verifies():
    h = passwords.hash_password("correct horse battery")
    assert h.startswith("$argon2id$") and "correct" not in h
    assert passwords.verify(h, "correct horse battery")
    assert not passwords.verify(h, "wrong horse battery")
    assert not passwords.verify("not-a-hash", "x")
    assert passwords.needs_rehash(h) is False
    assert passwords.needs_rehash("garbage") is True
    passwords.verify_dummy("anything")     # 예외 없이 돈다


def test_password_policy_reasons():
    assert passwords.policy_violation("short", "kim@example.com") == "too_short"
    assert passwords.policy_violation("x" * 129, "kim@example.com") == "too_long"
    assert passwords.policy_violation("Longername", "longername@example.com") == "same_as_email"
    assert passwords.policy_violation("a-perfectly-fine-pass", "kim@example.com") is None
    assert passwords.policy_violation("x" * 10, "kim@example.com") is None


def test_temporary_password_shape():
    seen = {passwords.temporary_password() for _ in range(20)}
    assert len(seen) == 20
    for pw in seen:
        assert len(pw) == 16 and pw.isalnum() and any(c.isdigit() for c in pw)


# ---------------------------------------------------------------- 세션

def test_session_token_and_hash():
    t = sessions.new_token()
    assert sessions.plausible_token(t) and len(t) >= 43
    assert sessions.token_hash(t) == sessions.token_hash(t) and len(sessions.token_hash(t)) == 32
    assert sessions.token_hash(t) != sessions.token_hash(sessions.new_token())
    for bad in (None, "", "short", "x" * 200, "a" * 40 + ";", "a" * 40 + " "):
        assert not sessions.plausible_token(bad)


def test_session_expiry_rules():
    cfg = settings(AUTH_SESSION_IDLE_MINUTES="60", AUTH_SESSION_MAX_DAYS="2")
    t0 = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)
    exp = sessions.expires_at(t0, cfg)
    assert exp == t0 + timedelta(days=2)
    assert not sessions.is_expired(t0 + timedelta(minutes=59), t0, exp, cfg)
    assert sessions.is_expired(t0 + timedelta(minutes=60), t0, exp, cfg)             # 마지막 사용 후 60분
    late = exp - timedelta(minutes=1)
    assert not sessions.is_expired(late, late - timedelta(minutes=5), exp, cfg)
    assert sessions.is_expired(exp, exp - timedelta(minutes=5), exp, cfg)            # 로그인 후 최대 수명
    assert not sessions.needs_touch(t0 + timedelta(seconds=59), t0)
    assert sessions.needs_touch(t0 + timedelta(seconds=60), t0)


def test_session_cookie_attributes():
    from fastapi import Response

    r = Response()
    sessions.set_cookie(r, "tok" * 15, settings(AUTH_COOKIE_SECURE="1"))
    cookie = r.headers["set-cookie"]
    assert cookie.startswith("dashkit_session=")
    for attr in ("HttpOnly", "Path=/", "SameSite=lax", "Secure", f"Max-Age={7 * 86400}"):
        assert attr.lower() in cookie.lower(), attr
    r = Response()
    sessions.set_cookie(r, "tok" * 15, settings())
    assert "secure" not in r.headers["set-cookie"].lower()


def test_clean_ip():
    assert store.clean_ip(" 10.0.0.1 ") == "10.0.0.1"
    assert store.clean_ip("::1") == "::1"
    assert store.clean_ip("not-an-ip") is None and store.clean_ip(None) is None


# ---------------------------------------------------------------- 요청 보호

ORIGINS = frozenset({"http://localhost:8080"})


@pytest.mark.parametrize("method,headers,expected", [
    ("GET", {}, None),
    ("GET", {"origin": "http://evil.example"}, None),                       # 읽기 요청은 보지 않는다
    ("POST", {"content-type": "application/json"}, None),
    ("POST", {"content-type": "application/json; charset=utf-8", "origin": "http://localhost:8080"}, None),
    ("POST", {"content-type": "application/json", "sec-fetch-site": "same-origin"}, None),
    ("POST", {}, (415, "unsupported_media_type")),
    ("POST", {"content-type": "text/plain"}, (415, "unsupported_media_type")),
    ("POST", {"content-type": "application/x-www-form-urlencoded"}, (415, "unsupported_media_type")),
    ("DELETE", {"content-type": "multipart/form-data"}, (415, "unsupported_media_type")),
    ("POST", {"content-type": "application/json", "origin": "http://evil.example"}, (403, "forbidden_origin")),
    ("POST", {"content-type": "application/json", "origin": "null"}, (403, "forbidden_origin")),
    ("PUT", {"content-type": "application/json", "sec-fetch-site": "cross-site"}, (403, "forbidden_origin")),
    ("PATCH", {"content-type": "text/plain", "origin": "http://evil.example"}, (403, "forbidden_origin")),
])
def test_guard_rules(method, headers, expected):
    got = guard.check(method, headers, ORIGINS)
    assert (got[:2] if got else None) == expected


def test_guard_middleware_rejects_before_app():
    import asyncio

    called = []

    async def inner(scope, receive, send):
        called.append(scope["path"])

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    g = guard.Guard(inner, ORIGINS)
    scope = {"type": "http", "method": "POST", "path": "/api/auth/login",
             "headers": [(b"content-type", b"text/plain")]}
    asyncio.run(g(scope, receive, send))
    assert called == [] and sent[0]["status"] == 415
    asyncio.run(g({**scope, "method": "GET"}, receive, send))
    assert called == ["/api/auth/login"]
    dup = {**scope, "headers": [(b"content-type", b"application/json"), (b"origin", b"http://localhost:8080"),
                                (b"origin", b"http://evil.example")]}
    sent.clear()
    asyncio.run(g(dup, receive, send))
    assert sent[0]["status"] == 400


# ---------------------------------------------------------------- CLI create-admin (DB 대신 가짜 store)

class FakeStore:
    def __init__(self, existing=()):
        self.users = {e: object() for e in existing}
        self.created = []

    def user_by_email(self, email):
        return self.users.get(email)

    def create_user(self, email, **kw):
        self.created.append((email, kw))


@pytest.fixture
def fake_store(monkeypatch):
    fake = FakeStore(existing=("taken@example.com",))
    monkeypatch.setattr(cli.store, "user_by_email", fake.user_by_email)
    monkeypatch.setattr(cli.store, "create_user", fake.create_user)
    monkeypatch.setenv("AUTH_EMAIL_DOMAINS", "example.com")
    return fake


def run_create_admin(monkeypatch, capsys, email, stdin):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    rc = cli.run(["create-admin", "--email", email, "--password-stdin"])
    out = capsys.readouterr()
    return rc, out.out + out.err


def test_create_admin(fake_store, monkeypatch, capsys):
    rc, out = run_create_admin(monkeypatch, capsys, " First.Admin@Example.com ", "a-long-enough-secret\n")
    assert rc == 0 and "first.admin@example.com" in out
    (email, kw), = fake_store.created
    assert email == "first.admin@example.com"
    assert kw["role"] == "admin" and kw["status"] == "active" and kw["password_hash"].startswith("$argon2id$")
    assert "a-long-enough-secret" not in out


@pytest.mark.parametrize("email,stdin,needle", [
    ("not-an-email", "a-long-enough-secret\n", "이메일 형식"),
    ("admin@other.example", "a-long-enough-secret\n", "허용하지 않는 도메인"),
    ("taken@example.com", "a-long-enough-secret\n", "이미 있습니다"),
    ("admin@example.com", "short\n", "10자 이상"),
    ("admin@example.com", "", "받지 못했습니다"),
])
def test_create_admin_rejects(fake_store, monkeypatch, capsys, email, stdin, needle):
    rc, out = run_create_admin(monkeypatch, capsys, email, stdin)
    assert rc == 1 and needle in out and fake_store.created == []
    if stdin.strip():
        assert stdin.strip() not in out          # 비밀번호를 출력하지 않는다


def test_latest_sql_version_matches_files():
    files = sorted(p.name for p in (ROOT / "dashkit/auth/sql").glob("*.sql"))
    assert files and all(re.fullmatch(r"\d{3}_[a-z0-9_]+\.sql", f) for f in files)
    assert [int(f[:3]) for f in files] == list(range(1, len(files) + 1))     # 001부터 빈 번호 없이
    assert cli.latest_version() == len(files)


# ---------------------------------------------------------------- 화면(정적 검사)

def test_auth_pages_have_no_inline_script():
    """CSP(script-src 'self')를 지킨다: 인라인 스크립트·이벤트 속성이 없다."""
    for page in ("index.html", "login.html", "signup.html", "forgot.html", "reset.html", "verify.html", "admin/index.html"):
        html = (ROOT / "web" / page).read_text(encoding="utf-8")
        for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I):
            assert "src=" in m.group(1) and not m.group(2).strip(), page
        assert not re.search(r"\son[a-z]+\s*=", html, re.I), page


def test_compose_gives_backend_only_reader_and_auth_accounts():
    """backend에는 owner DB 계정·Superset 관리자·DB superuser 비밀번호가 없고 인증 계정이 있다(7-5)."""
    import yaml

    doc = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    env = doc["services"]["backend"]["environment"]
    for forbidden in ("DATA_DB_OWNER_USER", "DATA_DB_OWNER_PASSWORD", "SUPERSET_ADMIN_PASSWORD",
                      "SUPERSET_ADMIN_USER", "DATA_DB_ADMIN_PASSWORD", "SUPERSET_SECRET_KEY"):
        assert forbidden not in env, forbidden
    for required in ("DATA_DB_READER_PASSWORD", "DATA_DB_AUTH_USER", "DATA_DB_AUTH_PASSWORD", "AUTH_EMAIL_DOMAINS",
                     "WEB_ORIGINS"):
        assert required in env, required
    assert all(p.startswith("127.0.0.1:") for s in doc["services"].values() for p in s.get("ports", []))
