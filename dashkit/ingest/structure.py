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
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

import psycopg
import yaml
from psycopg import sql

from .. import config, db
from ..loader import record as loader_record
from ..loader import validate as loader_validate
from ..loader.record import data_revision
from ..project import locks
from . import batches, compact, ledger, normalize, pending, recover
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


def _write_history(project: str, hid: str, action: str, files: list[tuple[str, bytes | None, str | None]],
                   *, irreversible: bool = False) -> None:
    """변경 전 사본과 전·후 해시를 남긴다(.state/history/<프로젝트>/<시각>-<동작>/). 최근 HISTORY_KEEP개만.

    files는 [(경로, 변경 전 내용 | None, 새 내용의 해시 | None)]. load까지 끝난 변경만 남긴다(되돌려진 변경은 남기지 않는다).
    변경 전 사본은 그때까지 묶음 기록에 있으므로, 커밋 뒤 이력을 쓰기 전에 끊겨도 복구가 기록에서 이력을 쓴다.
    """
    root = history_root(project) / hid
    if (root / "meta.json").is_file():
        return
    (root / "before").mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"id": hid, "action": action, "at": datetime.now(timezone.utc).isoformat(), "files": []}
    if irreversible:
        meta["irreversible"] = True
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
                    for f in record.get("files") or []],
                   irreversible=bool(record.get("irreversible")))


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
    if entry.get("irreversible"):
        raise IngestError(409, "irreversible_history", "배치 합치기를 동반한 구조 변경은 이전 판으로 되돌릴 수 없습니다")
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
                  new_revision: str, hid: str, action: str, loaded_at: Any,
                  *, compactions: list[dict[str, Any]] | None = None) -> tuple[Path, dict[str, Any]]:
    rid = pending.new_id()
    # loaded_at: 작업 전 적재 시각. 이 작업의 load가 커밋되면 바뀐다(커밋 표식. data_revision만으로는 설명·aliases처럼
    # 지문에 들지 않는 변경의 커밋 여부를 가를 수 없다)
    record: dict[str, Any] = {
        "id": rid, "kind": "config", "project": project.name, "data_revision": new_revision,
        "history": hid, "action": action, "loaded_at": loaded_at.isoformat() if loaded_at is not None else None,
        "files": [{"path": rel, "before": base64.b64encode(b).decode() if b is not None else None,
                   "after": _sha(a)} for rel, (b, a) in files.items()],
    }
    if compactions:
        record["compactions"] = compactions
        record["irreversible"] = True
    return pending.write_record(project.data_dir, record), record


def recover_config(project: config.Project, conn: psycopg.Connection | None, record: Mapping[str, Any], mode: str):
    """종류 config의 복구(recover.RECOVERERS). conn이 없으면(DB를 볼 수 없음) 되돌린다."""
    rid = str(record.get("id"))
    files = record.get("files") or []
    compactions = record.get("compactions") or []
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
    all_compactions_ready = True
    for comp in compactions:
        final, temp, _merged, _moves = compact._paths(project, comp)
        new_file = final if final.exists() else temp if temp.exists() else None
        if new_file is None or batches.content_hash(new_file) != comp.get("sha256"):
            all_compactions_ready = False
            break
    if loaded_now and all_new and all_compactions_ready:
        for comp in compactions:
            compact._finish(project, comp)
        _history_from_record(project.name, record)       # 커밋 뒤 이력을 쓰기 전에 끊겼으면 여기서 쓴다
        for comp in compactions:
            shutil.rmtree(batches.table_dir(project.data_dir, comp["table"]) / batches.MERGED_DIR, ignore_errors=True)
        return recover.Outcome(rid, "config", "committed", [f["path"] for f in files])
    for comp in compactions:
        final, temp, merged, moves = compact._paths(project, comp)
        for src, dst in moves:
            if dst.exists() and not src.exists():
                os.replace(dst, src)
        for p in (final, temp):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        shutil.rmtree(merged, ignore_errors=True)
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


_BIGINT_MIN = -(1 << 63)
_BIGINT_MAX = (1 << 63) - 1
_MAX_ROW_ERRORS = 200


def _expand_label_ops(project: config.Project, op_list: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """table: 데이터셋의 labels에 있는 열이 이름 변경·삭제될 때 labels도 함께 갱신하는 연산을 덧붙인다."""
    out = [dict(o) for o in op_list]
    explicit = {
        o.get("key")
        for o in op_list
        if o.get("op") == "dataset.update" and isinstance(o.get("set"), Mapping) and "labels" in o["set"]
    }
    ds_labels: dict[str, dict[str, Any]] = {}
    changed_ds: set[str] = set()
    for dskey, ds in project.dashboard["datasets"].items():
        if "table" in ds and isinstance(ds.get("labels"), Mapping) and dskey not in explicit:
            ds_labels[dskey] = dict(ds["labels"])

    for o in op_list:
        op_name = o.get("op")
        if op_name not in ("column.rename", "column.remove"):
            continue
        tname, cname = o.get("table"), o.get("column")
        if not isinstance(tname, str) or not isinstance(cname, str):
            continue
        for dskey, ds in project.dashboard["datasets"].items():
            if ds.get("table") != tname or dskey not in ds_labels:
                continue
            if cname in (ds.get("metrics") or {}):
                continue
            labels = ds_labels[dskey]
            if cname not in labels:
                continue
            if op_name == "column.remove":
                del labels[cname]
            else:
                to = str(o.get("to"))
                ds_labels[dskey] = {(to if k == cname else k): v for k, v in labels.items()}
            changed_ds.add(dskey)

    for dskey in sorted(changed_ds):
        labels = ds_labels[dskey]
        out.append({"op": "dataset.update", "key": dskey, "set": {"labels": labels if labels else None}})
    return out


def _table_evolution_plans(project: config.Project, op_list: list[Mapping[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], dict[str, bool]]:
    """테이블별 최종 열 매핑과 데이터 재작성 필요 여부를 계산한다."""
    from ..project import ops

    cols_state: dict[str, list[dict[str, Any]]] = {}
    needs_rewrite: dict[str, bool] = {}
    for tname, spec in project.schema["tables"].items():
        cols_state[tname] = [
            {"name": c, "source": c, "old_type": cs["type"], "type": cs["type"],
             "is_new": False, "default": "", "narrowed": False}
            for c, cs in spec["columns"].items()
        ]
        needs_rewrite[tname] = False

    for o in op_list:
        op_name = o.get("op")
        tname = o.get("table")
        if not isinstance(tname, str) or tname not in cols_state:
            continue
        mode = project.schema["tables"][tname].get("mode") or "replace"
        state = cols_state[tname]
        if op_name == "column.add":
            val = o.get("value") or {}
            state.append({
                "name": str(o["column"]), "source": None, "old_type": None,
                "type": val.get("type"), "is_new": True, "default": _default_text(val), "narrowed": False,
            })
            if mode == "replace":
                needs_rewrite[tname] = True
        elif op_name == "column.rename":
            for entry in state:
                if entry["name"] == o.get("column"):
                    entry["name"] = str(o["to"])
                    break
            needs_rewrite[tname] = True
        elif op_name == "column.remove":
            cols_state[tname] = [e for e in state if e["name"] != o.get("column")]
            needs_rewrite[tname] = True
        elif op_name == "column.update":
            new_type = (o.get("set") or {}).get("type") if isinstance(o.get("set"), Mapping) else None
            if isinstance(new_type, str):
                for entry in state:
                    if entry["name"] == o.get("column") and new_type != entry["type"]:
                        if (entry["type"], new_type) in ops._NARROWER:
                            entry["narrowed"] = True
                            needs_rewrite[tname] = True
                        elif mode == "replace":
                            needs_rewrite[tname] = True
                        entry["type"] = new_type
                        break
    return cols_state, needs_rewrite


def _convert_cell(raw_text: str, new_spec: Mapping[str, Any], *,
                  old_type: str | None, is_narrowed: bool) -> tuple[str | None, str | None]:
    if raw_text == "":
        if new_spec.get("required"):
            return None, "값이 필요함"
        return "", None
    target_type = new_spec["type"]
    if is_narrowed:
        if old_type == "number" and target_type == "integer":
            norm = normalize.normalize(raw_text, "number", {})
            try:
                d = Decimal(norm)
            except InvalidOperation:
                return None, f"정수로 바꿀 수 없음 '{raw_text}'"
            if not d.is_finite() or d != int(d):
                return None, f"정수로 바꿀 수 없음 '{raw_text}'"
            n = int(d)
            if not (_BIGINT_MIN <= n <= _BIGINT_MAX):
                return None, f"정수 범위를 벗어남 '{raw_text}'"
            text = str(n)
        elif old_type == "timestamp" and target_type == "date":
            norm = normalize.normalize(raw_text, "timestamp", {})
            try:
                dt = datetime.fromisoformat(norm)
            except ValueError:
                return None, f"날짜로 바꿀 수 없음 '{raw_text}'"
            text = dt.date().isoformat()
        else:
            text = normalize.normalize(raw_text, target_type, {})
    else:
        text = raw_text
    try:
        val = loader_validate._parse(text, dict(new_spec))
    except ValueError as exc:
        return None, str(exc)
    reasons = loader_validate._check_value(val, text, dict(new_spec))
    if reasons:
        return None, "; ".join(reasons)
    c = loader_record.canonical(val)
    return ("" if c is None else str(c)), None


def _rewrite_replace_csv(tname: str, content: bytes, col_plan: list[dict[str, Any]],
                         new_table_spec: Mapping[str, Any]) -> bytes:
    """replace 테이블의 CSV를 새 열 순서·이름·유형에 맞춰 변환하고 오류 위치를 수집한다."""
    text = content.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows:
        return content
    header = [h.strip() for h in rows[0]]
    old_idx = {h: i for i, h in enumerate(header)}
    missing_hdr = [entry["source"] for entry in col_plan if not entry["is_new"] and entry["source"] not in old_idx]
    if missing_hdr:
        raise IngestError(422, "invalid_rows", "CSV 머리글이 스키마와 맞지 않습니다",
                          [f"data/{tname}.csv:1: 열 '{c}'가 머리글에 없음" for c in missing_hdr])
    new_cols = new_table_spec["columns"]
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow([e["name"] for e in col_plan])
    errors: list[str] = []
    for line_no, row in enumerate(rows[1:], 2):
        if not row or (len(row) == 1 and row[0].strip() == ""):
            continue
        out_row: list[str] = []
        for entry in col_plan:
            cname = entry["name"]
            if entry["is_new"]:
                out_row.append(entry["default"])
                continue
            src = entry["source"]
            raw_text = row[old_idx[src]] if old_idx[src] < len(row) else ""
            converted, err = _convert_cell(raw_text, new_cols[cname],
                                           old_type=entry["old_type"], is_narrowed=entry["narrowed"])
            if err is not None:
                if len(errors) < _MAX_ROW_ERRORS:
                    errors.append(f"data/{tname}.csv:{line_no}:{cname}: {err}")
            else:
                out_row.append(converted or "")
        if not errors:
            w.writerow(out_row)
    if errors:
        raise IngestError(422, "invalid_rows", "변환할 수 없는 값이 있어 변경하지 않았습니다", errors)
    return out.getvalue().encode("utf-8")


def _find_append_cell_sources(project: config.Project, tname: str, olds: list[batches.SourceFile],
                              pk_cols: list[str], on_conflict: str,
                              target_keys: set[tuple[tuple[str, ...], str]]) -> dict[tuple[tuple[str, ...], str], tuple[str, int]]:
    """변환 불가 셀이 마지막으로 들어온 원본 배치 파일과 줄 번호를 찾는다."""
    found: dict[tuple[tuple[str, ...], str], tuple[str, int]] = {}
    if not target_keys:
        return found
    for src in olds:
        label = f"data/{tname}.csv" if src.number is None else f"data/{tname}/{src.name}"
        with batches.open_binary(src.path) as raw_io:
            reader = csv.reader(io.TextIOWrapper(raw_io, encoding="utf-8-sig", newline=""))
            hdr = next(reader, None)
            if not hdr:
                continue
            col_idx = {h: i for i, h in enumerate(hdr)}
            if any(pk not in col_idx for pk in pk_cols):
                continue
            for line_no, row in enumerate(reader, 2):
                if not row or (len(row) == 1 and row[0].strip() == ""):
                    continue
                row_pk = tuple(row[col_idx[pk]] if col_idx[pk] < len(row) else "" for pk in pk_cols)
                for pk_t, old_c in target_keys:
                    if row_pk == pk_t and old_c in col_idx:
                        if on_conflict == "skip" and (pk_t, old_c) in found:
                            continue
                        found[(pk_t, old_c)] = (label, line_no)
    return found


def _prepare_append_compaction(project: config.Project, new_project: config.Project,
                               conn: psycopg.Connection, tname: str,
                               col_plan: list[dict[str, Any]]) -> dict[str, Any] | None:
    """append 테이블의 현재 행을 새 스키마로 변환·검증해 _pending/incoming/에 정규형 합친 배치를 쓴다."""
    olds, _naming = batches.sources(project.data_dir, tname, "append")
    if not olds:
        return None
    old_spec = project.schema["tables"][tname]
    new_spec = new_project.schema["tables"][tname]
    new_cols = new_spec["columns"]
    pk_cols = list(old_spec["primary_key"])
    select_cols = list(old_spec["columns"])
    col_pos = {c: i for i, c in enumerate(select_cols)}
    pk_pos = [col_pos[pk] for pk in pk_cols]

    temp = pending.incoming_path(project.data_dir)
    writer = batches.CanonicalWriter(temp)
    rows = 0
    raw_errors: list[tuple[tuple[str, ...], str, str, str, str]] = []
    target_keys: set[tuple[tuple[str, ...], str]] = set()
    try:
        writer.header([e["name"] for e in col_plan])
        with conn.cursor(name="dk_evolve") as cur:
            cur.execute(sql.SQL("SELECT {} FROM {}.{} ORDER BY {}").format(
                sql.SQL(", ").join(map(sql.Identifier, select_cols)),
                sql.Identifier("raw"), sql.Identifier(tname),
                sql.SQL(", ").join(map(sql.Identifier, pk_cols))))
            for row in cur:
                pk_tuple = tuple("" if row[i] is None else str(loader_record.canonical(row[i])) for i in pk_pos)
                pk_label = pk_tuple[0] if len(pk_tuple) == 1 else ",".join(pk_tuple)
                out_row: list[str] = []
                for entry in col_plan:
                    cname = entry["name"]
                    if entry["is_new"]:
                        out_row.append(entry["default"])
                        continue
                    src = entry["source"]
                    raw_val = row[col_pos[src]]
                    raw_text = "" if raw_val is None else str(loader_record.canonical(raw_val))
                    converted, err = _convert_cell(raw_text, new_cols[cname],
                                                   old_type=entry["old_type"], is_narrowed=entry["narrowed"])
                    if err is not None:
                        if len(raw_errors) < _MAX_ROW_ERRORS:
                            raw_errors.append((pk_tuple, pk_label, cname, src, err))
                            target_keys.add((pk_tuple, src))
                    else:
                        out_row.append(converted or "")
                if not raw_errors:
                    writer.row(out_row)
                rows += 1
        conn.commit()
        if raw_errors:
            writer.abort()
            on_conflict = old_spec.get("on_conflict") or "error"
            found = _find_append_cell_sources(project, tname, olds, pk_cols, on_conflict, target_keys)
            formatted: list[str] = []
            for pk_tuple, pk_label, cname, src, err in raw_errors:
                loc = found.get((pk_tuple, src))
                if loc is not None:
                    formatted.append(f"{loc[0]}:{loc[1]}:{cname} (키 {pk_label}): {err}")
                else:
                    formatted.append(f"{tname}:{cname} (키 {pk_label}): {err}")
            raise IngestError(422, "invalid_rows", "변환할 수 없는 값이 있어 변경하지 않았습니다", formatted)
        sha = writer.close()
    except BaseException:
        writer.abort()
        raise

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = batches.next_name(project.data_dir, tname, merged=True)
    merged_rel = f"{batches.MERGED_DIR}/{stamp}"
    return {
        "table": tname, "name": name, "incoming": temp.name, "sha256": sha,
        "rows": rows, "merged": merged_rel,
        "olds": [{"name": f.name, "first": f.number is None} for f in olds],
    }


def plan_files(project: config.Project, op_list: list[Mapping[str, Any]],
               extra: Mapping[str, bytes | None] | None = None) -> dict[str, tuple[bytes | None, bytes | None]]:
    """연산과 함께 바뀌는 파일들: {경로: (지금 내용, 새 내용)}. 새 내용이 None이면 지운다.

    - YAML: 연산을 적용해 고친 곳만 다시 쓴다(store.edit_yaml).
    - replace 테이블의 열 추가·이름 변경·삭제·유형 변경은 그 테이블의 CSV도 함께 변환한다.
    """
    from ..project import ops, store

    expanded = _expand_label_ops(project, list(op_list))
    try:
        ops.check_ops(expanded)
    except ops.OpError as exc:
        raise IngestError(400, "invalid_op", str(exc), [str(exc)], op_index=exc.index) from None
    out: dict[str, tuple[bytes | None, bytes | None]] = {}
    for file in ops.files_of(expanded):
        before = _read(project.root / file)
        if before is None:
            raise IngestError(422, "invalid_config", "설정 파일을 읽을 수 없습니다", [f"{file}:(최상위): 파일이 없음"])
        try:
            text, _how = store.edit_yaml(before.decode("utf-8"), lambda d, f=file: ops.apply_ops(d, expanded, f))
        except store.StoreError as exc:
            raise IngestError(exc.status, exc.detail, exc.message, exc.errors) from None
        except ops.OpError as exc:
            raise IngestError(400, "invalid_op", str(exc), [str(exc)], op_index=exc.index) from None
        out[file] = (before, text.encode("utf-8"))

    schema_bytes = out[config.SCHEMA_FILE][1] if config.SCHEMA_FILE in out else _read(project.root / config.SCHEMA_FILE)
    new_schema = config._normalize_schema(store._dates_to_str(yaml.safe_load(schema_bytes.decode("utf-8")))) if schema_bytes else project.schema
    col_plans, needs_rewrite = _table_evolution_plans(project, list(op_list))
    for tname, rewrite in needs_rewrite.items():
        if not rewrite:
            continue
        spec = project.schema["tables"][tname]
        if (spec.get("mode") or "replace") == "append":
            continue
        rel = f"data/{tname}.csv"
        current = _read(project.root / rel)
        if current is not None and tname in new_schema["tables"]:
            out[rel] = (current, _rewrite_replace_csv(tname, current, col_plans[tname], new_schema["tables"][tname]))

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


def check_conflict_policy_with_rewrite(project: config.Project, new_project: config.Project,
                                       op_list: list[Mapping[str, Any]] | None) -> None:
    """append 테이블의 충돌 정책(on_conflict) 변경과 배치 재작성(열 이름 변경·삭제·유형 좁히기)은 함께 실행하지 않는다."""
    if not op_list:
        return
    _col_plans, needs_rewrite = _table_evolution_plans(project, op_list)
    for tname, rewrite in needs_rewrite.items():
        if not rewrite:
            continue
        old_t = project.schema["tables"].get(tname)
        new_t = new_project.schema["tables"].get(tname)
        if old_t is None or new_t is None or (old_t.get("mode") or "replace") != "append":
            continue
        if (old_t.get("on_conflict") or "error") != (new_t.get("on_conflict") or "error"):
            raise IngestError(422, "invalid_op",
                              f"충돌 정책(on_conflict) 변경과 배치 재작성은 함께 실행할 수 없습니다({tname}). 먼저 충돌 정책을 적용한 뒤 실행하세요")


def validate(project_name: str, op_list: list[Mapping[str, Any]],
             extra: Mapping[str, bytes | None] | None = None) -> list[str]:
    """잠금 없이 미리 검사한다(요청을 받을 때 400·422를 바로 알리려고). 작업은 잠금 안에서 다시 검사한다. 바뀔 파일 목록."""
    try:
        project = config.load_project(project_name)
    except config.ConfigError as exc:
        raise IngestError(422, "invalid_config", "지금 설정이 검사를 통과하지 못합니다", exc.errors) from None
    changes = plan_files(project, op_list, extra)
    if changes:
        staged_proj = _staged(project, changes).project
        check_mode_switch(project, staged_proj)
        check_conflict_policy_with_rewrite(project, staged_proj, op_list)
    return sorted(changes)


def change_config(project_name: str, op_list: list[Mapping[str, Any]] | None, revision: str | None, *, action: str,
                  restore: str | None = None, extra: Mapping[str, bytes | None] | None = None,
                  run_sync: bool = True, log_line: Callable[[str], None] | None = None,
                  dbname: str | None = None, wait_ms: int | None = None,
                  progress: Callable[[str, Mapping[str, Any]], None] | None = None) -> dict[str, Any]:
    """구조 변경 한 번(연산, 또는 restore: 설정 이력 id로 되돌리기). revision은 필수다(그 사이 바뀌었으면 409 stale_config).

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
        if progress:
            progress("lock", {})
        # DB 쓰기 잠금을 먼저 잡은 뒤 프로젝트 잠금에 진입해, 긴 대기 중에도 aliases 저장이 막히지 않게 한다
        with locked(project, conn, revision=revision, check_data_revision=False, wait_ms=wait_ms,
                    on_acquired=(lambda: progress("apply", {})) if progress else None) as session, \
                store.project_lock(project.name):
            if store.revision(project.root) != revision:
                raise StaleConfig()
            if restore is not None:
                # 이력의 해시 검사(지금 내용이 그 작업 바로 뒤의 판인지)를 잠금 안에서 한다
                files = history_files(project.name, restore)
                changes = {_safe_rel(k): (_read(project.root / k), v) for k, v in files.items()}
                changes = {k: v for k, v in changes.items() if v[0] != v[1]}
            else:
                from .newtable import leftover_data

                for o in op_list or []:          # 잠금 안에서 폴더를 다시 본다: 남은 데이터 파일을 새 테이블에 섞지 않는다
                    if o.get("op") == "table.add" and isinstance(o.get("table"), str) \
                            and o["table"] not in project.schema["tables"] and leftover_data(project, o["table"]):
                        raise IngestError(409, "table_exists", "그 이름의 데이터 파일이 남아 있습니다(지운 테이블). 다른 이름을 쓰세요")
                changes = plan_files(project, op_list or [], extra)
            if not changes:
                return {"changed": [], "history": None, "revision": store.revision(project.root), "counts": None,
                        "sync": None}
            say(f"▶ 검사: {', '.join(changes)}")
            staged = _staged(project, changes)
            check_mode_switch(project, staged.project)          # 잠금 안에서 폴더를 다시 본다(이력 되돌리기 포함)
            check_conflict_policy_with_rewrite(project, staged.project, op_list)
            new_revision = data_revision(staged.project.schema, staged.project.dashboard)
            compactions: list[dict[str, Any]] = []
            if restore is None and op_list:
                col_plans, needs_rewrite = _table_evolution_plans(project, op_list)
                need_append_rewrite = any(
                    rewrite and (project.schema["tables"][tname].get("mode") or "replace") == "append"
                    for tname, rewrite in needs_rewrite.items()
                )
                if need_append_rewrite and session.loaded.data_revision is not None \
                        and session.loaded.data_revision != data_revision(project.schema, project.dashboard):
                    raise IngestError(409, "rebuild_required",
                                      "적용되지 않은 설정 변경이 있어 배치를 합칠 수 없습니다. 먼저 apply한 뒤 실행하세요")
                for tname, rewrite in needs_rewrite.items():
                    if not rewrite or (project.schema["tables"][tname].get("mode") or "replace") != "append":
                        continue
                    diff = session.diff.get(tname)
                    if diff is not None and not diff.clean:
                        for c in compactions:
                            p = pending.incoming_dir(project.data_dir) / c["incoming"]
                            p.unlink(missing_ok=True)
                        raise IngestError(409, "rebuild_required",
                                          "반영되지 않았거나 바뀐 배치 파일이 있어 구조를 바꿀 수 없습니다. 먼저 적용하세요",
                                          diff.lines(tname))
                    try:
                        comp = _prepare_append_compaction(project, staged.project, conn, tname, col_plans[tname])
                    except BaseException:
                        for c in compactions:
                            p = pending.incoming_dir(project.data_dir) / c["incoming"]
                            p.unlink(missing_ok=True)
                        raise
                    if comp is not None:
                        compactions.append(comp)
            # 검증하는 사이 다른 곳(Studio의 CSV 교체 등. YAML revision에 들지 않는다)이 파일을 바꿨으면 덮어쓰지 않는다
            for rel, (before, _after) in changes.items():
                if _read(project.root / rel) != before:
                    for c in compactions:
                        p = pending.incoming_dir(project.data_dir) / c["incoming"]
                        p.unlink(missing_ok=True)
                    raise IngestError(409, "stale_config", f"{rel}이 그 사이에 바뀌어 고치지 않았습니다. 다시 불러온 뒤 시도하세요")
            hid = _new_history_id(action)
            record_path, record = _write_record(project, changes, new_revision, hid, action, session.loaded.loaded_at,
                                                compactions=compactions or None)
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
            for comp in compactions:
                compact._finish(project, comp)
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
            if compactions:
                compact.clear_merged(new_project)
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
