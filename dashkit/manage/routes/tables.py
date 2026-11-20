"""테이블과 배치: 목록·상세, 데이터 더하기, 배치 목록·내려받기·되돌리기·되살리기(contracts/manage.md "경로").

라우팅과 권한만 갖는다. 로직은 dashkit/ingest의 ingest()·remove_batch()·restore_batch()다(CLI·Studio와 같다).
읽기 경로(목록·상세·배치 목록)는 dash_inspect만 쓰고, owner 접속은 쓰기 작업(ingest 등) 안에서만 열린다.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from ... import config, db
from ...auth import store as auth_store
from ...auth.routes import Principal, client_ip, require_operator
from ...ingest import Item, IngestError, batches, ingest, ledger
from ...ingest.batches import REMOVED_DIR
from ...ingest.remove import NeedsRebuild, remove_batch, restore_batch
from ...project import upload
from .. import context
from ..runs_state import runs
from ..uploads import uploads

router = APIRouter()
MAX_JSON_BYTES = 10 * 1024 * 1024
METHODS = ("form", "paste")


# ---------------------------------------------------------------- 공통

def _project() -> config.Project:
    name = context.current_project()
    try:
        return config.load_project(name)
    except config.ConfigError as exc:
        raise IngestError(422, "invalid_config", "설정 검사를 통과하지 못했습니다", exc.errors) from None


def _table(project: config.Project, table: str) -> str:
    """경로의 {table} → schema.yaml의 테이블(목록 쪽 문자열). 없으면 404 unknown_table."""
    for known in project.schema["tables"]:
        if known == table:
            return known
    raise IngestError(404, "unknown_table", "schema.yaml에 없는 테이블입니다")


def _recorded() -> dict[str, dict[str, ledger.Recorded]]:
    """배치 기록(dash_inspect로 읽는다). 읽지 못하면 빈 dict(아직 적용하지 않은 DB 등)."""
    try:
        with db.inspect_conn() as conn:
            with conn.cursor() as cur:
                return ledger.read(cur)
    except (psycopg.Error, RuntimeError):
        return {}


def _audit(action: str, principal: Principal | None, request: Request | None, **detail: Any) -> None:
    try:
        auth_store.record_audit(action, actor_id=principal.id if principal else None,
                                actor_email=principal.email if principal else None, target_type="table",
                                target_id=detail.get("table"), target_label=detail.get("table"), detail=detail,
                                ip=client_ip(request) if request is not None else None)
    except Exception:     # 감사 로그 실패가 끝난 적재를 실패로 만들지 않는다
        pass


def _batch_log(project: str, table: str) -> dict[str, dict[str, Any]]:
    try:
        return auth_store.batch_log(project, table)
    except Exception:
        return {}


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    """감사 로그의 detail(데이터 값을 싣지 않는다: 파일 이름·해시·행 수뿐)."""
    return {"batches": [{"table": b["table"], "file": b["file"], "sha256": b["sha256"], "rows": b["rows"]}
                        for b in result.get("batches", [])]}


# ---------------------------------------------------------------- 목록·상세

@router.get("/tables")
def get_tables(principal: Principal = Depends(require_operator)) -> dict[str, Any]:
    project = _project()
    recorded = _recorded()
    hint = context.limits().compact_hint
    items = []
    for name, spec in project.schema["tables"].items():
        mode = spec.get("mode") or "replace"
        rec = recorded.get(name, {})
        files = batches.list_batches(project.data_dir, name)[0] if mode == "append" else []
        last = max(rec.values(), key=lambda r: (r.added_at is not None, r.added_at or 0, r.file), default=None) \
            if rec else None
        items.append({
            "name": name, "mode": mode, "on_conflict": (spec.get("on_conflict") or "error") if mode == "append" else None,
            "description": spec.get("description"),
            "rows": sum(r.inserted for r in rec.values()) if rec else None,
            "batches": len(files), "compact_hint": len(files) > hint,
            "last_batch": {"file": last.file, "added_at": last.added_at,
                           "actor_email": (_batch_log(project.name, name).get(last.file) or {}).get("actor_email")}
            if last else None,
        })
    return {"items": items, "total": len(items), "project": project.name}


@router.get("/tables/{table}")
def get_table(table: str, principal: Principal = Depends(require_operator)) -> dict[str, Any]:
    project = _project()
    name = _table(project, table)
    spec = project.schema["tables"][name]
    labels: dict[str, str] = {}
    datasets = []
    for key, ds in project.dashboard["datasets"].items():
        if ds.get("table") == name:
            datasets.append(key)
            for col, label in (ds.get("labels") or {}).items():
                labels.setdefault(col, label)
    columns = []
    for col, c in spec["columns"].items():
        item = {"name": col, "type": c["type"], "required": bool(c.get("required")),
                "primary_key": col in spec["primary_key"], "label": labels.get(col)}
        for key in ("values", "ref", "min", "max", "pattern", "description", "default", "aliases"):
            if key in c:
                item[key] = c[key]
        columns.append(item)
    return {"name": name, "mode": spec.get("mode") or "replace",
            "on_conflict": (spec.get("on_conflict") or "error") if spec.get("mode") == "append" else None,
            "description": spec.get("description"), "primary_key": list(spec["primary_key"]), "columns": columns,
            "checks": [c["name"] for c in spec.get("checks", [])], "datasets": datasets}


# ---------------------------------------------------------------- 데이터 더하기

async def _json(request: Request) -> dict[str, Any]:
    raw = await upload.read_body(request.stream(), request.headers.get("content-length"), MAX_JSON_BYTES)
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, ValueError):
        raise IngestError(400, "invalid_request", "본문이 JSON이 아닙니다") from None
    if not isinstance(body, dict):
        raise IngestError(400, "invalid_request", "본문은 JSON 객체여야 합니다")
    return body


def _flag(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on"))


def _columns(value: Any) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise IngestError(400, "invalid_request", "columns는 JSON 객체({머리글: 열 또는 null})입니다") from None
    if not isinstance(value, dict) or not all(isinstance(k, str) and (v is None or isinstance(v, str))
                                              for k, v in value.items()):
        raise IngestError(400, "invalid_request", "columns는 {머리글: 열 또는 null}입니다")
    return value


def _ingest_job(project_name: str, table: str, path: Path, source_name: str, principal: Principal,
                request: Request, *, dry_run: bool, force: bool, skip_invalid: bool, strict: bool,
                columns: dict[str, Any] | None, upload_id: str | None) -> JSONResponse:
    """파일 적재를 작업으로 돌린다(202). 미리보기면 결과에 upload_id를 싣고 파일을 잡아 둔다."""
    limits = context.limits()

    def work(ctx) -> dict[str, Any]:
        ctx.progress("read")
        result = ingest(project_name, [Item(table, {"kind": "csv", "path": path, "name": source_name}, columns)],
                        dry_run=dry_run, force=force, strict=strict, skip_invalid=skip_invalid,
                        max_rows=limits.max_rows, progress=lambda step, info: ctx.progress(step, **dict(info)),
                        wait_ms=limits.lock_wait_ms)
        if dry_run:
            result["upload_id"] = upload_id
        else:
            ctx.log(f"✔ {table}: {result['batches'][0]['file']} ({result['batches'][0]['rows']['total']}행)")
        return result

    def done(run) -> None:
        if upload_id is None:
            shutil.rmtree(path.parent, ignore_errors=True)   # 미리보기 없이 바로 올린 파일: 원래 파일은 보관하지 않는다
        if dry_run:
            if run.status != "succeeded" and upload_id:
                uploads.drop(upload_id)      # 결과에 upload_id가 없으니 화면은 다시 올린다. 잡아 둘 까닭이 없다
            return
        if upload_id and run.status == "succeeded":
            uploads.drop(upload_id)          # 넣었으면 푼다. 실패하면 1시간 동안 같은 upload_id로 다시 넣을 수 있다
        if run.status == "succeeded":
            _audit("data.batch.create", principal, None, project=project_name, method="upload",
                   source_name=source_name, **_summary(run.result)["batches"][0])
        else:
            _audit("data.batch.failed", principal, None, project=project_name, table=table, method="upload",
                   source_name=source_name, detail_code=(run.error or {}).get("detail"))

    run = runs.start("ingest", work, table=table, actor_id=principal.id, actor_email=principal.email, on_done=done)
    return JSONResponse({"job": run.public()}, status_code=202)


@router.post("/tables/{table}/batches")
async def post_batches(table: str, request: Request, principal: Principal = Depends(require_operator)):
    project = _project()
    name = _table(project, table)
    ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    limits = context.limits()
    if ctype == "multipart/form-data":
        folder = uploads.folder()
        try:
            parts = await upload.stream_multipart(
                request.headers.get("content-type"), request.stream(), request.headers.get("content-length"),
                folder=folder, file_limit=limits.max_upload_mb * 1024 * 1024,
                request_limit=limits.max_request_mb * 1024 * 1024)
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        files = [p for p in parts if p.name == "file" and p.filename is not None]
        fields = {p.name: p.value for p in parts if p.filename is None}
        if len(files) != 1:
            shutil.rmtree(folder, ignore_errors=True)
            raise IngestError(400, "bad_request", "multipart에 `file` 파일 하나를 보냅니다")
        try:
            source_name = upload.check_filename(files[0].filename)
        except upload.UploadError:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        dry_run = _flag(fields.get("dry_run"))
        path = files[0].path
        assert path is not None
        upload_id = uploads.hold(name, [(source_name, path)], principal.id) if dry_run else None
        return _ingest_job(project.name, name, path, source_name, principal, request, dry_run=dry_run,
                           force=_flag(fields.get("force")), skip_invalid=_flag(fields.get("skip_invalid")),
                           strict=_flag(fields.get("strict")), columns=_columns(fields.get("columns")),
                           upload_id=upload_id)
    body = await _json(request)
    if "upload_id" in body:
        held = uploads.get(body.get("upload_id"), name, principal.id)
        source_name, path = held.files[0]
        return _ingest_job(project.name, name, path, source_name, principal, request, dry_run=False,
                           force=_flag(body.get("force")), skip_invalid=_flag(body.get("skip_invalid")),
                           strict=_flag(body.get("strict")), columns=_columns(body.get("columns")),
                           upload_id=held.id)
    rows = body.get("rows")
    if isinstance(rows, list) and len(rows) > limits.inline_max_rows:
        raise IngestError(413, "too_many_rows",
                          f"한 번에 {limits.inline_max_rows}행까지 보낼 수 있습니다. 더 많으면 파일로 올려 주세요")
    method = body.get("method") if body.get("method") in METHODS else "form"
    dry_run = _flag(body.get("dry_run"))

    def work() -> dict[str, Any]:
        try:
            result = ingest(project.name, [Item(name, {"kind": "rows", "rows": rows}, _columns(body.get("columns")))],
                            dry_run=dry_run, force=_flag(body.get("force")), skip_invalid=_flag(body.get("skip_invalid")),
                            wait_ms=limits.lock_wait_ms)
        except IngestError as exc:
            if not dry_run:
                _audit("data.batch.failed", principal, request, project=project.name, table=name, method=method,
                       detail_code=exc.detail)
            raise
        if not dry_run:
            b = _summary(result)["batches"][0]
            _audit("data.batch.create", principal, request, project=project.name, method=method, **b)
        return result

    return await run_in_threadpool(work)


# ---------------------------------------------------------------- 배치

@router.get("/tables/{table}/batches")
def get_batches(table: str, removed: str | None = None, principal: Principal = Depends(require_operator)):
    project = _project()
    name = _table(project, table)
    rec = _recorded().get(name, {})
    log = _batch_log(project.name, name)
    files = batches.sources(project.data_dir, name, project.schema["tables"][name].get("mode") or "replace")[0]
    items = []

    def row(file: str, *, is_removed: bool) -> dict[str, Any]:
        r = rec.get(file)
        who = log.get(file) or {}
        return {"file": file, "removed": is_removed, "first": file == f"{name}.csv",
                "added_at": batches.stamp_of(file), "recorded": r is not None,
                "rows": r.rows if r else None, "inserted": r.inserted if r else None,
                "updated": r.updated if r else None, "skipped": r.skipped if r else None,
                "unchanged": r.unchanged if r else None,
                "actor_email": who.get("actor_email"), "method": who.get("method"),
                "source_name": who.get("source_name"),
                "mine": bool(who.get("actor_id")) and who.get("actor_id") == principal.id}

    for f in reversed(files):
        items.append(row(f.name, is_removed=False))
    if _flag(removed):
        folder = batches.table_dir(project.data_dir, name) / REMOVED_DIR
        if folder.is_dir():
            for p in sorted(folder.iterdir(), key=lambda p: p.name, reverse=True):
                if p.is_file() and batches.BATCH_RE.fullmatch(p.name):
                    items.append(row(p.name, is_removed=True))
    return {"items": items, "total": len(items)}


def _batch_path(project: config.Project, table: str, file: str) -> Path:
    """경로의 {file}을 폴더 목록과 대조한 뒤에만 쓴다(목록 쪽 경로)."""
    mode = project.schema["tables"][table].get("mode") or "replace"
    for f in batches.sources(project.data_dir, table, mode)[0]:
        if f.name == file:
            return f.path
    folder = batches.table_dir(project.data_dir, table) / REMOVED_DIR
    if folder.is_dir():
        for p in folder.iterdir():
            if p.name == file and p.is_file() and batches.BATCH_RE.fullmatch(p.name):
                return p
    raise IngestError(404, "unknown_batch", "그 배치 파일이 없습니다")


@router.get("/tables/{table}/batches/{file}")
def get_batch_file(table: str, file: str, principal: Principal = Depends(require_operator)):
    project = _project()
    name = _table(project, table)
    path = _batch_path(project, name, file)
    media = "application/gzip" if path.name.endswith(".gz") else "text/csv; charset=utf-8"
    return FileResponse(path, media_type=media, filename=f"{name}-{path.name}")


def _may_change(project: config.Project, table: str, file: str, principal: Principal) -> None:
    """operator는 자기가 더한 배치만 되돌리거나 되살린다(감사 로그의 data.batch.create). admin은 모두."""
    if principal.role == "admin":
        return
    who = _batch_log(project.name, table).get(file) or {}
    if not who.get("actor_id") or who.get("actor_id") != principal.id:
        raise IngestError(403, "forbidden", "자기가 더한 배치만 되돌릴 수 있습니다(admin은 모두)")


@router.delete("/tables/{table}/batches/{file}")
async def delete_batch(table: str, file: str, request: Request, principal: Principal = Depends(require_operator)):
    project = _project()
    name = _table(project, table)
    _may_change(project, name, file, principal)
    limits = context.limits()

    def fast() -> dict[str, Any] | None:
        try:
            result = remove_batch(project.name, name, file, fast_only=True, wait_ms=limits.lock_wait_ms)
        except NeedsRebuild:
            return None
        _audit("data.batch.remove", principal, request, project=project.name, table=name, file=file, fast=True)
        return result

    result = await run_in_threadpool(fast)
    if result is not None:
        return result

    def work(ctx) -> dict[str, Any]:
        ctx.progress("rebuild")
        return remove_batch(project.name, name, file, wait_ms=limits.lock_wait_ms)

    def done(run) -> None:
        if run.status == "succeeded":
            _audit("data.batch.remove", principal, None, project=project.name, table=name, file=file, fast=False)

    run = runs.start("remove", work, table=name, actor_id=principal.id, actor_email=principal.email, on_done=done)
    return JSONResponse({"job": run.public()}, status_code=202)


@router.post("/tables/{table}/batches/{file}/restore")
async def post_restore(table: str, file: str, request: Request, principal: Principal = Depends(require_operator)):
    await upload.read_body(request.stream(), request.headers.get("content-length"), MAX_JSON_BYTES)
    project = _project()
    name = _table(project, table)
    _may_change(project, name, file, principal)
    limits = context.limits()

    def work(ctx) -> dict[str, Any]:
        ctx.progress("rebuild")
        return restore_batch(project.name, name, file, wait_ms=limits.lock_wait_ms)

    def done(run) -> None:
        if run.status == "succeeded":
            _audit("data.batch.restore", principal, None, project=project.name, table=name, file=file)

    run = runs.start("restore", work, table=name, actor_id=principal.id, actor_email=principal.email, on_done=done)
    return JSONResponse({"job": run.public()}, status_code=202)
