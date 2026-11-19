"""1단계: DB에 접속하기 전에 CSV를 schema.yaml 규칙으로 검증하고 값을 파이썬 타입으로 바꾼다.

오류 형식: "data/<테이블>.csv:<줄 번호>:<열>: <이유>". 줄 번호는 파일의 줄(머리글 = 1행)이고,
여러 줄에 걸친 따옴표 값이면 그 레코드가 시작한 줄이다. 오류는 모두 모은다.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_INT_RE = re.compile(r"[+-]?\d+")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_BIGINT_MIN, _BIGINT_MAX = -(2 ** 63), 2 ** 63 - 1
_MAX_REPORTED_PER_FILE = 200


@dataclass
class TableData:
    name: str
    columns: list[str]                     # schema.yaml 순서
    rows: list[list[Any]] = field(default_factory=list)   # columns 순서의 파싱된 값(NULL = None)
    lines: list[int] = field(default_factory=list)        # 각 행의 시작 줄 번호
    header_ok: bool = True


class _Ctx:
    def __init__(self, label: str, errors: list[str]):
        self.label = label
        self.errors = errors
        self.count = 0

    def add(self, line: int | str, col: str, reason: str) -> None:
        self.count += 1
        if self.count <= _MAX_REPORTED_PER_FILE:
            self.errors.append(f"{self.label}:{line}:{col}: {reason}")
        elif self.count == _MAX_REPORTED_PER_FILE + 1:
            self.errors.append(f"{self.label}:{line}:-: 오류가 너무 많아 이 파일의 나머지는 생략함")


def _parse(value: str, spec: dict) -> Any:
    """값을 열 type으로 바꾼다. 실패하면 ValueError(이유)."""
    t = spec["type"]
    if t in ("text", "enum"):
        return value
    if t == "integer":
        if not _INT_RE.fullmatch(value):
            raise ValueError(f"정수가 아님 '{value}'")
        n = int(value)
        if not _BIGINT_MIN <= n <= _BIGINT_MAX:
            raise ValueError(f"정수 범위를 벗어남 '{value}'")
        return n
    if t == "number":
        try:
            d = Decimal(value)
        except InvalidOperation:
            raise ValueError(f"숫자가 아님 '{value}'") from None
        if not d.is_finite():
            raise ValueError(f"유한한 숫자가 아님 '{value}'")
        return d
    if t == "boolean":
        low = value.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        raise ValueError(f"true/false가 아님 '{value}'")
    if t == "date":
        if not _DATE_RE.fullmatch(value):
            raise ValueError(f"날짜 형식(YYYY-MM-DD)이 아님 '{value}'")
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"없는 날짜 '{value}'") from None
    if t == "timestamp":
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            raise ValueError(f"ISO 8601 시각이 아님 '{value}'") from None
    raise ValueError(f"알 수 없는 type '{t}'")


def _bound(spec: dict, key: str) -> Any:
    v = spec[key]
    if spec["type"] == "date":
        return date.fromisoformat(v)
    if spec["type"] == "number":
        return Decimal(str(v))
    return v


def _check_value(v: Any, raw: str, spec: dict) -> list[str]:
    reasons = []
    if spec["type"] == "enum" and v not in spec["values"]:
        reasons.append(f"허용되지 않은 값 '{raw}' (허용: {', '.join(spec['values'])})")
    if "pattern" in spec and not re.fullmatch(spec["pattern"], v):
        reasons.append(f"형식 불일치 '{raw}' (pattern {spec['pattern']})")
    if "min" in spec and v < _bound(spec, "min"):
        reasons.append(f"최솟값 {spec['min']}보다 작음 '{raw}'")
    if "max" in spec and v > _bound(spec, "max"):
        reasons.append(f"최댓값 {spec['max']}보다 큼 '{raw}'")
    return reasons


def _read_table(data_dir: Path, tname: str, table: dict, errors: list[str]) -> TableData:
    label = f"data/{tname}.csv"
    ctx = _Ctx(label, errors)
    columns = list(table["columns"])
    td = TableData(tname, columns)
    path = data_dir / f"{tname}.csv"
    if not path.is_file():
        ctx.add(1, "-", "파일이 없음")
        td.header_ok = False
        return td
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        ctx.add(1, "-", f"UTF-8이 아님 ({exc.reason}, 바이트 위치 {exc.start})")
        td.header_ok = False
        return td

    reader = csv.reader(text.splitlines(keepends=True))
    try:
        header = next(reader, None)
    except csv.Error as exc:
        ctx.add(1, "-", f"CSV 문법 오류: {exc}")
        td.header_ok = False
        return td
    if not header or all(h.strip() == "" for h in header):
        ctx.add(1, "-", "머리글이 없음")
        td.header_ok = False
        return td

    header = [h.strip() for h in header]
    seen: set[str] = set()
    for h in header:
        if h in seen:
            ctx.add(1, h, "머리글에 같은 열이 두 번 있음")
            td.header_ok = False
        seen.add(h)
        if h not in table["columns"]:
            ctx.add(1, h or "(빈 이름)", "schema.yaml에 없는 열")
            td.header_ok = False
    for c in columns:
        if c not in seen:
            ctx.add(1, c, "머리글에 열이 없음")
            td.header_ok = False
    if not td.header_ok:
        return td

    pos = {h: i for i, h in enumerate(header)}
    pk = table["primary_key"]
    pk_seen: dict[tuple, int] = {}
    prev_end = 1
    while True:
        try:
            record = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            ctx.add(reader.line_num, "-", f"CSV 문법 오류: {exc}")
            break
        line = prev_end + 1
        prev_end = reader.line_num
        if not record or (len(record) == 1 and record[0].strip() == ""):
            continue  # 빈 줄
        if len(record) != len(header):
            ctx.add(line, "-", f"값 개수 {len(record)}개가 머리글 {len(header)}개와 다름")
            continue
        row: list[Any] = []
        for c in columns:
            spec = table["columns"][c]
            raw = record[pos[c]]
            if raw == "":
                if spec.get("required"):
                    ctx.add(line, c, "값이 필요함")
                row.append(None)
                continue
            try:
                v = _parse(raw, spec)
            except ValueError as exc:
                ctx.add(line, c, str(exc))
                row.append(None)
                continue
            for reason in _check_value(v, raw, spec):
                ctx.add(line, c, reason)
            row.append(v)
        key = tuple(row[columns.index(k)] for k in pk)
        if None not in key:
            if key in pk_seen:
                shown = ", ".join(str(k) for k in key)
                ctx.add(line, ",".join(pk), f"기본 키 중복 ({shown}, 처음 나온 줄 {pk_seen[key]})")
            else:
                pk_seen[key] = line
        td.rows.append(row)
        td.lines.append(line)
    return td


def validate_csv(schema: dict, data_dir: Path, order: list[str]) -> tuple[dict[str, TableData], list[str]]:
    """schema.yaml의 모든 테이블 CSV를 읽고 검증한다. (테이블 → TableData, 오류 목록)."""
    errors: list[str] = []
    tables = schema["tables"]
    data: dict[str, TableData] = {}
    per_table_errors: dict[str, list[str]] = {}
    for tname in order:
        errs: list[str] = []
        data[tname] = _read_table(data_dir, tname, tables[tname], errs)
        per_table_errors[tname] = errs

    # 참조: 참조 대상 테이블의 CSV 값 기준. 대상 머리글이 깨졌으면 건너뛴다(그 오류는 이미 보고됨).
    for tname in order:
        td = data[tname]
        if not td.header_ok:
            continue
        ctx = _Ctx(f"data/{tname}.csv", per_table_errors[tname])
        ctx.count = len(per_table_errors[tname])
        for c, spec in tables[tname]["columns"].items():
            if "ref" not in spec:
                continue
            rt, rc = spec["ref"].split(".", 1)
            target = data.get(rt)
            if target is None or not target.header_ok:
                continue
            ri = target.columns.index(rc)
            existing = {r[ri] for r in target.rows if r[ri] is not None}
            ci = td.columns.index(c)
            for row, line in zip(td.rows, td.lines):
                v = row[ci]
                if v is not None and v not in existing:
                    ctx.add(line, c, f"참조 대상 {rt}.{rc}에 값 '{v}'가 없음")

    for tname in order:
        errors += per_table_errors[tname]
    return data, errors
