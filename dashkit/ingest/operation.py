"""폴더를 바꾸는 작업의 공통 순서(설계서 10-3, contracts/ingest.md "공통 순서"). 작업마다 따로 구현하지 않는다.

| 순서 | 내용 | 함수 |
|---|---|---|
| 1. 잠금 | 쓰기 잠금. 잡을 때마다 적재된 프로젝트·설정 revision·데이터 설정 지문을 본다 | `locked` |
| 2. 복구 | 이 프로젝트의 남은 묶음 기록(recover.py) | `locked` |
| 3. 대조 | 프로젝트의 모든 원본 데이터 파일 ↔ 배치 기록 | `locked`가 `Session.diff`로 준다 |
| 4. 작업 | 묶음 기록을 쓰고 파일과 DB를 바꾼다 | 작업(add.py 등)과 `finish` |
| 5. 규칙 검사 | DB의 행이 바뀌었으면 같은 트랜잭션에서 checks | `finish` |
| 6. 마무리 | 커밋 → 묶음 기록 지우기 → 잠금 풀기 → after_ingest 훅 | `finish`, `locked` |

잠금 밖에서 하는 일은 올린 내용만 보고 정해지는 것뿐이다(읽기, 머리글 맞추기, 다듬기와 검증, 정규형 쓰기, 해시,
임시 테이블 채우기). DB나 폴더의 상태를 보고 정하는 일은 모두 `locked` 안에서 한다.

커밋에 실패했거나 결과를 받지 못하면 직접 치우지 않는다. 새 접속으로 잠금을 다시 잡고 자기 묶음 기록에 대해 복구 절차를
그대로 돌려(`resolve`) 그 결과로 답한다.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

import psycopg

from .. import db
from ..config import Project, table_order
from ..loader.record import data_revision
from ..project import locks
from . import batches, ledger, pending, recover
from .apply import pg_message, run_checks
from .policies import BatchError

log = logging.getLogger("dashkit.ingest")

DEFAULT_LOCK_WAIT_MS = 5000


def lock_wait_ms() -> int:
    try:
        return max(int(os.environ.get("MANAGE_LOCK_WAIT_MS") or DEFAULT_LOCK_WAIT_MS), 0)
    except ValueError:
        return DEFAULT_LOCK_WAIT_MS


class IngestError(Exception):
    """API 오류로 그대로 나가는 실패: status, detail(영문 코드), message(한국어), errors, extra(응답에 더할 값)."""

    def __init__(self, status: int, detail: str, message: str, errors: list[str] | None = None, **extra: Any):
        self.status = status
        self.detail = detail
        self.message = message
        self.errors = list(errors) if errors is not None else None
        self.extra = extra
        super().__init__(message)


from .faults import FAULTS, SimulatedCrash  # noqa: E402,F401  (테스트 전용 실패 주입 자리)
from .faults import fault as _fault  # noqa: E402


class StaleConfig(Exception):
    """잠금을 잡고 보니 접수·검증에 쓴 설정 파일이 그 사이 바뀌었다(부른 쪽이 다시 접수한다)."""


# ---------------------------------------------------------------- 대조

@dataclass
class TableDiff:
    """테이블 하나의 원본 파일 ↔ 배치 기록."""
    new_after: list[batches.SourceFile] = field(default_factory=list)   # 기록에 없고 번호가 기록의 모든 배치보다 뒤
    new_before: list[str] = field(default_factory=list)                 # 기록에 없고 순서를 지킬 수 없음
    changed: list[str] = field(default_factory=list)                    # 기록에 있는데 내용이 다름
    missing: list[str] = field(default_factory=list)                    # 기록에 있는데 파일이 없음
    naming: list[str] = field(default_factory=list)                     # 이름 규칙 오류

    @property
    def clean(self) -> bool:
        return not (self.new_after or self.new_before or self.changed or self.missing or self.naming)

    @property
    def broken(self) -> bool:
        """새 파일을 먼저 반영하는 것으로는 맞출 수 없는 차이."""
        return bool(self.new_before or self.changed or self.missing or self.naming)

    def lines(self, table: str) -> list[str]:
        out = [f"data/{table}: 반영되지 않은 파일 {f.name}" for f in self.new_after]
        out += [f"data/{table}: 반영된 배치보다 앞에 놓인 새 파일 {n}" for n in self.new_before]
        out += [f"data/{table}: 반영한 뒤 바뀐 파일 {n}" for n in self.changed]
        out += [f"data/{table}: 반영했는데 없어진 파일 {n}" for n in self.missing]
        return out + self.naming


def _number(name: str) -> int:
    m = batches.BATCH_RE.fullmatch(name)
    return int(m.group(1)) if m else 0


def reconcile(project: Project, recorded: Mapping[str, Mapping[str, ledger.Recorded]]) -> dict[str, TableDiff]:
    """프로젝트의 모든 원본 데이터 파일의 이름·해시를 배치 기록과 견준다(해시는 크기·수정 시각이 같으면 캐시)."""
    out: dict[str, TableDiff] = {}
    for tname, table in project.schema["tables"].items():
        mode = table.get("mode") or "replace"
        files, naming = batches.sources(project.data_dir, tname, mode)
        rec = dict(recorded.get(tname, {}))
        diff = TableDiff(naming=naming)
        last = max((_number(n) for n in rec), default=0)
        for f in files:
            r = rec.pop(f.name, None)
            if r is None:
                if mode == "append" and f.number is not None and f.number > last:
                    diff.new_after.append(f)
                else:
                    diff.new_before.append(f.name)
                continue
            if batches.content_hash(f.path) != r.sha256:
                diff.changed.append(f.name)
        diff.missing = sorted(rec)
        out[tname] = diff
    return out


# ---------------------------------------------------------------- 잠금 구간

@dataclass
class Session:
    """잠금을 잡은 작업 하나. conn은 잠금을 쥔 owner 접속이다."""
    project: Project
    conn: psycopg.Connection
    loaded: ledger.Loaded
    recorded: dict[str, dict[str, ledger.Recorded]]
    diff: dict[str, TableDiff]
    recovered: list[recover.Outcome]

    def reject_unless_clean(self, *, allow_new_after: set[str] = frozenset()) -> list[batches.SourceFile]:   # type: ignore[assignment]
        """대조 결과가 깨끗하지 않으면 409 rebuild_required. allow_new_after의 테이블은 뒤에 놓인 새 파일을 허용하고
        그 파일들(먼저 반영할 것)을 돌려준다."""
        problems: list[str] = []
        first: list[batches.SourceFile] = []
        for tname, d in self.diff.items():
            if d.clean:
                continue
            if tname in allow_new_after and not d.broken:
                first += d.new_after
                continue
            problems += d.lines(tname)
        if problems:
            raise IngestError(409, "rebuild_required",
                              "폴더의 데이터 파일이 DB와 다릅니다. 먼저 적용(apply)으로 다시 만드세요", problems)
        return first


@contextmanager
def locked(project: Project, conn: psycopg.Connection, *, revision: str | None, check_data_revision: bool = True,
           wait_ms: int | None = None, on_acquired: Callable[[], None] | None = None) -> Iterator[Session]:
    """1~3단계. revision은 접수·검증에 쓴 설정 파일의 revision(store.revision)이다. 다르면 StaleConfig."""
    from ..project import store

    try:
        locks.acquire(conn, lock_wait_ms() if wait_ms is None else wait_ms)
    except locks.LockBusy:
        raise IngestError(409, "apply_running", "다른 쓰기 작업(적용·적재)이 진행 중입니다. 끝난 뒤 다시 시도하세요") from None
    try:
        if on_acquired is not None:
            on_acquired()
        with conn.cursor() as cur:
            now = ledger.loaded(cur)
        if now.project != project.name:
            if now.project is None:
                raise IngestError(409, "rebuild_required", "아직 적용(apply)한 적이 없는 DB입니다. 먼저 적용하세요")
            raise IngestError(409, "project_mismatch",
                              f"DB에 다른 프로젝트 '{now.project}'가 적재돼 있습니다. 이 프로젝트를 먼저 적용하세요")
        if revision is not None and store.revision(project.root) != revision:
            raise StaleConfig()
        if check_data_revision and now.data_revision != data_revision(project.schema, project.dashboard):
            raise IngestError(409, "rebuild_required",
                              "설정이 바뀌었는데 아직 적용(apply)하지 않았습니다. 먼저 적용하세요")
        try:
            outcomes = recover.recover(project, conn, mode="online")
        except recover.RecoverUnknown as exc:
            raise IngestError(503, "recovery_unknown", "끝나지 않은 폴더 작업의 복구 결과를 확인하지 못했습니다. "
                              "잠시 뒤 다시 시도하세요(파일은 그대로입니다)", exc.errors) from None
        except recover.RecoverError as exc:
            raise IngestError(409, "rebuild_required", "끝나지 않은 폴더 작업을 처리할 수 없습니다", exc.errors) from None
        with conn.cursor() as cur:
            recorded = ledger.read(cur)
        conn.commit()
        yield Session(project, conn, now, recorded, reconcile(project, recorded), outcomes)
    finally:
        locks.release(conn)


# ---------------------------------------------------------------- 4~6단계

@dataclass
class Move:
    """확정 때 옮길 파일 하나: incoming의 정규형 파일 → 배치 폴더의 제 이름."""
    table: str
    name: str
    source: Path
    sha256: str


def finish(session: Session, kind: str, moves: list[Move], db_work: Callable[[psycopg.Cursor], None], *,
           dry_run: bool = False, extra: Mapping[str, Any] | None = None,
           on_record: Callable[[], None] | None = None,
           post_move: Callable[[psycopg.Cursor], None] | None = None) -> None:
    """4~6단계를 한 트랜잭션으로: db_work(DB 반영) → checks → (dry_run이면 롤백) → 묶음 기록 → 파일 옮기기 →
    post_move(배치 기록 더하기) → 커밋 → 묶음 기록 지우기.
    커밋을 확인하지 못하면 resolve로 복구한 결과로 답한다(IngestError 또는 정상 반환)."""
    project, conn = session.project, session.conn
    order = table_order(project)
    record_path: Path | None = None
    record: dict[str, Any] = {}
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                db_work(cur)
                errors = run_checks(cur, project.schema, order)
                if errors:
                    raise BatchError("check_failed", "규칙(checks)을 위반해 넣지 않았습니다", errors)
                if dry_run:
                    raise _DryRun()
                record = {"id": pending.new_id(), "kind": kind, "project": project.name,
                          "files": [{"table": m.table, "name": m.name, "incoming": m.source.name} for m in moves],
                          **(extra or {})}
                record_path = pending.write_record(project.data_dir, record)
                if on_record is not None:
                    on_record()        # 이제부터 incoming 파일은 복구 절차만 치운다
                _fault("after_record")
                for m in moves:
                    target = batches.table_dir(project.data_dir, m.table) / m.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(m.source, target)
                    batches.remember_hash(target, m.sha256)
                    _fault("after_move")
                if post_move is not None:
                    post_move(cur)
                _fault("before_commit")
        _fault("after_commit")
    except _DryRun:
        return
    except SimulatedCrash:
        raise
    except BatchError as exc:
        if record_path is None:
            raise IngestError(422, exc.detail, exc.message, exc.errors, **exc.extra) from None
        _resolve_or_raise(session, record, exc)
        return
    except BaseException as exc:
        if record_path is None:
            if isinstance(exc, (psycopg.errors.IntegrityError, psycopg.errors.DataError)):
                raise IngestError(422, "invalid_rows", "DB가 행을 받지 않았습니다", [pg_message(exc)]) from None
            if isinstance(exc, psycopg.Error):
                raise IngestError(503, "db_unavailable", f"DB 오류: {pg_message(exc)}") from None
            raise
        _resolve_or_raise(session, record, exc)
        return
    pending.remove_record(record_path)


class _DryRun(Exception):
    pass


def _resolve_or_raise(session: Session, record: Mapping[str, Any], cause: BaseException) -> None:
    log.warning("확정 결과를 확인하지 못함(%s). 복구 절차로 판정한다", type(cause).__name__)
    dbname = session.conn.info.dbname
    try:
        session.conn.close()      # 이 접속의 잠금을 놓는다. 판정은 새 접속으로 한다
    except Exception:
        pass
    try:
        result = resolve(session.project, record, dbname=dbname)
    except (locks.LockBusy, psycopg.Error, recover.RecoverError):
        raise IngestError(500, "commit_unknown", "확정 결과를 판정하지 못했습니다. 다음 작업이 복구합니다") from None
    if result in ("committed", "applied"):
        return
    if result == "project_mismatch":
        raise IngestError(409, "project_mismatch", "다른 프로젝트가 적재돼 있어 이 작업을 판정하지 못했습니다. "
                          "파일과 묶음 기록을 그대로 두었습니다(이 프로젝트를 다시 적용할 때 정해집니다)")
    raise IngestError(500, "commit_failed", "확정하지 못했습니다(폴더와 DB는 그대로입니다)")


def resolve(project: Project, record: Mapping[str, Any], *, dbname: str | None = None,
            wait_ms: int | None = None) -> str:
    """커밋 결과를 모르는 작업의 판정. 새 접속으로 잠금을 다시 잡고 그 묶음 기록만 복구한다.

    결과: committed | applied | discarded | project_mismatch(복구하지 않고 파일·기록을 남김)
    """
    rid = record.get("id")
    conn = db.owner_conn(dbname)
    try:
        locks.acquire(conn, lock_wait_ms() if wait_ms is None else wait_ms)
        with conn.cursor() as cur:
            now = ledger.loaded(cur)
        conn.commit()
        if now.project != project.name:
            return "project_mismatch"
        if now.data_revision != data_revision(project.schema, project.dashboard):
            raise recover.RecoverUnknown(["DB에 적용된 설정이 이 작업의 설정과 다름. 다음 load가 파일로 처리한다"])
        outcomes = recover.recover(project, conn, mode="online", only=str(rid))
        if outcomes:
            return outcomes[0].result
        # 기록이 이미 없다: 다른 프로세스가 복구했다. 배치 기록으로 답한다
        files = record.get("files") or []
        with conn.cursor() as cur:
            recorded = ledger.read(cur, {f["table"] for f in files})
        conn.commit()
        return "committed" if files and all(f["name"] in recorded.get(f["table"], {}) for f in files) else "discarded"
    finally:
        locks.release(conn)
        conn.close()


def recover_on_start(project: Project, *, dbname: str | None = None) -> list[recover.Outcome]:
    """관리 서비스가 시작할 때: DB에 적재된 프로젝트가 이 프로젝트이면 남은 묶음 기록을 처리한다.

    잠금을 바로 잡지 못하면(다른 쓰기 작업이 도는 중) 건너뛴다. 다음 작업이 공통 순서 2에서 처리한다.
    """
    conn = db.owner_conn(dbname)
    try:
        if not locks.try_acquire(conn):
            return []
        try:
            with conn.cursor() as cur:
                now = ledger.loaded(cur)
            conn.commit()
            # 온라인 복구는 지금 설정이 DB에 적용된 설정일 때만 한다(아니면 다음 load가 파일로 처리한다)
            if now.project != project.name or now.data_revision != data_revision(project.schema, project.dashboard):
                return []
            return recover.recover(project, conn, mode="online")
        finally:
            locks.release(conn)
    finally:
        conn.close()


AFTER_INGEST: list[Callable[[Project, list[str]], None]] = []


def after_ingest(project: Project, tables: list[str]) -> None:
    """적재 뒤 훅(5-8). 지금은 하는 일이 없다. Superset 캐시를 켜는 배포가 캐시 무효화를 여기에 등록한다."""
    for fn in AFTER_INGEST:
        try:
            fn(project, tables)
        except Exception:   # 훅의 실패가 커밋된 적재를 실패로 만들지 않게
            log.exception("after_ingest 훅 실패")
