"""관리 서비스 선택 기능 플래그(`MANAGE_FEATURES`).

표준 라이브러리만 사용하는 소형 공용 모듈로, `backend`와 `manager`가 이미지 의존성 결합 없이 함께 import한다.
지원 기능: `export`, `profile`, `freshness`, `sql`, `tokens` (contracts/manage.md, 설계서 10-6절).
"""
from __future__ import annotations

import os

KNOWN_MANAGE_FEATURES: frozenset[str] = frozenset({"export", "profile", "freshness", "sql", "tokens"})


def manage_features() -> frozenset[str]:
    """환경 변수 `MANAGE_FEATURES`(쉼표 구분)에서 알려진 기능 집합을 파싱한다. 알 수 없는 토큰은 무시한다."""
    raw = os.environ.get("MANAGE_FEATURES", "")
    tokens = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return frozenset(tokens & KNOWN_MANAGE_FEATURES)


def has_manage_feature(name: str) -> bool:
    """선택 기능 `name`이 현재 활성화돼 있는지 반환한다."""
    return name in manage_features()


has_feature = has_manage_feature
