"""CSV 폴더에서 프로젝트 초안을 만든다(보강설계서 5-1, `make new`).

세 단계로 나뉜다. Studio의 프로젝트 마법사(보강설계서 6-3)가 같은 함수를 쓴다.

1. 추론 `infer(sources)`: CSV(파일 이름 → 바이트) → 추론 결과(dict, JSON으로 직렬화 가능). 파일을 쓰지 않는다.
2. 생성 `render(name, inference, sources)`: (사람이 고쳤을 수도 있는) 추론 결과 → 파일 내용
   {"schema.yaml", "dashboard.yaml", "data/<테이블>.csv"}. 추론한 곳마다 "# 추론: 확인 필요" 주석을 단다.
3. 쓰기 `write_project(name, files)`: 임시 폴더에서 load_project와 validate_csv로 검증한 뒤 projects/<이름>/에 쓴다.
   이미 있는 프로젝트는 덮어쓰지 않는다.

추론 결과의 형식은 contracts/scaffold.md가 기준이다.
"""
from __future__ import annotations

import csv
import io
import json
import re
import shutil
import tempfile
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

import yaml

from .config import ConfigError, default_projects_dir, load_project, table_order
from .reserved_words import is_reserved

FORMAT = 1
CONFIG_VERSION = 1
MARK = "# 추론: 확인 필요"
ROW_COUNT = "row_count"

# ---- 추론 기준(바꾸면 contracts/scaffold.md와 README도 고친다)
ENUM_MAX_VALUES = 12       # 고유값이 이 개수 이하이고
ENUM_MAX_RATIO = 0.5       # 고유값 수가 값 있는 행의 절반 이하이며(= 값이 평균 두 번 이상 나옴)
ENUM_MAX_LENGTH = 40       # 가장 긴 값이 이 글자 수 이하인 text 열을 enum으로 본다
COMPOSITE_KEY_SPAN = 8     # 단일 열 키가 없을 때 앞에서부터 이 개수의 열 안에서 두 열 조합을 찾는다
MAX_BARS = 2               # 테이블마다 값별 건수 막대 수 상한(선·원은 하나씩 → 테이블마다 차트 최대 6개)
MAX_LIST_COLUMNS = 12      # 원본 행 표에 넣는 열 수 상한
KANBAN_MAX_ROWS = 500      # 이보다 행이 많은 테이블에는 칸반을 만들지 않는다
KANBAN_CHIPS = 2

# 이름의 토막(`_`로 나눈 조각)으로 역할을 짐작한다
_STATE_TOKENS = frozenset({"status", "state", "step"})
_BEGIN_TOKENS = frozenset({"start", "starts", "started", "begin", "begins", "began", "from", "opened", "launch", "launched"})
_FINISH_TOKENS = frozenset({"end", "ends", "ended", "finish", "finishes", "finished", "until", "due", "to", "close",
                            "closed", "deadline", "completed"})
_CAPTION_TOKENS = frozenset({"name", "title", "label", "subject"})
_PERCENT_TOKENS = frozenset({"progress", "pct", "percent", "percentage"})
_CODE_TOKENS = frozenset({"id", "no", "code", "key", "seq", "num", "idx"})

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_IDENT_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")
_IDENT_MAX = 63
_INT_RE = re.compile(r"[+-]?\d+")
_PADDED_INT_RE = re.compile(r"[+-]?0\d+")
_NUMBER_RE = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")
_GROUPED_NUMBER_RE = re.compile(r"[+-]?\d{1,3}(,\d{3})+(\.\d+)?")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?")
_BIGINT_MIN, _BIGINT_MAX = -(2 ** 63), 2 ** 63 - 1
_PLAIN_RE = re.compile(r"[A-Za-z가-힣_][A-Za-z0-9가-힣_.-]*")
# YAML이 줄바꿈으로 읽는 NEL(U+0085), LS(U+2028), PS(U+2029)도 이스케이프 대상이다(따옴표 안에서도 공백으로 접힌다)
_UNPRINTABLE_RE = re.compile("[^\t\n\r\x20-\x7e\xa0-\u2027\u202a-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_MAX_ERRORS_PER_FILE = 20


class ScaffoldError(Exception):
    """초안을 만들 수 없음. errors는 한 줄에 하나씩 보여 줄 이유 목록이다."""

    def __init__(self, errors: list[str], *, detail: str | None = None):
        self.errors = list(errors)
        self.detail = detail
        super().__init__("\n".join(self.errors))


# ---------------------------------------------------------------- CSV 읽기

def read_folder(folder: Path) -> dict[str, bytes]:
    """폴더 바로 아래의 *.csv를 {파일 이름: 내용}으로 읽는다(이름순)."""
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise ScaffoldError([f"CSV 폴더가 없음 ({folder})"])
    files = sorted((p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".csv"), key=lambda p: p.name)
    if not files:
        raise ScaffoldError([f"폴더에 CSV 파일(*.csv)이 없음 ({folder})"])
    return {p.name: p.read_bytes() for p in files}


def _decode(source: str, raw: bytes) -> tuple[str, bool]:
    """(본문, UTF-8이었는지). UTF-8이 아니면 CP949(엑셀의 한글 CSV)로 한 번 더 읽어 본다."""
    try:
        return raw.decode("utf-8-sig"), True
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp949"), False
    except UnicodeDecodeError:
        raise ScaffoldError([f"{source}: UTF-8도 CP949도 아니어서 읽을 수 없음. UTF-8로 저장해 다시 시도"]) from None


def _read_csv(source: str, raw: bytes) -> tuple[list[str], list[list[str]], bool]:
    """(머리글(앞뒤 공백 제거), 데이터 행, UTF-8이었는지). 읽는 방식은 dashkit/loader/validate.py와 같다:
    빈 줄은 건너뛰고, 값은 다듬지 않으며, 빈 문자열만 NULL이다."""
    text, was_utf8 = _decode(source, raw)
    reader = csv.reader(text.splitlines(keepends=True))
    errors: list[str] = []
    try:
        header = next(reader, None)
    except csv.Error as exc:
        raise ScaffoldError([f"{source}:1: CSV 문법 오류: {exc}"]) from None
    if not header or all(h.strip() == "" for h in header):
        raise ScaffoldError([f"{source}:1: 머리글이 없음(첫 줄에 열 이름이 있어야 함)"])
    header = [h.strip() for h in header]
    rows: list[list[str]] = []
    prev_end = 1
    while True:
        try:
            record = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            errors.append(f"{source}:{reader.line_num}: CSV 문법 오류: {exc}")
            break
        line = prev_end + 1
        prev_end = reader.line_num
        if not record or (len(record) == 1 and record[0].strip() == ""):
            continue
        if len(record) != len(header):
            if len(errors) < _MAX_ERRORS_PER_FILE:
                errors.append(f"{source}:{line}: 값 개수 {len(record)}개가 머리글 {len(header)}개와 다름")
            continue
        rows.append(record)
    if errors:
        raise ScaffoldError(errors)
    return header, rows, was_utf8


# ---------------------------------------------------------------- 이름 바꾸기

def to_ident(raw: str, kind: str, position: int, taken: set[str]) -> tuple[str, str | None]:
    """머리글이나 파일 이름을 스키마의 식별자(`^[a-z][a-z0-9_]{0,62}$`, 예약어 아님)로 바꾼다.

    kind는 "column" 또는 "table", position은 1부터 센 자리다. (이름, 바꾼 이유 또는 None)을 돌려주고
    이름을 taken에 더한다. 규칙: 유니코드 호환 정규화 → camelCase를 나눔 → 소문자 → 영문·숫자가 아닌
    글자는 `_` → 남는 것이 없으면 `column_<자리>`·`table_<자리>` → 숫자로 시작하면 `c_`·`t_`를 붙임 →
    예약어면 `_col`·`_tbl`을 붙임 → 63자로 자름 → 같은 이름이 있으면 `_2`, `_3`….
    """
    reasons: list[str] = []
    s = unicodedata.normalize("NFKC", raw).strip()
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s = s.lower()
    if re.search(r"[^\x00-\x7f]", s):
        reasons.append("영문·숫자가 아닌 글자를 뺌")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    if not s:
        s = f"{kind}_{position}"
        reasons.append("쓸 수 있는 글자가 없어 자리 번호로 지음")
    if s[0].isdigit():
        s = f"{kind[0]}_{s}"
        reasons.append("숫자로 시작해 접두사를 붙임")
    if is_reserved(s):
        s += "_col" if kind == "column" else "_tbl"
        reasons.append("SQL 예약어라 접미사를 붙임")
    if len(s) > _IDENT_MAX:
        s = s[:_IDENT_MAX].rstrip("_")
        reasons.append(f"{_IDENT_MAX}자로 자름")
    base, n = s, 1
    while s in taken:
        n += 1
        suffix = f"_{n}"
        s = base[:_IDENT_MAX - len(suffix)] + suffix
    if n > 1:
        reasons.append("같은 이름이 있어 번호를 붙임")
    taken.add(s)
    if s == raw:
        return s, None
    return s, ", ".join(reasons) or "소문자 snake_case로 바꿈"


def _tokens(name: str) -> list[str]:
    return [t for t in name.split("_") if t]


def _singular(word: str) -> str:
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and re.search(r"(s|x|z|ch|sh)es$", word):
        return word[:-2]
    if len(word) > 1 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


# ---------------------------------------------------------------- 유형 추론

def _is_date(value: str) -> bool:
    if not _DATE_RE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_timestamp(value: str) -> bool:
    if not _TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _is_number(value: str) -> bool:
    if not _NUMBER_RE.fullmatch(value):
        return False
    try:
        return Decimal(value).is_finite()
    except InvalidOperation:
        return False


def _infer_type(values: list[str]) -> tuple[str, str, str, list[str]]:
    """비어 있지 않은 값들 → (type, 근거, 확신, 덧붙일 말). 판정 표기는 loader/validate.py가 받는 것보다
    넓지 않다(예: 공백이 붙은 숫자, `1_000`, `inf`는 숫자로 보지 않는다)."""
    notes: list[str] = []
    if not values:
        return "text", "값이 모두 비어 있어 text로 둠", "low", notes
    sure = "high" if len(values) >= 5 else "medium"
    if all(_INT_RE.fullmatch(v) for v in values):
        if any(_PADDED_INT_RE.fullmatch(v) for v in values):
            return "text", "숫자지만 0으로 시작하는 값이 있어 코드로 보고 text로 둠", "medium", notes
        if all(_BIGINT_MIN <= int(v) <= _BIGINT_MAX for v in values):
            return "integer", "모든 값이 정수", sure, notes
        return "number", "정수지만 bigint 범위를 벗어난 값이 있어 number로 둠", "medium", notes
    if all(_is_number(v) for v in values):
        return "number", "모든 값이 숫자", sure, notes
    if all(v.lower() in ("true", "false") for v in values):
        return "boolean", "모든 값이 true/false", sure, notes
    if all(_is_date(v) for v in values):
        return "date", "모든 값이 YYYY-MM-DD", sure, notes
    if all(_is_timestamp(v) for v in values):
        return "timestamp", "모든 값이 ISO 8601 시각", "medium", notes
    if all(_GROUPED_NUMBER_RE.fullmatch(v) for v in values):
        notes.append("천 단위 쉼표가 있는 숫자로 보임. 쉼표를 지우면 integer/number로 바꿀 수 있음")
    return "text", "숫자·날짜·true/false가 아닌 값이 있음", sure, notes


def _comparable(value: str, kind: str) -> Any:
    """검증기가 파싱한 뒤 비교하는 것과 같은 결과가 나오도록 값을 바꾼다(키 중복, 참조 확인용)."""
    if kind == "integer":
        return int(value)
    if kind == "number":
        return Decimal(value)
    if kind == "timestamp":
        return datetime.fromisoformat(value)
    if kind == "boolean":
        return value.lower()
    return value


def _column_values(rows: list[list[str]], index: int) -> list[str]:
    return [r[index] for r in rows]


def _read_xlsx(source: str, raw: bytes) -> tuple[list[str], list[list[str]], bool]:
    from .ingest.intake import IntakeError, RowError
    from .ingest.xlsx import XlsxParser

    with tempfile.TemporaryDirectory(prefix="dashkit-xlsx-") as tmp:
        src = Path(tmp) / "in.xlsx"
        src.write_bytes(raw)
        parser = XlsxParser(src, source)
        try:
            header = parser.header()
            rows: list[list[str]] = []
            errors: list[str] = []
            for _at, rec in parser.rows():
                if isinstance(rec, RowError):
                    if len(errors) < _MAX_ERRORS_PER_FILE:
                        errors.append(f"{rec.where}: {rec.reason}")
                    if rec.fatal:
                        break
                    continue
                rows.append(rec)
            if errors:
                raise ScaffoldError(errors, detail="invalid_csv")
            return header, rows, True
        except IntakeError as exc:
            raise ScaffoldError(exc.errors or [exc.message], detail=exc.detail) from None
        finally:
            parser.close()


def _read_source(source: str, raw: bytes) -> tuple[list[str], list[list[str]], bool, bool]:
    """(머리글, 데이터 행, UTF-8 호환 여부, xlsx 여부)."""
    if source.lower().endswith(".xlsx"):
        header, rows, was_utf8 = _read_xlsx(source, raw)
        return header, rows, was_utf8, True
    header, rows, was_utf8 = _read_csv(source, raw)
    return header, rows, was_utf8, False


def _infer_table(source: str, raw: bytes, position: int, taken_tables: set[str]) -> tuple[dict, dict[str, list[str]]]:
    """표 하나의 열·유형·기본 키를 추론한다. (테이블 기록, {열 이름: 원본 값 목록})."""
    header, rows, was_utf8, _is_xlsx = _read_source(source, raw)
    stem = Path(source).stem
    name, renamed = to_ident(stem, "table", position, taken_tables)
    notes: list[str] = []
    if renamed:
        notes.append(f"테이블 이름을 '{stem}'에서 바꿈: {renamed}")
    if not was_utf8:
        notes.append("UTF-8이 아니어서 CP949로 읽음. 글자가 깨졌는지 확인(사본은 UTF-8로 씀)")
    if not rows:
        notes.append("데이터 행이 없어 유형과 기본 키를 추론할 수 없음")

    columns: list[dict] = []
    cells: dict[str, list[str]] = {}
    taken: set[str] = set()
    for i, head in enumerate(header):
        cname, why = to_ident(head, "column", i + 1, taken)
        values = _column_values(rows, i)
        filled = [v for v in values if v != ""]
        kind, basis, sure, extra = _infer_type(filled)
        col_notes = list(extra)
        if why:
            col_notes.append(f"열 이름을 '{head}'에서 바꿈: {why}")
        stats: dict[str, Any] = {"rows": len(values), "empty": len(values) - len(filled),
                                 "distinct": len({_comparable(v, kind) for v in filled})}
        if kind in ("integer", "number") and filled:
            numbers = [Decimal(v) for v in filled]
            stats["min"], stats["max"] = float(min(numbers)), float(max(numbers))
        columns.append({
            "name": cname, "source": head, "type": kind,
            "required": bool(filled) and len(filled) == len(values),
            "basis": basis, "confidence": sure, "notes": col_notes, "stats": stats,
        })
        cells[cname] = values

    table = {"name": name, "source": source, "label": stem, "rows": len(rows),
             "primary_key": [], "primary_key_basis": "", "primary_key_confidence": "low",
             "columns": columns, "notes": notes}
    _infer_primary_key(table, cells)
    return table, cells


def _key_candidate(col: dict) -> bool:
    s = col["stats"]
    return s["rows"] > 0 and s["empty"] == 0 and col["type"] not in ("number", "boolean")


def _infer_primary_key(table: dict, cells: dict[str, list[str]]) -> None:
    columns = table["columns"]
    for i, col in enumerate(columns):
        if _key_candidate(col) and col["stats"]["distinct"] == col["stats"]["rows"]:
            table["primary_key"] = [col["name"]]
            table["primary_key_basis"] = "값이 유일하고 비어 있지 않은 첫 열"
            table["primary_key_confidence"] = "high" if i == 0 or _tokens(col["name"])[-1] in _CODE_TOKENS else "medium"
            return
    span = [c for c in columns[:COMPOSITE_KEY_SPAN] if _key_candidate(c)]
    for i, a in enumerate(span):
        for b in span[i + 1:]:
            pairs = {(_comparable(x, a["type"]), _comparable(y, b["type"]))
                     for x, y in zip(cells[a["name"]], cells[b["name"]])}
            if len(pairs) == table["rows"]:
                table["primary_key"] = [a["name"], b["name"]]
                table["primary_key_basis"] = "유일한 열이 없어, 함께 쓰면 유일한 첫 두 열 조합을 고름"
                table["primary_key_confidence"] = "low"
                return
    table["primary_key_basis"] = "유일하고 비어 있지 않은 열(또는 두 열 조합)이 없음"


def _infer_refs(tables: list[dict], cells: dict[str, dict[str, list[str]]]) -> None:
    """`<테이블>_id` 꼴 열(단수·복수 대응)과, 다른 테이블의 단일 열 기본 키와 이름이 같은 열을 ref 후보로 본다.
    대상의 기본 키와 type이 같고, 값이 모두 대상에 있고, 참조가 순환하지 않을 때만 ref로 만든다."""
    deps: dict[str, set[str]] = {t["name"]: set() for t in tables}

    def reaches(start: str, goal: str) -> bool:
        seen, pending = set(), [start]
        while pending:
            cur = pending.pop()
            if cur == goal:
                return True
            if cur not in seen:
                seen.add(cur)
                pending.extend(deps[cur])
        return False

    for table in tables:
        for col in table["columns"]:
            cname = col["name"]
            if cname in table["primary_key"]:
                continue
            candidates: list[tuple[dict, str, str]] = []
            if cname.endswith("_id") and len(cname) > 3:
                stem = _singular(cname[:-3])
                candidates += [(t, f"이름이 '<테이블>_id' 꼴({t['name']})", "high") for t in tables
                               if t is not table and _singular(t["name"]) == stem]
            candidates += [(t, f"{t['name']}의 기본 키와 이름이 같음", "medium") for t in tables
                           if t is not table and t["primary_key"] == [cname] and all(t is not c[0] for c in candidates)]
            accepted: list[tuple[dict, str, str]] = []
            rejected: list[str] = []
            values = [v for v in cells[table["name"]][cname] if v != ""]
            for target, why, sure in candidates:
                tname = target["name"]
                if len(target["primary_key"]) != 1:
                    rejected.append(f"{tname}: 단일 열 기본 키가 없음")
                    continue
                key = target["primary_key"][0]
                key_col = next(c for c in target["columns"] if c["name"] == key)
                if key_col["type"] != col["type"]:
                    rejected.append(f"{tname}.{key}: type이 다름({col['type']} ≠ {key_col['type']})")
                    continue
                known = {_comparable(v, key_col["type"]) for v in cells[tname][key] if v != ""}
                missing = [v for v in values if _comparable(v, col["type"]) not in known]
                if missing:
                    rejected.append(f"{tname}.{key}: 대상에 없는 값이 {len(missing)}건(예: '{missing[0]}')")
                    continue
                if reaches(tname, table["name"]):
                    rejected.append(f"{tname}.{key}: 참조가 순환하게 됨")
                    continue
                accepted.append((target, why, sure))
            if len(accepted) == 1:
                target, why, sure = accepted[0]
                col["ref"] = f"{target['name']}.{target['primary_key'][0]}"
                col["ref_basis"] = why + ", 값이 모두 대상에 있음"
                col["ref_confidence"] = sure
                deps[table["name"]].add(target["name"])
            elif len(accepted) > 1:
                names = ", ".join(t["name"] for t, _why, _sure in accepted)
                col["notes"].append(f"ref 후보가 여럿이라 정하지 않음({names})")
            for reason in rejected:
                col["notes"].append(f"ref 후보였으나 만들지 않음. {reason}")


def _state_like(name: str) -> bool:
    """이름의 마지막 토막이 상태를 뜻하는 말인가(예: `<무엇>_status`)."""
    return _tokens(name)[-1] in _STATE_TOKENS


def _infer_enums(table: dict, cells: dict[str, list[str]]) -> None:
    for col in table["columns"]:
        if col["type"] != "text" or col["name"] in table["primary_key"] or "ref" in col:
            continue
        seen: dict[str, None] = {}
        for v in cells[col["name"]]:
            if v != "":
                seen.setdefault(v)
        values = list(seen)
        filled = col["stats"]["rows"] - col["stats"]["empty"]
        if not values or len(values) > ENUM_MAX_VALUES or max(len(v) for v in values) > ENUM_MAX_LENGTH:
            continue
        repeated = len(values) <= filled * ENUM_MAX_RATIO
        if not repeated and not (_state_like(col["name"]) and len(values) < filled):
            continue
        col["type"] = "enum"
        col["values"] = values
        if repeated:
            col["basis"] = f"text 열의 고유값이 {len(values)}개(값 있는 {filled}행의 절반 이하)"
            col["confidence"] = "high" if filled >= 4 * len(values) else "medium"
        else:
            col["basis"] = f"고유값 {len(values)}개(값 있는 {filled}행). 반복은 적지만 이름이 상태 꼴"
            col["confidence"] = "low"


def infer_tables(sources: Mapping[str, bytes]) -> list[dict]:
    """CSV들 → 테이블 추론 목록(이름순). 읽을 수 없는 파일이 있으면 ScaffoldError."""
    if not sources:
        raise ScaffoldError(["CSV 파일이 없음"])
    errors: list[str] = []
    tables: list[dict] = []
    cells: dict[str, dict[str, list[str]]] = {}
    taken: set[str] = set()
    for position, source in enumerate(sorted(sources), 1):
        if "/" in source or "\\" in source or source in ("", ".", ".."):
            errors.append(f"{source!r}: 파일 이름에 경로 문자를 쓸 수 없음")
            continue
        try:
            table, values = _infer_table(source, bytes(sources[source]), position, taken)
        except ScaffoldError as exc:
            errors += exc.errors
            continue
        tables.append(table)
        cells[table["name"]] = values
    if errors:
        raise ScaffoldError(errors)
    _infer_refs(tables, cells)
    for table in tables:
        _infer_enums(table, cells[table["name"]])
    return tables


# ---------------------------------------------------------------- 대시보드 초안

def _key(taken: set[str], *parts: str) -> str:
    base = "_".join(p for p in parts if p)[:_IDENT_MAX].rstrip("_")
    key, n = base, 1
    while key in taken:
        n += 1
        suffix = f"_{n}"
        key = base[:_IDENT_MAX - len(suffix)] + suffix
    taken.add(key)
    return key


def _shown(col: dict) -> str:
    return col.get("source") or col["name"]


def _pick_caption(table: dict) -> str:
    """카드·막대의 제목으로 쓸 열: 이름 꼴 text 열 → 첫 text 열 → 기본 키의 첫 열 → 첫 열."""
    plain = [c for c in table["columns"]
             if c["type"] == "text" and c["name"] not in table["primary_key"] and "ref" not in c]
    for col in plain:
        if _CAPTION_TOKENS & set(_tokens(col["name"])):
            return col["name"]
    if plain:
        return plain[0]["name"]
    return (table["primary_key"] or [table["columns"][0]["name"]])[0]


def _pick_span(dates: list[dict]) -> tuple[dict, dict] | None:
    """시작·종료 꼴 date 열 한 쌍. 표식 토막을 뺀 나머지 이름이 같은 쌍(예: `<시작>_on`·`<종료>_on`)을 먼저 고른다."""
    begins = [c for c in dates if _BEGIN_TOKENS & set(_tokens(c["name"]))]
    finishes = [c for c in dates if _FINISH_TOKENS & set(_tokens(c["name"]))]
    pairs = [(b, f) for b in begins for f in finishes if b is not f]
    for b, f in pairs:
        if [t for t in _tokens(b["name"]) if t not in _BEGIN_TOKENS] == [t for t in _tokens(f["name"]) if t not in _FINISH_TOKENS]:
            return b, f
    return pairs[0] if pairs else None


def suggest_dashboard(tables: list[dict]) -> dict:
    """테이블 추론 → dashboard.yaml 초안({datasets, dashboards, tabs}). 사람이 테이블 추론을 고친 뒤 다시 불러도 된다.

    테이블마다 데이터셋(`table:` 축약)과 `row_count` 지표, Superset 대시보드 하나, 웹앱 탭 하나를 만든다.
    차트·섹션의 `note`는 근거이고, 생성할 때 "# 추론: 확인 필요" 주석이 된다.
    """
    datasets: dict[str, dict] = {}
    dashboards: dict[str, dict] = {}
    tabs: list[dict] = []
    chart_keys: set[str] = set()

    for table in tables:
        t = table["name"]
        label = table.get("label") or t
        cols = table["columns"]
        keyed = set(table["primary_key"])
        dates = [c for c in cols if c["type"] == "date"]
        times = dates or [c for c in cols if c["type"] == "timestamp"]
        enums = [c for c in cols if c["type"] == "enum"]
        measures = [c for c in cols if c["type"] in ("integer", "number") and c["name"] not in keyed
                    and "ref" not in c and _tokens(c["name"])[-1] not in _CODE_TOKENS]

        metrics = {ROW_COUNT: "COUNT(*)"}
        labels = {c["name"]: c["source"] for c in cols if c.get("source") and c["source"] != c["name"]}
        labels[ROW_COUNT] = "건수"
        total = None
        if measures and (times or enums):
            total = _key(set(metrics) | {c["name"] for c in cols}, "sum", measures[0]["name"])
            metrics[total] = f"SUM({measures[0]['name']})"
            labels[total] = f"{_shown(measures[0])} 합계"
        datasets[t] = {"table": t, "labels": labels, "metrics": metrics}

        charts: list[dict] = []
        count = _key(chart_keys, t, "count")
        charts.append({"key": count, "title": f"{label} 건수", "viz": "big_number_total", "dataset": t,
                       "metric": ROW_COUNT, "subtitle": "건", "note": "테이블마다 만드는 건수"})
        line = None
        if total and times:
            line = _key(chart_keys, t, times[0]["name"], "weekly")
            charts.append({"key": line, "title": f"주별 {_shown(measures[0])} 합계", "viz": "line", "dataset": t,
                           "x": times[0]["name"], "time_grain": "P1W", "metrics": [total],
                           "note": f"날짜 열 {times[0]['name']} + 숫자 열 {measures[0]['name']}"})
        bars: list[str] = []
        for col in enums[:MAX_BARS]:
            bars.append(_key(chart_keys, t, "by", col["name"]))
            charts.append({"key": bars[-1], "title": f"{_shown(col)}별 건수", "viz": "bar", "dataset": t,
                           "x": col["name"], "metrics": [ROW_COUNT], "note": f"enum 열 {col['name']}"})
        pie = None
        if total and enums:
            pie = _key(chart_keys, t, measures[0]["name"], "by", enums[0]["name"])
            charts.append({"key": pie, "title": f"{_shown(enums[0])}별 {_shown(measures[0])} 합계", "viz": "pie",
                           "dataset": t, "groupby": [enums[0]["name"]], "metric": total,
                           "note": f"enum 열 {enums[0]['name']} + 숫자 열 {measures[0]['name']}"})
        listing = _key(chart_keys, t, "rows")
        ordering = [[k, "asc"] for k in table["primary_key"]]
        charts.append({"key": listing, "title": f"{label} 원본 행", "viz": "table", "dataset": t,
                       "columns": [c["name"] for c in cols[:MAX_LIST_COLUMNS]],
                       **({"order_by": ordering} if ordering else {}), "search": True,
                       "note": "테이블마다 만드는 원본 행 표" + (f"(앞의 {MAX_LIST_COLUMNS}개 열만)" if len(cols) > MAX_LIST_COLUMNS else "")})

        partner = line or (bars[0] if bars else None)
        if partner:
            layout: list[Any] = [[{"chart": count, "width": 3}, {"chart": partner, "width": 9}]]
            rest = [k for k in bars + ([pie] if pie else []) if k != partner]
            if rest:
                layout.append(rest)
            layout.append([{"chart": listing, "height": 60}])
        else:
            layout = [[{"chart": count, "width": 3}, {"chart": listing, "width": 9}]]
        dashboards[t] = {"title": label, "charts": charts, "layout": layout}

        sections: list[dict] = [{"type": "superset", "dashboard": t}]
        icon = "table"
        states = [c for c in enums if _state_like(c["name"])]
        span = _pick_span(dates)
        caption = _pick_caption(table)
        if span:
            begin, finish = span
            fields = {"label": caption, "start": begin["name"], "end": finish["name"]}
            for col in cols:
                s = col.get("stats") or {}
                if (col["type"] in ("integer", "number") and _PERCENT_TOKENS & set(_tokens(col["name"]))
                        and s.get("min", -1) >= 0 and s.get("max", 101) <= 100):
                    fields["progress"] = col["name"]
                    break
            if states:
                fields["status"] = states[0]["name"]
            sections.append({"type": "gantt", "title": f"{label} 기간", "dataset": t, "fields": fields,
                             "note": f"시작·종료 꼴 date 열 {begin['name']}, {finish['name']}"})
            icon = "calendar"
        if states and table["rows"] <= KANBAN_MAX_ROWS:
            group = states[0]
            chips = [c["name"] for c in enums if c is not group][:KANBAN_CHIPS]
            sections.append({"type": "kanban", "title": f"{_shown(group)}별 {label}", "dataset": t,
                             "group_by": group["name"],
                             "columns": [{"value": v, "label": v} for v in group["values"]],
                             "card": {"title": caption, "chips": chips},
                             "note": f"상태 꼴 enum 열 {group['name']}"})
            icon = icon if span else "list"
        tabs.append({"key": t, "label": label, "icon": icon, "sections": sections})

    return {"datasets": datasets, "dashboards": dashboards, "tabs": tabs}


def problems(tables: list[dict]) -> list[str]:
    """이대로는 프로젝트를 만들 수 없는 이유(없으면 빈 목록). 사람이 추론 결과를 고치면 사라진다."""
    out = []
    for table in tables:
        if not table.get("primary_key"):
            why = "데이터 행이 없음" if not table.get("rows") else table.get("primary_key_basis") or "기본 키가 없음"
            out.append(f"{table.get('source') or table['name']}: 기본 키를 정할 수 없음({why}). "
                       "행마다 다른 값이 들어 있는 열(예: id)을 CSV에 넣거나 이 파일을 폴더에서 빼고 다시 시도")
    return out


def infer(sources: Mapping[str, bytes]) -> dict:
    """CSV들(파일 이름 → 내용) → 추론 결과. 파일을 쓰지 않는다. 형식은 contracts/scaffold.md."""
    tables = infer_tables(sources)
    return {"format": FORMAT, "tables": tables, "dashboard": suggest_dashboard(tables), "problems": problems(tables)}


def infer_folder(folder: Path) -> dict:
    return infer(read_folder(folder))


# ---------------------------------------------------------------- YAML 조립

def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if _PLAIN_RE.fullmatch(text):
        try:
            if yaml.safe_load(text) == text:
                return text
        except yaml.YAMLError:
            pass
    quoted = json.dumps(text, ensure_ascii=False)
    return _UNPRINTABLE_RE.sub(lambda m: f"\\u{ord(m.group()):04x}", quoted)


def _flow(value: Any) -> str:
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "{ " + ", ".join(f"{_scalar(k)}: {_flow(v)}" for k, v in value.items()) + " }"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_flow(v) for v in value) + "]"
    return _scalar(value)


def _remark(text: str) -> str:
    """주석에 넣을 한 줄 글."""
    return _UNPRINTABLE_RE.sub("?", re.sub(r"\s+", " ", str(text))).strip()


def _marked(basis: str | None) -> str:
    return f"{MARK} - {_remark(basis)}" if basis else MARK


def _schema_yaml(name: str, tables: list[dict]) -> str:
    out = [
        f"# {name}: CSV에서 추론해 만든 초안. \"{MARK}\"가 붙은 곳을 확인하고, 맞으면 주석을 지운다.",
        "# 테이블 키 = data/<키>.csv = raw.<키>. 열 규칙: type, required, values, ref, min, max, pattern.",
        "# 열 규칙으로 표현할 수 없는 규칙은 테이블의 checks(위반 행을 돌려주는 SQL)에 둔다.",
        f"version: {CONFIG_VERSION}",
        "tables:",
    ]
    for table in tables:
        keys = list(table["primary_key"])
        out.append("")
        out.append(f"  {_scalar(table['name'])}:    # 원본: {_remark(table.get('source') or '')}, {table.get('rows', 0)}행")
        for note in table.get("notes") or []:
            out.append(f"    {_marked(note)}")
        key_text = _scalar(keys[0]) if len(keys) == 1 else _flow(keys)
        out.append(f"    primary_key: {key_text}    {_marked(table.get('primary_key_basis'))}")
        out.append("    columns:")
        # 키도 값처럼 인코딩한다: off, yes 같은 이름은 따옴표가 없으면 YAML이 불리언으로 읽는다
        width = max(len(_scalar(c["name"])) for c in table["columns"]) + 1
        for col in table["columns"]:
            spec: dict[str, Any] = {"type": col["type"]}
            if col.get("required") and col["name"] not in keys:
                spec["required"] = True
            if col["type"] == "enum":
                spec["values"] = list(col["values"])
            if col.get("ref"):
                spec["ref"] = col["ref"]
            for extra in ("min", "max", "pattern", "description"):
                if col.get(extra) is not None:
                    spec[extra] = col[extra]
            basis = col.get("basis") or ""
            if col.get("ref"):
                basis += f" / ref: {col.get('ref_basis') or '사람이 지정'}"
            for note in col.get("notes") or []:
                out.append(f"      {_marked(note)}")
            out.append(f"      {(_scalar(col['name']) + ':').ljust(width)} {_flow(spec)}    {_marked(basis)}")
    return "\n".join(out) + "\n"


def _without_note(item: dict) -> dict:
    return {k: v for k, v in item.items() if k != "note"}


def _dashboard_yaml(name: str, dash: dict) -> str:
    app = dict(dash.get("app") or {})
    app.setdefault("title", name)
    app.setdefault("logo_text", re.sub(r"[^A-Za-z0-9]", "", name)[:2].upper() or name[:1].upper())
    app.setdefault("as_of", "today")
    out = [
        f"# {name}: CSV에서 추론해 만든 초안. \"{MARK}\"가 붙은 곳을 확인하고 고친다.",
        "# 차트 문법은 contracts/chart-shorthand.md, 위젯 옵션은 contracts/widgets.md를 본다.",
        f"version: {CONFIG_VERSION}",
        "app:",
    ]
    for k, v in app.items():
        remark = "    # 기준일. 예시 데이터처럼 날짜가 고정된 자료면 \"YYYY-MM-DD\"로 바꾼다" if k == "as_of" else ""
        out.append(f"  {k}: {_flow(v)}{remark}")

    out += ["", "datasets:    # 키마다 dash.<키> 뷰가 된다. 조인이나 계산 열이 필요하면 table 대신 sql을 쓴다"]
    for key, ds in dash["datasets"].items():
        out.append(f"  {_scalar(key)}:")
        for field in ("table", "sql", "description"):
            if ds.get(field) is not None:
                out.append(f"    {field}: {_scalar(ds[field])}")
        if ds.get("labels"):
            out.append(f"    labels: {_flow(ds['labels'])}")
        if ds.get("metrics"):
            out.append("    metrics:")
            for mkey, expr in ds["metrics"].items():
                out.append(f"      {_scalar(mkey)}: {_scalar(expr)}")

    out += ["", "dashboards:    # Superset 대시보드. layout은 12칸 격자의 행 목록"]
    for key, board in dash["dashboards"].items():
        out += [f"  {_scalar(key)}:", f"    title: {_scalar(board['title'])}"]
        if board.get("cross_filters"):
            out.append("    cross_filters: true")
        out.append("    charts:")
        for chart in board["charts"]:
            out.append(f"      {_marked(chart.get('note'))}")
            out.append(f"      - {_flow(_without_note(chart))}")
        out.append("    layout:")
        for row in board["layout"]:
            out.append(f"      - {_flow(row)}")
        if board.get("filters"):
            out.append("    filters:")
            for flt in board["filters"]:
                out.append(f"      - {_flow(flt)}")
        out.append("")

    out.append("tabs:    # 웹앱 탭. 섹션은 superset(대시보드 임베드) 또는 위젯")
    for tab in dash["tabs"]:
        out += [f"  - key: {_scalar(tab['key'])}", f"    label: {_scalar(tab['label'])}",
                f"    icon: {_scalar(tab['icon'])}", "    sections:"]
        for sec in tab["sections"]:
            if sec.get("type") != "superset":
                out.append(f"      {_marked(sec.get('note'))}")
            out.append(f"      - {_flow(_without_note(sec))}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- 생성·검증·쓰기

def _csv_copy(table: dict, sources: Mapping[str, bytes]) -> bytes:
    source = table.get("source")
    if source not in sources:
        raise ScaffoldError([f"{table['name']}: 원본 CSV '{source}'가 없음"])
    raw = bytes(sources[source])
    header, rows, was_utf8, is_xlsx = _read_source(source, raw)
    names = [c["name"] for c in table["columns"]]
    if len(names) != len(header):
        raise ScaffoldError([f"{source}: 열 수가 추론 결과와 다름(원본 {len(header)}개, 추론 결과 {len(names)}개). "
                             "열을 빼려면 원본에서도 빼야 함"])
    for col, head in zip(table["columns"], header):
        if col.get("source", head) != head:
            raise ScaffoldError([f"{source}: 열 순서가 추론 결과와 다름('{head}' 자리에 '{col.get('source')}')"])
    if not is_xlsx and was_utf8 and names == header:
        return raw
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(names)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def check_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name or ""):
        raise ScaffoldError([f"프로젝트 이름 형식 오류 '{name}' (소문자·숫자로 시작, 소문자·숫자·_·-만)"])


def render(name: str, inference: Mapping[str, Any], sources: Mapping[str, bytes]) -> dict[str, bytes]:
    """추론 결과 → {"schema.yaml", "dashboard.yaml", "data/<테이블>.csv"}의 내용. 파일을 쓰지 않는다.

    열 이름이 바뀐 테이블과 UTF-8이 아니던 파일은 머리글을 고친 UTF-8 사본을, 나머지는 원본 바이트를 그대로 담는다.
    """
    check_name(name)
    try:
        if inference.get("format") != FORMAT:
            raise ScaffoldError([f"추론 결과의 format이 {FORMAT}이 아님({inference.get('format')!r})"])
        tables = list(inference["tables"])
        if not tables:
            raise ScaffoldError(["테이블이 없음"])
        blocking = problems(tables)
        seen: set[str] = set()
        for table in tables:
            idents = [table["name"]] + [c["name"] for c in table["columns"]]
            for ident in idents:
                if not isinstance(ident, str) or not _IDENT_RE.fullmatch(ident):
                    blocking.append(f"{table.get('source')}: 이름 형식 오류 {ident!r} (소문자로 시작, 소문자·숫자·_, 63자 이하)")
            if table["name"] in seen:
                blocking.append(f"{table.get('source')}: 테이블 이름 '{table['name']}'이 중복됨")
            seen.add(table["name"])
            if not table["columns"]:
                blocking.append(f"{table.get('source')}: 열이 없음")
        if blocking:
            raise ScaffoldError(blocking)
        files: dict[str, bytes] = {
            "schema.yaml": _schema_yaml(name, tables).encode("utf-8"),
            "dashboard.yaml": _dashboard_yaml(name, inference["dashboard"]).encode("utf-8"),
        }
        for table in tables:
            files[f"data/{table['name']}.csv"] = _csv_copy(table, sources)
    except (KeyError, TypeError, AttributeError, IndexError, ValueError) as exc:
        raise ScaffoldError([f"추론 결과의 형식이 잘못됨({type(exc).__name__}: {exc}). contracts/scaffold.md 참고"]) from exc
    return files


def _write_files(root: Path, files: Mapping[str, bytes]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def verify(name: str, files: Mapping[str, bytes]) -> None:
    """만든 파일이 `make check`(config.load_project)와 CSV 검증(validate_csv)을 통과하는지 임시 폴더에서 확인한다.
    통과하지 못하면 원인을 담아 ScaffoldError."""
    from .loader.validate import validate_csv

    check_name(name)
    with tempfile.TemporaryDirectory(prefix="dashkit-new-") as tmp:
        _write_files(Path(tmp) / name, files)
        try:
            project = load_project(name, Path(tmp))
        except ConfigError as exc:
            raise ScaffoldError(["만든 초안이 설정 검사를 통과하지 못함:"] + exc.errors) from exc
        _data, errors = validate_csv(project.schema, project.data_dir, table_order(project))
        if errors:
            raise ScaffoldError(["만든 초안의 CSV가 검증을 통과하지 못함:"] + errors)


def write_project(name: str, files: Mapping[str, bytes], projects_dir: Path | None = None) -> Path:
    """검증한 뒤 projects/<이름>/에 쓴다. 이미 있으면 덮어쓰지 않고 ScaffoldError. 실패하면 아무것도 남기지 않는다."""
    check_name(name)
    root = (Path(projects_dir) if projects_dir else default_projects_dir()) / name
    if root.exists():
        raise ScaffoldError([f"프로젝트 '{name}'이 이미 있음({root}). 덮어쓰지 않음. 다른 이름을 쓰거나 폴더를 지우고 다시 시도"])
    verify(name, files)
    try:
        root.mkdir(parents=True)
    except FileExistsError:
        raise ScaffoldError([f"프로젝트 '{name}'이 이미 있음({root}). 덮어쓰지 않음"]) from None
    try:
        _write_files(root, files)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root


def create_project(name: str, sources: Mapping[str, bytes], projects_dir: Path | None = None) -> tuple[Path, dict]:
    """추론 → 생성 → 검증 → 쓰기를 한 번에(`python -m dashkit new`). (프로젝트 폴더, 추론 결과)."""
    check_name(name)
    root = (Path(projects_dir) if projects_dir else default_projects_dir()) / name
    if root.exists():
        raise ScaffoldError([f"프로젝트 '{name}'이 이미 있음({root}). 덮어쓰지 않음. 다른 이름을 쓰거나 폴더를 지우고 다시 시도"])
    inference = infer(sources)
    if inference["problems"]:
        raise ScaffoldError(inference["problems"])
    return write_project(name, render(name, inference, sources), projects_dir), inference


def overview(inference: Mapping[str, Any]) -> dict[str, int]:
    """추론 결과에서 센 개수(보고용)."""
    dash = inference["dashboard"]
    sections = [s for tab in dash["tabs"] for s in tab["sections"]]
    return {
        "tables": len(inference["tables"]),
        "columns": sum(len(t["columns"]) for t in inference["tables"]),
        "renamed": sum(1 for t in inference["tables"] for c in t["columns"] if c.get("source") != c["name"]),
        "enums": sum(1 for t in inference["tables"] for c in t["columns"] if c["type"] == "enum"),
        "refs": sum(1 for t in inference["tables"] for c in t["columns"] if c.get("ref")),
        "charts": sum(len(d["charts"]) for d in dash["dashboards"].values()),
        "tabs": len(dash["tabs"]),
        "widgets": sum(1 for s in sections if s.get("type") != "superset"),
    }
