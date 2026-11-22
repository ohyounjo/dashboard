"""기본 경로: 상태 확인(로그인 불필요)과 운용 화면의 설정(운용자). contracts/manage.md."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from ...auth.routes import Principal, require_operator
from ...features import manage_features
from .. import context

router = APIRouter()


@router.get("/health")
def get_health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config")
def get_config(principal: Principal = Depends(require_operator)) -> dict[str, Any]:
    """운용 화면이 메뉴를 그리는 데 쓴다. 이 응답이 없으면(관리 서비스가 떠 있지 않으면) 데이터 메뉴를 숨긴다."""
    return {
        "project": context.current_project(),
        "role": principal.role,
        "limits": context.limits().public(),
        "features": sorted(manage_features()),
    }
