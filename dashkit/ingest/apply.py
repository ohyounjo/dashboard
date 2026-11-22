"""배치 하나를 DB에 넣는다(설계서 5-4, 5-7, contracts/ingest.md "반영").

**운영 중의 추가 적재와 load의 다시 만들기는 같은 함수를 쓴다.** load는 빈 테이블에 배치를 번호순으로 하나씩
`stage_batch` → `apply_batch`로 넣고, 추가 적재도 같은 두 함수로 넣는다. 그래서 하나씩 더한 결과와 처음부터 다시 만든
결과가 같다(5-7).

- stage_batch(잠금 밖에서 해도 되는 일): 접수한 행을 한 줄씩 검증하며 임시 테이블에 COPY하고(정규형 파일도 함께 쓸 수
  있다) 배치 안의 기본 키 중복을 본다. 올린 내용만 보고 정해지는 일이다.
- apply_batch(잠금 안): 참조 대조 → 기존 행과 기본 키로 비교 → 충돌 정책 → 새 행 넣기(빠진 열은 default 또는 NULL).
  DB 상태를 보고 정하는 일이다.
- run_checks: 프로젝트의 checks(위반 행이 하나라도 있으면 실패). load와 추가 적재가 같은 함수를 쓴다.
"""
from __future__ import annotations

import itertools
from array import array
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

import psycopg
from psycopg import sql

from ..loader.validate import _parse
from . import policies
from .intake import CsvParser, Intake, IntakeError, MAX_REPORTED, redact_sensitive_errors
from .batches import CanonicalWriter, SourceFile
from .policies import ApplyContext, BatchError, key_label

RAW = "raw"
MAX_KEYS_SHOWN = 20

PG_TYPES = {
    "text": "text",
    "enum": "text",
    "integer": "bigint",
    "number": "numeric",
    "boolean": "boolean",
    "date": "date",
    "timestamp": "timestamptz",
}

_seq = itertools.count(1)


@dataclass
class StagedBatch:
    """임시 테이블에 채운 배치 하나."""
    table: str
    header: list[str]
    temp: str
    rows: int                                  # 임시 테이블의 행 수(넣을 행)
    where: Callable[[int, str], str]
    total: int = 0                             # 읽은 행 수(오류 행 포함)
    invalid: int = 0
    errors: list[str] = field(default_factory=list)
    error_count: int = 0
    normalized: dict[str, Any] = field(default_factory=dict)
    columns: dict[str, Any] = field(default_factory=dict)
    sha256: str | None = None                  # 정규형 내용의 해시(정규형 파일을 썼거나 입력이 정규형일 때)
    fatal: bool = False
    written: Any = None                        # 오류 행을 빼고 넣을 때: 정규형 파일에 쓴 행의 위치(array)
    rejects: Any = None                        # 오류 행을 빼고 넣을 때: 빠진 행 파일(RejectWriter)
    dropped: list[int] = field(default_factory=list)   # 잠금 안에서 뺀 행(참조·새 행 필수 열)의 위치
    skip_invalid: bool = False                 # 오류 행을 빼고 넣는다(빠진 행의 오류는 errors에 알리기만 한다)
    duplicates: int = 0                        # 배치 안의 기본 키 중복(언제나 전체 거절)

    @property
    def ok(self) -> bool:
        """반영해도 되는가. 오류 행을 빼고 넣는 경우에도 더 읽을 수 없는 오류와 배치 안의 키 중복은 전체 거절이다."""
        if self.fatal or self.duplicates:
            return False
        return self.skip_invalid or not self.errors


def default_value(spec: Mapping[str, Any]) -> Any:
    """열의 default를 파이썬 값으로(없으면 None). 설정 검사(config.py)가 미리 형식을 본다."""
    if "default" not in spec or spec["default"] is None:
        return None
    raw = spec["default"]
    text = ("true" if raw else "false") if isinstance(raw, bool) else str(raw)
    return _parse(text, spec)


class RejectWriter:
    """빠진 행 파일(설계서 5-4 "오류 행 빼고 넣기"): 빠진 행의 값(정규형 머리글 순서) + `오류` 열. 엑셀에서 열리도록
    UTF-8 BOM을 붙인다. 고쳐서 그 파일만 다시 올리면 된다."""

    ERROR_COLUMN = "오류"

    def __init__(self, path: Any):
        import csv as _csv

        self.path = path
        self._file = open(path, "w", encoding="utf-8-sig", newline="")
        self._writer = _csv.writer(self._file, lineterminator="\r\n")
        self.header: list[str] | None = None
        self.rows = 0

    def start(self, header: list[str]) -> None:
        if self.header is None:
            self.header = list(header)
            self._writer.writerow([*header, self.ERROR_COLUMN])

    def row(self, values: list[str], reasons: list[str]) -> None:
        width = len(self.header or [])
        cells = list(values) + [""] * (width - len(values))
        self._writer.writerow([*cells[:width], "; ".join(reasons)])
        self.rows += 1

    def close(self) -> None:
        self._file.close()


def stage_batch(cur: psycopg.Cursor, intake: Intake, *, writer: CanonicalWriter | None = None,
                skip_invalid: bool = False, rejects: "RejectWriter | None" = None,
                progress: Callable[[int, int, int | None], None] | None = None) -> StagedBatch:
    """intake의 행을 검증하며 임시 테이블에 넣는다. 오류는 StagedBatch.errors에 모은다(예외를 던지지 않는다).

    writer가 있으면 넣는 행만 정규형 파일에 쓴다(skip_invalid면 오류 행을 빼고 쓴다). skip_invalid이고 rejects가
    있으면 빠진 행을 그 파일에 쓴다. progress(읽은 행, 읽은 바이트, 전체 바이트)는 1만 행마다 부른다.
    """
    table = intake.table
    name = f"_dk_stage_{next(_seq)}"
    temp = sql.Identifier(name)
    cols = [sql.SQL("_at bigint NOT NULL")] + [
        sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(PG_TYPES[table["columns"][c]["type"]])) for c in intake.header]
    cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(temp))
    cur.execute(sql.SQL("CREATE TEMP TABLE {} ({})").format(temp, sql.SQL(", ").join(cols)))
    if writer is not None:
        writer.header(intake.header)
    rows = 0
    report = intake.report
    written = array("q")           # 정규형 파일에 쓴 행의 위치(오류 행을 뺄 때 파일을 다시 쓰려고)
    if rejects is not None and skip_invalid:
        rejects.start(intake.header)
    stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(
        temp, sql.SQL(", ").join([sql.Identifier("_at")] + [sql.Identifier(c) for c in intake.header]))
    sensitive = {c for c, s in table["columns"].items() if isinstance(s, Mapping) and s.get("sensitive")}
    with cur.copy(stmt) as copy:
        for at, values, typed in intake.rows():
            if progress is not None and report.total % 10000 == 0:
                done, total = intake.parser.progress()
                progress(report.total, done, total)
            if typed is None:
                if skip_invalid:
                    report.rejected.append(at)
                    if rejects is not None:
                        shown_vals = ["***" if (c in sensitive and v != "") else v for c, v in zip(intake.header, values)]
                        rejects.row(shown_vals, [f"{intake.parser.where(at, '-').rsplit(':', 1)[0]}: {p}"
                                                  for p in intake.last_problems])
                continue
            if report.invalid and not skip_invalid:
                continue          # 오류가 하나라도 있으면 더 넣지 않고 나머지 오류만 모은다
            copy.write_row([at, *typed])
            if writer is not None:
                writer.row(values)
                if skip_invalid:
                    written.append(at)
            rows += 1
    staged = StagedBatch(intake.table_name, list(intake.header), name, rows, intake.parser.where,
                         total=report.total, invalid=report.invalid, errors=list(report.errors),
                         error_count=report.error_count, normalized=dict(report.normalized),
                         columns=dict(report.columns), fatal=report.fatal, skip_invalid=skip_invalid)
    staged.written = written
    staged.rejects = rejects if skip_invalid else None
    if staged.invalid and not skip_invalid:
        return staged
    _check_duplicates(cur, staged, intake)
    return staged


def _check_duplicates(cur: psycopg.Cursor, staged: StagedBatch, intake: Intake) -> None:
    """배치 안의 기본 키 중복(올린 내용만 보고 정해진다). 오류는 staged.errors에."""
    pk = list(intake.table["primary_key"])
    keys = sql.SQL(", ").join(sql.Identifier(c) for c in pk)
    cur.execute(sql.SQL(
        "SELECT {}, array_agg(_at ORDER BY _at) FROM {} GROUP BY {} HAVING count(*) > 1 ORDER BY min(_at) LIMIT %s"
    ).format(keys, sql.Identifier(staged.temp), keys), (MAX_REPORTED + 1,))
    first_word = "줄" if intake.parser.kind in ("csv", "xlsx") else "위치"
    for r in cur.fetchall():
        values, ats = list(r[:-1]), r[-1]
        shown = ", ".join(str(v) for v in values)
        first = ats[0] if intake.parser.kind in ("csv", "xlsx") else intake.parser.where(ats[0], "-")
        for at in ats[1:]:
            staged.error_count += 1
            staged.invalid += 1
            staged.duplicates += 1
            if len(staged.errors) < MAX_REPORTED:
                line = f"{staged.where(at, ','.join(pk))}: 기본 키 중복 ({shown}, 처음 나온 {first_word} {first})"
                staged.errors.append(redact_sensitive_errors(intake.table, [line])[0])


def source_label(src: SourceFile) -> str:
    """오류 위치에 쓰는 원본 파일 이름(프로젝트 기준): data/<테이블>.csv 또는 data/<테이블>/<배치>."""
    return f"data/{src.name}" if src.number is None else f"data/{src.table}/{src.name}"


def stage_source(cur: psycopg.Cursor, project_schema: Mapping[str, Any], src: SourceFile) -> StagedBatch:
    """원본 데이터 파일(정규형) 하나를 엄격하게 읽어 임시 테이블에 넣는다. 로더와 복구가 쓴다.

    머리글 오류(IntakeError)도 StagedBatch.errors로 돌려준다. sha256은 압축을 푼 내용의 해시다.
    """
    table = project_schema["tables"][src.table]
    parser = CsvParser(src.path, source_label(src), strict=True, hash_input=True)
    try:
        try:
            intake = Intake(parser, src.table, table, strict=True)
        except IntakeError as exc:
            return StagedBatch(src.table, [], "", 0, parser.where, errors=exc.errors or [exc.message],
                               error_count=len(exc.errors) or 1, fatal=True)
        staged = stage_batch(cur, intake)
        if not staged.fatal:
            # 나머지를 끝까지 읽어 해시를 마친다(오류로 일찍 멈춘 경우에도 해시가 필요하지는 않지만 같은 값을 준다)
            for _chunk in iter(lambda: parser._text.read(1024 * 1024), ""):
                pass
        staged.sha256 = parser.input_hash
        return staged
    finally:
        parser.close()


def drop_stage(cur: psycopg.Cursor, staged: StagedBatch) -> None:
    if not staged.temp:
        return
    cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(staged.temp)))


def _target(table: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(RAW), sql.Identifier(table))


def apply_batch(cur: psycopg.Cursor, project_schema: Mapping[str, Any], staged: StagedBatch, *,
                changes_limit: int = 0) -> dict[str, Any]:
    """임시 테이블의 배치를 raw.<테이블>에 넣는다. 실패하면 BatchError(트랜잭션은 부른 쪽이 되돌린다).

    결과: {"total", "inserted", "updated", "skipped", "unchanged", "changes": [...]}
    """
    tname = staged.table
    table = project_schema["tables"][tname]
    if not staged.ok:
        raise BatchError("invalid_rows", "검증을 통과하지 못한 행이 있습니다", staged.errors)
    temp = sql.Identifier(staged.temp)
    target = _target(tname)
    pk = list(table["primary_key"])
    _check_refs(cur, project_schema, staged)
    counts = {"total": staged.rows, "inserted": 0, "updated": 0, "skipped": 0, "unchanged": 0}
    join = sql.SQL(" AND ").join(
        sql.SQL("t.{} = b.{}").format(sql.Identifier(c), sql.Identifier(c)) for c in pk)
    cur.execute(sql.SQL("SELECT count(*) FROM {} b JOIN {} t ON {}").format(temp, target, join))
    matches = int(cur.fetchone()[0])
    ctx = ApplyContext(cur, tname, target, temp, pk, staged.header, matches, counts, changes_limit)
    policy = policies.POLICIES[table.get("on_conflict") or policies.DEFAULT_POLICY]
    sensitive_pk = any(table["columns"][k].get("sensitive") for k in pk)
    try:
        policy(ctx)
    except BatchError as exc:
        if sensitive_pk and exc.detail == "key_conflict":
            raise BatchError(exc.detail, exc.message, redact_sensitive_errors(table, exc.errors),
                             keys=["***" for _ in (exc.extra.get("keys") or [])]) from None
        raise
    sensitive = {c for c, s in table["columns"].items() if isinstance(s, Mapping) and s.get("sensitive")}
    if sensitive and ctx.changes:
        for ch in ctx.changes:
            for side in ("before", "after"):
                side_map = ch.get(side)
                if isinstance(side_map, dict):
                    for col in list(side_map):
                        if col in sensitive and side_map[col] is not None:
                            side_map[col] = "***"
    _insert_new(cur, table, staged, target, temp, join, counts)
    counts["total"] = staged.rows        # 새 행의 필수 열 오류로 뺀 행이 있으면 줄어든다(배치 파일의 행 수와 같게)
    return {**counts, "changes": ctx.changes}


def _check_refs(cur: psycopg.Cursor, project_schema: Mapping[str, Any], staged: StagedBatch) -> None:
    """참조 열의 값이 대상 테이블(DB)에 있는지. 없으면 줄 단위로 알린다."""
    table = project_schema["tables"][staged.table]
    errors: list[tuple[int, str, str]] = []
    for c in staged.header:
        ref = table["columns"][c].get("ref")
        if not ref:
            continue
        rt, rc = ref.split(".", 1)
        cur.execute(sql.SQL(
            "SELECT b._at, b.{c} FROM {temp} b WHERE b.{c} IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM {target} r WHERE r.{rc} = b.{c}) ORDER BY b._at LIMIT %s").format(
            c=sql.Identifier(c), temp=sql.Identifier(staged.temp), target=_target(rt), rc=sql.Identifier(rc)),
            (None if staged.skip_invalid else MAX_REPORTED,))
        found_limited = cur.fetchall()
        for at, v in found_limited:
            reason = "참조 대상 {0}.{1}에 값 '{2}'가 없음".format(
                rt, rc, "***" if table["columns"][c].get("sensitive") else v)
            errors.append((at, c, reason))
    if errors and staged.skip_invalid:
        _drop_rows(cur, table, staged, errors)
        return
    if errors:
        raise BatchError("invalid_rows", "참조 대상에 없는 값이 있습니다",
                         [f"{staged.where(at, c)}: {reason}" for at, c, reason in errors[:MAX_REPORTED]])


def _drop_rows(cur: psycopg.Cursor, table: Mapping[str, Any], staged: StagedBatch, found: list[tuple[int, str, str]]) -> None:
    """오류 행을 빼고 넣을 때(skip_invalid): DB를 보고 정해진 오류(참조, 새 행의 필수 열)가 난 행을 임시 테이블에서 뺀다.
    빠진 행 파일에 쓰고 staged.dropped에 위치를 적는다(부른 쪽이 정규형 파일을 다시 쓴다)."""
    from ..loader.record import canonical

    by_at: dict[int, list[str]] = {}
    for at, col, reason in found:
        by_at.setdefault(at, []).append(f"{col}: {reason}")
        staged.error_count += 1
        if len(staged.errors) < MAX_REPORTED:
            staged.errors.append(f"{staged.where(at, col)}: {reason}")
    ats = sorted(by_at)
    temp = sql.Identifier(staged.temp)
    if staged.rejects is not None:
        sensitive = {c for c, s in table["columns"].items() if isinstance(s, Mapping) and s.get("sensitive")}
        cols = sql.SQL(", ").join(sql.Identifier(c) for c in staged.header)
        cur.execute(sql.SQL("SELECT _at, {} FROM {} WHERE _at = ANY(%s) ORDER BY _at").format(cols, temp), (ats,))
        for row in cur.fetchall():
            values = [
                ("" if v is None else ("***" if c in sensitive else str(canonical(v))))
                for c, v in zip(staged.header, row[1:])
            ]
            where = staged.where(row[0], "-").rsplit(":", 1)[0]
            staged.rejects.row(values, [f"{where}: {r}" for r in by_at[row[0]]])
    cur.execute(sql.SQL("DELETE FROM {} WHERE _at = ANY(%s)").format(temp), (ats,))
    staged.dropped += ats
    staged.invalid += len(ats)
    staged.rows -= len(ats)


def _insert_new(cur: psycopg.Cursor, table: Mapping[str, Any], staged: StagedBatch, target: sql.Composable,
                temp: sql.Identifier, join: sql.Composable, counts: dict[str, int]) -> None:
    """새 키의 행을 넣는다. 머리글에 없는 열은 default 또는 NULL이다."""
    columns = list(table["columns"])
    missing_required = [c for c in columns if c not in staged.header and table["columns"][c].get("required")
                        and default_value(table["columns"][c]) is None]
    new_only = sql.SQL("NOT EXISTS (SELECT 1 FROM {} t WHERE {})").format(target, join)
    if missing_required:
        cur.execute(sql.SQL("SELECT b._at FROM {} b WHERE {} ORDER BY b._at LIMIT %s").format(temp, new_only),
                    (None if staged.skip_invalid else MAX_KEYS_SHOWN,))
        ats = [r[0] for r in cur.fetchall()]
        if ats:
            col = missing_required[0]
            reason = f"값이 필요함(새 행. 열 '{col}'이 머리글에 없고 default도 없음)"
            if staged.skip_invalid:
                _drop_rows(cur, table, staged, [(at, col, reason) for at in ats])
            else:
                raise BatchError("invalid_rows", "새 행에 필요한 열이 배치에 없습니다",
                                 [f"{staged.where(at, col)}: {reason}" for at in ats])
    select: list[sql.Composable] = []
    params: list[Any] = []
    for c in columns:
        if c in staged.header:
            select.append(sql.SQL("b.{}").format(sql.Identifier(c)))
        else:
            value = default_value(table["columns"][c])
            if value is None:
                select.append(sql.SQL("NULL::{}").format(sql.SQL(PG_TYPES[table["columns"][c]["type"]])))
            else:
                select.append(sql.SQL("%s::{}").format(sql.SQL(PG_TYPES[table["columns"][c]["type"]])))
                params.append(value)
    cur.execute(sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {} b WHERE {} ORDER BY b._at").format(
        target, sql.SQL(", ").join(sql.Identifier(c) for c in columns), sql.SQL(", ").join(select), temp, new_only),
        params)
    counts["inserted"] += cur.rowcount


def run_checks(cur: psycopg.Cursor, project_schema: Mapping[str, Any], order: list[str]) -> list[str]:
    """schema.yaml의 checks를 모두 실행하고 위반·오류를 모은다(로더와 같은 형식). 각 규칙은 저장점 안에서 돈다."""
    errors: list[str] = []
    tables = project_schema["tables"]
    for tname in order:
        for i, check in enumerate(tables[tname]["checks"]):
            try:
                with cur.connection.transaction():
                    cur.execute(sql.SQL(check["sql"].strip().rstrip(";").strip()))
                    rows = cur.fetchall() if cur.description else []
            except psycopg.Error as exc:
                errors.append(f"schema.yaml:tables.{tname}.checks[{i}] ({check['name']}): SQL 오류: {pg_message(exc)}")
                continue
            if rows:
                keys = [str(r[0]) for r in rows[:MAX_KEYS_SHOWN]]
                more = f" 외 {len(rows) - MAX_KEYS_SHOWN}건" if len(rows) > MAX_KEYS_SHOWN else ""
                errors.append(f"{check['name']}: 위반 키 {', '.join(keys)}{more}")
    return errors


def pg_message(exc: BaseException) -> str:
    diag = getattr(exc, "diag", None)
    msg = (diag.message_primary if diag and diag.message_primary else str(exc)).strip()
    detail = diag.message_detail if diag else None
    return f"{msg} ({detail})" if detail else msg


__all__ = ["StagedBatch", "stage_batch", "apply_batch", "drop_stage", "run_checks", "default_value", "BatchError",
           "key_label", "PG_TYPES"]
