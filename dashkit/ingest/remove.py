"""배치 되돌리기와 되살리기(설계서 5-6, contracts/ingest.md "되돌리기와 되살리기").

- **되돌리기 = 그 배치 파일을 빼고 다시 만들기.** 결과는 "그 파일이 처음부터 없었을 때"와 같다. 파일은 `_removed/`로 간다.
- **방금 추가 취소(빠른 길):** 그 테이블의 가장 최근 배치이고 모든 행이 새 키였으면(배치 기록의 inserted = rows),
  폴더가 배치 기록과 같으면, 다시 만들지 않고 그 배치 파일에 있는 키의 행만 지운다. 같은 트랜잭션에서 checks를 돌리고,
  다른 테이블의 행이 그 행을 참조하면 거절한다.
- **되살리기:** `_removed/`의 파일을 폴더로 돌려놓고 다시 만든다(번호가 뒤의 배치보다 앞이라 언제나 다시 만든다).

순서는 공통 순서(operation.locked) 위에서 이렇게 한다: DB 작업(빠른 길의 삭제, 또는 그 파일을 빼거나 넣은 다시 만들기)과
checks를 트랜잭션 안에서 먼저 하고 → 묶음 기록(종류 remove·restore) → 파일 옮기기 → 커밋 → 묶음 기록 지우기.
DB 작업이 실패하면 파일을 건드리지 않는다.

끊김 복구(recover.py의 RECOVERERS): 커밋 여부를 판정해 파일을 그 결과에 맞는 자리(폴더 또는 `_removed/`)에 둔다.
빠른 길은 배치 기록(그 줄이 지워졌는지), 다시 만드는 길은 적재 스냅샷의 loaded_at(다시 만들면 바뀐다)으로 판정한다.
load 방식은 기록만 지운다(파일이 놓인 자리대로 다시 만든다).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

import psycopg
from psycopg import sql

from .. import db
from ..config import Project, table_order
from . import batches, ledger, pending
from .add import check_table, load_stable
from .apply import drop_stage, pg_message, run_checks, stage_source
from .batches import REMOVED_DIR, SourceFile
from .faults import fault
from .operation import IngestError, Session, _resolve_or_raise, after_ingest, locked


class NeedsRebuild(Exception):
    """빠른 길로 할 수 없다(부른 쪽이 다시 만드는 작업을 시작한다)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Target:
    table: str
    name: str
    folder_path: Path
    removed_path: Path


def _target(project: Project, table: str, name: str, *, removed: bool) -> Target:
    """경로의 {file}을 폴더 목록과 대조한다. 없으면 404 unknown_batch."""
    folder = batches.table_dir(project.data_dir, table)
    if removed:
        candidates = {p.name for p in (folder / REMOVED_DIR).iterdir() if p.is_file()} \
            if (folder / REMOVED_DIR).is_dir() else set()
        ok = name in candidates and batches.BATCH_RE.fullmatch(name) is not None
    else:
        ok = name in {f.name for f in batches.list_batches(project.data_dir, table)[0]}
    if not ok:
        raise IngestError(404, "unknown_batch", "그 배치 파일이 없습니다" + (" (뺀 배치 목록)" if removed else ""))
    if not removed and batches.is_merged(name):
        # 합친 배치는 그 테이블의 모든 행을 담는다. 빼면 마지막에 더한 행이 아니라 테이블 전체가 사라진다
        raise IngestError(409, "merged_batch", "합친 배치는 되돌릴 수 없습니다(그 테이블의 행 전부가 들어 있습니다)")
    return Target(table, name, folder / name, folder / REMOVED_DIR / name)


def _number(name: str) -> int:
    m = batches.BATCH_RE.fullmatch(name)
    return int(m.group(1)) if m else 0


def remove_batch(project_name: str, table: str, name: str, *, fast_only: bool = False,
                 projects_dir: Path | None = None, dbname: str | None = None, wait_ms: int | None = None,
                 progress: Callable[[str, Mapping[str, Any]], None] | None = None
                 ) -> dict[str, Any]:
    """배치를 되돌린다. 빠른 길이면 {"fast": true, …}, 아니면 다시 만든다(fast_only면 NeedsRebuild)."""
    project, revision = load_stable(project_name, projects_dir, dbname=dbname)
    tname = check_table(project, table)
    conn = _connect(dbname)
    try:
        if progress:
            progress("lock", {})
        with locked(project, conn, revision=revision, wait_ms=wait_ms,
                    on_acquired=(lambda: progress("rebuild", {})) if progress else None) as session:
            target = _target(project, tname, name, removed=False)     # 복구가 파일을 제자리로 돌린 뒤에 본다
            reason = _fast_blocker(session, target)
            if reason is None:
                result = _fast_remove(session, target)
            elif fast_only:
                raise NeedsRebuild(reason)
            else:
                result = _rebuild_move(session, target, "remove")
                result["reason"] = reason
        after_ingest(project, [tname])
        return result
    finally:
        _close(conn)


def restore_batch(project_name: str, table: str, name: str, *, projects_dir: Path | None = None,
                  dbname: str | None = None, wait_ms: int | None = None,
                  progress: Callable[[str, Mapping[str, Any]], None] | None = None) -> dict[str, Any]:
    """`_removed/`의 배치를 되살린다(다시 만든다)."""
    project, revision = load_stable(project_name, projects_dir, dbname=dbname)
    tname = check_table(project, table)
    conn = _connect(dbname)
    try:
        if progress:
            progress("lock", {})
        with locked(project, conn, revision=revision, wait_ms=wait_ms,
                    on_acquired=(lambda: progress("rebuild", {})) if progress else None) as session:
            target = _target(project, tname, name, removed=True)      # 복구가 파일을 제자리로 돌린 뒤에 본다
            if target.folder_path.exists():
                raise IngestError(409, "batch_exists", "같은 이름의 배치가 이미 폴더에 있습니다")
            result = _rebuild_move(session, target, "restore")
        after_ingest(project, [tname])
        return result
    finally:
        _close(conn)


def _connect(dbname: str | None) -> psycopg.Connection:
    try:
        return db.owner_conn(dbname)
    except (psycopg.Error, RuntimeError) as exc:
        raise IngestError(503, "db_unavailable", f"DB 접속 실패: {pg_message(exc)}") from None


def _close(conn: psycopg.Connection) -> None:
    if not conn.closed:
        try:
            conn.rollback()
        except psycopg.Error:
            pass
        conn.close()


def _fast_blocker(session: Session, target: Target) -> str | None:
    """빠른 길을 막는 이유. 없으면 None."""
    folder = [f.name for f in batches.list_batches(session.project.data_dir, target.table)[0]]
    if not folder or max(folder, key=_number) != target.name:
        return "가장 최근 배치가 아님"
    rec = session.recorded.get(target.table, {}).get(target.name)
    if rec is None:
        return "반영되지 않은 배치"
    if rec.inserted != rec.rows or rec.updated or rec.skipped or rec.unchanged:
        return "이미 있던 키의 행이 들어 있음(고침·건너뜀·같은 값)"
    if batches.content_hash(target.folder_path) != rec.sha256:
        return "배치 파일이 기록과 다름"
    if any(not d.clean for d in session.diff.values()):
        return "폴더의 데이터 파일이 배치 기록과 다름"
    return None


def _record(session: Session, kind: str, target: Target, *, fast: bool) -> tuple[Path, dict[str, Any]]:
    """묶음 기록. 커밋 여부를 판정할 표식을 함께 적는다: 빠른 길은 배치 기록의 그 줄(fast), 다시 만드는 길은 작업 전의
    적재 시각(loaded_at. 다시 만들면 바뀐다. 반영되지 않은 손 파일을 빼는 경우에도 판정할 수 있다)."""
    loaded_at = session.loaded.loaded_at
    record = {"id": pending.new_id(), "kind": kind, "project": session.project.name,
              "table": target.table, "name": target.name, "fast": fast,
              "loaded_at": loaded_at.isoformat() if loaded_at is not None else None}
    return pending.write_record(session.project.data_dir, record), record


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise IngestError(409, "batch_exists", f"옮길 자리에 같은 이름의 파일이 있습니다: {dst.name}")
    os.replace(src, dst)


def _fast_remove(session: Session, target: Target) -> dict[str, Any]:
    project, conn = session.project, session.conn
    table = project.schema["tables"][target.table]
    pk = list(table["primary_key"])
    record_path: Path | None = None
    record: dict[str, Any] = {}
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                src = SourceFile(target.table, target.name, target.folder_path, _number(target.name))
                staged = stage_source(cur, project.schema, src)
                try:
                    if not staged.ok:
                        raise IngestError(409, "rebuild_required", "배치 파일을 다시 읽지 못했습니다", staged.errors)
                    join = sql.SQL(" AND ").join(sql.SQL("t.{} = b.{}").format(sql.Identifier(c), sql.Identifier(c))
                                                 for c in pk)
                    try:
                        with conn.transaction():
                            cur.execute(sql.SQL("DELETE FROM {}.{} t USING {} b WHERE {}").format(
                                sql.Identifier("raw"), sql.Identifier(target.table), sql.Identifier(staged.temp), join))
                    except psycopg.errors.ForeignKeyViolation as exc:
                        raise IngestError(422, "check_failed", "다른 테이블의 행이 이 배치의 행을 참조해 되돌릴 수 없습니다",
                                          [pg_message(exc)]) from None
                    deleted = cur.rowcount
                finally:
                    drop_stage(cur, staged)
                errors = run_checks(cur, project.schema, table_order(project))
                if errors:
                    raise IngestError(422, "check_failed", "규칙(checks)을 위반해 되돌리지 않았습니다", errors)
                record_path, record = _record(session, "remove", target, fast=True)
                fault("after_record")
                _move(target.folder_path, target.removed_path)
                fault("after_move")
                ledger.remove(cur, target.table, target.name)
                fault("before_commit")
        fault("after_commit")
    except IngestError:
        if record_path is None:
            raise
        _resolve_or_raise(session, record, RuntimeError("되돌리기 확정 실패"))
        return {"ok": True, "fast": True, "table": target.table, "file": target.name, "rows": None}
    except BaseException as exc:
        from .faults import SimulatedCrash

        if isinstance(exc, SimulatedCrash):
            raise
        if record_path is None:
            if isinstance(exc, psycopg.Error):
                raise IngestError(503, "db_unavailable", f"DB 오류: {pg_message(exc)}") from None
            raise
        _resolve_or_raise(session, record, exc)
        return {"ok": True, "fast": True, "table": target.table, "file": target.name, "rows": None}
    pending.remove_record(record_path)
    return {"ok": True, "fast": True, "table": target.table, "file": target.name, "rows": deleted}


def _rebuild_move(session: Session, target: Target, kind: str) -> dict[str, Any]:
    """그 파일을 빼거나(remove) 넣은(restore) 채로 다시 만든 뒤 파일을 옮긴다."""
    from ..loader import LoadError, rebuild_locked

    project, conn = session.project, session.conn
    record_path: Path | None = None
    record: dict[str, Any] = {}
    src, dst = (target.folder_path, target.removed_path) if kind == "remove" else (target.removed_path, target.folder_path)
    try:
        with conn.transaction():
            try:
                if kind == "remove":
                    counts = rebuild_locked(project, conn, exclude=frozenset({(target.table, target.name)}))
                else:
                    counts = rebuild_locked(project, conn, include=(
                        SourceFile(target.table, target.name, target.removed_path, _number(target.name)),))
            except LoadError as exc:
                raise IngestError(422, "check_failed" if kind == "remove" else "invalid_rows",
                                  "그 배치를 " + ("빼고" if kind == "remove" else "넣고") + " 다시 만들 수 없습니다(파일은 그대로입니다)",
                                  exc.errors) from None
            record_path, record = _record(session, kind, target, fast=False)
            fault("after_record")
            _move(src, dst)
            fault("before_commit")
        fault("after_commit")
    except IngestError:
        if record_path is None:
            raise
        _resolve_or_raise(session, record, RuntimeError(f"{kind} 확정 실패"))
        return {"ok": True, "fast": False, "table": target.table, "file": target.name, "counts": None}
    except BaseException as exc:
        from .faults import SimulatedCrash

        if isinstance(exc, SimulatedCrash):
            raise
        if record_path is None:
            if isinstance(exc, psycopg.Error):
                raise IngestError(503, "db_unavailable", f"DB 오류: {pg_message(exc)}") from None
            raise
        _resolve_or_raise(session, record, exc)
        return {"ok": True, "fast": False, "table": target.table, "file": target.name, "counts": None}
    pending.remove_record(record_path)
    return {"ok": True, "fast": False, "table": target.table, "file": target.name, "counts": counts}


# ---------------------------------------------------------------- 복구(recover.RECOVERERS에 등록)

def recover_move(project: Project, conn: psycopg.Connection | None, record: Mapping[str, Any], mode: str):
    """종류 remove·restore. 커밋됐는지는 배치 기록으로 판정하고, 파일을 그 결과에 맞는 자리에 둔다."""
    from .recover import Outcome, RecoverError

    kind = str(record.get("kind"))
    tname, name = record.get("table"), record.get("name")
    rid = str(record.get("id"))
    if tname not in project.schema["tables"] or not isinstance(name, str) or batches.BATCH_RE.fullmatch(name) is None:
        raise RecoverError([f"data/_pending/{rid}.json: table·name 형식이 틀림"])
    folder = batches.table_dir(project.data_dir, tname)
    in_folder, in_removed = folder / name, folder / REMOVED_DIR / name
    if mode != "online":
        return Outcome(rid, kind, "applied", [f"{tname}/{name}"])       # 파일이 놓인 자리대로 load가 다시 만든다
    assert conn is not None
    with conn.cursor() as cur:
        recorded = name in ledger.read(cur, [tname]).get(tname, {})
        now = ledger.loaded(cur)
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        conn.commit()
    if record.get("fast") is False:
        # 다시 만드는 길: 커밋되면 적재 시각이 바뀐다(그 사이의 load는 이 기록을 load 방식으로 먼저 처리한다)
        before = record.get("loaded_at")
        committed = (now.loaded_at.isoformat() if now.loaded_at is not None else None) != before
    else:
        committed = (not recorded) if kind == "remove" else recorded
    want_removed = committed if kind == "remove" else not committed
    src, dst = (in_folder, in_removed) if want_removed else (in_removed, in_folder)
    if src.exists() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
    return Outcome(rid, kind, "committed" if committed else "discarded", [f"{tname}/{name}"])
