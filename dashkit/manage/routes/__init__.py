"""경로 모듈 목록. 모듈은 `router`(FastAPI APIRouter, prefix 없이 /api/manage 아래의 경로)를 내보낸다.

기능을 더하는 일은 모듈 하나와 이 목록 한 줄이다(설계서 9-4와 같은 방식).
"""
from __future__ import annotations

from . import base

MODULES = [base]
