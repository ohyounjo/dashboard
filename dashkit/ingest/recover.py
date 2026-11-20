"""끊김 복구(설계서 5-4, 10-3, contracts/ingest.md "묶음 기록과 끊김 복구").

**파일을 지워도 되는지를 판정하는 곳은 이 모듈 하나뿐이다.** 커밋에 실패했거나 결과를 받지 못한 작업도 직접 치우지 않고
자기 묶음 기록에 대해 이 절차를 그대로 돌린다.

복구는 쓰기 잠금을 잡은 뒤에만 한다. 살아 있는 작업은 모두 같은 잠금을 잡고 있으므로, 잠금을 잡은 쪽이 보는 기록은
전부 끊긴 작업의 것이다. 처리 방법은 기록의 종류마다 등록 표 `RECOVERERS`에 둔다.

두 가지 방식으로 부른다.
- online: 관리 서비스·추가 적재가 작업 전에(DB에 적재된 프로젝트가 이 프로젝트일 때만). DB의 배치 기록을 보고 판정한다.
- load: 로더가 다시 만들기 전에. 어차피 폴더의 파일로 전부 다시 만드므로 파일만 정리한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import psycopg

from ..config import Project, table_order
from . import ledger, pending
from .apply import apply_batch, drop_stage, pg_message, run_checks, stage_source
from .batches import BATCH_RE, SourceFile, table_dir
from .faults import fault
from .policies import BatchError

log = logging.getLogger("dashkit.ingest")


class RecoverError(Exception):
    """복구할 수 없는 기록(형식이 틀렸거나 이 dashkit이 모르는 종류)."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


class RecoverUnknown(RecoverError):
    """복구의 반영을 커밋했는지 알 수 없다(접속이 끊김). 파일과 묶음 기록을 그대로 두고 다음 복구가 다시 판정한다."""


class _Rejected(Exception):
    """반영이 확정적으로 거절됐다(검증·참조·checks·DB 제약). 트랜잭션을 되돌린다."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass
class Outcome:
    id: str
    kind: str
    result: str              # "committed"(반영돼 있음) | "applied"(지금 반영함) | "discarded"(없던 일로 함)
    files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


Recoverer = Callable[[Project, "psycopg.Connection | None", Mapping[str, Any], str], Outcome]


def _final_path(project: Project, item: Mapping[str, Any]) -> Path:
    return table_dir(project.data_dir, item["table"]) / item["name"]


def _drop_incoming(project: Project, record: Mapping[str, Any]) -> None:
    for item in record.get("files", []):
        name = item.get("incoming")
        if name:
            p = pending.incoming_dir(project.data_dir) / name
            try:
                p.unlink()
            except FileNotFoundError:
                pass


def _check_items(project: Project, record: Mapping[str, Any], key: str, *, required: bool) -> list[dict[str, Any]]:
    items = record.get(key, [] if not required else None)
    tables = project.schema["tables"]
    if not isinstance(items, list) or (required and not items):
        raise RecoverError([f"data/_pending/{record.get('id')}.json: {key}가 없음"])
    for item in items:
        if not isinstance(item, dict) or item.get("table") not in tables or not isinstance(item.get("name"), str) \
                or "/" in item["name"] or item["name"].startswith((".", "_")):
            raise RecoverError([f"data/_pending/{record.get('id')}.json: {key} 항목 형식이 틀림"])
    return items


def _check_add(project: Project, record: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _check_items(project, record, "files", required=True)


def recover_add(project: Project, conn: psycopg.Connection | None, record: Mapping[str, Any], mode: str) -> Outcome:
    """종류 add(데이터 더하기). 위에서부터 차례로 본다.

    ① 적힌 배치가 배치 기록에 있다 → 파일은 그대로, 기록만 지운다(커밋은 됐다)
    ② 배치 기록에 없고 적힌 파일이 모두 제자리에 있다 → 묶음 전체를 한 트랜잭션으로 반영(load 방식은 반영할 필요가 없다).
       반영이 실패하면 ③
    ③ 일부만 제자리에 있다 → 옮겨진 파일을 모두 지운다. 묶음 전체가 없던 일이 된다

    기록의 first는 그 작업이 같은 트랜잭션에서 먼저 반영하던 손 파일(원래부터 폴더에 있던 것)이다. ②에서 함께 다시
    반영하고(참조 순서·번호순으로 새 파일보다 먼저), ③에서도 지우지 않는다.
    """
    files = _check_add(project, record)
    first = _check_items(project, record, "first", required=False)
    rid = str(record.get("id"))
    names = [f"{f['table']}/{f['name']}" for f in files]
    present = [_final_path(project, f).is_file() for f in files]
    if mode == "online":
        assert conn is not None
        with conn.cursor() as cur:
            recorded = ledger.read(cur, {f["table"] for f in files})
        if any(f["name"] in recorded.get(f["table"], {}) for f in files):
            _drop_incoming(project, record)
            return Outcome(rid, "add", "committed", names)
        if all(present):
            errors = _apply_group(project, conn, files, first)
            if not errors:
                _drop_incoming(project, record)
                return Outcome(rid, "add", "applied", names)
            log.warning("묶음 %s 반영 실패, 없던 일로 함: %s", rid, "; ".join(errors[:5]))
            _discard(project, files)
            _drop_incoming(project, record)
            return Outcome(rid, "add", "discarded", names, errors)
    else:
        if all(present):
            _drop_incoming(project, record)
            return Outcome(rid, "add", "applied", names)
    _discard(project, files)
    _drop_incoming(project, record)
    return Outcome(rid, "add", "discarded", names)


def _discard(project: Project, files: list[Mapping[str, Any]]) -> None:
    for f in files:
        try:
            _final_path(project, f).unlink()
        except FileNotFoundError:
            pass


def _number(name: str) -> int:
    m = BATCH_RE.fullmatch(name)
    return int(m.group(1)) if m else 0


def _apply_group(project: Project, conn: psycopg.Connection, files: list[Mapping[str, Any]],
                 first: list[Mapping[str, Any]] = ()) -> list[str]:
    """묶음의 파일 전체를 한 트랜잭션으로 반영한다(먼저 반영할 손 파일 → 새 파일, 각각 참조 순서대로).
    오류 목록(없으면 반영·커밋됨). 이미 배치 기록에 있는 손 파일은 건너뛴다."""
    order = table_order(project)
    rank = {t: i for i, t in enumerate(order)}
    with conn.cursor() as cur:
        done = ledger.read(cur, {f["table"] for f in first})
    first = [f for f in first if f["name"] not in done.get(f["table"], {})]
    items = sorted(first, key=lambda f: (rank[f["table"]], _number(f["name"]))) + \
        sorted(files, key=lambda f: rank[f["table"]])
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        conn.commit()          # 앞의 조회가 연 트랜잭션을 끝낸다. 아래 블록이 저장점이 아니라 진짜 트랜잭션이 되게
    try:
        with conn.transaction():
            try:
                with conn.cursor() as cur:
                    for f in items:
                        path = _final_path(project, f)
                        m = BATCH_RE.fullmatch(f["name"])
                        src = SourceFile(f["table"], f["name"], path, int(m.group(1)) if m else None)
                        staged = stage_source(cur, project.schema, src)
                        try:
                            counts = apply_batch(cur, project.schema, staged)
                        finally:
                            drop_stage(cur, staged)
                        ledger.add(cur, f["table"], f["name"], staged.sha256 or "", counts)
                    errors = run_checks(cur, project.schema, order)
                    if errors:
                        raise _Rejected(errors)
                    fault("recover_before_commit")
            except BatchError as exc:
                raise _Rejected(exc.errors or [exc.message]) from None
            except (psycopg.errors.IntegrityError, psycopg.errors.DataError,
                    psycopg.errors.ProgrammingError) as exc:      # DB가 행·문장을 거절했다(커밋 전)
                raise _Rejected([f"DB 오류: {pg_message(exc)}"]) from None
    except _Rejected as exc:
        return exc.errors
    except psycopg.Error as exc:
        # 접속이 끊겼거나 COMMIT의 응답을 받지 못했다: 반영됐는지 모른다. 파일을 지우지 않는다
        raise RecoverUnknown([f"복구 반영의 결과를 확인하지 못함({pg_message(exc)}). 다음 복구가 다시 판정한다"]) from None
    # 반영이 실제로 커밋됐는지 새 조회로 확인한다(묶음 기록은 그 뒤에만 지운다)
    try:
        with conn.cursor() as cur:
            recorded = ledger.read(cur, {f["table"] for f in [*files, *first]})
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            conn.commit()
    except psycopg.Error as exc:
        raise RecoverUnknown([f"복구 반영의 결과를 확인하지 못함({pg_message(exc)})"]) from None
    if not all(f["name"] in recorded.get(f["table"], {}) for f in [*files, *first]):
        raise RecoverUnknown(["복구 반영 뒤 배치 기록이 보이지 않음. 다음 복구가 다시 판정한다"])
    return []


def _recover_move(project: Project, conn: "psycopg.Connection | None", record: Mapping[str, Any], mode: str) -> Outcome:
    from .remove import recover_move      # remove.py가 이 모듈을 쓰므로 부를 때 가져온다

    return recover_move(project, conn, record, mode)


def _recover_config(project: Project, conn: "psycopg.Connection | None", record: Mapping[str, Any], mode: str) -> Outcome:
    from .structure import recover_config      # structure.py가 이 모듈을 쓰므로 부를 때 가져온다

    return recover_config(project, conn, record, mode)


def _recover_compact(project: Project, conn: "psycopg.Connection | None", record: Mapping[str, Any], mode: str) -> Outcome:
    from .compact import recover_compact      # compact.py가 이 모듈을 쓰므로 부를 때 가져온다

    return recover_compact(project, conn, record, mode)


RECOVERERS: dict[str, Recoverer] = {
    "add": recover_add,
    "compact": _recover_compact,   # 합치기(compact.py)
    "config": _recover_config,     # 구조 변경(structure.py)
    "remove": _recover_move,       # 되돌리기(remove.py)
    "restore": _recover_move,      # 되살리기(remove.py)
}


def recover(project: Project, conn: psycopg.Connection | None = None, *, mode: str = "online",
            only: str | None = None) -> list[Outcome]:
    """이 프로젝트 폴더의 남은 묶음 기록을 처리한다. 쓰기 잠금을 잡은 뒤에 부른다.

    only를 주면 그 id의 기록만 처리한다(커밋 결과를 받지 못한 작업이 자기 기록을 복구할 때).
    """
    outcomes: list[Outcome] = []
    keep: set[str] = set()
    for path, record in pending.list_records(project.data_dir):
        if record is None:
            raise RecoverError([f"data/_pending/{path.name}: 묶음 기록을 읽을 수 없음"])
        if only is not None and record.get("id") != only:
            for item in (record.get("files", []) if isinstance(record.get("files"), list) else []):
                if isinstance(item, dict) and item.get("incoming"):
                    keep.add(str(item["incoming"]))
            continue
        fn = RECOVERERS.get(str(record.get("kind")))
        if fn is None:
            raise RecoverError([f"data/_pending/{path.name}: 처리할 수 없는 종류 '{record.get('kind')}'"])
        outcome = fn(project, conn, record, mode)
        pending.remove_record(path)
        log.info("끊긴 묶음 %s(%s) 복구: %s", outcome.id, outcome.kind, outcome.result)
        outcomes.append(outcome)
    if only is None:
        pending.sweep_incoming(project.data_dir, keep)
    return outcomes
