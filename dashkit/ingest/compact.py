"""배치 합치기(설계서 6-4, contracts/ingest.md "합치기"). 한 행씩 더해 쌓인 작은 배치를 파일 하나로 정리한다. DB의 행은 바뀌지 않는다.

공통 순서 위에서 돈다. 전제: 그 테이블의 배치 파일과 배치 기록이 같아야 한다(반영되지 않았거나 바뀐 파일이 있으면 409
rebuild_required. 합친 파일은 DB의 행에서 만들기 때문에, 반영되지 않은 배치가 있으면 그 행을 잃는다).

1. 지금 테이블의 행을 기본 키 순으로 정규형 **임시 파일**(`data/_pending/incoming/`)로 쓴다(모든 열의 머리글).
2. 묶음 기록(종류 compact: 새 파일의 최종 이름·해시·행 수, 옮길 옛 파일 목록, `_merged/<시각>/` 폴더).
3. 옛 배치 파일(과 `data/<테이블>.csv`)을 `_merged/<시각>/`으로 옮긴다.
4. 임시 파일을 제 이름(`<다음 번호>-<시각>-merged.csv.gz`. 이름이 합친 배치의 표식이다)으로 바꾼다.
5. 배치 기록을 고쳐(그 테이블의 줄을 지우고 새 줄 하나) 커밋하고 묶음 기록을 지운다.

복구: 새 파일(임시 이름이든 제 이름이든)이 있고 해시가 기록과 같으면 **끝까지 진행**한다(남은 옛 파일을 옮기고, 새 파일을
제 이름으로, 배치 기록을 고친다). 아니면 **전체를 되돌린다**(`_merged/<시각>/`의 파일을 제자리로, 새 파일을 지운다).
그래서 로더가 옛 배치의 일부와 그 행을 이미 담은 합친 배치를 함께 읽는 일이 없다. `_merged/`는 다음 load가 성공하면 지운다.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import psycopg
from psycopg import sql

from .. import db
from ..config import Project
from ..loader.record import canonical
from . import batches, ledger, pending
from .add import check_table, load_stable
from .batches import MERGED_DIR, CanonicalWriter
from .faults import SimulatedCrash, fault
from .operation import IngestError, Session, _resolve_or_raise, locked


def compact(project_name: str, table: str, *, projects_dir: Path | None = None, dbname: str | None = None,
            wait_ms: int | None = None) -> dict[str, Any]:
    project, revision = load_stable(project_name, projects_dir)
    tname = check_table(project, table)
    try:
        conn = db.owner_conn(dbname)
    except (psycopg.Error, RuntimeError) as exc:
        raise IngestError(503, "db_unavailable", f"DB 접속 실패: {exc}") from None
    try:
        with locked(project, conn, revision=revision, wait_ms=wait_ms) as session:
            diff = session.diff.get(tname)
            if diff is not None and not diff.clean:
                raise IngestError(409, "rebuild_required", "반영되지 않았거나 바뀐 배치 파일이 있어 합치지 않습니다. 먼저 적용하세요",
                                  diff.lines(tname))
            return _compact(session, tname)
    finally:
        if not conn.closed:
            conn.close()


def _compact(session: Session, tname: str) -> dict[str, Any]:
    project, conn = session.project, session.conn
    spec = project.schema["tables"][tname]
    olds, _naming = batches.sources(project.data_dir, tname, "append")
    if len(olds) < 2:
        return {"ok": True, "table": tname, "file": None, "merged": 0, "rows": None}
    columns = list(spec["columns"])
    temp = pending.incoming_path(project.data_dir)
    writer = CanonicalWriter(temp)
    rows = 0
    try:
        writer.header(columns)
        with conn.cursor(name="dk_compact") as cur:     # 서버 쪽 커서로 조금씩 읽는다(큰 테이블)
            cur.execute(sql.SQL("SELECT {} FROM {}.{} ORDER BY {}").format(
                sql.SQL(", ").join(map(sql.Identifier, columns)), sql.Identifier("raw"), sql.Identifier(tname),
                sql.SQL(", ").join(map(sql.Identifier, spec["primary_key"]))))
            for row in cur:
                writer.row(["" if v is None else str(canonical(v)) for v in row])
                rows += 1
        conn.commit()
        sha = writer.close()
    except BaseException:
        writer.abort()
        raise
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = batches.next_name(project.data_dir, tname, merged=True)
    merged_rel = f"{MERGED_DIR}/{stamp}"
    record = {"id": pending.new_id(), "kind": "compact", "project": project.name, "table": tname,
              "name": name, "incoming": temp.name, "sha256": sha, "rows": rows, "merged": merged_rel,
              "olds": [{"name": f.name, "first": f.number is None} for f in olds]}
    record_path = pending.write_record(project.data_dir, record)
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                _finish(project, record)
                fault("before_commit")
                cur.execute(sql.SQL("DELETE FROM {}.{} WHERE table_name = %s").format(
                    sql.Identifier("dashkit_meta"), sql.Identifier("batch")), (tname,))
                ledger.add(cur, tname, name, sha, {"total": rows, "inserted": rows, "updated": 0, "skipped": 0,
                                                   "unchanged": 0})
        fault("after_commit")
    except SimulatedCrash:
        raise
    except BaseException as exc:
        _resolve_or_raise(session, record, exc)
        return {"ok": True, "table": tname, "file": name, "merged": len(olds), "rows": rows}
    pending.remove_record(record_path)
    return {"ok": True, "table": tname, "file": name, "merged": len(olds), "rows": rows}


def _paths(project: Project, record: Mapping[str, Any]) -> tuple[Path, Path, Path, list[tuple[Path, Path]]]:
    t = record["table"]
    folder = batches.table_dir(project.data_dir, t)
    merged = folder / record["merged"]
    moves = []
    for old in record["olds"]:
        src = batches.first_file(project.data_dir, t) if old.get("first") else folder / old["name"]
        moves.append((src, merged / old["name"]))
    return folder / record["name"], pending.incoming_dir(project.data_dir) / record["incoming"], merged, moves


def _finish(project: Project, record: Mapping[str, Any]) -> None:
    """3~4단계(되풀이해도 된다): 남은 옛 파일을 _merged/로 옮기고 새 파일을 제 이름으로."""
    final, temp, merged, moves = _paths(project, record)
    merged.mkdir(parents=True, exist_ok=True)
    for src, dst in moves:
        if src.exists() and not dst.exists():
            os.replace(src, dst)
            fault("after_move")
    if temp.exists() and not final.exists():
        os.replace(temp, final)
    batches.remember_hash(final, record["sha256"])


def recover_compact(project: Project, conn: psycopg.Connection | None, record: Mapping[str, Any], mode: str):
    from .recover import Outcome

    rid = str(record.get("id"))
    final, temp, merged, moves = _paths(project, record)
    new = final if final.exists() else temp if temp.exists() else None
    if new is not None and batches.content_hash(new) == record.get("sha256"):
        _finish(project, record)        # 끝까지 진행한다
        if conn is not None and mode == "online":
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql.SQL("DELETE FROM {}.{} WHERE table_name = %s").format(
                        sql.Identifier("dashkit_meta"), sql.Identifier("batch")), (record["table"],))
                    ledger.add(cur, record["table"], record["name"], record["sha256"],
                               {"total": record["rows"], "inserted": record["rows"], "updated": 0, "skipped": 0,
                                "unchanged": 0})
        return Outcome(rid, "compact", "applied", [record["name"]])
    for src, dst in moves:              # 전체를 되돌린다
        if dst.exists() and not src.exists():
            os.replace(dst, src)
    for p in (final, temp):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    shutil.rmtree(merged, ignore_errors=True)
    return Outcome(rid, "compact", "discarded", [record["name"]])


def clear_merged(project: Project) -> None:
    """합친 뒤의 load가 한 번 성공했으면 _merged/를 지운다(로더가 커밋 뒤에 부른다)."""
    for tname, spec in project.schema["tables"].items():
        if (spec.get("mode") or "replace") == "append":
            shutil.rmtree(batches.table_dir(project.data_dir, tname) / MERGED_DIR, ignore_errors=True)
