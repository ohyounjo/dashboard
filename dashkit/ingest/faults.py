"""테스트 전용 실패 주입 자리(설계서 "죽인 뒤 복구" 테스트). 운영 코드는 FAULTS를 비워 둔다.

자리 이름 → 부를 함수. 자리:
- after_record: 묶음 기록을 쓴 뒤 / after_move: 파일 하나를 옮긴 뒤 / before_commit: 배치 기록까지 쓰고 커밋 전 /
  after_commit: 커밋 뒤, 묶음 기록을 지우기 전 (operation.finish)
- recover_before_commit: 복구의 반영을 커밋하기 직전(recover._apply_group)
"""
from __future__ import annotations

from typing import Callable


class SimulatedCrash(BaseException):
    """프로세스가 그 자리에서 죽은 것처럼 뒷정리 없이 빠져나간다."""


FAULTS: dict[str, Callable[[], None]] = {}


def fault(point: str) -> None:
    fn = FAULTS.get(point)
    if fn is not None:
        fn()
