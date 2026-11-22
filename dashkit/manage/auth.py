"""관리 서비스 인증: 세션 운용자 또는 자동 적재용 API 토큰(contracts/manage.md)."""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Request

from ..api.errors import ApiError
from ..auth import sessions
from ..auth import store as auth_store
from ..auth.routes import Principal, authenticate, require_operator, require_user
from ..features import has_manage_feature

API_TOKEN_PREFIX = "dkt_"
_TOKEN_BYTES = 32


def new_api_token() -> str:
    """새 API 토큰 원문을 만든다(dkt_ + 256비트 URL-safe 임의 값)."""
    return API_TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)


def plausible_api_token(token: str | None) -> bool:
    """API 토큰 형식만 먼저 본다. 맞지 않으면 DB를 조회하지 않는다."""
    if not isinstance(token, str) or not token.startswith(API_TOKEN_PREFIX):
        return False
    if not 36 <= len(token) <= 128:
        return False
    return all(c.isalnum() or c in "-_" for c in token)


@dataclass(frozen=True)
class ManageActor:
    """적재·작업 상태 경로의 행위자. 세션 운용자 또는 API 토큰 중 하나만 채워진다."""

    principal: Principal | None
    token: auth_store.ApiToken | None

    @property
    def actor_id(self) -> str | None:
        return self.principal.id if self.principal else None

    @property
    def actor_email(self) -> str:
        if self.principal is not None:
            return self.principal.email
        assert self.token is not None
        return f"token:{self.token.name}"

    @property
    def upload_actor_id(self) -> str:
        if self.principal is not None:
            return self.principal.id
        assert self.token is not None
        return f"token:{self.token.id}"

    @property
    def token_id(self) -> str | None:
        return self.token.id if self.token else None


def require_ingest_actor(request: Request) -> ManageActor:
    """적재·자기 작업 조회 전용 인증 의존성.

    - Authorization 헤더가 있으면 API 토큰으로 인증한다(MANAGE_FEATURES=tokens가 꺼져 있으면 401).
    - 없으면 기존 세션 운용자(require_operator)로 인증한다.
    """
    auth = request.headers.get("authorization")
    if auth is not None and auth.strip():
        if not has_manage_feature("tokens"):
            raise ApiError(401, "unauthenticated", "로그인이 필요합니다")
        parts = auth.strip().split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise ApiError(401, "invalid_token", "유효하지 않거나 만료된 API 토큰입니다")
        raw = parts[1].strip()
        if not plausible_api_token(raw):
            raise ApiError(401, "invalid_token", "유효하지 않거나 만료된 API 토큰입니다")
        tok = auth_store.api_token_by_hash(sessions.token_hash(raw))
        now = auth_store.now()
        if tok is None or tok.revoked_at is not None or tok.expires_at <= now:
            raise ApiError(401, "invalid_token", "유효하지 않거나 만료된 API 토큰입니다")
        if tok.last_used_at is None or now - tok.last_used_at >= sessions.TOUCH_INTERVAL:
            auth_store.touch_api_token(tok.id)
        return ManageActor(principal=None, token=tok)
    principal = require_operator(require_user(authenticate(request)))
    return ManageActor(principal=principal, token=None)
