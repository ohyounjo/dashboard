"""세션 토큰과 쿠키, 만료 계산(contracts/auth.md "세션").

- 로그인하면 256비트 임의 값을 만들어 쿠키 dashkit_session에 넣고, DB에는 그 SHA-256만 저장한다.
- 만료: 마지막 사용 후 idle_minutes, 로그인 후 max_days. last_seen_at은 1분에 한 번만 갱신한다.
- 일회용 토큰(메일 인증·재설정·초대)도 같은 방식(256비트, SHA-256만 저장)이다.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from fastapi import Response

from .settings import Settings

COOKIE_NAME = "dashkit_session"
TOUCH_INTERVAL = timedelta(minutes=1)
_TOKEN_BYTES = 32   # 256비트


def new_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def plausible_token(token: str | None) -> bool:
    """형식만 본다(길이·문자). 맞지 않으면 DB를 조회하지 않는다."""
    if not token or not 32 <= len(token) <= 128:
        return False
    return all(c.isalnum() or c in "-_" for c in token)


def expires_at(now: datetime, settings: Settings) -> datetime:
    return now + timedelta(days=settings.max_days)


def is_expired(now: datetime, last_seen_at: datetime, expires: datetime, settings: Settings) -> bool:
    return now >= expires or now >= last_seen_at + timedelta(minutes=settings.idle_minutes)


def needs_touch(now: datetime, last_seen_at: datetime) -> bool:
    return now - last_seen_at >= TOUCH_INTERVAL


def set_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=settings.max_days * 86400,
        path="/", httponly=True, samesite="lax", secure=settings.cookie_secure,
    )


def clear_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=settings.cookie_secure)
