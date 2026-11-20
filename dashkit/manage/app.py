"""관리 서비스 앱. 계약: contracts/manage.md.

uvicorn dashkit.manage.app:app 으로 띄운다(`python -m dashkit manage`). 모든 경로는 /api/manage 아래다.
- 로그인 확인은 dashkit/auth의 require_operator·require_admin을 그대로 쓴다(세션 쿠키는 웹앱과 같은 출처라 그대로 온다).
- 상태를 바꾸는 요청은 dashkit/auth/guard.py가 출처와 본문 형식을 먼저 검사한다(backend와 같다).
- 응답에 Cache-Control: no-store를 붙인다. 오류 형식은 {"detail", "message"}(contracts/api.md).
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..api.errors import ApiError
from ..auth import settings as auth_settings
from ..auth import store as auth_store
from ..auth.guard import Guard
from ..ingest.operation import IngestError
from .routes import MODULES

log = logging.getLogger("dashkit.manage")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

PREFIX = "/api/manage"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    auth_settings.reset_cache()
    try:
        auth_settings.load()
    except auth_settings.AuthConfigError as exc:
        log.error("인증 설정 오류로 시작할 수 없습니다: %s", exc)
        raise
    _recover_on_start()
    yield
    auth_store.reset_pool()


def _recover_on_start() -> None:
    """시작할 때 이 프로젝트의 끊긴 폴더 작업을 처리한다(contracts/ingest.md "묶음 기록과 끊김 복구"). 실패해도 뜬다."""
    from .. import config
    from ..ingest.operation import recover_on_start
    from .context import current_project

    name = current_project()
    try:
        outcomes = recover_on_start(config.load_project(name))
    except Exception as exc:     # DB가 아직 없거나 설정이 깨졌어도 서비스는 뜬다. 다음 작업이 다시 시도한다
        log.warning("시작 때 복구를 건너뜀(%s): %s", name, type(exc).__name__)
        return
    for o in outcomes:
        log.info("끊긴 묶음 %s(%s): %s", o.id, o.kind, o.result)


api = FastAPI(title="dashkit manage", openapi_url=None, lifespan=lifespan)
for module in MODULES:
    api.include_router(module.router, prefix=PREFIX)


def _error(status: int, detail: str, message: str, **extra) -> JSONResponse:
    return JSONResponse({"detail": detail, "message": message, **extra}, status_code=status)


@api.middleware("http")
async def _no_store(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@api.exception_handler(ApiError)
async def _api_error(_req: Request, exc: ApiError):
    resp = _error(exc.status, exc.detail, exc.message, **exc.extra)
    for k, v in exc.headers.items():
        resp.headers[k] = v
    return resp


@api.exception_handler(IngestError)
async def _ingest_error(_req: Request, exc: IngestError):
    extra = dict(exc.extra)
    if exc.errors is not None:
        extra["errors"] = exc.errors
    return _error(exc.status, exc.detail, exc.message, **extra)


@api.exception_handler(auth_store.Unavailable)
async def _auth_unavailable(_req: Request, _exc: auth_store.Unavailable):
    return _error(503, "auth_unavailable", "인증 DB를 쓸 수 없습니다")


@api.exception_handler(RequestValidationError)
async def _validation_error(_req: Request, _exc: RequestValidationError):
    return _error(422, "invalid_request", "요청 형식이 올바르지 않습니다")


_HTTP_DETAILS = {404: "not_found", 405: "method_not_allowed"}


@api.exception_handler(StarletteHTTPException)
async def _http_error(_req: Request, exc: StarletteHTTPException):
    return _error(exc.status_code, _HTTP_DETAILS.get(exc.status_code, "http_error"), str(exc.detail))


@api.exception_handler(Exception)
async def _unhandled(_req: Request, exc: Exception):
    log.error("처리하지 못한 오류: %s", type(exc).__name__, exc_info=exc)
    return _error(500, "internal_error", "서버 오류")


# 요청 보호를 씌운 ASGI 앱. uvicorn은 이것을 띄운다
app = Guard(api)
