"""이메일 주소 정규화와 형식 검사. 주소는 소문자로 저장하고 비교한다."""
from __future__ import annotations

import re

MAX_LENGTH = 254
_EMAIL_RE = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+")


def normalize(raw: str) -> str:
    return raw.strip().lower()


def valid(email: str) -> bool:
    """정규화한 주소의 형식이 맞는지. 국제화 주소와 따옴표 주소는 받지 않는다."""
    if not email or len(email) > MAX_LENGTH:
        return False
    if not _EMAIL_RE.fullmatch(email):
        return False
    local = email.partition("@")[0]
    return len(local) <= 64 and not local.startswith(".") and not local.endswith(".") and ".." not in local
