"""인증 설정. 값은 환경변수에서만 읽는다(contracts/auth.md "환경변수").

backend는 시작할 때 load()를 한 번 불러 검사한다. AUTH_EMAIL_DOMAINS가 없거나 형식이 틀리면 시작하지 않는다.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

MAIL_MODES = ("test", "smtp")
DEFAULT_PUBLIC_URL = "http://localhost:8080"
DEFAULT_ORIGINS = "http://localhost:8080,http://127.0.0.1:8080"
_DOMAIN_RE = re.compile(r"(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}")


class AuthConfigError(Exception):
    """인증 설정 오류. 메시지에 비밀값을 넣지 않는다."""


@dataclass(frozen=True)
class Settings:
    email_domains: tuple[str, ...]      # 소문자. 가입·초대·관리자 생성에 허용하는 메일 도메인
    mail_mode: str                      # "test" | "smtp"
    public_url: str                     # 메일 링크의 주소(끝의 / 없음)
    cookie_secure: bool                 # 세션 쿠키에 Secure를 붙인다(HTTPS 배포)
    idle_minutes: int                   # 마지막 사용 후 세션 만료
    max_days: int                       # 로그인 후 세션 최대 수명
    web_origins: frozenset[str]         # 상태를 바꾸는 요청의 Origin 허용 목록

    def domain_allowed(self, email: str) -> bool:
        return email.rpartition("@")[2] in self.email_domains


def _int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise AuthConfigError(f"{name}는 정수여야 합니다") from None
    if not low <= value <= high:
        raise AuthConfigError(f"{name}는 {low}~{high} 사이여야 합니다")
    return value


def _origin(url: str) -> str | None:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    # 브라우저는 Origin 헤더에서 그 스킴의 기본 포트(http 80, https 443)를 생략한다. 같은 모양으로 맞춘다
    if port == {"http": 80, "https": 443}[parts.scheme]:
        port = None
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")


def parse_domains(raw: str) -> tuple[str, ...]:
    domains: list[str] = []
    for part in raw.split(","):
        d = part.strip().lower().lstrip("@")
        if not d:
            continue
        if not _DOMAIN_RE.fullmatch(d):
            raise AuthConfigError(f"AUTH_EMAIL_DOMAINS의 도메인 형식 오류: {d!r}")
        if d not in domains:
            domains.append(d)
    if not domains:
        raise AuthConfigError("AUTH_EMAIL_DOMAINS가 필요합니다(가입을 허용할 메일 도메인, 쉼표 구분)")
    return tuple(domains)


def load(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    domains = parse_domains(env.get("AUTH_EMAIL_DOMAINS") or "")
    mode = (env.get("AUTH_MAIL_MODE") or "test").strip().lower()
    if mode not in MAIL_MODES:
        raise AuthConfigError(f"AUTH_MAIL_MODE는 {' · '.join(MAIL_MODES)} 중 하나여야 합니다")
    public = (env.get("WEB_PUBLIC_URL") or DEFAULT_PUBLIC_URL).strip().rstrip("/")
    if _origin(public) is None:
        raise AuthConfigError("WEB_PUBLIC_URL은 http(s) 주소여야 합니다")
    secure = (env.get("AUTH_COOKIE_SECURE") or "0").strip().lower()
    if secure not in ("0", "1", "true", "false", "yes", "no"):
        raise AuthConfigError("AUTH_COOKIE_SECURE는 0 또는 1이어야 합니다")
    origins = {o for o in (_origin(u) for u in (env.get("WEB_ORIGINS") or DEFAULT_ORIGINS).split(",") if u.strip()) if o}
    origins.add(_origin(public) or "")
    origins.discard("")
    return Settings(
        email_domains=domains,
        mail_mode=mode,
        public_url=public,
        cookie_secure=secure in ("1", "true", "yes"),
        idle_minutes=_int(env, "AUTH_SESSION_IDLE_MINUTES", 480, 5, 60 * 24 * 30),
        max_days=_int(env, "AUTH_SESSION_MAX_DAYS", 7, 1, 90),
        web_origins=frozenset(origins),
    )


_cached: Settings | None = None


def get() -> Settings:
    """프로세스당 한 번 읽은 설정. backend는 시작할 때 load()로 먼저 검사한다."""
    global _cached
    if _cached is None:
        _cached = load()
    return _cached


def reset_cache() -> None:
    global _cached
    _cached = None
