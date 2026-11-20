"""데이터 더하기 한 길(설계서 5-1, 5-4, contracts/ingest.md "더하기").

`ingest(project, items)` 하나를 REST·CLI·Studio가 부른다. items는 (테이블, 입력) 쌍의 목록이고 파일 하나는 그 한 개짜리
경우다. 한 행도 배치다. 여러 항목은 한 트랜잭션이다(전부 들어가거나 하나도 들어가지 않는다).

    잠금 밖: 설정 읽기 → 접수·검증·정규형 쓰기(incoming)·임시 테이블 채우기
    잠금 안: 프로젝트·설정 확인 → 복구 → 대조 → 중복 확인 → (뒤에 놓인 새 파일 먼저 반영) → apply_batch → checks →
             묶음 기록 → 파일 옮기기 → 배치 기록 → 커밋 → 묶음 기록 지우기
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import psycopg

from .. import config, db
from ..config import Project, table_order
from . import batches, ledger, pending
from .apply import StagedBatch, apply_batch, drop_stage, pg_message, stage_batch, stage_source
from .batches import CanonicalWriter
from .intake import PARSERS, Intake, IntakeError, Parser, table_labels
from .operation import IngestError, Move, StaleConfig, Session, after_ingest, finish, locked

log = logging.getLogger("dashkit.ingest")

CHANGES_SHOWN = 20
METHODS = ("form", "paste", "upload", "api", "cli")


@dataclass
class Item:
    """더할 입력 하나. source는 파서를 만드는 정보: {"kind": "csv", "path": Path, "name": str} 또는
    {"kind": "rows", "rows": [...]}. columns는 사람이 정한 머리글 맞춤({머리글: 열 또는 None})."""
    table: str
    source: Mapping[str, Any]
    columns: Mapping[str, Any] | None = None


@dataclass
class _Prepared:
    item: Item
    staged: StagedBatch
    writer_path: Path | None
    sha256: str
    normalized: dict[str, Any] = field(default_factory=dict)
    columns: dict[str, Any] = field(default_factory=dict)


def make_parser(source: Mapping[str, Any], *, strict: bool) -> Parser:
    kind = source.get("kind")
    if kind == "csv":
        return PARSERS["csv"](Path(source["path"]), str(source.get("name") or Path(source["path"]).name), strict=strict)
    if kind == "rows":
        return PARSERS["rows"](source.get("rows"), str(source.get("name") or "rows"))
    raise IngestError(400, "bad_request", f"알 수 없는 입력 종류 '{kind}'")


def _load(name: str, projects_dir: Path | None) -> Project:
    try:
        return config.load_project(name, projects_dir)
    except config.ConfigError as exc:
        raise IngestError(422, "invalid_config", "설정 검사를 통과하지 못했습니다", exc.errors) from None


def check_table(project: Project, table: str) -> str:
    """경로의 {table} → schema.yaml의 테이블. 없으면 404, replace 테이블이면 409 not_append."""
    for known, spec in project.schema["tables"].items():
        if known == table:
            if (spec.get("mode") or "replace") != "append":
                raise IngestError(409, "not_append", f"'{known}'은 append 테이블이 아닙니다(mode: replace는 파일을 통째로 바꿉니다)")
            return known
    raise IngestError(404, "unknown_table", "schema.yaml에 없는 테이블입니다")


def ingest(project_name: str, items: list[Item], *, dry_run: bool = False, force: bool = False,
           strict: bool = False, skip_invalid: bool = False, projects_dir: Path | None = None,
           dbname: str | None = None, wait_ms: int | None = None,
           progress: Callable[[str, Mapping[str, Any]], None] | None = None) -> dict[str, Any]:
    """데이터를 더한다. 성공하면 결과(contracts/ingest.md "결과 형식"), 실패하면 IngestError.

    dry_run이면 반영까지 해 보고 되돌린다(미리보기). 결과 형식은 같다.
    """
    if not items:
        raise IngestError(400, "bad_request", "더할 입력이 없습니다")
    for attempt in (1, 2):
        project = _load(project_name, projects_dir)
        from ..project import store

        revision = store.revision(project.root)
        tables = [check_table(project, it.table) for it in items]
        try:
            conn = db.owner_conn(dbname)
        except (psycopg.Error, RuntimeError) as exc:
            raise IngestError(503, "db_unavailable", f"DB 접속 실패: {pg_message(exc)}") from None
        prepared: list[_Prepared] = []
        try:
            prepared = _prepare(conn, project, items, tables, strict=strict, skip_invalid=skip_invalid,
                                progress=progress)
            invalid = [p for p in prepared if not p.staged.ok]
            if invalid and not dry_run:
                errors = [e for p in invalid for e in p.staged.errors]
                raise IngestError(422, "invalid_rows", "검증을 통과하지 못한 행이 있습니다(아무것도 넣지 않았습니다)",
                                  errors[:200], error_count=sum(p.staged.error_count for p in invalid),
                                  batches=[_result(p, None) for p in prepared])
            if invalid:
                return {"ok": False, "dry_run": True, "batches": [_result(p, None) for p in prepared]}
            if progress:
                progress("lock", {})
            try:
                with locked(project, conn, revision=revision, wait_ms=wait_ms) as session:
                    return _commit(session, prepared, force=force, dry_run=dry_run)
            except StaleConfig:
                if attempt == 2:
                    raise IngestError(409, "stale_config",
                                      "설정 파일이 그 사이에 바뀌었습니다. 다시 시도하세요") from None
                log.info("설정이 바뀌어 다시 접수한다(%s)", project.name)
                continue
        finally:
            for p in prepared:
                if p.writer_path is not None:
                    try:
                        p.writer_path.unlink()
                    except FileNotFoundError:
                        pass
            if not conn.closed:
                try:
                    conn.rollback()
                    with conn.cursor() as cur:
                        for p in prepared:
                            drop_stage(cur, p.staged)
                    conn.commit()
                except psycopg.Error:
                    pass
                conn.close()
    raise AssertionError("unreachable")


def _prepare(conn: psycopg.Connection, project: Project, items: list[Item], tables: list[str], *,
             strict: bool, skip_invalid: bool, progress: Callable[[str, Mapping[str, Any]], None] | None
             ) -> list[_Prepared]:
    """잠금 밖: 접수·검증·정규형 쓰기·임시 테이블 채우기. 올린 내용만 보고 정해지는 일이다."""
    out: list[_Prepared] = []
    for item, tname in zip(items, tables):
        table = project.schema["tables"][tname]
        if progress:
            progress("read", {"table": tname})
        try:
            parser = make_parser(item.source, strict=strict)
        except IntakeError as exc:
            raise IngestError(422, exc.detail, exc.message, exc.errors or None, **exc.extra) from None
        try:
            try:
                intake = Intake(parser, tname, table, labels=table_labels(project.dashboard, tname),
                                overrides=item.columns, strict=strict)
            except IntakeError as exc:
                raise IngestError(422, exc.detail, exc.message, exc.errors or None, **exc.extra) from None
            path = pending.incoming_path(project.data_dir)
            writer = CanonicalWriter(path)
            try:
                with conn.cursor() as cur:
                    staged = stage_batch(cur, intake, writer=writer, skip_invalid=skip_invalid)
                conn.commit()
                sha = writer.close()
            except BaseException:
                writer.abort()
                raise
            staged.sha256 = sha
            out.append(_Prepared(item, staged, path, sha, staged.normalized, staged.columns))
        finally:
            parser.close()
    return out


def _commit(session: Session, prepared: list[_Prepared], *, force: bool, dry_run: bool) -> dict[str, Any]:
    project = session.project
    targets = {p.staged.table for p in prepared}
    # 같은 내용 다시 올리기(잠금 안에서 배치 기록과 견준다). 같은 요청 안의 같은 내용도 막는다
    if not force:
        dups: list[dict[str, str]] = []
        seen: dict[tuple[str, str], str] = {}
        for p in prepared:
            for name, rec in session.recorded.get(p.staged.table, {}).items():
                if rec.sha256 == p.sha256:
                    dups.append({"table": p.staged.table, "file": name})
                    break
            else:
                key = (p.staged.table, p.sha256)
                if key in seen:
                    dups.append({"table": p.staged.table, "file": seen[key]})
                seen[key] = "(같은 요청)"
        if dups:
            raise IngestError(409, "duplicate_file", "같은 내용의 배치가 이미 있습니다(force=true면 그래도 넣습니다)",
                              [f"data/{d['table']}/{d['file']}" for d in dups], duplicates=dups)
    first = session.reject_unless_clean(allow_new_after=targets)
    order = table_order(project)
    rank = {t: i for i, t in enumerate(order)}
    # 손으로 넣은 파일도 참조 순서(부모 테이블 먼저)로, 테이블 안에서는 번호순으로 반영한다(load와 같은 순서)
    first = sorted(first, key=lambda f: (rank[f.table], f.number or 0))
    ordered = sorted(range(len(prepared)), key=lambda i: (rank[prepared[i].staged.table], i))
    # 이름 매기기(잠금 안). 같은 테이블에 여럿이면 번호를 이어서 준다
    names: dict[int, str] = {}
    taken: dict[str, int] = {}
    for i in ordered:
        t = prepared[i].staged.table
        base = taken.get(t)
        if base is None:
            base = max(batches.all_numbers(project.data_dir, t), default=0)
        base += 1
        taken[t] = base
        stamp = batches.next_name(project.data_dir, t).split("-", 1)[1]
        names[i] = f"{base:0{batches.NUMBER_WIDTH}d}-{stamp}"
    results: dict[int, dict[str, Any]] = {}
    hand_placed: list[dict[str, Any]] = []

    def work(cur: psycopg.Cursor) -> None:
        for src in first:      # 손으로 넣은 정규형 파일(번호가 기록의 모든 배치보다 뒤)을 먼저 반영한다
            staged = stage_source(cur, project.schema, src)
            try:
                counts = apply_batch(cur, project.schema, staged)
            finally:
                drop_stage(cur, staged)
            ledger.add(cur, src.table, src.name, staged.sha256 or "", counts)
            hand_placed.append({"table": src.table, "file": src.name, "rows": counts["total"]})
        for i in ordered:
            p = prepared[i]
            results[i] = apply_batch(cur, project.schema, p.staged, changes_limit=CHANGES_SHOWN)

    def add_records(cur: psycopg.Cursor) -> None:    # 파일을 옮긴 뒤 배치 기록을 더한다(5-4의 확정 순서)
        for i in ordered:
            ledger.add(cur, prepared[i].staged.table, names[i], prepared[i].sha256, results[i])

    moves = [Move(prepared[i].staged.table, names[i], prepared[i].writer_path, prepared[i].sha256)   # type: ignore[arg-type]
             for i in ordered]
    def recorded() -> None:
        for p in prepared:
            p.writer_path = None          # 묶음 기록이 가리킨다. 지우지 않는다(복구 절차만 치운다)

    # 먼저 반영하는 손 파일도 묶음 기록에 적는다: 커밋 전에 끊기면 복구가 새 파일과 함께 다시 반영한다(지우지는 않는다)
    finish(session, "add", moves, work, dry_run=dry_run, on_record=recorded, post_move=add_records,
           extra={"first": [{"table": f.table, "name": f.name} for f in first]})
    if not dry_run:
        after_ingest(project, sorted(targets))
    out = [_result(prepared[i], results.get(i), None if dry_run else names[i]) for i in range(len(prepared))]
    body: dict[str, Any] = {"ok": True, "dry_run": dry_run, "batches": out}
    if hand_placed:
        body["applied_first"] = hand_placed
    if session.recovered:
        body["recovered"] = [{"id": o.id, "kind": o.kind, "result": o.result} for o in session.recovered]
    return body


def _result(p: _Prepared, counts: Mapping[str, Any] | None, name: str | None = None) -> dict[str, Any]:
    st = p.staged
    rows: dict[str, Any] = {"total": st.total, "invalid": st.invalid}
    if counts is not None:
        rows.update({k: counts[k] for k in ("inserted", "updated", "skipped", "unchanged")})
    return {
        "table": st.table,
        "file": name,
        "sha256": p.sha256,
        "rows": rows,
        "normalized": {k: v for k, v in st.normalized.items()},
        "columns": st.columns,
        "errors": st.errors,
        "error_count": st.error_count,
        "changes": list((counts or {}).get("changes") or []),
    }
