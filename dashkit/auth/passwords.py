"""비밀번호 해시와 정책(contracts/auth.md "비밀번호").

- 저장은 Argon2id(argon2-cffi의 기본 매개변수)뿐이다. 평문과 복호화 가능한 형태는 어디에도 남기지 않는다.
- 매개변수가 바뀌면 다음 로그인 성공 때 needs_rehash()로 알아내 다시 해시한다.
- 없는 계정의 로그인도 verify_dummy()로 해시 검증 한 번을 수행해 응답 시간을 맞춘다.
- 이 모듈의 함수는 비밀번호·해시를 로그나 예외 메시지에 싣지 않는다.
"""
from __future__ import annotations

import secrets
import string
import threading

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 10
MAX_LENGTH = 128
TEMPORARY_LENGTH = 16

_hasher = PasswordHasher()
_dummy_lock = threading.Lock()
_dummy_hash: str | None = None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except (InvalidHashError, ValueError):
        return True


def verify_dummy(password: str) -> None:
    """없는 계정(또는 비밀번호가 없는 계정)에도 같은 비용의 검증을 한 번 수행한다. 결과는 버린다."""
    global _dummy_hash
    with _dummy_lock:
        if _dummy_hash is None:
            _dummy_hash = _hasher.hash(secrets.token_urlsafe(24))
        stored = _dummy_hash
    verify(stored, password)


def policy_violation(password: str, email: str) -> str | None:
    """정책 위반 사유 코드. 통과하면 None.

    too_short(10자 미만) · too_long(128자 초과) · same_as_email(이메일의 @ 앞부분과 같음, 대소문자 무시)
    """
    if len(password) < MIN_LENGTH:
        return "too_short"
    if len(password) > MAX_LENGTH:
        return "too_long"
    local = email.rpartition("@")[0].strip().lower()
    if local and password.strip().lower() == local:
        return "same_as_email"
    return None


def temporary_password() -> str:
    """임시 비밀번호(16자, 영문 대소문자·숫자). 응답에 한 번만 싣고 어디에도 기록하지 않는다."""
    alphabet = string.ascii_letters + string.digits
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(TEMPORARY_LENGTH))
        if any(c.isdigit() for c in pw) and any(c.isalpha() for c in pw):
            return pw
