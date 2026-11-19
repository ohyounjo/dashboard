"""sync 패키지 공통 예외. client와 sync 본체가 함께 쓴다."""
from __future__ import annotations


class SyncError(Exception):
    """sync 실패. 메시지 하나에 원인과 다음 할 일을 담는다."""
