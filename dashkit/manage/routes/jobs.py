"""작업: 오래 걸리는 일(파일 적재, 다시 만들기)의 상태·진행률·로그·결과(contracts/manage.md "작업")."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from ...auth.routes import Principal, require_operator
from ...ingest import IngestError
from ..runs_state import runs

router = APIRouter()


@router.get("/jobs")
def get_jobs(principal: Principal = Depends(require_operator)) -> dict[str, Any]:
    items = [t.public() for t in runs.list()]
    for item in items:
        item.pop("log", None)
    return {"items": items, "total": len(items)}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, principal: Principal = Depends(require_operator)) -> dict[str, Any]:
    run = runs.get(job_id)
    if run is None:
        raise IngestError(404, "unknown_job", "없는 작업입니다(관리 서비스가 다시 떴으면 지난 작업은 감사 로그에 있습니다)")
    return run.public()


@router.get("/jobs/{job_id}/rejects")
def get_rejects(job_id: str, principal: Principal = Depends(require_operator)):
    """오류 행을 빼고 넣은 작업의 빠진 행(CSV. 여러 파일이면 zip): 올린 행 + `오류` 열. 고쳐서 그 파일만 다시 올린다."""
    run = runs.get(job_id)
    if run is None or run.rejects is None or not run.rejects.is_file():
        raise IngestError(404, "unknown_job", "빠진 행 파일이 없습니다")
    zipped = run.rejects.suffix == ".zip"
    return FileResponse(run.rejects, media_type="application/zip" if zipped else "text/csv; charset=utf-8",
                        filename=f"rejects-{run.id}.{'zip' if zipped else 'csv'}")
