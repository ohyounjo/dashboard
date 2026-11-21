"""구조 변경(admin): 설정 연산, 새 테이블, 합치기, 설정 변경 이력과 되돌리기, 설정 내려받기, 계보(contracts/manage.md
"구조 변경", 설계서 6·7절).

라우팅과 권한만 갖는다. 로직은 dashkit/ingest의 structure·newtable·compact·lineage다(Studio와 같은 연산·저장 방식).
계보·이력 보기는 DB의 적재 스냅샷을 dash_inspect로 읽고, owner 접속은 쓰기 작업(구조 변경·합치기) 안에서만 열린다.
"""
from __future__ import annotations

import io
import json
import shutil
import zipfile
from typing import Any, Mapping

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from ... import config
from ...auth import store as auth_store
from ...auth.routes import Principal, client_ip, require_admin, require_operator
from ...ingest import IngestError, lineage, newtable, structure
from ...ingest.compact import compact
from ...project import store, upload
from .. import context
from ..runs_state import runs
from ..uploads import uploads
from .tables import _json, _lineage, _project, _receive, _table

router = APIRouter()

# 운용 화면의 구조 변경이 쓰는 연산(설계서 6-3). 탭·섹션·차트 같은 화면 연산은 Studio에서 한다
STRUCT_OPS = ("table.add", "table.update", "table.remove", "column.add", "column.update",
              "dataset.add", "dataset.update", "dataset.remove")
MAX_INFER_ROWS = 2_000_000


def _audit(action: str, principal: Principal, request: Request | None, target_type: str, target: str | None,
           **detail: Any) -> None:
    try:
        auth_store.record_audit(action, actor_id=principal.id, actor_email=principal.email, target_type=target_type,
                                target_id=target, target_label=target, detail=detail,
                                ip=client_ip(request) if request is not None else None)
    except Exception:     # 감사 로그 실패가 끝난 변경을 실패로 만들지 않는다
        pass


# ---------------------------------------------------------------- 계보

@router.get("/lineage")
def get_lineage(principal: Principal = Depends(require_operator)) -> dict[str, Any]:
    return _lineage(_project())


@router.get("/tables/{table}/used_by")
def get_used_by(table: str, principal: Principal = Depends(require_operator)) -> dict[str, Any]:
    """테이블의 "쓰이는 곳": 읽는 데이터셋 → 그래프(차트·위젯 섹션) → 탭."""
    project = _project()
    return lineage.used_by(_lineage(project), _table(project, table))


# ---------------------------------------------------------------- 설정 연산(PATCH /config)

def _check_ops(project: config.Project, op_list: Any) -> list[dict[str, Any]]:
    if not isinstance(op_list, list) or not op_list or not all(isinstance(o, dict) for o in op_list):
        raise IngestError(400, "invalid_request", "ops는 연산 객체의 목록입니다")
    bad = sorted({str(o.get("op")) for o in op_list if o.get("op") not in STRUCT_OPS})
    if bad:
        raise IngestError(422, "invalid_op", "운용 화면에서 쓸 수 없는 연산입니다(" + ", ".join(bad) + ")",
                          [f"쓸 수 있는 연산: {', '.join(STRUCT_OPS)}"])
    graph = None
    for o in op_list:
        name = o.get("table")
        spec = project.schema["tables"].get(name) if isinstance(name, str) else None
        if o["op"] == "table.remove" and spec is not None:
            graph = graph or _lineage(project)
            used = lineage.used_by(graph, name)
            refs = [f"{t}.{c}" for t, s in project.schema["tables"].items() for c, cs in s["columns"].items()
                    if str(cs.get("ref") or "").split(".")[0] == name and t != name]
            if used["datasets"] or refs:
                raise IngestError(409, "in_use", "쓰는 곳이 있는 테이블은 지우지 않습니다(데이터셋·참조를 먼저 정리하세요)",
                                  [f"데이터셋 {d}" for d in used["datasets"]] + [f"참조 {r}" for r in refs],
                                  used_by=used)
        if o["op"] == "dataset.remove" and o.get("key") in project.dashboard["datasets"]:
            graph = graph or _lineage(project)
            charts = [c["key"] for c in graph["charts"] if c["dataset"] == o["key"]]
            sections = [f"{s['tab']}#{s['index']}" for s in graph["sections"] if s["dataset"] == o["key"]]
            if charts or sections:
                raise IngestError(409, "in_use", "그래프가 쓰는 데이터셋은 지우지 않습니다",
                                  [f"차트 {c}" for c in charts] + [f"섹션 {s}" for s in sections])
    return op_list


def _affected(project: config.Project, op_list: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """유형을 바꾸는 열 연산이 닿는 그래프(설계서 6-2 "영향 목록")."""
    graph = None
    out = []
    for o in op_list:
        if o["op"] != "column.update" or "type" not in (o.get("set") or {}):
            continue
        graph = graph or _lineage(project)
        used = lineage.used_by(graph, o.get("table"))
        cols = graph["columns_used"]
        charts = [c["key"] for c in used["charts"] if o.get("column") in cols.get(c["dataset"], [])]
        out.append({"table": o.get("table"), "column": o.get("column"), "datasets": used["datasets"], "charts": charts})
    return out


def _audit_ops(principal: Principal, project_name: str, op_list: list[dict[str, Any]], result: Mapping[str, Any]) -> None:
    for o in op_list:
        kind = o["op"]
        if kind.startswith("dataset."):
            action = {"dataset.add": "data.dataset.create", "dataset.remove": "data.dataset.drop"}.get(
                kind, "data.dataset.alter")
            _audit(action, principal, None, "dataset", o.get("key"), project=project_name, op=kind,
                   history=result.get("history"))
        else:
            action = {"table.add": "data.table.create", "table.remove": "data.table.drop"}.get(kind, "data.table.alter")
            _audit(action, principal, None, "table", o.get("table"), project=project_name, op=kind,
                   column=o.get("column"), history=result.get("history"))


def _config_job(kind: str, project_name: str, principal: Principal, *, table: str | None, run_change,
                on_success=None) -> JSONResponse:
    limits = context.limits()

    def work(ctx) -> dict[str, Any]:
        ctx.progress("apply")
        result = run_change(ctx, limits)
        return result

    def done(run) -> None:
        if run.status == "succeeded" and on_success is not None:
            on_success(run.result or {})

    run = runs.start(kind, work, table=table, actor_id=principal.id, actor_email=principal.email, on_done=done)
    return JSONResponse({"job": run.public()}, status_code=202)


@router.patch("/config")
async def patch_config(request: Request, principal: Principal = Depends(require_admin)):
    """{ops, revision} → 202 작업(검증 → 이력·묶음 기록 → 저장 → load → sync). contracts/manage.md "구조 변경"."""
    body = await _json(request)
    project = _project()
    revision = body.get("revision")
    if not isinstance(revision, str):
        raise IngestError(400, "invalid_request", "revision이 필요합니다(설정을 읽을 때 받은 값)")
    op_list = _check_ops(project, body.get("ops"))
    await run_in_threadpool(structure.validate, project.name, op_list)       # 형식·설정 검사는 바로 알린다
    affected = await run_in_threadpool(_affected, project, op_list)
    tables = {o.get("table") for o in op_list if isinstance(o.get("table"), str)}

    def change(ctx, limits) -> dict[str, Any]:
        result = structure.change_config(project.name, op_list, revision, action=_action(op_list), log_line=ctx.log,
                                         wait_ms=limits.lock_wait_ms)
        result["affected"] = affected
        added = [(o["table"], o["column"]) for o in op_list if o["op"] == "column.add"]
        if added and result["changed"]:
            graph = _lineage(config.load_project(project.name))
            result["datasets_without_column"] = [
                {"table": t, "column": c, "datasets": lineage.without_column(graph, t, c)} for t, c in added]
        return result

    return _config_job("config", project.name, principal, table=next(iter(tables)) if len(tables) == 1 else None,
                       run_change=change, on_success=lambda r: r.get("changed") and _audit_ops(principal, project.name,
                                                                                               op_list, r))


def _action(op_list: list[dict[str, Any]]) -> str:
    """이력 폴더 이름의 동작(영문 소문자·하이픈만)."""
    kinds = sorted({o["op"].replace(".", "-") for o in op_list})
    return kinds[0] if len(kinds) == 1 else "config"


# ---------------------------------------------------------------- 새 테이블

@router.post("/tables/infer")
async def post_infer(request: Request, principal: Principal = Depends(require_admin)):
    """multipart `file`(CSV 하나) → 200 {"upload_id", "table": 추론, "mode", "dataset", "suggest"}. 파일은 쓰지 않고 1시간 잡아 둔다."""
    folder, files, _fields = await _receive(request, "file", single=True)
    source_name, path = files[0]
    project = _project()

    def infer() -> dict[str, Any]:
        content = path.read_bytes()
        table = newtable.infer_one(project, source_name, content)
        if int(table.get("rows") or 0) > MAX_INFER_ROWS:
            raise IngestError(413, "too_many_rows", f"새 테이블의 첫 파일은 {MAX_INFER_ROWS}행까지입니다")
        return table

    try:
        table = await run_in_threadpool(infer)
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    upload_id = uploads.hold("+new", files, principal.id)
    return {"upload_id": upload_id, "source_name": source_name, "table": table, "mode": "append",
            "on_conflict": "error", "dataset": True, "suggest": False}


@router.post("/tables")
async def post_table(request: Request, principal: Principal = Depends(require_admin)):
    """{upload_id, table: {name, primary_key, columns: [{name, type, source?, required?, values?, ref?, …}]}, mode?,
    on_conflict?, dataset?, suggest?, revision} → 202 작업(table.add·dataset.add(·초안 그래프) + 첫 파일 → apply)."""
    body = await _json(request)
    project = _project()
    held = uploads.get(body.get("upload_id"), "+new", principal.id)
    source_name, path = held.files[0]
    revision = body.get("revision")
    if not isinstance(revision, str):
        raise IngestError(400, "invalid_request", "revision이 필요합니다(설정을 읽을 때 받은 값)")
    table = body.get("table")
    if not isinstance(table, dict):
        raise IngestError(400, "invalid_request", "table이 필요합니다(추론 결과를 고친 것)")
    mode = body.get("mode") or "append"
    on_conflict = body.get("on_conflict") if mode == "append" else None
    if on_conflict not in (None, "error", "skip", "update"):
        raise IngestError(422, "invalid_request", "on_conflict는 error, skip, update입니다")
    dataset = body.get("dataset", True) is not False
    suggest = body.get("suggest") is True

    def build() -> tuple[list[dict[str, Any]], dict[str, bytes]]:
        return newtable.build(project, table, path.read_bytes(), source_name, mode=mode,
                              on_conflict=None if on_conflict in (None, "error") else on_conflict,
                              dataset=dataset, suggest=suggest)

    op_list, first = await run_in_threadpool(build)        # 이름·규칙 오류는 바로 422로 알린다
    name = op_list[0]["table"]
    await run_in_threadpool(structure.validate, project.name, op_list, first)

    def change(ctx, limits) -> dict[str, Any]:
        result = structure.change_config(project.name, op_list, revision, action="table-create", extra=first,
                                         log_line=ctx.log, wait_ms=limits.lock_wait_ms)
        result["table"] = name
        result["dataset"] = next((o["key"] for o in op_list if o["op"] == "dataset.add"), None)
        result["dashboard"] = next((o["key"] for o in op_list if o["op"] == "dashboard.add"), None)
        result["tab"] = next((o["tab"]["key"] for o in op_list if o["op"] == "tab.add"), None)
        return result

    def success(result: Mapping[str, Any]) -> None:
        uploads.drop(held.id)
        _audit("data.table.create", principal, None, "table", name, project=project.name, mode=mode,
               source_name=source_name, dataset=result.get("dataset"), dashboard=result.get("dashboard"),
               history=result.get("history"))

    return _config_job("create", project.name, principal, table=name, run_change=change, on_success=success)


# ---------------------------------------------------------------- 합치기

@router.post("/tables/{table}/compact")
async def post_compact(table: str, request: Request, principal: Principal = Depends(require_admin)):
    await upload.read_body(request.stream(), request.headers.get("content-length"), 1024 * 1024)
    project = _project()
    name = _table(project, table)
    if (project.schema["tables"][name].get("mode") or "replace") != "append":
        raise IngestError(409, "not_append", "누적(append) 테이블만 합칩니다")

    def change(ctx, limits) -> dict[str, Any]:
        return compact(project.name, name, wait_ms=limits.lock_wait_ms)

    def success(result: Mapping[str, Any]) -> None:
        if result.get("file"):
            _audit("data.batch.compact", principal, None, "table", name, project=project.name, file=result["file"],
                   merged=result.get("merged"), rows=result.get("rows"))

    return _config_job("compact", project.name, principal, table=name, run_change=change, on_success=success)


# ---------------------------------------------------------------- 설정 변경 이력·되돌리기·내려받기

@router.get("/history")
def get_history(principal: Principal = Depends(require_admin)) -> dict[str, Any]:
    project = _project()
    items = structure.list_history(project.name)
    return {"items": items, "total": len(items), "revision": store.revision(project.root)}


@router.post("/history/{hid}/restore")
async def post_history_restore(hid: str, request: Request, principal: Principal = Depends(require_admin)):
    """{revision} → 202 작업. 그 작업이 바꾼 파일을 **함께** 변경 전으로 되돌리고 apply한다(되돌리기도 구조 변경이다)."""
    body = await _json(request)
    project = _project()
    revision = body.get("revision")
    if not isinstance(revision, str):
        raise IngestError(400, "invalid_request", "revision이 필요합니다(설정을 읽을 때 받은 값)")
    if not any(m.get("id") == hid for m in structure.list_history(project.name)):
        raise IngestError(404, "unknown_history", "그 설정 이력이 없습니다")

    def change(ctx, limits) -> dict[str, Any]:
        # 그 뒤에 다시 바뀐 파일이 있으면 409 stale_config(검사는 작업이 쓰기 잠금을 잡은 뒤에 한다)
        result = structure.change_config(project.name, None, revision, action="restore", restore=hid,
                                         log_line=ctx.log, wait_ms=limits.lock_wait_ms)
        result["restored"] = hid
        return result

    def success(result: Mapping[str, Any]) -> None:
        _audit("data.config.restore", principal, None, "project", project.name, project=project.name, restored=hid,
               history=result.get("history"), files=result.get("changed"))

    return _config_job("restore_config", project.name, principal, table=None, run_change=change, on_success=success)


@router.get("/export")
def get_export(principal: Principal = Depends(require_admin)) -> Response:
    """설정 파일 내려받기(zip: schema.yaml, dashboard.yaml. 데이터는 넣지 않는다). 사람이 저장소에 커밋한다(D4)."""
    project = _project()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in store.CONFIG_FILES:
            path = project.root / name
            if path.is_file():
                z.writestr(f"{project.name}/{name}", path.read_bytes())
        z.writestr(f"{project.name}/REVISION", json.dumps({"revision": store.revision(project.root)}) + "\n")
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{project.name}-config.zip"'})
