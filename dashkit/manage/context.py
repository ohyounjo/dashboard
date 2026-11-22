"""관리 서비스의 현재 프로젝트와 한도 값(contracts/manage.md "현재 프로젝트", "한도").

현재 프로젝트는 backend와 같은 규칙이다: `.state/embed.json`의 project(마지막으로 sync에 성공한 프로젝트), 쓸 수 없으면
env PROJECT. 경로에 프로젝트 이름이 없다(설계서 15절 "한 번에 한 프로젝트만").
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

from ..api.state import default_state_dir

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def current_project() -> str:
    try:
        with open(default_state_dir() / "embed.json", encoding="utf-8") as f:
            data = json.load(f)
        name = data.get("project") if isinstance(data, dict) else None
        if isinstance(name, str) and NAME_RE.fullmatch(name):
            return name
    except (OSError, ValueError):
        pass
    return os.environ.get("PROJECT", "")


def _int(name: str, default: int) -> int:
    try:
        return max(int(os.environ.get(name) or default), 0)
    except ValueError:
        return default


@dataclass(frozen=True)
class Limits:
    inline_max_rows: int
    max_upload_mb: int
    max_rows: int
    max_request_mb: int
    compact_hint: int
    statement_timeout_ms: int
    lock_wait_ms: int
    job_lock_wait_ms: int
    export_max_rows: int

    def public(self) -> dict[str, Any]:
        return {
            "inline_max_rows": self.inline_max_rows,
            "max_upload_mb": self.max_upload_mb,
            "max_rows": self.max_rows,
            "max_request_mb": self.max_request_mb,
            "compact_hint": self.compact_hint,
            "page_max": 200,
        }


def limits() -> Limits:
    """환경변수의 한도(설계서 12절, 13절 D8). 값의 기준은 contracts/manage.md "한도"."""
    return Limits(
        inline_max_rows=_int("INGEST_INLINE_MAX_ROWS", 500),
        max_upload_mb=_int("INGEST_MAX_UPLOAD_MB", 200),
        max_rows=_int("INGEST_MAX_ROWS", 2_000_000),
        max_request_mb=_int("INGEST_MAX_REQUEST_MB", 500),
        compact_hint=_int("INGEST_COMPACT_HINT", 200),
        statement_timeout_ms=_int("MANAGE_STATEMENT_TIMEOUT_MS", 15000),
        lock_wait_ms=_int("MANAGE_LOCK_WAIT_MS", 5000),
        job_lock_wait_ms=_int("MANAGE_JOB_LOCK_WAIT_MS", 600000),
        export_max_rows=max(_int("EXPORT_MAX_ROWS", 1_000_000), 1),
    )
