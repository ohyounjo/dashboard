"""테이블과 배치: 목록·상세, 데이터 더하기, 배치 목록·내려받기·되돌리기·되살리기(contracts/manage.md "경로").

라우팅과 권한만 갖는다. 로직은 dashkit/ingest의 ingest()·remove_batch()·restore_batch()다(CLI·Studio와 같다).
읽기 경로(목록·상세·배치 목록)는 dash_inspect만 쓰고, owner 접속은 쓰기 작업(ingest 등) 안에서만 열린다.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from ... import config, db, freshness
from ...auth import store as auth_store
from ...auth.routes import Principal, client_ip, require_admin, require_operator
from ...features import has_manage_feature
from ...ingest import Item, IngestError, batches, ingest, ledger, lineage
from ...ingest.batches import REMOVED_DIR
from ...ingest.remove import NeedsRebuild, remove_batch, restore_batch
from ...project import store, upload
from .. import context
from ..runs_state import runs
from ..uploads import uploads

router = APIRouter()
MAX_JSON_BYTES = 10 * 1024 * 1024
METHODS = ("form", "paste")
FAST_CHECK_LOCK_TIMEOUT_MS = 200


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


def _recorded(*, lock_timeout_ms: int | None = None, raise_on_lock: bool = False) -> dict[str, dict[str, ledger.Recorded]]:
    """배치 기록(dash_inspect로 읽는다). 읽지 못하면 빈 dict(아직 적용하지 않은 DB 등)."""
    try:
        with db.inspect_conn() as conn:
            with conn.cursor() as cur:
                if lock_timeout_ms is not None:
                    # PostgreSQL에서 lock_timeout=0은 무제한 대기이므로 최소 1ms로 제한한다
                    cur.execute("SELECT set_config('lock_timeout', %s, true)", (f"{max(int(lock_timeout_ms), 1)}ms",))
                return ledger.read(cur)
    except psycopg.errors.LockNotAvailable:
        if raise_on_lock:
            raise
        return {}
    except (psycopg.Error, RuntimeError):
        return {}


def _lineage(project: config.Project) -> dict[str, Any]:
    """계보(적재 스냅샷은 dash_inspect로 읽는다. 읽지 못하면 설정만으로)."""
    snap = None
    try:
        with db.inspect_conn() as conn:
            with conn.cursor() as cur:
                snap = ledger.loaded(cur).snapshot
    except (psycopg.Error, RuntimeError):
        pass
    return lineage.build(project, snap)


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
    graph = _lineage(project)
    show_freshness = has_manage_feature("freshness")
    items = []
    for name, spec in project.schema["tables"].items():
        mode = spec.get("mode") or "replace"
        rec = recorded.get(name, {})
        files = batches.list_batches(project.data_dir, name)[0] if mode == "append" else []
        last = max(rec.values(), key=lambda r: (r.added_at is not None, r.added_at or 0, r.file), default=None) \
            if rec else None
        item: dict[str, Any] = {
            "name": name, "mode": mode, "on_conflict": (spec.get("on_conflict") or "error") if mode == "append" else None,
            "description": spec.get("description"),
            "rows": sum(r.inserted for r in rec.values()) if rec else None,
            "batches": len(files), "compact_hint": len(files) > hint,
            "used_by": len(lineage.used_by(graph, name)["datasets"]),
            "last_batch": {"file": last.file, "added_at": last.added_at,
                           "actor_email": (_batch_log(project.name, name).get(last.file) or {}).get("actor_email")}
            if last else None,
        }
        if show_freshness:
            last_added = freshness.table_last_added_at(recorded, name)
            item["freshness"] = spec.get("freshness")
            item["stale"] = freshness.is_stale(last_added, spec.get("freshness"))
        items.append(item)
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
    out: dict[str, Any] = {
        "name": name, "mode": spec.get("mode") or "replace",
        "on_conflict": (spec.get("on_conflict") or "error") if spec.get("mode") == "append" else None,
        "description": spec.get("description"), "primary_key": list(spec["primary_key"]), "columns": columns,
        "checks": [c["name"] for c in spec.get("checks", [])], "datasets": datasets,
        "revision": store.revision(project.root),
    }
    if "freshness" in spec:
        out["freshness"] = spec["freshness"]
    return out


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


def _ingest_job(project_name: str, entries: list[dict[str, Any]], principal: Principal, *, dry_run: bool,
                force: bool, skip_invalid: bool, strict: bool, upload_id: str | None,
                folder: Path) -> JSONResponse:
    """파일 적재(하나 또는 여럿, 한 트랜잭션)를 작업으로 돌린다(202). entries는 [{table, path, name, columns}].

    미리보기면 결과에 upload_id를 싣고 파일을 잡아 둔다. 오류 행을 빼고 넣으면 빠진 행 파일을 작업에 붙인다
    (GET /jobs/{id}/rejects).
    """
    limits = context.limits()
    rejects_dir = Path(tempfile.mkdtemp(prefix="dashkit-rejects-")) if skip_invalid else None
    one = entries[0]["table"] if len(entries) == 1 else None

    def work(ctx) -> dict[str, Any]:
        ctx.progress("read")
        items = [Item(e["table"], {"kind": "csv", "path": e["path"], "name": e["name"]}, e.get("columns"))
                 for e in entries]
        result = ingest(project_name, items, dry_run=dry_run, force=force, strict=strict, skip_invalid=skip_invalid,
                        max_rows=limits.max_rows, rejects_dir=rejects_dir,
                        progress=lambda step, info: ctx.progress(step, **dict(info)), wait_ms=limits.job_lock_wait_ms)
        if dry_run:
            result["upload_id"] = upload_id
        else:
            for b in result["batches"]:
                ctx.log(f"✔ {b['table']}: {b['file']} ({b['rows']['total']}행)")
        if rejects_dir is not None and any(b.get("rejected") for b in result["batches"]):
            ctx.run.rejects = _merge_rejects(rejects_dir, result["batches"], entries)
            result["rejects"] = True
        return result

    def done(run) -> None:
        if upload_id is None:
            shutil.rmtree(folder, ignore_errors=True)        # 미리보기 없이 바로 올린 파일: 원래 파일은 보관하지 않는다
        if rejects_dir is not None and run.rejects is None:
            shutil.rmtree(rejects_dir, ignore_errors=True)
        if dry_run:
            if run.status != "succeeded" and upload_id:
                uploads.drop(upload_id)      # 결과에 upload_id가 없으니 화면은 다시 올린다. 잡아 둘 까닭이 없다
            return
        if upload_id and run.status == "succeeded":
            uploads.drop(upload_id)          # 넣었으면 푼다. 실패하면 1시간 동안 같은 upload_id로 다시 넣을 수 있다
        if run.status == "succeeded":
            for e, b in zip(entries, _summary(run.result)["batches"]):
                _audit("data.batch.create", principal, None, project=project_name, method="upload",
                       source_name=e["name"], **b)
        else:
            for e in entries:
                _audit("data.batch.failed", principal, None, project=project_name, table=e["table"], method="upload",
                       source_name=e["name"], detail_code=(run.error or {}).get("detail"))

    run = runs.start("ingest", work, table=one, actor_id=principal.id, actor_email=principal.email, on_done=done)
    return JSONResponse({"job": run.public()}, status_code=202)


def _merge_rejects(folder: Path, batches_: list[dict[str, Any]], entries: list[dict[str, Any]]) -> Path:
    """항목마다의 빠진 행 파일. 파일이 하나면 그대로, 여럿이면 zip 하나로 묶는다."""
    from ...ingest.add import rejects_file_name

    files = [(e, folder / rejects_file_name(i)) for i, (e, b) in enumerate(zip(entries, batches_))
             if b.get("rejected") and (folder / rejects_file_name(i)).is_file()]
    if len(files) == 1:
        return files[0][1]
    out = folder / "rejects.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for e, path in files:
            z.write(path, f"{Path(e['name']).stem}-빠진행.csv")
    for p in folder.iterdir():          # zip에 담은 파일은 지운다(폴더에는 내려받을 파일 하나만)
        if p != out:
            p.unlink()
    return out


async def _receive(request: Request, field: str, *, single: bool) -> tuple[Path, list[tuple[str, Path]], dict[str, str]]:
    """multipart를 디스크로 받는다. (요청 폴더, [(파일 이름, 경로)], 글 부분). 실패하면 폴더를 지운다."""
    limits = context.limits()
    folder = uploads.folder()
    try:
        parts = await upload.stream_multipart(
            request.headers.get("content-type"), request.stream(), request.headers.get("content-length"),
            folder=folder, file_limit=limits.max_upload_mb * 1024 * 1024,
            request_limit=limits.max_request_mb * 1024 * 1024)
        files = [p for p in parts if p.name == field and p.filename is not None]
        fields = {p.name: p.value or "" for p in parts if p.filename is None}
        if not files or (single and len(files) != 1):
            raise IngestError(400, "bad_request", f"multipart에 `{field}` 파일을 " + ("하나 " if single else "") + "보냅니다")
        names = [upload.check_filename(p.filename) for p in files]
        if len(set(names)) != len(names):
            raise IngestError(400, "invalid_filename", "같은 이름의 파일이 두 번 있습니다")
        return folder, [(n, p.path) for n, p in zip(names, files)], fields   # type: ignore[misc]
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


@router.post("/tables/{table}/batches")
async def post_batches(table: str, request: Request, principal: Principal = Depends(require_operator)):
    project = _project()
    name = _table(project, table)
    ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    limits = context.limits()
    if ctype == "multipart/form-data":
        folder, files, fields = await _receive(request, "file", single=True)
        source_name, path = files[0]
        dry_run = _flag(fields.get("dry_run"))
        columns = _columns(fields.get("columns"))
        upload_id = uploads.hold(name, files, principal.id, columns={source_name: columns}) if dry_run else None
        return _ingest_job(project.name, [{"table": name, "path": path, "name": source_name, "columns": columns}],
                           principal, dry_run=dry_run, force=_flag(fields.get("force")),
                           skip_invalid=_flag(fields.get("skip_invalid")), strict=_flag(fields.get("strict")),
                           upload_id=upload_id, folder=folder)
    body = await _json(request)
    if "upload_id" in body:
        held = uploads.get(body.get("upload_id"), name, principal.id)
        source_name, path = held.files[0]
        columns = _columns(body.get("columns")) if "columns" in body else (held.columns or {}).get(source_name)
        return _ingest_job(project.name, [{"table": name, "path": path, "name": source_name, "columns": columns}],
                           principal, dry_run=False, force=_flag(body.get("force")),
                           skip_invalid=_flag(body.get("skip_invalid")), strict=_flag(body.get("strict")),
                           upload_id=held.id, folder=path.parent)
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
        return {"file": file, "removed": is_removed, "first": file == f"{name}.csv", "merged": batches.is_merged(file),
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


def _is_fast_remove_candidate(project: config.Project, table: str, file: str, *,
                              lock_timeout_ms: int = FAST_CHECK_LOCK_TIMEOUT_MS) -> bool:
    """잠금 전 읽기 전용 사전 판별: 빠른 길 비후보(이전 배치·수정/건너뜀 포함)는 5초 대기 없이 바로 202 작업으로 넘긴다.

    없는 배치(404)와 합친 배치(409)는 기존 즉시 응답 경로를 타도록 True를 돌려준다.
    배치 기록 조회가 SQL 잠금 대기에 걸리면(다시 만들기 진행 중 등) 비동기 202 작업으로 넘겨 쓰기 잠금 안에서 판별한다.
    """
    files = batches.list_batches(project.data_dir, table)[0]
    if not any(f.name == file for f in files) or batches.is_merged(file):
        return True
    if max(files, key=lambda f: f.number or 0).name != file:
        return False
    try:
        rec = _recorded(lock_timeout_ms=lock_timeout_ms, raise_on_lock=True).get(table, {}).get(file)
    except psycopg.errors.LockNotAvailable:
        return False
    if rec is not None and (rec.inserted != rec.rows or rec.updated or rec.skipped or rec.unchanged):
        return False
    return True


@router.delete("/tables/{table}/batches/{file}")
async def delete_batch(table: str, file: str, request: Request, principal: Principal = Depends(require_operator)):
    project = _project()
    name = _table(project, table)
    _may_change(project, name, file, principal)
    limits = context.limits()

    def fast() -> dict[str, Any] | None:
        if not _is_fast_remove_candidate(project, name, file,
                                         lock_timeout_ms=max(1, min(limits.lock_wait_ms, FAST_CHECK_LOCK_TIMEOUT_MS))):
            return None
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
        ctx.progress("lock")
        return remove_batch(project.name, name, file, wait_ms=limits.job_lock_wait_ms,
                            progress=lambda step, info: ctx.progress(step, **dict(info)))

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
        ctx.progress("lock")
        return restore_batch(project.name, name, file, wait_ms=limits.job_lock_wait_ms,
                             progress=lambda step, info: ctx.progress(step, **dict(info)))

    def done(run) -> None:
        if run.status == "succeeded":
            _audit("data.batch.restore", principal, None, project=project.name, table=name, file=file)

    run = runs.start("restore", work, table=name, actor_id=principal.id, actor_email=principal.email, on_done=done)
    return JSONResponse({"job": run.public()}, status_code=202)


# ---------------------------------------------------------------- 여러 파일 한꺼번에(한 트랜잭션)

MAX_FILES = 50


def _map_files(project: config.Project, names: list[str], mapping: Any) -> dict[str, str]:
    """파일 이름 → 테이블. map이 없으면 파일 이름(확장자 제외)이 테이블 이름이어야 한다. 테이블은 목록과 대조한다."""
    if mapping in (None, ""):
        mapping = {}
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping)
        except ValueError:
            raise IngestError(400, "invalid_request", "map은 JSON 객체({파일 이름: 테이블})입니다") from None
    if not isinstance(mapping, dict):
        raise IngestError(400, "invalid_request", "map은 {파일 이름: 테이블}입니다")
    out: dict[str, str] = {}
    errors: list[str] = []
    for n in names:
        wanted = mapping.get(n, Path(n).stem)
        found = next((t for t in project.schema["tables"] if t == wanted), None)
        if found is None:
            errors.append(f"{n}: 맞는 테이블이 없음('{wanted}')")
        else:
            out[n] = found
    if errors:
        raise IngestError(422, "unknown_table", "파일과 테이블을 짝지을 수 없습니다(map으로 정해 주세요)", errors)
    return out


@router.post("/batches")
async def post_many(request: Request, principal: Principal = Depends(require_operator)):
    """여러 파일을 한 트랜잭션으로(설계서 5-2, D12): 전부 들어가거나 하나도 들어가지 않는다. 참조 순서대로 넣는다."""
    project = _project()
    ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if ctype == "multipart/form-data":
        folder, files, fields = await _receive(request, "files", single=False)
        try:
            if len(files) > MAX_FILES:
                raise IngestError(400, "too_many_files", f"한 번에 {MAX_FILES}개까지 올릴 수 있습니다")
            mapping = _map_files(project, [n for n, _p in files], fields.get("map"))
            columns = _columns_map(fields.get("columns"))
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        dry_run = _flag(fields.get("dry_run"))
        entries = [{"table": mapping[n], "path": p, "name": n, "columns": columns.get(n)} for n, p in files]
        upload_id = uploads.hold("*", files, principal.id, map=mapping, columns=columns) if dry_run else None
        return _ingest_job(project.name, entries, principal, dry_run=dry_run, force=_flag(fields.get("force")),
                           skip_invalid=_flag(fields.get("skip_invalid")), strict=_flag(fields.get("strict")),
                           upload_id=upload_id, folder=folder)
    body = await _json(request)
    held = uploads.get(body.get("upload_id"), "*", principal.id)
    mapping = _map_files(project, [n for n, _p in held.files], body.get("map", held.map))
    columns = _columns_map(body["columns"]) if "columns" in body else held.columns
    entries = [{"table": mapping[n], "path": p, "name": n, "columns": columns.get(n)} for n, p in held.files]
    return _ingest_job(project.name, entries, principal, dry_run=False, force=_flag(body.get("force")),
                       skip_invalid=_flag(body.get("skip_invalid")), strict=_flag(body.get("strict")),
                       upload_id=held.id, folder=held.files[0][1].parent)


def _columns_map(value: Any) -> dict[str, Any]:
    """여러 파일의 머리글 맞춤: {파일 이름: {머리글: 열 또는 null}}."""
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise IngestError(400, "invalid_request", "columns는 {파일 이름: {머리글: 열 또는 null}}입니다") from None
    if not isinstance(value, dict):
        raise IngestError(400, "invalid_request", "columns는 {파일 이름: {머리글: 열 또는 null}}입니다")
    return {str(k): _columns(v) for k, v in value.items()}


# ---------------------------------------------------------------- 빈 양식, 머리글 맞춤 저장

_EXAMPLES = {"text": "", "integer": "0", "number": "0", "boolean": "true", "timestamp": "2026-01-01T00:00:00+09:00"}


@router.get("/tables/{table}/template")
def get_template(table: str, principal: Principal = Depends(require_operator)):
    """빈 양식(설계서 9-2): 머리글(열 이름)과 예시 한 줄. 엑셀에서 열리도록 UTF-8 BOM. 데이터 값은 싣지 않는다."""
    import csv
    import io

    project = _project()
    name = _table(project, table)
    spec = project.schema["tables"][name]
    example = []
    for col, c in spec["columns"].items():
        if "default" in c and c["default"] is not None:
            raw = c["default"]
            example.append(("true" if raw else "false") if isinstance(raw, bool) else str(raw))
        elif c["type"] == "enum":
            example.append(c["values"][0])
        elif c["type"] == "date":
            example.append(str(c.get("min") or "2026-01-01"))
        elif c["type"] in ("integer", "number") and "min" in c:
            example.append(str(c["min"]))
        else:
            example.append(_EXAMPLES.get(c["type"], ""))
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(list(spec["columns"]))
    w.writerow(example)
    return Response(("﻿" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}-template.csv"'})


@router.post("/tables/{table}/aliases")
async def post_aliases(table: str, request: Request, principal: Principal = Depends(require_admin)):
    """머리글 맞춤을 다음에도 쓰도록 aliases에 저장한다(admin. 설계서 5-3, 9-2 "다음에도 이렇게").

    {"aliases": {"<머리글>": "<열>"}, "revision": "<GET /tables/{t}의 revision>"} → 200 {"revision", "changed"}.
    schema.yaml의 column.update 연산으로 고친 곳만 다시 쓴다(Studio와 같은 저장). aliases는 DB와 상관없어 적용이 필요 없다.
    """
    body = await _json(request)
    project = _project()
    name = _table(project, table)
    aliases = body.get("aliases")
    if not isinstance(aliases, dict) or not aliases:
        raise IngestError(400, "invalid_request", "aliases는 {머리글: 열}입니다")
    if not isinstance(body.get("revision"), str):
        raise IngestError(400, "invalid_request", "revision이 필요합니다(설정을 읽을 때 받은 값)")
    cols = project.schema["tables"][name]["columns"]
    adds: dict[str, list[str]] = {}
    for header, col in aliases.items():
        if not isinstance(header, str) or not header.strip() or col not in cols:
            raise IngestError(422, "invalid_op", f"머리글 '{header}' → 열 '{col}'은 저장할 수 없습니다")
        if header == col or header in (cols[col].get("aliases") or []):
            continue
        adds.setdefault(col, list(cols[col].get("aliases") or [])).append(header.strip())
    ops = [{"op": "column.update", "table": name, "column": col, "set": {"aliases": values}}
           for col, values in adds.items()]

    def work() -> dict[str, Any]:
        if not ops:
            return {"revision": store.revision(project.root), "changed": []}
        try:
            with store.project_lock(project.name):
                out = store.save_ops(project.name, ops, body["revision"])
        except store.StoreError as exc:
            raise IngestError(exc.status, exc.detail, exc.message, exc.errors) from None
        _audit("data.table.alter", principal, request, project=project.name, table=name,
               aliases={c: v[-1:] for c, v in adds.items()})
        return {"revision": out["revision"], "changed": out["changed"]}

    return await run_in_threadpool(work)
