"""자동 적재용 API 토큰 관리 경로(admin 세션 전용, MANAGE_FEATURES=tokens). contracts/manage.md."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from ...api.errors import ApiError
from ...auth import sessions
from ...auth import store as auth_store
from ...auth.routes import Principal, client_ip, require_admin
from ...features import has_manage_feature
from ...ingest import IngestError
from ..auth import new_api_token
from .tables import _json, _project

router = APIRouter()

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_TTL = timedelta(days=365)


def _require_tokens_feature() -> None:
    if not has_manage_feature("tokens"):
        raise ApiError(404, "not_found", "비활성화된 기능입니다")


def _parse_expires_at(raw: Any, now: datetime) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise IngestError(422, "invalid_request", "만료일(expires_at)을 입력해 주세요")
    val = raw.strip()
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    if _DATE_RE.fullmatch(val):
        try:
            d = date.fromisoformat(val)
        except ValueError:
            raise IngestError(422, "invalid_request", "만료일 형식이 올바르지 않습니다") from None
        dt = datetime.combine(d, time(23, 59, 59), tzinfo=local_tz).astimezone(timezone.utc)
    else:
        try:
            dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
        except ValueError:
            raise IngestError(422, "invalid_request", "만료일 형식이 올바르지 않습니다") from None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=local_tz)
        dt = dt.astimezone(timezone.utc)
    if dt <= now:
        raise IngestError(422, "invalid_request", "만료일은 현재 시각보다 뒤여야 합니다")
    if dt > now + _MAX_TTL:
        raise IngestError(422, "invalid_request", "만료일은 최대 365일 이내여야 합니다")
    return dt


def _parse_tables(raw: Any, project_tables: dict[str, Any]) -> list[str]:
    if not isinstance(raw, list) or not raw or not all(isinstance(t, str) and t.strip() for t in raw):
        raise IngestError(422, "invalid_request", "허용 테이블(tables)을 하나 이상 고르세요")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = item.strip()
        if name in seen:
            continue
        seen.add(name)
        spec = project_tables.get(name)
        if spec is None:
            raise IngestError(422, "unknown_table", f"schema.yaml에 없는 테이블입니다: {name}")
        mode = spec.get("mode") or "replace"
        if mode != "append":
            raise IngestError(422, "not_append", f"append 테이블에만 API 토큰을 발급할 수 있습니다: {name}")
        out.append(name)
    return sorted(out)


@router.get("/tokens")
def get_tokens(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    _require_tokens_feature()
    project = _project()
    now = auth_store.now()
    items = [t.public(now) for t in auth_store.list_api_tokens(project.name)]
    return {"items": items, "total": len(items)}


@router.post("/tokens")
async def post_tokens(request: Request, principal: Principal = Depends(require_admin)) -> JSONResponse:
    _require_tokens_feature()
    project = _project()
    body = await _json(request)
    raw_name = body.get("name")
    if not isinstance(raw_name, str) or not (1 <= len(raw_name.strip()) <= 100):
        raise IngestError(422, "invalid_request", "토큰 이름은 1~100자여야 합니다")
    name = raw_name.strip()
    tables = _parse_tables(body.get("tables"), project.schema["tables"])
    now = auth_store.now()
    expires_at = _parse_expires_at(body.get("expires_at"), now)

    raw_token = new_api_token()
    tok = auth_store.create_api_token(
        name,
        project.name,
        sessions.token_hash(raw_token),
        tables,
        expires_at,
        created_by=principal.id,
    )
    try:
        auth_store.record_audit(
            "data.token.create",
            actor_id=principal.id,
            actor_email=principal.email,
            target_type="api_token",
            target_id=tok.id,
            target_label=tok.name,
            detail={"project": project.name, "tables": tables, "expires_at": expires_at.isoformat()},
            ip=client_ip(request),
        )
    except Exception:
        pass
    return JSONResponse(
        {
            "id": tok.id,
            "name": tok.name,
            "project": tok.project,
            "tables": list(tok.tables),
            "created_at": tok.created_at.isoformat(),
            "expires_at": tok.expires_at.isoformat(),
            "token": raw_token,
        },
        status_code=201,
    )


@router.delete("/tokens/{token_id}")
def delete_token(token_id: str, request: Request, principal: Principal = Depends(require_admin)) -> Response:
    _require_tokens_feature()
    project = _project()
    tok, newly_revoked = auth_store.revoke_api_token(token_id, revoked_by=principal.id, project=project.name)
    if tok is None:
        raise IngestError(404, "unknown_token", "없는 API 토큰입니다")
    if newly_revoked:
        try:
            auth_store.record_audit(
                "data.token.revoke",
                actor_id=principal.id,
                actor_email=principal.email,
                target_type="api_token",
                target_id=tok.id,
                target_label=tok.name,
                detail={"project": tok.project, "tables": list(tok.tables)},
                ip=client_ip(request),
            )
        except Exception:
            pass
    return Response(status_code=204)
