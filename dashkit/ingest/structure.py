"""구조 변경: 설정 파일을 고치고 적용하기(설계서 6-1~6-3, contracts/ingest.md "구조 변경").

구조 변경은 **설정 파일을 고치고 apply**다. Studio와 같은 연산(dashkit/project/ops.py)과 저장 방식(고친 곳만 다시 쓰기 →
사본 검증)을 쓰고, 파일 여러 개(두 YAML, replace 테이블의 CSV, 새 테이블의 첫 파일)를 바꾸므로 묶음 기록(종류 config)을
쓴다. 순서:

    쓰기 잠금(설정 revision 확인, 데이터 설정 지문은 보지 않는다) → 복구 → 새 내용 만들기·사본 검증 → 설정 이력(변경 전 사본)
    → 묶음 기록(바꿀 파일, 변경 전 사본, 새 설정의 데이터 설정 지문) → 파일 바꾸기 → load(같은 잠금, 한 트랜잭션) → 커밋
    → 묶음 기록 지우기 → (잠금 밖) sync

복구(종류 config): DB의 data_revision이 기록의 새 지문과 같고 모든 파일이 새 판이면(load까지 끝났다) 그대로 두고 기록만
지운다. 그 밖에는 변경 전 사본으로 모든 파일을 되돌리고 새로 만든 파일은 지운다. load와 `dashkit check`는 설정을 읽기 전에
recover_before_read로 이 복구를 먼저 한다.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import psycopg

from .. import config, db
from ..loader.record import data_revision
from ..project import locks
from . import ledger, pending, recover
from .faults import fault
from .operation import IngestError, StaleConfig, locked

log = logging.getLogger("dashkit.ingest")

HISTORY_KEEP = 50


def _sha(content: bytes | None) -> str | None:
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _safe_rel(rel: str) -> str:
    """프로젝트 기준 경로(우리가 만든 값만 온다). 경로 탈출을 한 번 더 막는다."""
    parts = Path(rel).parts
    if not parts or Path(rel).is_absolute() or any(p in ("", ".", "..") for p in parts):
        raise IngestError(500, "internal_error", f"잘못된 파일 경로: {rel}")
    return rel


# ---------------------------------------------------------------- 설정 이력

def history_root(project: str) -> Path:
    from ..project import store

    return store.state_dir() / "history" / project


def _new_history_id(action: str) -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{action}"


def _write_history(project: str, hid: str, action: str, files: list[tuple[str, bytes | None, str | None]]) -> None:
    """변경 전 사본과 전·후 해시를 남긴다(.state/history/<프로젝트>/<시각>-<동작>/). 최근 HISTORY_KEEP개만.

    files는 [(경로, 변경 전 내용 | None, 새 내용의 해시 | None)]. load까지 끝난 변경만 남긴다(되돌려진 변경은 남기지 않는다).
    변경 전 사본은 그때까지 묶음 기록에 있으므로, 커밋 뒤 이력을 쓰기 전에 끊겨도 복구가 기록에서 이력을 쓴다.
    """
    root = history_root(project) / hid
    if (root / "meta.json").is_file():
        return
    (root / "before").mkdir(parents=True, exist_ok=True)
    meta = {"id": hid, "action": action, "at": datetime.now(timezone.utc).isoformat(), "files": []}
    for rel, before, after_sha in files:
        if before is not None:
            target = root / "before" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(before)
        meta["files"].append({"path": rel, "before": _sha(before), "after": after_sha})
    tmp = root / ".meta.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, root / "meta.json")          # meta.json이 있으면 다 쓴 이력이다
    entries = sorted(p for p in history_root(project).iterdir() if p.is_dir())
    for old in entries[:-HISTORY_KEEP]:
        shutil.rmtree(old, ignore_errors=True)


def _history_from_record(project: str, record: Mapping[str, Any]) -> None:
    if not record.get("history"):
        return
    _write_history(project, record["history"], str(record.get("action") or "config"),
                   [(f["path"], base64.b64decode(f["before"]) if f.get("before") is not None else None, f.get("after"))
                    for f in record.get("files") or []])


def list_history(project: str) -> list[dict[str, Any]]:
    root = history_root(project)
    if not root.is_dir():
        return []
    out = []
    for p in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        try:
            meta = json.loads((p / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append(meta)
    return out


def history_files(project: str, hid: str) -> dict[str, bytes | None]:
    """되돌릴 내용: {경로: 변경 전 내용(그 작업이 새로 만든 파일이면 None = 지운다)}. 지금 내용이 그 작업 바로 뒤의 판이
    아니면(그 뒤에 다시 바뀌었으면) 409 stale_config."""
    from ..project import store

    entry = next((m for m in list_history(project) if m.get("id") == hid), None)
    if entry is None:
        raise IngestError(404, "unknown_history", "그 설정 이력이 없습니다")
    root = store.projects_dir() / project
    out: dict[str, bytes | None] = {}
    for f in entry["files"]:
        rel = _safe_rel(f["path"])
        if _sha(_read(root / rel)) != f["after"]:
            raise IngestError(409, "stale_config", f"{rel}이 그 작업 뒤에 다시 바뀌어 되돌리지 않았습니다")
        out[rel] = (history_root(project) / hid / "before" / rel).read_bytes() if f["before"] is not None else None
    return out


# ---------------------------------------------------------------- 묶음 기록(종류 config)

def _write_record(project: config.Project, files: Mapping[str, tuple[bytes | None, bytes | None]],
                  new_revision: str, hid: str, action: str, loaded_at: Any) -> tuple[Path, dict[str, Any]]:
    rid = pending.new_id()
    # loaded_at: 작업 전 적재 시각. 이 작업의 load가 커밋되면 바뀐다(커밋 표식. data_revision만으로는 설명·aliases처럼
    # 지문에 들지 않는 변경의 커밋 여부를 가를 수 없다)
    record = {"id": rid, "kind": "config", "project": project.name, "data_revision": new_revision,
              "history": hid, "action": action, "loaded_at": loaded_at.isoformat() if loaded_at is not None else None,
              "files": [{"path": rel, "before": base64.b64encode(b).decode() if b is not None else None,
                         "after": _sha(a)} for rel, (b, a) in files.items()]}
    return pending.write_record(project.data_dir, record), record


def recover_config(project: config.Project, conn: psycopg.Connection | None, record: Mapping[str, Any], mode: str):
    """종류 config의 복구(recover.RECOVERERS). conn이 없으면(DB를 볼 수 없음) 되돌린다."""
    rid = str(record.get("id"))
    files = record.get("files") or []
    loaded_now = False
    if conn is not None:
        with conn.cursor() as cur:
            now = ledger.loaded(cur)
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            conn.commit()
        stamp = now.loaded_at.isoformat() if now.loaded_at is not None else None
        # 이 작업의 load가 커밋됐다: 적재 시각이 작업 전과 다르고(다시 만들었다) 지문이 새 설정의 것이다
        loaded_now = stamp != record.get("loaded_at") and now.data_revision == record.get("data_revision")
    root = project.root
    all_new = all(_sha(_read(root / _safe_rel(f["path"]))) == f.get("after") for f in files)
    if loaded_now and all_new:
        _history_from_record(project.name, record)       # 커밋 뒤 이력을 쓰기 전에 끊겼으면 여기서 쓴다
        return recover.Outcome(rid, "config", "committed", [f["path"] for f in files])
    for f in files:
        path = root / _safe_rel(f["path"])
        if f.get("before") is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            from ..project.store import _replace_atomic

            _replace_atomic(path, base64.b64decode(f["before"]))
    return recover.Outcome(rid, "config", "discarded", [f["path"] for f in files])


def recover_before_read(name: str, projects_dir: Path | None = None, *, dbname: str | None = None,
                        wait_ms: int = 10_000) -> list[recover.Outcome]:
    """설정을 읽기 전에(load·check·apply) 끝나지 않은 구조 변경을 정리한다. 기록이 없으면 DB에 닿지 않는다."""
    root = (Path(projects_dir) if projects_dir else config.default_projects_dir()) / name
    if not any(r and r.get("kind") == "config" for _p, r in pending.list_records(root / "data")):
        return []          # 기록이 없으면 DB에 닿지 않는다(잠금을 잡은 뒤 다시 읽는다)
    shell = config.Project(name=name, root=root, schema={"tables": {}}, dashboard={})
    try:
        conn = db.owner_conn(dbname)
    except (psycopg.Error, RuntimeError):
        return []          # DB에 닿지 못하면 판정할 수 없다. 기록을 그대로 두고 다음 작업·적용이 처리한다
    try:
        locks.acquire(conn, wait_ms)
        with conn.cursor() as cur:
            loaded_project = ledger.loaded(cur).project
        conn.commit()
    except (psycopg.Error, locks.LockBusy):
        conn.close()
        return []
    use = conn if loaded_project == name else None      # 다른 프로젝트가 적재돼 있으면 이 변경의 load는 없었다
    outcomes = []
    try:
        # 잠금을 기다리는 사이 원래 작업이나 다른 복구가 기록을 처리했을 수 있다. 잠금을 잡은 뒤 남은 기록만 본다
        records = [(p, r) for p, r in pending.list_records(root / "data") if r and r.get("kind") == "config"]
        for path, record in records:
            outcome = recover_config(shell, use, record, "load")
            pending.remove_record(path)
            log.info("끊긴 구조 변경 %s: %s", outcome.id, outcome.result)
            outcomes.append(outcome)
    finally:
        locks.release(conn)
        conn.close()
    return outcomes


# ---------------------------------------------------------------- 구조 변경 한 번

def _csv_add_column(content: bytes, column: str, value: str) -> bytes:
    """replace 테이블의 CSV에 열 하나를 더한다(값은 default 또는 빈 칸). 설계서 6-2."""
    text = content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    for i, row in enumerate(rows):
        if not row or (len(row) == 1 and row[0].strip() == ""):
            continue
        w.writerow(row + [column if i == 0 else value])
    return out.getvalue().encode("utf-8")


def _default_text(spec: Mapping[str, Any]) -> str:
    if "default" not in spec or spec["default"] is None:
        return ""
    raw = spec["default"]
    return ("true" if raw else "false") if isinstance(raw, bool) else str(raw)


def plan_files(project: config.Project, op_list: list[Mapping[str, Any]],
               extra: Mapping[str, bytes | None] | None = None) -> dict[str, tuple[bytes | None, bytes | None]]:
    """연산과 함께 바뀌는 파일들: {경로: (지금 내용, 새 내용)}. 새 내용이 None이면 지운다.

    - YAML: 연산을 적용해 고친 곳만 다시 쓴다(store.edit_yaml).
    - column.add가 replace 테이블에 열을 더하면 그 테이블의 CSV에도 열을 더한다(6-2).
    """
    from ..project import ops, store

    try:
        ops.check_ops(op_list)
    except ops.OpError as exc:
        raise IngestError(400, "invalid_op", str(exc), [str(exc)], op_index=exc.index) from None
    out: dict[str, tuple[bytes | None, bytes | None]] = {}
    for file in ops.files_of(op_list):
        before = _read(project.root / file)
        if before is None:
            raise IngestError(422, "invalid_config", "설정 파일을 읽을 수 없습니다", [f"{file}:(최상위): 파일이 없음"])
        try:
            text, _how = store.edit_yaml(before.decode("utf-8"), lambda d, f=file: ops.apply_ops(d, op_list, f))
        except store.StoreError as exc:
            raise IngestError(exc.status, exc.detail, exc.message, exc.errors) from None
        except ops.OpError as exc:
            raise IngestError(400, "invalid_op", str(exc), [str(exc)], op_index=exc.index) from None
        out[file] = (before, text.encode("utf-8"))
    for item in op_list:
        if item.get("op") != "column.add":
            continue
        spec = project.schema["tables"].get(item["table"])
        if spec is None or (spec.get("mode") or "replace") == "append":
            continue
        rel = f"data/{item['table']}.csv"
        current = out[rel][1] if rel in out else _read(project.root / rel)
        if current is not None:
            out[rel] = (out[rel][0] if rel in out else current,
                        _csv_add_column(current, str(item["column"]), _default_text(item.get("value") or {})))
    for rel, content in (extra or {}).items():
        out[_safe_rel(rel)] = (_read(project.root / rel), content)
    return {k: v for k, v in out.items() if v[0] != v[1]}


def check_mode_switch(project: config.Project, new_project: config.Project) -> None:
    """배치 파일이 있는 append 테이블이 새 설정에서 replace가 되는 변경은 거절한다(배치를 읽지 않게 되어 그 행이
    사라진다). 연산이든 이력 되돌리기든 새 설정과 견주어 본다. 폴더 상태를 보는 검사라 쓰기 잠금 안에서 파일을 바꾸기
    전에 다시 한다(요청을 받을 때도 미리 본다)."""
    from . import batches

    for name, spec in project.schema["tables"].items():
        new = new_project.schema["tables"].get(name)
        if new is None or (spec.get("mode") or "replace") != "append" or (new.get("mode") or "replace") == "append":
            continue
        if batches.list_batches(project.data_dir, name)[0]:
            raise IngestError(422, "invalid_op", f"배치 파일이 있는 누적 테이블({name})은 replace로 바꾸지 않습니다"
                              "(배치를 읽지 않게 됩니다)")


def _staged(project: config.Project, changes: Mapping[str, tuple[bytes | None, bytes | None]]):
    """바뀐 설정·replace CSV를 사본에 덮어 검증한다(store.prepare). 실패하면 IngestError(422)."""
    from ..project import store

    yaml_new = {k: v[1] for k, v in changes.items() if k in store.CONFIG_FILES and v[1] is not None}
    csv_new = {k: v[1] for k, v in changes.items() if k.startswith("data/") and k.endswith(".csv")
               and "/" not in k[len("data/"):] and v[1] is not None}
    try:
        return store.prepare(project.name, {**yaml_new, **csv_new}, with_csv=True)
    except store.StoreError as exc:
        raise IngestError(exc.status, exc.detail, exc.message, exc.errors) from None


def validate(project_name: str, op_list: list[Mapping[str, Any]],
             extra: Mapping[str, bytes | None] | None = None) -> list[str]:
    """잠금 없이 미리 검사한다(요청을 받을 때 400·422를 바로 알리려고). 작업은 잠금 안에서 다시 검사한다. 바뀔 파일 목록."""
    try:
        project = config.load_project(project_name)
    except config.ConfigError as exc:
        raise IngestError(422, "invalid_config", "지금 설정이 검사를 통과하지 못합니다", exc.errors) from None
    changes = plan_files(project, op_list, extra)
    if changes:
        check_mode_switch(project, _staged(project, changes).project)
    return sorted(changes)


def change_config(project_name: str, op_list: list[Mapping[str, Any]] | None, revision: str | None, *, action: str,
                  files: Mapping[str, bytes | None] | None = None, extra: Mapping[str, bytes | None] | None = None,
                  run_sync: bool = True, log_line: Callable[[str], None] | None = None,
                  dbname: str | None = None, wait_ms: int | None = None) -> dict[str, Any]:
    """구조 변경 한 번(연산 또는 파일 내용). revision은 필수다(그 사이 바뀌었으면 409 stale_config).

    결과: {"changed": [경로], "history": id, "revision", "counts", "sync": {"ok", "errors"}}
    """
    from ..loader import LoadError, rebuild_locked
    from ..project import store

    say = log_line or (lambda _s: None)
    if revision is None:
        raise IngestError(400, "invalid_request", "revision이 필요합니다(설정을 읽을 때 받은 값)")
    recover_before_read(project_name, dbname=dbname)
    try:
        project = config.load_project(project_name)
    except config.ConfigError as exc:
        raise IngestError(422, "invalid_config", "지금 설정이 검사를 통과하지 못합니다", exc.errors) from None
    try:
        conn = db.owner_conn(dbname)
    except (psycopg.Error, RuntimeError) as exc:
        raise IngestError(503, "db_unavailable", f"DB 접속 실패: {exc}") from None
    record_path: Path | None = None
    try:
        # 같은 프로세스의 다른 설정 저장(aliases 저장 등)과도 겹치지 않게 프로젝트 잠금을 함께 잡는다
        with store.project_lock(project.name), \
                locked(project, conn, revision=revision, check_data_revision=False, wait_ms=wait_ms) as session:
            if files is not None:
                changes = {_safe_rel(k): (_read(project.root / k), v) for k, v in files.items()}
                changes = {k: v for k, v in changes.items() if v[0] != v[1]}
            else:
                changes = plan_files(project, op_list or [], extra)
            if not changes:
                return {"changed": [], "history": None, "revision": store.revision(project.root), "counts": None,
                        "sync": None}
            say(f"▶ 검사: {', '.join(changes)}")
            staged = _staged(project, changes)
            check_mode_switch(project, staged.project)          # 잠금 안에서 폴더를 다시 본다(이력 되돌리기 포함)
            new_revision = data_revision(staged.project.schema, staged.project.dashboard)
            hid = _new_history_id(action)
            record_path, record = _write_record(project, changes, new_revision, hid, action, session.loaded.loaded_at)
            for rel, (_b, after) in changes.items():
                path = project.root / rel
                if after is None:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                else:
                    store._replace_atomic(path, after)
                fault("after_move")
            say("▶ load: 다시 만들기")
            new_project = config.load_project(project.name)
            try:
                with conn.transaction():
                    counts = rebuild_locked(new_project, conn)
                    fault("before_commit")
                fault("after_commit")
                _history_from_record(project.name, record)
            except LoadError as exc:
                # 확정적인 실패(검증·뷰·checks). 복구 절차로 모든 파일을 되돌린다(DB는 롤백됐다)
                recover.recover(project, conn, mode="online", only=record["id"])
                record_path = None
                raise IngestError(422, "load_failed", "적용(load)에 실패해 모든 파일을 되돌렸습니다", exc.errors) from None
            pending.remove_record(record_path)
            record_path = None
            say(f"✔ load: {sum(counts.values())}행")
        result: dict[str, Any] = {"changed": sorted(changes), "history": hid, "revision": store.revision(project.root),
                                  "counts": counts, "sync": None}
    except StaleConfig:
        raise IngestError(409, "stale_config", "설정 파일이 그 사이에 바뀌었습니다. 다시 불러온 뒤 시도하세요") from None
    except Exception:
        # 그 밖의 실패(DB 연결 끊김 등): 잠금을 놓은 뒤 복구가 DB의 지문으로 커밋 여부를 판정해 파일을 정리한다
        if record_path is not None:
            conn.close()
            try:
                recover_before_read(project_name, dbname=dbname)
                record_path = None
            except Exception:
                log.exception("구조 변경의 복구 실패. 다음 작업·적용이 다시 복구한다")
        raise
    finally:
        if record_path is not None:
            log.warning("구조 변경이 끝나지 않음. 다음 작업·적용이 복구한다(%s)", record_path.name)
        conn.close()
    if run_sync:
        result["sync"] = _sync(project.name, say)
    return result


def _sync(name: str, say: Callable[[str], None]) -> dict[str, Any]:
    """Superset 반영(잠금 밖). 실패하면 파일과 DB는 새 판이고 다시 적용하면 이어 간다."""
    try:
        from ..sync import SyncError, sync

        say("▶ sync: Superset에 반영")
        sync(config.load_project(name), log=say)
        say("✔ sync")
        return {"ok": True, "errors": []}
    except Exception as exc:          # sync는 외부 서비스라 어떤 오류든 결과로 알린다
        from ..project.jobs import scrub, secret_values

        message = scrub(f"{type(exc).__name__}: {exc}", secret_values())
        say(f"✖ sync: {message}")
        return {"ok": False, "errors": [message]}
