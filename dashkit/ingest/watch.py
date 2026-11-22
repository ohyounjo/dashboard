"""감시 폴더(Watch Folder) 자동 적재(설계서 4절).

지정된 입고 폴더(기본: projects/<PROJECT>/data/_inbox/<테이블>/ 또는 --dir)에 떨어지는
.csv, .csv.gz, .xlsx 파일을 감지해 기존 ingest() 절차로 적재한다.

- .watch.lock 배타 잠금으로 동일 폴더 중복 실행 방지
- _processing/ 중단 파일 회수(적재 커밋 직후 종료된 파일은 duplicate_file 연동으로 _done/ 정상 마감)
- 쓰기 완료(settle_ms) 확인 후 원자적 이동(_processing/ → _done/ 또는 _failed/ + .error.json)
- sensitive: true 열 오류 값은 redact_sensitive_errors로 마스킹해 .error.json에 절대 남기지 않음
"""
from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import logging
import os
import re
import signal
import time
import zipfile
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .. import config
from . import batches
from .intake import redact_sensitive_errors
from .operation import IngestError

log = logging.getLogger("dashkit.ingest.watch")

DEFAULT_INTERVAL_SEC = 5.0
DEFAULT_SETTLE_MS = 500
DEFAULT_KEEP_DONE = 100
DEFAULT_MAX_UPLOAD_MB = 200

_LOCK_NAME = ".watch.lock"
_PROCESSING_DIR = "_processing"
_DONE_DIR = "_done"
_FAILED_DIR = "_failed"
_SKIP_EXTS = (".tmp", ".part", ".crdownload", ".error.json", ".lock")
_ALLOWED_EXTS = (".csv.gz", ".csv", ".xlsx")
_PROC_PREFIX_RE = re.compile(r"^(\d{8}T\d{6}(?:\d{6})?Z)-(.+)$")
_TRANSIENT_DETAILS = frozenset({"apply_running", "db_unavailable", "recovery_unknown", "commit_unknown"})
_MAX_ARCHIVE_NAME_BYTES = 180


def _max_upload_bytes() -> int:
    try:
        mb = int(os.environ.get("INGEST_MAX_UPLOAD_MB") or DEFAULT_MAX_UPLOAD_MB)
    except ValueError:
        mb = DEFAULT_MAX_UPLOAD_MB
    return max(mb, 1) * 1024 * 1024


def _utc_stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _parse_proc_name(name: str, fallback_mtime: float) -> tuple[datetime, str]:
    m = _PROC_PREFIX_RE.fullmatch(name)
    if m:
        stamp, orig = m.group(1), m.group(2)
        fmt = "%Y%m%dT%H%M%S%fZ" if len(stamp) == 22 else "%Y%m%dT%H%M%SZ"
        try:
            return datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc), orig
        except ValueError:
            pass
    return datetime.fromtimestamp(fallback_mtime, tz=timezone.utc), name


def _source_kind(name: str) -> str:
    return "xlsx" if name.lower().endswith(".xlsx") else "csv"


def _bounded_orig_name(orig_name: str, *, max_bytes: int = _MAX_ARCHIVE_NAME_BYTES) -> str:
    """타임스탬프·충돌 접두사·.error.json 접미사를 붙여도 NAME_MAX(255바이트)를 넘지 않게 보관 이름을 줄인다."""
    encoded = orig_name.encode("utf-8")
    if len(encoded) <= max_bytes:
        return orig_name
    low = orig_name.lower()
    ext = next((e for e in _ALLOWED_EXTS if low.endswith(e)), Path(orig_name).suffix)
    stem = orig_name[:-len(ext)] if ext else orig_name
    digest = hashlib.sha256(encoded).hexdigest()[:8]
    suffix = f"-{digest}{ext}"
    budget = max(max_bytes - len(suffix.encode("utf-8")), 8)
    trimmed = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore")
    return f"{trimmed}{suffix}"


def _unique_target(folder: Path, prefix: str, orig_name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    safe_name = _bounded_orig_name(orig_name)
    candidate = folder / f"{prefix}-{safe_name}"
    if not candidate.exists() and not candidate.is_symlink():
        return candidate
    n = 1
    while True:
        candidate = folder / f"{prefix}-{n}-{safe_name}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
        n += 1


def _prune_done(done_dir: Path, keep_done: int) -> None:
    if keep_done < 0 or not done_dir.is_dir():
        return
    files = sorted((p for p in done_dir.iterdir() if p.is_file() or p.is_symlink()), key=lambda p: p.name)
    excess = len(files) - keep_done
    for p in files[:max(excess, 0)]:
        try:
            p.unlink()
        except OSError:
            pass


def _audit_watch(project: str, table: str, source_name: str, *, batch: Mapping[str, Any] | None = None,
                 detail_code: str | None = None) -> None:
    try:
        from ..auth import store as auth_store

        if batch is not None:
            auth_store.record_audit(
                "data.batch.create",
                target_type="table",
                target_id=table,
                target_label=table,
                detail={
                    "project": project,
                    "table": table,
                    "file": batch["file"],
                    "sha256": batch["sha256"],
                    "rows": batch["rows"],
                    "method": "watch",
                    "source_name": source_name,
                },
            )
        else:
            auth_store.record_audit(
                "data.batch.failed",
                target_type="table",
                target_id=table,
                target_label=table,
                detail={
                    "project": project,
                    "table": table,
                    "method": "watch",
                    "source_name": source_name,
                    "detail_code": detail_code or "unknown",
                },
            )
    except Exception:
        pass


def _quarantine(
    folder: Path,
    src_path: Path,
    orig_name: str,
    table: str,
    table_spec: Mapping[str, Any] | None,
    *,
    status: int,
    detail: str,
    message: str,
    errors: list[str] | None = None,
) -> Path:
    stamp = _utc_stamp()
    failed_dir = folder / _FAILED_DIR
    dest = _unique_target(failed_dir, stamp, orig_name)
    os.replace(src_path, dest)
    redacted_errors = redact_sensitive_errors(table_spec, errors or []) if table_spec else list(errors or [])
    redacted_msg = redact_sensitive_errors(table_spec, [message])[0] if (table_spec and message) else message
    payload = {
        "at": datetime.now(timezone.utc).isoformat(),
        "table": table,
        "file": orig_name,
        "status": status,
        "detail": detail,
        "message": redacted_msg,
        "errors": redacted_errors,
    }
    err_path = dest.with_name(dest.name + ".error.json")
    err_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dest


def _finish_done(folder: Path, src_path: Path, orig_name: str, *, delete_done: bool, keep_done: int) -> None:
    if delete_done:
        try:
            src_path.unlink()
        except FileNotFoundError:
            pass
        return
    done_dir = folder / _DONE_DIR
    dest = _unique_target(done_dir, _utc_stamp(), orig_name)
    os.replace(src_path, dest)
    _prune_done(done_dir, keep_done)


def _dup_after_processing(project: config.Project, table: str, exc: IngestError, proc_at: datetime,
                          proc_mtime: float) -> bool:
    dups = exc.extra.get("duplicates") if isinstance(exc.extra, dict) else None
    if not isinstance(dups, list) or not dups:
        return False
    proc_minute = proc_at.replace(second=0, microsecond=0)
    for d in dups:
        if not isinstance(d, Mapping) or d.get("table") != table:
            continue
        fname = d.get("file")
        if not isinstance(fname, str):
            continue
        batch_path = project.data_dir / table / fname
        try:
            if batch_path.is_file() and batch_path.stat().st_mtime >= proc_mtime - 1.0:
                return True
        except OSError:
            pass
        stamp = batches.stamp_of(fname)
        if stamp is not None and stamp >= proc_minute:
            return True
    return False


def _acquire_watch_lock(root: Path) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / _LOCK_NAME
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise IngestError(409, "watch_running", f"감시 폴더 잠금이 이미 사용 중입니다: {lock_path}") from None
    return os.fdopen(fd, "r+")


def _resolve_targets(project: config.Project, watch_dir: Path | None, table: str | None) -> tuple[Path, list[tuple[str, Path]]]:
    from .add import check_table

    if table is not None:
        tname = check_table(project, table)
        if watch_dir is None:
            root = project.data_dir / "_inbox"
            return root, [(tname, root / tname)]
        root = Path(watch_dir)
        return root, [(tname, root)]

    root = Path(watch_dir) if watch_dir is not None else (project.data_dir / "_inbox")
    targets: list[tuple[str, Path]] = []
    for tname in config.table_order(project):
        spec = project.schema["tables"][tname]
        if (spec.get("mode") or "replace") != "append":
            continue
        sub = root / tname
        if sub.is_dir():
            targets.append((tname, sub))
    return root, targets


def _process_one(
    project: config.Project,
    tname: str,
    folder: Path,
    proc_path: Path,
    orig_name: str,
    proc_at: datetime,
    proc_mtime: float,
    *,
    is_recovery: bool,
    delete_done: bool,
    keep_done: int,
    projects_dir: Path | None,
    dbname: str | None,
    wait_ms: int | None,
    stats: dict[str, int],
) -> None:
    from .add import Item, ingest

    table_spec = project.schema["tables"][tname]
    stats["processed"] += 1
    try:
        result = ingest(
            project.name,
            [Item(tname, {"kind": _source_kind(orig_name), "path": proc_path, "name": orig_name})],
            projects_dir=projects_dir,
            dbname=dbname,
            wait_ms=wait_ms,
        )
    except IngestError as exc:
        if exc.detail in _TRANSIENT_DETAILS:
            log.warning("감시 폴더 일시 오류로 다음 주기에 재시도(%s/%s): %s", tname, orig_name, exc.detail)
            stats["retrying"] += 1
            return
        if is_recovery and exc.detail == "duplicate_file" and _dup_after_processing(project, tname, exc, proc_at, proc_mtime):
            log.info("중단 복구: 이미 적재 완료된 파일로 확인되어 _done으로 이동(%s/%s)", tname, orig_name)
            _finish_done(folder, proc_path, orig_name, delete_done=delete_done, keep_done=keep_done)
            stats["succeeded"] += 1
            return
        _audit_watch(project.name, tname, orig_name, detail_code=exc.detail)
        _quarantine(
            folder, proc_path, orig_name, tname, table_spec,
            status=exc.status, detail=exc.detail, message=exc.message, errors=exc.errors,
        )
        stats["failed"] += 1
        return
    except (gzip.BadGzipFile, EOFError, zipfile.BadZipFile, zlib.error, UnicodeError) as exc:
        _audit_watch(project.name, tname, orig_name, detail_code="invalid_csv")
        _quarantine(
            folder, proc_path, orig_name, tname, table_spec,
            status=422, detail="invalid_csv", message="손상되었거나 읽을 수 없는 파일입니다",
            errors=[f"{orig_name}:1:-: {exc}"],
        )
        stats["failed"] += 1
        return

    _audit_watch(project.name, tname, orig_name, batch=result["batches"][0])
    _finish_done(folder, proc_path, orig_name, delete_done=delete_done, keep_done=keep_done)
    stats["succeeded"] += 1


def watch_once(
    project_name: str,
    *,
    watch_dir: Path | None = None,
    table: str | None = None,
    settle_ms: int = DEFAULT_SETTLE_MS,
    delete_done: bool = False,
    keep_done: int = DEFAULT_KEEP_DONE,
    projects_dir: Path | None = None,
    dbname: str | None = None,
    wait_ms: int | None = None,
    _lock_file: Any = None,
    _seen: dict[Path, tuple[int, int, float]] | None = None,
) -> dict[str, int]:
    """감시 폴더를 1회 스캔해 _processing/ 복구 후 안정된 대기 파일을 적재한다."""
    try:
        project = config.load_project(project_name, projects_dir)
    except config.ConfigError as exc:
        raise IngestError(422, "invalid_config", "설정 검사를 통과하지 못했습니다", exc.errors) from None

    root, targets = _resolve_targets(project, watch_dir, table)
    own_lock = _lock_file is None
    lock_file = _acquire_watch_lock(root) if own_lock else _lock_file
    stats = {"processed": 0, "succeeded": 0, "failed": 0, "retrying": 0, "skipped": 0}
    max_bytes = _max_upload_bytes()
    settle_ms = max(int(settle_ms), 0)

    try:
        for tname, folder in targets:
            folder.mkdir(parents=True, exist_ok=True)
            table_spec = project.schema["tables"][tname]
            proc_dir = folder / _PROCESSING_DIR
            # 1) 이전 실행이 _processing/에 남긴 중단 파일 먼저 회수
            if proc_dir.is_dir():
                for proc_path in sorted(proc_dir.iterdir(), key=lambda p: p.name):
                    if proc_path.name.startswith(".") or not proc_path.is_file():
                        continue
                    try:
                        proc_mtime = proc_path.stat().st_mtime
                    except OSError:
                        continue
                    proc_at, orig_name = _parse_proc_name(proc_path.name, proc_mtime)
                    _process_one(
                        project, tname, folder, proc_path, orig_name, proc_at, proc_mtime,
                        is_recovery=True, delete_done=delete_done, keep_done=keep_done,
                        projects_dir=projects_dir, dbname=dbname, wait_ms=wait_ms, stats=stats,
                    )

            # 2) 대기 폴더 스캔(이름 오름차순)
            for entry in sorted(folder.iterdir(), key=lambda p: p.name):
                name = entry.name
                if name.startswith((".", "_")):
                    continue
                if entry.is_symlink():
                    _audit_watch(project.name, tname, name, detail_code="invalid_filename")
                    _quarantine(
                        folder, entry, name, tname, table_spec,
                        status=400, detail="invalid_filename",
                        message="심볼릭 링크는 처리할 수 없습니다",
                        errors=[f"{name}:1:-: 심볼릭 링크는 허용되지 않습니다"],
                    )
                    stats["failed"] += 1
                    continue
                if not entry.is_file():
                    continue
                low = name.lower()
                if any(low.endswith(ext) for ext in _SKIP_EXTS):
                    stats["skipped"] += 1
                    continue
                if not any(low.endswith(ext) for ext in _ALLOWED_EXTS):
                    log.warning("지원하지 않는 확장자 건너뜀(%s): %s", tname, name)
                    stats["skipped"] += 1
                    continue

                try:
                    st = entry.stat()
                except OSError:
                    stats["skipped"] += 1
                    continue

                if st.st_size > max_bytes:
                    _audit_watch(project.name, tname, name, detail_code="upload_too_large")
                    _quarantine(
                        folder, entry, name, tname, table_spec,
                        status=413, detail="upload_too_large",
                        message=f"파일이 너무 큽니다(상한 {max_bytes // (1024 * 1024)}MB)",
                        errors=[f"{name}:1:-: 파일 크기 {st.st_size}바이트가 상한({max_bytes}바이트)을 넘음"],
                    )
                    stats["failed"] += 1
                    continue

                if settle_ms > 0:
                    if _seen is not None:
                        now_mono = time.monotonic()
                        prev = _seen.get(entry)
                        if prev is None or prev[0] != st.st_size or prev[1] != st.st_mtime_ns:
                            _seen[entry] = (st.st_size, st.st_mtime_ns, now_mono)
                            stats["skipped"] += 1
                            continue
                        if (now_mono - prev[2]) * 1000 < settle_ms and (time.time_ns() - st.st_mtime_ns) < settle_ms * 1_000_000:
                            stats["skipped"] += 1
                            continue
                    else:
                        if (time.time_ns() - st.st_mtime_ns) < settle_ms * 1_000_000:
                            stats["skipped"] += 1
                            continue

                try:
                    with open(entry, "rb"):
                        pass
                except OSError:
                    stats["skipped"] += 1
                    continue

                if _seen is not None:
                    _seen.pop(entry, None)

                proc_at = datetime.now(timezone.utc)
                proc_path = _unique_target(proc_dir, _utc_stamp(proc_at), name)
                os.replace(entry, proc_path)
                proc_mtime = proc_path.stat().st_mtime
                _process_one(
                    project, tname, folder, proc_path, name, proc_at, proc_mtime,
                    is_recovery=False, delete_done=delete_done, keep_done=keep_done,
                    projects_dir=projects_dir, dbname=dbname, wait_ms=wait_ms, stats=stats,
                )
    finally:
        if own_lock and lock_file is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            lock_file.close()
    return stats


def watch_loop(
    project_name: str,
    *,
    watch_dir: Path | None = None,
    table: str | None = None,
    interval: float = DEFAULT_INTERVAL_SEC,
    settle_ms: int = DEFAULT_SETTLE_MS,
    delete_done: bool = False,
    keep_done: int = DEFAULT_KEEP_DONE,
    projects_dir: Path | None = None,
    dbname: str | None = None,
    wait_ms: int | None = None,
    max_cycles: int | None = None,
) -> None:
    """상주 폴링 모드로 감시 폴더를 주기적으로 스캔한다."""
    try:
        project = config.load_project(project_name, projects_dir)
    except config.ConfigError as exc:
        raise IngestError(422, "invalid_config", "설정 검사를 통과하지 못했습니다", exc.errors) from None

    root, _targets = _resolve_targets(project, watch_dir, table)
    lock_file = _acquire_watch_lock(root)
    stop = False
    seen: dict[Path, tuple[int, int, float]] = {}

    def _handle_stop(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    old_term = signal.signal(signal.SIGTERM, _handle_stop)
    old_int = signal.signal(signal.SIGINT, _handle_stop)
    cycles = 0
    try:
        while not stop:
            watch_once(
                project_name,
                watch_dir=watch_dir,
                table=table,
                settle_ms=settle_ms,
                delete_done=delete_done,
                keep_done=keep_done,
                projects_dir=projects_dir,
                dbname=dbname,
                wait_ms=wait_ms,
                _lock_file=lock_file,
                _seen=seen,
            )
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            if not stop:
                time.sleep(max(float(interval), 0.05))
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_file.close()
