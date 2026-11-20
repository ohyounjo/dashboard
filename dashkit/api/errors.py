"""API 오류. 본문은 {"detail": "<영문 코드>", "message": "<한국어 설명>"}(contracts/api.md)."""
from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, detail: str, message: str, headers: dict[str, str] | None = None):
        self.status = status
        self.detail = detail
        self.message = message
        self.headers = headers or {}
