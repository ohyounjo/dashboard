"""관리 서비스 앱. 계약: contracts/manage.md.

uvicorn dashkit.manage.app:app 으로 띄운다(`python -m dashkit manage`). 모든 경로는 /api/manage 아래다.
- 로그인 확인은 dashkit/auth의 require_operator·require_admin을 그대로 쓴다(세션 쿠키는 웹앱과 같은 출처라 그대로 온다).
- 상태를 바꾸는 요청은 dashkit/auth/guard.py가 출처와 본문 형식을 먼저 검사한다(backend와 같다).
- 응답에 Cache-Control: no-store를 붙인다. 오류 형식은 {"detail", "message"}(contracts/api.md).
"""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..api.errors import ApiError
from ..auth import sessions as auth_sessions
from ..auth import settings as auth_settings
from ..auth import store as auth_store
from ..auth.guard import UNSAFE_METHODS, Guard, check, send_rejection
from ..ingest.operation import IngestError
from ..project import upload
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
    from ..ingest.structure import recover_before_read
    from .context import current_project

    name = current_project()
    try:
        # 끝나지 않은 구조 변경을 설정을 읽기 전에 먼저 정리하고, 정리된 설정으로 나머지 기록을 복구한다
        outcomes = recover_before_read(name)
        outcomes += recover_on_start(config.load_project(name))
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


@api.exception_handler(upload.UploadError)
async def _upload_error(_req: Request, exc: upload.UploadError):
    return _error(exc.status, exc.detail, exc.message)


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


def _has_session_cookie(cookie_headers: list[str]) -> bool:
    for raw in cookie_headers:
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            continue
        morsel = jar.get(auth_sessions.COOKIE_NAME)
        if morsel is not None and morsel.value != "":
            return True
    return False


class ManageGuard(Guard):
    """backend와 같은 요청 보호.

    - Authorization 헤더가 2개 이상이면 400 bad_request.
    - Authorization 헤더와 dashkit_session 쿠키가 함께 오면(모든 메서드·경로) 400 ambiguous_auth.
    - 업로드 경로(UPLOAD_PATHS)의 POST만 multipart/form-data 본문도 받는다(출처 검사는 그대로 한다).
    - 쿠키 없이 Bearer 토큰으로 호출한 POST /api/manage/tables/{t}/batches만 Origin/Sec-Fetch-Site 검사를 건너뛴다
      (Content-Type 검사는 유지). contracts/manage.md "요청 보호".
    """

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw_headers = list(scope.get("headers") or [])
        auth_values: list[str] = []
        cookie_values: list[str] = []
        for k, v in raw_headers:
            name = k.decode("latin-1").lower()
            val = v.decode("latin-1")
            if name == "authorization":
                auth_values.append(val)
            elif name == "cookie":
                cookie_values.append(val)
        if len(auth_values) > 1:
            await send_rejection(send, (400, "bad_request", "Authorization 헤더가 여러 개입니다"))
            return
        has_auth = bool(auth_values and auth_values[0].strip())
        has_cookie = _has_session_cookie(cookie_values)
        if has_auth and has_cookie:
            await send_rejection(send, (400, "ambiguous_auth", "세션 쿠키와 API 토큰을 함께 보낼 수 없습니다"))
            return

        method = scope.get("method", "").upper()
        path = scope.get("path", "")
        if method == "POST" and has_auth and not has_cookie \
                and auth_values[0].strip().lower().startswith("bearer ") \
                and TOKEN_INGEST_PATH.fullmatch(path):
            ctypes = [v.decode("latin-1") for k, v in raw_headers if k.decode("latin-1").lower() == "content-type"]
            if len(ctypes) > 1:
                await send_rejection(send, (400, "bad_request", "Origin 또는 Content-Type 헤더가 여러 개입니다"))
                return
            ctype = (ctypes[0].split(";", 1)[0].strip().lower()) if ctypes else ""
            if ctype not in ("application/json", "multipart/form-data"):
                await send_rejection(
                    send, (415, "unsupported_media_type", "Content-Type은 application/json 또는 multipart/form-data여야 합니다")
                )
                return
            await self.app(scope, receive, send)
            return

        if method == "POST" and UPLOAD_PATHS.fullmatch(path):
            ctypes = [v for k, v in raw_headers if k.lower() == b"content-type"]
            if len(ctypes) == 1 and ctypes[0].split(b";", 1)[0].strip().lower() == b"multipart/form-data":
                # 본문 형식 검사만 multipart로 통과시킨다. 원래 헤더는 앱에 그대로 넘긴다
                checked = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw_headers}
                checked["content-type"] = "application/json"
                rejected = check("POST", checked, self.origins)
                if rejected is None:
                    await self.app(scope, receive, send)
                else:
                    await send_rejection(send, rejected)
                return
        await super().__call__(scope, receive, send)


TOKEN_INGEST_PATH = re.compile(r"/api/manage/tables/[a-z][a-z0-9_]{0,62}/batches")
UPLOAD_PATHS = re.compile(r"/api/manage/(?:tables/[a-z][a-z0-9_]{0,62}/batches|batches|tables/infer)")

# 요청 보호를 씌운 ASGI 앱. uvicorn은 이것을 띄운다
app = ManageGuard(api)
