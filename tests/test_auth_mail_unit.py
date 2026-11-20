"""메일 템플릿·링크, 요청 제한·없는 주소 잠금, 보존 기간 정리 주기, 흔한 비밀번호(단위, DB 없이)."""
from __future__ import annotations

import pytest

from dashkit.auth import limits, mailer, maintenance, passwords
from dashkit.auth import settings as auth_settings


@pytest.fixture
def cfg(monkeypatch):
    def use(**env):
        settings = auth_settings.load({"AUTH_EMAIL_DOMAINS": "example.com", **env})
        monkeypatch.setattr(auth_settings, "_cached", settings)
        return settings
    yield use
    auth_settings.reset_cache()


def test_templates_cover_every_kind_and_put_the_link_in_the_body():
    assert set(mailer.TEMPLATES) == set(mailer.KINDS)
    for kind in mailer.KINDS:
        subject, body = mailer.render(kind, "https://dash.example.com/reset.html#token=abc")
        assert subject and body and "{link}" not in body
        if kind != "already_registered":
            assert "#token=abc" in body


def test_links_use_public_url_and_fragment(cfg):
    cfg(WEB_PUBLIC_URL="https://dash.example.com/")
    assert mailer.link("reset.html", "t0k") == "https://dash.example.com/reset.html#token=t0k"


def test_mail_mode_selects_mailer(cfg):
    cfg(AUTH_MAIL_MODE="test")
    assert isinstance(mailer.get(), mailer.OutboxMailer)
    cfg(AUTH_MAIL_MODE="smtp")
    smtp = mailer.get()
    with pytest.raises(mailer.MailUnavailable):
        smtp.send("a@example.com", "invite", "x")
    assert mailer.send_quietly("a@example.com", "invite", "x") is False     # 로그인 전 경로는 응답을 바꾸지 않는다


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_rate_limiter_window():
    clock = Clock()
    rl = limits.RateLimiter(limit=3, window=60, clock=clock)
    assert [rl.hit("a") for _ in range(4)] == [True, True, True, False]
    assert rl.hit("b") is True                                   # IP마다 따로
    assert 1 <= rl.retry_after("a") <= 61
    clock.t += 59
    assert rl.hit("a") is False
    clock.t += 1.5
    assert rl.hit("a") is True                                   # 가장 오래된 요청이 창 밖으로 나감


def test_ghost_locks_match_account_locks():
    clock = Clock()
    g = limits.GhostLocks(limit=5, minutes=15, clock=clock)
    assert [g.fail("x@example.com") for _ in range(5)] == [False, False, False, False, True]
    assert g.locked("x@example.com") and not g.locked("y@example.com")
    clock.t += 15 * 60 - 1
    assert g.locked("x@example.com")
    clock.t += 2
    assert not g.locked("x@example.com")
    assert g.fail("x@example.com") is False                      # 잠금이 끝나면 다시 5번


def test_maintenance_runs_once_a_day(monkeypatch):
    calls = []
    monkeypatch.setattr(maintenance.store, "cleanup", lambda *a: calls.append(a) or {"outbox": 0})
    maintenance.reset()
    clock = Clock()
    assert maintenance.maybe_run(clock) is True
    assert maintenance.maybe_run(clock) is False
    clock.t += 24 * 3600
    assert maintenance.maybe_run(clock) is True
    assert calls == [(7, 7), (7, 7)]
    maintenance.reset()


def test_common_password_list():
    common = passwords.common_passwords()
    assert len(common) >= 9000 and "password" in common and "qwertyuiop" in common
    assert passwords.policy_violation("QwertyUIOP", "kim@example.com") == "too_common"
    assert passwords.policy_violation("qwertyuiop-but-longer", "kim@example.com") is None
