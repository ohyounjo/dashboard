"""/api/auth/* 경로와 로그인 확인 의존성(contracts/auth.md "인증 API", "로그인 확인").

로그인 확인(authenticate)은 요청마다 DB의 세션을 읽는다. 등급·상태가 바뀌면 다음 요청부터 반영된다.

| 의존성 | 통과 조건 |
|---|---|
| require_session | 유효한 세션(비밀번호 변경이 필요한 계정도 통과. 내 정보·비밀번호 변경·로그아웃용) |
| require_user | require_session + 비밀번호 변경이 필요하지 않음(아니면 403 password_change_required) |
| require_operator | require_user + 등급이 admin 또는 operator(아니면 403 forbidden) |
| require_admin | require_user + 등급이 admin(아니면 403 forbidden) |
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field, StrictStr

from ..api.errors import ApiError
from . import emails, passwords, sessions, store
from . import settings as auth_settings

log = logging.getLogger("dashkit.auth")

ROLES = ("admin", "operator", "user")
OPERATOR_ROLES = ("admin", "operator")

router = APIRouter(prefix="/api/auth")


@dataclass(frozen=True)
class Principal:
    """로그인한 사용자. 요청 하나 동안 쓴다."""
    id: str
    email: str
    role: str
    must_change_password: bool = False
    session_id: str | None = None

    @property
    def is_operator(self) -> bool:
        return self.role in OPERATOR_ROLES

    def public(self) -> dict:
        return {"email": self.email, "role": self.role}


def client_ip(request: Request) -> str | None:
    """nginx가 넣는 X-Real-IP. 없으면 직접 연결한 주소."""
    ip = request.headers.get("x-real-ip")
    if ip:
        return ip.strip()
    return request.client.host if request.client else None


def _unauthenticated() -> ApiError:
    return ApiError(401, "unauthenticated", "로그인이 필요합니다")


def authenticate(request: Request) -> Principal:
    """세션 쿠키 → Principal. 없거나 만료됐으면 401 unauthenticated(만료된 세션 행은 지운다)."""
    cached = getattr(request.state, "principal", None)
    if isinstance(cached, Principal):
        return cached
    token = request.cookies.get(sessions.COOKIE_NAME)
    if not sessions.plausible_token(token):
        raise _unauthenticated()
    cfg = auth_settings.get()
    info = store.session_by_hash(sessions.token_hash(token))
    if info is None:
        raise _unauthenticated()
    now = store.now()
    if info.status != "active" or sessions.is_expired(now, info.last_seen_at, info.expires_at, cfg):
        store.delete_session(info.session_id)
        raise _unauthenticated()
    if sessions.needs_touch(now, info.last_seen_at):
        store.touch_session(info.session_id)
    principal = Principal(info.user_id, info.email, info.role, info.must_change_password, info.session_id)
    request.state.principal = principal
    return principal


def require_session(request: Request) -> Principal:
    return authenticate(request)


def require_user(principal: Principal = Depends(require_session)) -> Principal:
    if principal.must_change_password:
        raise ApiError(403, "password_change_required", "비밀번호를 먼저 바꿔야 합니다")
    return principal


def require_operator(principal: Principal = Depends(require_user)) -> Principal:
    if not principal.is_operator:
        raise ApiError(403, "forbidden", "권한이 없습니다")
    return principal


def require_admin(principal: Principal = Depends(require_user)) -> Principal:
    if principal.role != "admin":
        raise ApiError(403, "forbidden", "권한이 없습니다")
    return principal


# ---------------------------------------------------------------- 경로

@router.get("/config")
def get_config():
    cfg = auth_settings.get()
    return {"email_domains": list(cfg.email_domains), "mail_mode": cfg.mail_mode, "password_min": passwords.MIN_LENGTH}


class LoginRequest(BaseModel):
    email: StrictStr = Field(max_length=320)
    password: StrictStr = Field(max_length=1024)


def _invalid_credentials() -> ApiError:
    return ApiError(401, "invalid_credentials", "이메일 또는 비밀번호가 올바르지 않습니다")


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response):
    cfg = auth_settings.get()
    email = emails.normalize(body.email)
    user = store.user_by_email(email) if emails.valid(email) else None
    if user is None or user.password_hash is None:
        passwords.verify_dummy(body.password)       # 없는 계정도 같은 비용(응답 시간)
        raise _invalid_credentials()
    if not passwords.verify(user.password_hash, body.password):
        raise _invalid_credentials()
    # 비밀번호가 맞은 뒤에만 상태를 알려 준다(상태로 계정 존재 여부를 알아낼 수 없다)
    if user.status == "pending":
        raise ApiError(403, "email_not_verified", "메일 인증을 마쳐야 로그인할 수 있습니다")
    if user.status == "suspended":
        raise ApiError(403, "account_suspended", "정지된 계정입니다")
    if user.status != "active":
        raise _invalid_credentials()
    new_hash = passwords.hash_password(body.password) if passwords.needs_rehash(user.password_hash) else None
    token = sessions.new_token()
    store.create_session(user.id, sessions.token_hash(token), sessions.expires_at(store.now(), cfg),
                         client_ip(request), request.headers.get("user-agent"))
    store.record_login_success(user.id, new_hash)
    sessions.set_cookie(response, token, cfg)
    return {"email": user.email, "role": user.role, "must_change_password": user.must_change_password}


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response):
    token = request.cookies.get(sessions.COOKIE_NAME)
    if sessions.plausible_token(token):
        store.delete_session_by_hash(sessions.token_hash(token))
    sessions.clear_cookie(response, auth_settings.get())
    response.status_code = 204
    return None
