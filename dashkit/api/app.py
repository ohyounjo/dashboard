"""백엔드 API. 계약: contracts/api.md.

uvicorn dashkit.api.app:app 으로 띄운다. 설정은 시작 때 env PROJECT로 읽고, 그 뒤로는 요청마다 설정 파일의
서명(mtime, 크기, inode)을 확인해 바뀌었을 때만 다시 읽는다(state.ProjectState). 다시 읽은 설정이 검증에 실패하면
마지막으로 유효했던 설정을 계속 쓴다.
backend는 Superset 계정을 갖지 않고 Superset에 요청하지 않는다(guest token은 직접 서명).
"""
from __future__ import annotations

import copy
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import jwt
import psycopg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictStr
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import config, widgets
from . import data
from .state import EmbedState, ProjectState, default_state_dir

log = logging.getLogger("dashkit.api")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

TOKEN_LIFETIME = 300
GUEST_USER = {"username": "embed-viewer", "first_name": "Embed", "last_name": "Viewer"}


class ApiError(Exception):
    def __init__(self, status: int, detail: str, message: str):
        self.status = status
        self.detail = detail
        self.message = message


class _Ctx:
    project_state: ProjectState
    embed_state: EmbedState

    @property
    def project(self) -> config.Project:
        """지금 유효한 설정. 요청 하나 안에서는 한 번만 읽어 지역 변수로 쓴다(도중에 설정이 바뀌어도 응답이 섞이지 않게)."""
        return self.project_state.project()


ctx = _Ctx()


def _load() -> None:
    name = os.environ.get("PROJECT", "")
    try:
        state = ProjectState(name)
    except config.ConfigError as exc:
        log.error("설정 오류로 시작할 수 없습니다 (PROJECT=%s, 오류 %d개):", name, len(exc.errors))
        for e in exc.errors:
            log.error("  %s", e)
        raise
    ctx.project_state = state
    project = state.project()
    ctx.embed_state = EmbedState(default_state_dir() / "embed.json", project.name)
    if not os.environ.get("GUEST_TOKEN_JWT_SECRET"):
        log.warning("GUEST_TOKEN_JWT_SECRET이 없음. /api/guest-token은 500을 돌려줍니다")
    if not os.environ.get("GUEST_TOKEN_JWT_AUDIENCE"):
        log.warning("GUEST_TOKEN_JWT_AUDIENCE가 없음. /api/guest-token은 500을 돌려줍니다")
    log.info("프로젝트 '%s'를 읽음. 위젯 데이터셋 %d개", project.name, len(config.widget_datasets(project)))
    ctx.embed_state.embeds()  # 시작 시 상태를 한 번 로그에 남긴다


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _load()
    yield


# openapi_url=None이면 스키마·대화형 API 안내 페이지 경로도 만들어지지 않는다(모든 경로가 계약 형식의 404)
app = FastAPI(title="dashkit", openapi_url=None, lifespan=lifespan)


# ---------------------------------------------------------------- 공통 응답

def _error(status: int, detail: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": detail, "message": message}, status_code=status)


@app.middleware("http")
async def _no_store(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(ApiError)
async def _api_error(_req: Request, exc: ApiError):
    return _error(exc.status, exc.detail, exc.message)


@app.exception_handler(RequestValidationError)
async def _validation_error(_req: Request, exc: RequestValidationError):
    return _error(422, "invalid_request", "요청 형식이 올바르지 않습니다")


_HTTP_DETAILS = {404: "not_found", 405: "method_not_allowed"}


@app.exception_handler(StarletteHTTPException)
async def _http_error(_req: Request, exc: StarletteHTTPException):
    detail = _HTTP_DETAILS.get(exc.status_code, "http_error")
    resp = _error(exc.status_code, detail, str(exc.detail))
    if exc.headers:
        for k, v in exc.headers.items():
            resp.headers[k] = v
    return resp


@app.exception_handler(Exception)
async def _unhandled(_req: Request, exc: Exception):
    log.error("처리하지 못한 오류: %s", type(exc).__name__)
    # 미들웨어 밖에서 만들어지는 응답이라 헤더를 직접 붙인다
    resp = _error(500, "internal_error", "서버 오류")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- 도우미

def _sections_with_embeds(project: config.Project, embeds: dict[str, str]) -> tuple[list[dict], bool]:
    tabs = copy.deepcopy(project.dashboard["tabs"])
    applied = True
    for tab in tabs:
        for sec in tab["sections"]:
            if sec["type"] == "superset":
                sec["embed_id"] = embeds.get(sec["dashboard"])
                if sec["embed_id"] is None:
                    applied = False
    return tabs, applied


def _applied(project: config.Project, embeds: dict[str, str]) -> bool:
    return all(
        sec["dashboard"] in embeds
        for tab in project.dashboard["tabs"]
        for sec in tab["sections"]
        if sec["type"] == "superset"
    )


# ---------------------------------------------------------------- 엔드포인트

@app.get("/api/app")
def get_app():
    project = ctx.project
    embeds = ctx.embed_state.embeds()
    tabs, applied = _sections_with_embeds(project, embeds)
    app_cfg = project.dashboard["app"]
    return {
        "project": project.name,
        "app": {
            "title": app_cfg["title"],
            "logo_text": app_cfg.get("logo_text"),
            "as_of": config.resolve_as_of(project).isoformat(),
            "theme": app_cfg.get("theme"),
        },
        "superset": {"url": os.environ.get("SUPERSET_PUBLIC_URL") or None},
        "applied": applied,
        # 셸이 import()를 허용할 위젯 유형(매니페스트에서 읽은 목록)
        "widgets": widgets.type_list(),
        "tabs": tabs,
    }


class GuestTokenRequest(BaseModel):
    dashboard: StrictStr


@app.post("/api/guest-token")
def post_guest_token(body: GuestTokenRequest):
    key = body.dashboard
    if key not in ctx.project.dashboard["dashboards"]:
        raise ApiError(404, "unknown_dashboard", "설정에 없는 대시보드입니다")
    embed_id = ctx.embed_state.embeds().get(key)
    if not embed_id:
        raise ApiError(409, "not_applied", "대시보드가 아직 적용되지 않았습니다(make apply)")
    secret = os.environ.get("GUEST_TOKEN_JWT_SECRET")
    audience = os.environ.get("GUEST_TOKEN_JWT_AUDIENCE")
    if not secret or not audience:
        raise ApiError(500, "token_unavailable", "guest token 서명 설정이 없습니다")
    iat = int(time.time())
    exp = iat + TOKEN_LIFETIME
    claims = {
        "user": dict(GUEST_USER),
        "resources": [{"type": "dashboard", "id": embed_id}],
        "rls_rules": [],
        "iat": iat,
        "exp": exp,
        "aud": audience,
        "type": "guest",
    }
    token = jwt.encode(claims, secret, algorithm="HS256")
    expires_at = datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"token": token, "expires_at": expires_at}


@app.get("/api/data/{dataset}")
def get_data(dataset: str):
    allowed = config.widget_datasets(ctx.project)
    if dataset not in allowed:
        raise ApiError(404, "unknown_dataset", "위젯이 참조하지 않는 데이터셋입니다")
    # 이후에는 허용 목록 쪽 문자열만 SQL에 넣는다
    name = next(d for d in allowed if d == dataset)
    try:
        return data.fetch(name)
    except psycopg.errors.UndefinedTable:
        log.warning("뷰 dash.%s가 없음(적재 전)", name)
        raise ApiError(503, "not_loaded", "데이터가 아직 적재되지 않았습니다(make load)")
    except (psycopg.Error, RuntimeError) as exc:
        log.warning("데이터 DB 오류(%s): %s", name, type(exc).__name__)
        raise ApiError(503, "db_unavailable", "데이터 DB에 접속할 수 없습니다")


@app.get("/api/health")
def get_health():
    ok = data.ping()
    project = ctx.project
    status = ctx.project_state.status()
    body = {
        "status": "ok" if ok else "degraded",
        "project": project.name,
        "db": ok,
        "applied": _applied(project, ctx.embed_state.embeds()),
        # 설정 파일이 바뀌었는데 검증에 실패해 이전 설정을 쓰는 중이면 valid가 false다(오류 내용은 backend 로그)
        "config": {"valid": status["valid"], "errors": status["errors"]},
    }
    if not ok:
        log.warning("health: 데이터 DB SELECT 1 실패")
    return JSONResponse(body, status_code=200 if ok else 503)
