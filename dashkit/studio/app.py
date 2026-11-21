"""Studio API. 계약: contracts/studio.md.

`python -m dashkit studio`(uvicorn dashkit.studio.app:app)로 띄운다. `app`은 요청 보호(guard.Guard)를 씌운 ASGI 앱이다.
화면 정적 파일은 STUDIO_WEB_DIR(기본 저장소의 web/studio)에서 `/`(index.html)와 `/studio/static/…`으로 내보낸다.
운용 화면과 함께 쓰는 화면 모듈은 STUDIO_SHARED_DIR(기본 저장소의 web/shared)에서 `/studio/shared/…`로 내보낸다.
화면 파일이 없어도 API는 뜬다.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import charts, config, plan, scaffold, widgets
from ..api.state import file_signature
from ..loader import current
from . import jobs, ops, store, upload
from .guard import DEFAULT_WEB_URL, Guard, studio_origin
from .store import StoreError

log = logging.getLogger("dashkit.studio")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

MAX_JSON_BYTES = 5 * 1024 * 1024
PREFIX = "/studio/api"

# openapi_url=None: 스키마·대화형 API 안내 경로를 만들지 않는다
api = FastAPI(title="dashkit studio", openapi_url=None)
runner = jobs.ApplyRunner()


def web_dir() -> Path:
    env = os.environ.get("STUDIO_WEB_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[2] / "web" / "studio"


# ---------------------------------------------------------------- 공통 응답

def _error(status: int, detail: str, message: str, errors: list[str] | None = None, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"detail": detail, "message": message}
    if errors is not None:
        body["errors"] = errors
    body.update(extra)
    return JSONResponse(body, status_code=status)


@api.exception_handler(StoreError)
async def _store_error(_req: Request, exc: StoreError):
    return _error(exc.status, exc.detail, exc.message, exc.errors)


@api.exception_handler(upload.UploadError)
async def _upload_error(_req: Request, exc: upload.UploadError):
    return _error(exc.status, exc.detail, exc.message)


@api.exception_handler(ops.OpError)
async def _op_error(_req: Request, exc: ops.OpError):
    return _error(400, "invalid_op", str(exc), [str(exc)], op_index=exc.index)


_HTTP_DETAILS = {404: "not_found", 405: "method_not_allowed"}


@api.exception_handler(StarletteHTTPException)
async def _http_error(_req: Request, exc: StarletteHTTPException):
    resp = _error(exc.status_code, _HTTP_DETAILS.get(exc.status_code, "http_error"), str(exc.detail))
    for k, v in (exc.headers or {}).items():
        resp.headers[k] = v
    return resp


@api.exception_handler(Exception)
async def _unhandled(_req: Request, exc: Exception):
    log.error("처리하지 못한 오류: %s", type(exc).__name__, exc_info=exc)
    return _error(500, "internal_error", "서버 오류")


# ---------------------------------------------------------------- 요청 읽기

async def _json_body(request: Request, *, optional: bool = False) -> dict[str, Any]:
    """JSON 객체 본문. Content-Type이 application/json이 아니면 415(optional이면 본문 없는 요청도 받는다)."""
    ctype = request.headers.get("content-type")
    media = (ctype or "").split(";", 1)[0].strip().lower()
    if ctype is None and optional:
        raw = await upload.read_body(request.stream(), request.headers.get("content-length"), MAX_JSON_BYTES)
        if raw.strip():
            raise StoreError(415, "unsupported_media_type", "Content-Type은 application/json이어야 합니다")
        return {}
    if media != "application/json":
        raise StoreError(415, "unsupported_media_type", "Content-Type은 application/json이어야 합니다")
    raw = await upload.read_body(request.stream(), request.headers.get("content-length"), MAX_JSON_BYTES)
    if optional and not raw.strip():
        return {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise StoreError(400, "invalid_request", "본문이 JSON이 아닙니다") from None
    if not isinstance(body, dict):
        raise StoreError(400, "invalid_request", "본문은 JSON 객체여야 합니다")
    return body


async def _csv_upload(request: Request, field: str, *, single: bool = False) -> dict[str, bytes]:
    ctype = request.headers.get("content-type")
    if (ctype or "").split(";", 1)[0].strip().lower() != "multipart/form-data":
        raise upload.UploadError(415, "unsupported_media_type", "업로드는 multipart/form-data로 보냅니다")
    raw = await upload.read_body(request.stream(), request.headers.get("content-length"), upload.max_request_bytes())
    return upload.csv_files(upload.parse_multipart(ctype, raw), field, single=single)


class _Busy:
    """프로젝트 잠금을 잡는다. apply가 돌고 있어 잡지 못하면 409."""

    def __init__(self, name: str):
        self.lock = store.project_lock(name)

    def __enter__(self) -> None:
        if not self.lock.acquire(timeout=10):
            raise StoreError(409, "apply_running", "이 프로젝트에 적용(apply)이 실행 중입니다. 끝난 뒤 다시 시도하세요")

    def __exit__(self, *_exc: Any) -> None:
        self.lock.release()


# ---------------------------------------------------------------- 기본

@api.get(PREFIX + "/health")
def get_health():
    return {"status": "ok"}


_listing_cache: dict[str, tuple[tuple, dict[str, Any]]] = {}
_listing_lock = threading.Lock()


def _listing_row(name: str) -> dict[str, Any]:
    """목록의 한 줄. 설정 파일이 바뀌지 않았으면 지난 결과를 쓴다(프로젝트마다 검증을 되풀이하지 않게)."""
    root = store.projects_dir() / name
    sig = tuple(file_signature(root / f) for f in store.CONFIG_FILES)
    with _listing_lock:
        cached = _listing_cache.get(name)
        if cached is not None and cached[0] == sig:
            return cached[1]
    out = store.listing_row(name)
    with _listing_lock:
        _listing_cache[name] = (sig, out)
    return out


@api.get(PREFIX + "/projects")
def get_projects():
    return {
        "projects": [_listing_row(name) for name in store.project_names()],
        "current": store.current_project(),
        "web_url": os.environ.get("WEB_PUBLIC_URL") or DEFAULT_WEB_URL,
        # 미리보기(웹앱 iframe)는 이 주소로 연 Studio에서만 뜬다. 화면이 다른 주소로 열렸으면 이 주소를 안내한다
        "studio_url": studio_origin(),
    }


@api.get(PREFIX + "/changes")
def get_changes():
    return {"files": store.changes.list()}


# ---------------------------------------------------------------- 프로젝트 마법사

@api.post(PREFIX + "/projects/infer")
async def post_infer(request: Request):
    sources = await _csv_upload(request, "files")

    def work() -> dict[str, Any]:
        try:
            inference = scaffold.infer(sources)
        except scaffold.ScaffoldError as exc:
            raise StoreError(422, "invalid_csv", "CSV를 읽을 수 없습니다", exc.errors) from None
        return {"inference": inference, "upload_id": store.uploads.put(sources)}

    return await run_in_threadpool(work)


@api.post(PREFIX + "/projects")
async def post_project(request: Request):
    body = await _json_body(request)

    def work() -> str:
        sources = store.uploads.get(body.get("upload_id"))
        name = store.create_project(body.get("name"), body.get("inference"), sources,
                                    resuggest=body.get("resuggest") is True)
        store.uploads.drop(body["upload_id"])
        return name

    name = await run_in_threadpool(work)
    log.info("프로젝트 '%s'를 만듦", name)
    return JSONResponse({"name": name}, status_code=201)


# ---------------------------------------------------------------- 설정

_PG_TYPES = {
    "bigint": "integer", "integer": "integer", "smallint": "integer",
    "numeric": "number", "double precision": "number", "real": "number", "money": "number",
    "boolean": "boolean", "date": "date",
    "timestamp with time zone": "timestamp", "timestamp without time zone": "timestamp",
}
_SCHEMA_TYPES = {"enum": "text"}


def _table_columns(table: Any) -> list[dict[str, str]]:
    cols = table.get("columns") if isinstance(table, dict) else None
    if not isinstance(cols, dict):
        return []
    return [{"name": str(c), "type": str(spec.get("type", "text")) if isinstance(spec, dict) else "text"}
            for c, spec in cols.items()]


def _dataset_columns(name: str, schema: Any, dashboard: Any) -> tuple[dict[str, list[dict[str, str]]], str]:
    """데이터셋 → 열 목록과 그 출처("db" | "schema").

    지금 DB에 이 프로젝트가 적재돼 있으면(적재 기록의 project가 같으면) 뷰의 열을 읽는다(dash_reader).
    아니면 `table:` 데이터셋만 schema.yaml에서 채운다(`sql:` 데이터셋의 열은 적용해야 알 수 있다).
    """
    datasets = dashboard.get("datasets") if isinstance(dashboard, dict) else None
    tables = schema.get("tables") if isinstance(schema, dict) else None
    if not isinstance(datasets, dict):
        return {}, "schema"
    out: dict[str, list[dict[str, str]]] = {}
    source = "schema"
    try:
        with current.reader() as conn:
            record = current.read_load_record(conn, tables=[])
            if record is not None and record.project == name:
                views = current.read_view_columns(conn, [str(k) for k in datasets])
                for key, cols in views.items():
                    out[key] = [{"name": c, "type": _PG_TYPES.get(t, "text")} for c, t in cols]
                    source = "db"
    except current.CurrentError:
        pass
    for key, ds in datasets.items():
        if key in out or not isinstance(ds, dict) or not isinstance(tables, dict):
            continue
        table = tables.get(ds.get("table")) if isinstance(ds.get("table"), str) else None
        if table is not None:
            out[key] = [{"name": c["name"], "type": _SCHEMA_TYPES.get(c["type"], c["type"])}
                        for c in _table_columns(table)]
    return out, source


def _dataset_metrics(dashboard: Any) -> dict[str, list[str]]:
    """데이터셋 → 지표 이름(파일에 쓴 순서). 차트 폼의 지표 선택에 쓴다. 지표가 없는 데이터셋은 빈 목록이다."""
    datasets = dashboard.get("datasets") if isinstance(dashboard, dict) else None
    if not isinstance(datasets, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, ds in datasets.items():
        metrics = ds.get("metrics") if isinstance(ds, dict) else None
        out[str(key)] = [str(m) for m in metrics] if isinstance(metrics, dict) else []
    return out


def _config_body(name: str) -> dict[str, Any]:
    root = store.project_root(name)
    schema = store.read_raw(root, config.SCHEMA_FILE)
    dashboard = store.read_raw(root, config.DASHBOARD_FILE)
    try:
        config.load_project(root.name, store.projects_dir())
        errors: list[str] = []
    except config.ConfigError as exc:
        errors = exc.errors
    columns, source = _dataset_columns(root.name, schema, dashboard)
    tables: dict[str, Any] = {}
    if isinstance(schema, dict) and isinstance(schema.get("tables"), dict):
        for t, spec in schema["tables"].items():
            if isinstance(t, str) and store.TABLE_RE.fullmatch(t):
                tables[t] = {"rows": store.csv_row_count(root / "data" / f"{t}.csv"), "columns": _table_columns(spec)}
    return {"name": root.name, "schema": schema, "dashboard": dashboard, "valid": not errors, "errors": errors,
            "columns": columns, "columns_source": source, "metrics": _dataset_metrics(dashboard), "tables": tables,
            "revision": store.revision(root)}


@api.get(PREFIX + "/projects/{p}/config")
def get_config(p: str):
    return _config_body(store.resolve_project(p))


@api.patch(PREFIX + "/projects/{p}/config")
async def patch_config(p: str, request: Request):
    body = await _json_body(request)
    name = store.resolve_project(p)
    op_list = ops.check_ops(body.get("ops"))
    base = body.get("revision")

    def work() -> dict[str, Any]:
        with _Busy(name):
            out = store.save_ops(name, op_list, base)
        if out["changed"]:
            log.info("프로젝트 '%s' 설정 저장: %s (%s)", name, ", ".join(out["changed"]),
                     ", ".join(f"{f}={h}" for f, h in out["written"].items()))
        return out

    return await run_in_threadpool(work)


# ---------------------------------------------------------------- 데이터

def _one(files: dict[str, bytes]) -> bytes:
    return next(iter(files.values()))


def _table_diff(project: config.Project, table: str, rows: list) -> tuple[dict[str, Any] | None, str | None]:
    """올린 CSV와 지난 적재의 차이(dashkit.plan의 방식, dash_reader만 쓴다). 읽지 못하면 (None, 이유)."""
    try:
        with current.reader() as conn:
            record = current.read_load_record(conn, [table])
    except current.CurrentError as exc:
        return None, jobs.scrub(str(exc), jobs.secret_values())
    if record is None:
        return plan.unrecorded_table(table, len(rows)), None
    other = record.project if record.project != project.name else None
    recorded = (record.snapshot.get("tables") or {}).get(table)
    return plan.table_diff(table, project.schema["tables"][table], rows, recorded,
                           record.fingerprints.get(table, []), replaced_project=other), None


@api.post(PREFIX + "/projects/{p}/data/{table}/check")
async def post_data_check(p: str, table: str, request: Request):
    content = _one(await _csv_upload(request, "file", single=True))
    name = store.resolve_project(p)

    def work() -> dict[str, Any]:
        tname = store.resolve_replace_table(store.load(name), table)
        try:
            staged = store.prepare(name, {f"data/{tname}.csv": content}, with_csv=True)
        except StoreError as exc:
            if exc.detail != "invalid_csv":
                raise
            return {"valid": False, "errors": exc.errors or [], "diff": None}
        assert staged.data is not None
        rows = staged.data[tname].rows
        diff, reason = _table_diff(staged.project, tname, rows)
        out: dict[str, Any] = {"valid": True, "errors": [], "diff": diff, "rows": len(rows)}
        if reason is not None:
            out["diff_error"] = reason
        return out

    return await run_in_threadpool(work)


@api.put(PREFIX + "/projects/{p}/data/{table}")
async def put_data(p: str, table: str, request: Request):
    content = _one(await _csv_upload(request, "file", single=True))
    name = store.resolve_project(p)

    def work() -> dict[str, Any]:
        tname = store.resolve_replace_table(store.load(name), table)
        files = {f"data/{tname}.csv": content}
        with _Busy(name):
            staged = store.prepare(name, files, with_csv=True)
            changed = store.commit(name, files)
        assert staged.data is not None
        if changed:
            log.info("프로젝트 '%s' 데이터 교체: %s (%d행)", name, changed[0], len(staged.data[tname].rows))
        return {"changed": changed, "rows": len(staged.data[tname].rows)}

    return await run_in_threadpool(work)


# ---------------------------------------------------------------- 적용

@api.post(PREFIX + "/projects/{p}/apply")
async def post_apply(p: str, request: Request):
    await _json_body(request, optional=True)        # 본문은 쓰지 않는다. 형식만 본다
    name = store.resolve_project(p)
    job = await run_in_threadpool(runner.start, name, store.project_lock(name))
    if job is None:
        raise StoreError(409, "apply_running", "적용(apply)이 이미 실행 중입니다. 끝난 뒤 다시 시도하세요")
    log.info("apply 시작: %s", name)
    return StreamingResponse(jobs.stream(job), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no"})


@api.get(PREFIX + "/apply")
def get_apply():
    return runner.status()


@api.get(PREFIX + "/projects/{p}/plan")
def get_plan(p: str):
    project = store.load(store.resolve_project(p))
    try:
        return plan.make_plan(project)
    except plan.PlanError as exc:
        secrets = jobs.secret_values()
        errors = [jobs.scrub(e, secrets) for e in exc.errors]
        if "CSV" in exc.title:
            return _error(422, "invalid_csv", exc.title, errors)
        return _error(503, "plan_unavailable", exc.title, errors)


# ---------------------------------------------------------------- 레지스트리

def _field(f: charts.Field) -> dict[str, Any]:
    out: dict[str, Any] = {"name": f.name, "kind": f.kind, "required": f.required}
    if f.has_default:
        out["default"] = f.default
    if f.kind == "enum" and f.values is not None:
        out["values"] = list(f.values)
    for key in ("description", "min", "max", "min_items"):
        if getattr(f, key) is not None:
            out[key] = getattr(f, key)
    return out


def _refs(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            found.add(ref[len("#/$defs/"):])
        for v in node.values():
            _refs(v, found)
    elif isinstance(node, list):
        for v in node:
            _refs(v, found)


def _registry_body() -> dict[str, Any]:
    schema = config.load_schema("dashboard.schema.json")
    defs = schema["$defs"]
    widget_types = [{"type": t.type, "label": t.label, "options": dict(t.options)} for t in widgets.registry().values()]
    # 위젯 옵션이 가리키는 $defs(icon, tone, column 등)를 따라가며 모아 준다(화면이 $ref를 풀 수 있게)
    needed: set[str] = set()
    _refs([w["options"] for w in widget_types], needed)
    found_defs: dict[str, Any] = {}
    while needed - set(found_defs):
        for key in sorted(needed - set(found_defs)):
            found_defs[key] = defs.get(key, {})
            _refs(found_defs[key], needed)
    return {
        "charts": [{"viz": t.viz, "label": t.label, "viz_type": t.viz_type, "fields": [_field(f) for f in t.fields]}
                   for t in charts.registry().values()],
        "widgets": widget_types,
        "icons": list(defs["icon"]["enum"]),
        "tones": list(defs["tone"]["enum"]),
        "defs": found_defs,
    }


@api.get(PREFIX + "/registry")
def get_registry():
    return _registry_body()


# ---------------------------------------------------------------- 화면(정적 파일)

_MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml",
    ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8",
}


def shared_dir() -> Path:
    """운용 화면과 함께 쓰는 화면 모듈(web/shared). web/studio의 모듈이 `../shared/…`로 import하면 `/studio/shared/…`가 된다."""
    env = os.environ.get("STUDIO_SHARED_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[2] / "web" / "shared"


def _static(path: str, root: Path | None = None) -> Response:
    base = (root or web_dir()).resolve()
    parts = path.split("/")
    if not path or "\\" in path or "\0" in path or any(part in ("", ".", "..") or part.startswith(".") for part in parts):
        return _error(404, "not_found", "Not Found")
    target = (base / path).resolve()
    if base not in target.parents or not target.is_file():
        return _error(404, "not_found", "Not Found")
    media = _MEDIA_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media)


@api.get("/")
def get_index():
    resp = _static("index.html")
    if resp.status_code == 404:
        return _error(404, "not_found", f"화면 파일이 없습니다({web_dir()}/index.html). API는 {PREFIX}/ 아래에서 동작합니다")
    return resp


@api.get("/studio/static/{path:path}")
def get_static(path: str):
    return _static(path)


@api.get("/studio/shared/{path:path}")
def get_shared(path: str):
    return _static(path, shared_dir())


# 요청 보호를 씌운 ASGI 앱. uvicorn은 이것을 띄운다
app = Guard(api)
