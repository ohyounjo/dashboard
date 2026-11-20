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
from . import emails, limits, maintenance, mailer, passwords, sessions, store
from . import settings as auth_settings

log = logging.getLogger("dashkit.auth")

ROLES = ("admin", "operator", "user")
OPERATOR_ROLES = ("admin", "operator")

def rate_limit(request: Request) -> None:
    """인증 경로의 POST는 IP당 1분에 20번(429 rate_limited, Retry-After)."""
    if request.method != "POST":
        return
    ip = client_ip(request) or "unknown"
    if not limits.auth_rate.hit(ip):
        raise ApiError(429, "rate_limited", "요청이 너무 많습니다. 잠시 뒤에 다시 시도하세요",
                       headers={"Retry-After": str(limits.auth_rate.retry_after(ip))})


router = APIRouter(prefix="/api/auth", dependencies=[Depends(rate_limit)])


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


def _locked() -> ApiError:
    return ApiError(423, "account_locked", "로그인에 여러 번 실패해 잠시 잠겼습니다")


def _audit_login(request: Request, action: str, email: str, user_id: str | None = None, **detail) -> None:
    store.record_audit(action, actor_id=user_id, actor_email=email[:320] or None, target_type="user",
                       target_id=user_id, target_label=email[:320] or None, detail=detail, ip=client_ip(request))


def _fail_login(request: Request, email: str, user: store.User | None, reason: str) -> ApiError:
    """실패를 센다. 연속 5번이면 15분 잠그고 423. 없는 계정도 같은 횟수에서 같은 응답이다."""
    _audit_login(request, "auth.login_failed", email, user.id if user else None, reason=reason)
    if user is None:
        just_locked = limits.ghosts.fail(email)
    else:
        just_locked = store.record_login_failure(user.id, limits.LOGIN_FAILURE_LIMIT, limits.LOCK_MINUTES) is not None
        if just_locked:
            _audit_login(request, "auth.locked", email, user.id, minutes=limits.LOCK_MINUTES)
    return _locked() if just_locked else _invalid_credentials()


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response):
    cfg = auth_settings.get()
    maintenance.maybe_run()
    email = emails.normalize(body.email)
    user = store.user_by_email(email) if emails.valid(email) else None
    now = store.now()
    # 잠긴 주소는 비밀번호와 상관없이 423(없는 계정도 같은 횟수에서 잠긴 것처럼 답한다)
    if (user is not None and user.locked_until is not None and user.locked_until > now) or \
            (user is None and limits.ghosts.locked(email)):
        passwords.verify_dummy(body.password)
        _audit_login(request, "auth.login_failed", email, user.id if user else None, reason="locked")
        raise _locked()
    if user is None or user.password_hash is None:
        passwords.verify_dummy(body.password)       # 없는 계정도 같은 비용(응답 시간)
        raise _fail_login(request, email, None if user is None else user, "no_password" if user else "unknown_account")
    if not passwords.verify(user.password_hash, body.password):
        raise _fail_login(request, email, user, "wrong_password")
    # 비밀번호가 맞은 뒤에만 상태를 알려 준다(상태로 계정 존재 여부를 알아낼 수 없다)
    if user.status == "pending":
        _audit_login(request, "auth.login_failed", email, user.id, reason="email_not_verified")
        raise ApiError(403, "email_not_verified", "메일 인증을 마쳐야 로그인할 수 있습니다")
    if user.status == "suspended":
        _audit_login(request, "auth.login_failed", email, user.id, reason="account_suspended")
        raise ApiError(403, "account_suspended", "정지된 계정입니다")
    if user.status != "active":
        raise _fail_login(request, email, user, "not_active")
    new_hash = passwords.hash_password(body.password) if passwords.needs_rehash(user.password_hash) else None
    token = sessions.new_token()
    store.create_session(user.id, sessions.token_hash(token), sessions.expires_at(now, cfg),
                         client_ip(request), request.headers.get("user-agent"))
    store.record_login_success(user.id, new_hash)
    _audit_login(request, "auth.login", user.email, user.id)
    sessions.set_cookie(response, token, cfg)
    return {"email": user.email, "role": user.role, "must_change_password": user.must_change_password}


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response):
    token = request.cookies.get(sessions.COOKIE_NAME)
    if sessions.plausible_token(token):
        ended = store.delete_session_by_hash(sessions.token_hash(token))
        if ended is not None:
            _audit_login(request, "auth.logout", ended["email"], ended["user_id"])
    sessions.clear_cookie(response, auth_settings.get())
    response.status_code = 204
    return None


# ---------------------------------------------------------------- 가입·메일 인증·비밀번호 찾기

TOKEN_TTL = {"verify_email": 24 * 60, "reset_password": 30, "invite": 72 * 60}     # 분


def issue_token(user_id: str, purpose: str) -> str:
    """일회용 토큰(256비트, DB에는 SHA-256만). 같은 사용자·같은 용도의 이전 토큰은 무효가 된다."""
    from datetime import timedelta

    token = sessions.new_token()
    store.create_token(user_id, purpose, sessions.token_hash(token), store.now() + timedelta(minutes=TOKEN_TTL[purpose]))
    return token


def weak_password(reason: str) -> ApiError:
    return ApiError(422, "weak_password", "비밀번호 정책에 맞지 않습니다", extra={"reason": reason})


def check_new_email(raw: str) -> str:
    """정규화한 주소. 형식 422 invalid_email, 도메인 422 email_domain_not_allowed."""
    email = emails.normalize(raw)
    if not emails.valid(email):
        raise ApiError(422, "invalid_email", "이메일 형식이 올바르지 않습니다")
    if not auth_settings.get().domain_allowed(email):
        raise ApiError(422, "email_domain_not_allowed", "가입할 수 없는 메일 도메인입니다")
    return email


class SignupRequest(BaseModel):
    email: StrictStr = Field(max_length=320)
    password: StrictStr = Field(max_length=1024)


ACCEPTED = {"status": "accepted"}


@router.post("/signup", status_code=202)
def signup(body: SignupRequest, request: Request):
    """항상 같은 202다. 이미 있는 주소면 그 주소로 '이미 가입된 주소' 메일을 보낸다(가입 화면으로 계정 존재를 알 수 없다)."""
    maintenance.maybe_run()
    email = check_new_email(body.email)
    reason = passwords.policy_violation(body.password, email)
    if reason:
        raise weak_password(reason)
    password_hash = passwords.hash_password(body.password)       # 어느 길이든 같은 비용
    user = store.user_by_email(email)
    if user is None:
        try:
            user = store.create_user(email, role="user", status="pending", source="signup", password_hash=password_hash)
        except store.Conflict:          # 동시에 같은 주소로 가입
            return ACCEPTED
        store.record_audit("auth.signup", actor_id=user.id, actor_email=email, target_type="user",
                           target_id=user.id, target_label=email, ip=client_ip(request))
        mailer.send_quietly(email, "verify_email", mailer.link("verify.html", issue_token(user.id, "verify_email")))
    elif user.status == "pending":
        # 인증 전 계정에 다시 가입: 새 비밀번호와 새 인증 링크(이전 링크는 무효)
        store.set_pending_password(user.id, password_hash)
        mailer.send_quietly(email, "verify_email", mailer.link("verify.html", issue_token(user.id, "verify_email")))
    else:
        mailer.send_quietly(email, "already_registered", None)
    return ACCEPTED


class TokenRequest(BaseModel):
    token: StrictStr = Field(max_length=200)


def _token_expired() -> ApiError:
    return ApiError(410, "token_expired", "링크가 만료되었거나 이미 쓰였습니다. 다시 요청하세요")


@router.post("/verify", status_code=204)
def verify(body: TokenRequest, request: Request):
    if not sessions.plausible_token(body.token):
        raise _token_expired()
    owner = store.token_owner(sessions.token_hash(body.token), ("verify_email",))
    if owner is None or owner["status"] != "pending" or not store.consume_token(owner["token_id"]):
        raise _token_expired()
    if not store.activate_user(owner["user_id"], "pending"):
        raise _token_expired()
    store.record_audit("auth.verify_email", actor_id=owner["user_id"], actor_email=owner["email"], target_type="user",
                       target_id=owner["user_id"], target_label=owner["email"], ip=client_ip(request))
    return Response(status_code=204)


class EmailRequest(BaseModel):
    email: StrictStr = Field(max_length=320)


@router.post("/verify/resend", status_code=202)
def verify_resend(body: EmailRequest):
    email = emails.normalize(body.email)
    user = store.user_by_email(email) if emails.valid(email) else None
    if user is not None and user.status == "pending":
        mailer.send_quietly(email, "verify_email", mailer.link("verify.html", issue_token(user.id, "verify_email")))
    return ACCEPTED


@router.post("/password/forgot", status_code=202)
def password_forgot(body: EmailRequest, request: Request):
    """계정이 있든 없든 202. active 계정에만 재설정 메일(30분, 한 번)을 보낸다. 정지된 계정은 받지 않는다."""
    email = emails.normalize(body.email)
    user = store.user_by_email(email) if emails.valid(email) else None
    if user is not None and user.status == "active":
        mailer.send_quietly(email, "reset_password", mailer.link("reset.html", issue_token(user.id, "reset_password")))
        store.record_audit("auth.password_reset_requested", actor_id=user.id, actor_email=email, target_type="user",
                           target_id=user.id, target_label=email, ip=client_ip(request))
    return ACCEPTED


class ResetRequest(BaseModel):
    token: StrictStr = Field(max_length=200)
    password: StrictStr = Field(max_length=1024)


@router.post("/password/reset", status_code=204)
def password_reset(body: ResetRequest, request: Request):
    """재설정(reset_password)과 초대 수락(invite) 공용. 성공하면 그 사용자의 모든 세션을 지우고 잠금을 푼다."""
    if not sessions.plausible_token(body.token):
        raise _token_expired()
    owner = store.token_owner(sessions.token_hash(body.token), ("reset_password", "invite"))
    allowed_status = {"reset_password": "active", "invite": "invited"}
    if owner is None or owner["status"] != allowed_status[owner["purpose"]]:
        raise _token_expired()
    reason = passwords.policy_violation(body.password, owner["email"])
    if reason:
        raise weak_password(reason)          # 토큰은 그대로 두고 다시 입력하게 한다
    password_hash = passwords.hash_password(body.password)
    if not store.consume_token(owner["token_id"]):
        raise _token_expired()
    store.set_password(owner["user_id"], password_hash)
    if owner["purpose"] == "invite":
        store.activate_user(owner["user_id"], "invited")
        action = "auth.invite_accepted"
    else:
        action = "auth.password_reset"
    store.delete_user_sessions(owner["user_id"])
    store.record_audit(action, actor_id=owner["user_id"], actor_email=owner["email"], target_type="user",
                       target_id=owner["user_id"], target_label=owner["email"], ip=client_ip(request))
    return Response(status_code=204)
