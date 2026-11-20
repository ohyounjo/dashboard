"""로그인 실패 잠금과 IP별 요청 제한(contracts/auth.md "잠금과 요청 제한"). backend가 프로세스 하나이므로 메모리에 센다.

- IP별: 인증 경로(/api/auth/*)의 POST는 IP당 1분에 20번. 넘으면 429 rate_limited. IP는 nginx가 넣는 X-Real-IP.
- 없는 계정의 잠금 흉내: 계정이 있는 주소는 DB(failed_count, locked_until)로 잠근다. 없는 주소도 같은 횟수에서 같은
  423을 주도록 여기서 센다(잠금 응답으로 계정 존재를 알아낼 수 없게).
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

LOGIN_FAILURE_LIMIT = 5
LOCK_MINUTES = 15
RATE_LIMIT = 20
RATE_WINDOW = 60.0
_MAX_KEYS = 20000


class RateLimiter:
    """키(IP)마다 최근 window초 안의 요청 수를 센다."""

    def __init__(self, limit: int = RATE_LIMIT, window: float = RATE_WINDOW, clock=time.monotonic):
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        """한 번 센다. 한도 안이면 True."""
        now = self._clock()
        with self._lock:
            q = self._hits.pop(key, None) or deque()
            while q and q[0] <= now - self.window:
                q.popleft()
            allowed = len(q) < self.limit
            if allowed:
                q.append(now)
            self._hits[key] = q
            while len(self._hits) > _MAX_KEYS:
                self._hits.popitem(last=False)
            return allowed

    def retry_after(self, key: str) -> int:
        with self._lock:
            q = self._hits.get(key)
            if not q:
                return 0
            return max(1, int(q[0] + self.window - self._clock()) + 1)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


class GhostLocks:
    """없는 계정 주소의 실패 횟수와 잠금(메모리)."""

    def __init__(self, limit: int = LOGIN_FAILURE_LIMIT, minutes: int = LOCK_MINUTES, clock=time.monotonic):
        self.limit = limit
        self.seconds = minutes * 60
        self._clock = clock
        self._state: OrderedDict[str, tuple[int, float]] = OrderedDict()   # 주소 → (실패 횟수, 잠금 끝)
        self._lock = threading.Lock()

    def locked(self, email: str) -> bool:
        with self._lock:
            count, until = self._state.get(email, (0, 0.0))
            return until > self._clock()

    def fail(self, email: str) -> bool:
        """실패를 하나 센다. 이번 실패로 잠기면 True."""
        now = self._clock()
        with self._lock:
            count, until = self._state.pop(email, (0, 0.0))
            count += 1
            just = count >= self.limit
            self._state[email] = (0, now + self.seconds) if just else (count, until)
            while len(self._state) > _MAX_KEYS:
                self._state.popitem(last=False)
            return just

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


auth_rate = RateLimiter()
ghosts = GhostLocks()
