"""API 오류. 본문은 {"detail": "<영문 코드>", "message": "<한국어 설명>"}(contracts/api.md).

extra가 있으면 본문에 그 키를 더한다(예: 비밀번호 정책 위반의 reason). detail·message는 덮어쓰지 않는다.
"""
from __future__ import annotations

from typing import Any


class ApiError(Exception):
    def __init__(self, status: int, detail: str, message: str, headers: dict[str, str] | None = None,
                 extra: dict[str, Any] | None = None):
        self.status = status
        self.detail = detail
        self.message = message
        self.headers = headers or {}
        self.extra = {k: v for k, v in (extra or {}).items() if k not in ("detail", "message")}
