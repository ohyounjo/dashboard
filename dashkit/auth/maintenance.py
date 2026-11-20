"""보존 기간 정리(contracts/auth.md "보존 기간"). 인증 경로를 처리하는 중에 하루 한 번 돈다(별도 스케줄러 없음).

- 메일 인증 없이 7일 지난 pending 계정, 7일 지난 테스트 메일(outbox), 만료된 일회용 토큰과 세션
"""
from __future__ import annotations

import logging
import threading
import time

from . import store

log = logging.getLogger("dashkit.auth")

PENDING_DAYS = 7
OUTBOX_DAYS = 7
INTERVAL = 24 * 3600.0

_lock = threading.Lock()
_last: float | None = None


def run() -> dict[str, int]:
    removed = store.cleanup(PENDING_DAYS, OUTBOX_DAYS)
    if any(removed.values()):
        log.info("인증 DB 정리: %s", ", ".join(f"{k} {v}" for k, v in removed.items() if v))
    return removed


def maybe_run(clock=time.monotonic) -> bool:
    """마지막 정리 후 하루가 지났으면 정리한다. 실패해도 요청을 실패시키지 않는다."""
    global _last
    now = clock()
    with _lock:
        if _last is not None and now - _last < INTERVAL:
            return False
        _last = now
    try:
        run()
    except store.Unavailable:
        log.warning("인증 DB 정리를 하지 못함(다음 날 다시 시도)")
    return True


def reset() -> None:
    global _last
    with _lock:
        _last = None
