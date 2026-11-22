"""접수: 받을 때는 너그럽게, 저장은 정규형으로(설계서 5-3, contracts/ingest.md "접수").

입력이 무엇이든 같은 길을 지난다.

    파서(PARSERS) → 머리글 맞추기 → 값 다듬기(normalize.NORMALIZERS) → 한 줄씩 검증 → (정규형 쓰기, 임시 테이블 채우기)

- 파서는 `header() -> list[str]`과 `rows() -> Iterator[(위치, 값들)]`만 내놓는다. 위치는 정수(CSV는 레코드가 시작한
  줄 번호, JSON 행은 0부터의 번호)이고, `where(위치, 열)`이 사용자가 올린 것 기준의 오류 위치 글을 만든다.
- `strict=True`면 아무것도 바꾸지 않고 정규형만 받는다(UTF-8, 쉼표, 열 이름 그대로, 값 다듬기 없음).
  로더가 배치 파일을 읽을 때 쓰는 길이다.
- 검증 규칙과 오류 형식은 지금의 CSV 검증(dashkit/loader/validate.py)과 같다. 값을 파이썬 타입으로 바꾸는 함수도
  그 모듈의 것을 그대로 쓴다.
"""
from __future__ import annotations

import codecs
import csv
import difflib
import gzip
import io
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Callable, Iterator, Mapping, Sequence

from ..loader.validate import _check_value, _parse
from . import batches, normalize

MAX_REPORTED = 200        # 파일(요청) 하나에서 글로 보여 주는 오류 수(로더와 같다)
# 칸 하나의 최대 글자 수. 파이썬 csv의 기본(131072자)보다 큰 값을 JSON 행으로 받아 저장하면 다시 읽을 수 없게 되므로,
# 읽기 한도를 크게 두고(아래) 접수할 때 이 길이를 넘는 값을 오류로 막는다. 저장한 배치는 언제나 다시 읽힌다.
MAX_CELL_CHARS = 10 * 1024 * 1024
csv.field_size_limit(max(csv.field_size_limit(), MAX_CELL_CHARS + 1))
_DELIMITERS = (",", "\t", ";")


class IntakeError(Exception):
    """접수를 끝낼 수 없음(파일을 읽을 수 없음, 머리글이 맞지 않음, 행 형식 오류).

    detail은 API 오류 코드(invalid_csv, invalid_rows, unknown_columns), errors는 위치가 붙은 오류 줄들이다.
    """

    def __init__(self, detail: str, message: str, errors: list[str] | None = None, **extra: Any):
        self.detail = detail
        self.message = message
        self.errors = list(errors or [])
        self.extra = extra
        super().__init__(message if not self.errors else f"{message}: " + "; ".join(self.errors[:5]))


# ---------------------------------------------------------------- 파서

class Parser:
    """접수 방식 하나. encoding·delimiter는 미리보기의 normalized에 싣는다(해당 없으면 None)."""
    kind = ""
    name = ""
    encoding: str | None = None
    delimiter: str | None = None

    def header(self) -> list[str]:
        raise NotImplementedError

    def rows(self) -> Iterator[tuple[int, "list[str] | RowError"]]:
        """(위치, 값들). 그 행을 읽을 수 없으면 값들 대신 RowError(fatal이면 그 뒤를 읽지 않는다)."""
        raise NotImplementedError

    def where(self, at: int, column: str) -> str:
        raise NotImplementedError

    def progress(self) -> tuple[int, int | None]:
        """(읽은 바이트, 전체 바이트). 모르면 (0, None)."""
        return 0, None

    def close(self) -> None:
        pass


def _utf8_ok(path: Path) -> bool:
    """파일 전체가 UTF-8로 읽히는지(조각씩 확인한다. 메모리에 전부 올리지 않는다)."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with batches.open_binary(path) as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                decoder.decode(chunk)
            decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    except (gzip.BadGzipFile, EOFError, zlib.error, OSError) as exc:
        raise IntakeError("invalid_csv", "CSV 파일을 읽을 수 없습니다",
                          [f"{path.name}:1:-: 압축 해제 또는 파일 읽기 오류 ({exc})"]) from None
    return True


def _sniff_delimiter(first_line: str) -> str:
    """머리글 줄에서 쉼표·탭·세미콜론 가운데 가장 많이 나오는 것(따옴표 안은 세지 않는다). 없으면 쉼표."""
    counts = {d: 0 for d in _DELIMITERS}
    quoted = False
    for ch in first_line:
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch in counts:
            counts[ch] += 1
    best = max(_DELIMITERS, key=lambda d: counts[d])
    return best if counts[best] else ","


class CsvParser(Parser):
    """CSV 파일(디스크의 파일. .gz면 풀면서 읽는다). 인코딩은 UTF-8(BOM 허용), 안 되면 CP949.

    hash_input이면 압축을 푼 원래 바이트의 SHA-256을 구한다(로더가 배치 파일의 내용 해시를 함께 구할 때).
    """
    kind = "csv"

    def __init__(self, path: Path, name: str, *, strict: bool = False, hash_input: bool = False):
        self.path = path
        self.name = name
        self.strict = strict
        if strict:
            self.encoding = "utf-8"
        else:
            self.encoding = "utf-8" if _utf8_ok(path) else "cp949"
        try:
            raw = batches.open_binary(path)
        except (gzip.BadGzipFile, EOFError, zlib.error, OSError) as exc:
            raise IntakeError("invalid_csv", "CSV 파일을 열 수 없습니다",
                              [f"{name}:1:-: 압축 해제 또는 파일 열기 오류 ({exc})"]) from None
        self._raw_file = getattr(raw, "fileobj", None) or raw     # gzip이면 바깥 파일
        try:
            self._size: int | None = path.stat().st_size
        except OSError:
            self._size = None
        self._hasher = batches.HashingReader(raw) if hash_input else None
        binary: IO[bytes] = io.BufferedReader(self._hasher) if self._hasher else raw   # type: ignore[arg-type]
        enc = "utf-8-sig" if self.encoding == "utf-8" else "cp949"
        self._text = io.TextIOWrapper(binary, encoding=enc, newline="")
        self._header: list[str] | None = None
        self._reader: Any = None
        self._prev_end = 1

    def _start(self) -> None:
        if self._reader is not None:
            return
        try:
            first = self._text.readline()
        except UnicodeDecodeError as exc:
            self.close()
            raise IntakeError("invalid_csv", "CSV를 읽을 수 없습니다",
                              [f"{self.name}:1:-: UTF-8이 아님 ({exc.reason}, 바이트 위치 {exc.start})"]) from None
        except (gzip.BadGzipFile, EOFError, zlib.error, OSError) as exc:
            self.close()
            raise IntakeError("invalid_csv", "CSV 파일을 읽을 수 없습니다",
                              [f"{self.name}:1:-: 압축 해제 또는 파일 읽기 오류 ({exc})"]) from None
        self.delimiter = "," if self.strict else _sniff_delimiter(first)
        rest = self._text
        self._reader = csv.reader(_chain(first, rest), delimiter=self.delimiter)
        try:
            header = next(self._reader, None)
        except csv.Error as exc:
            self.close()
            raise IntakeError("invalid_csv", "CSV를 읽을 수 없습니다", [f"{self.name}:1:-: CSV 문법 오류: {exc}"]) from None
        except (gzip.BadGzipFile, EOFError, zlib.error, OSError) as exc:
            self.close()
            raise IntakeError("invalid_csv", "CSV 파일을 읽을 수 없습니다",
                              [f"{self.name}:1:-: 압축 해제 또는 파일 읽기 오류 ({exc})"]) from None
        if not header or all(h.strip() == "" for h in header):
            raise IntakeError("invalid_csv", "CSV를 읽을 수 없습니다", [f"{self.name}:1:-: 머리글이 없음"])
        self._header = [h.strip() for h in header]
        self._prev_end = self._reader.line_num

    def header(self) -> list[str]:
        self._start()
        assert self._header is not None
        return list(self._header)

    def rows(self) -> Iterator[tuple[int, "list[str] | RowError"]]:
        self._start()
        width = len(self._header or [])
        reader = self._reader
        while True:
            try:
                record = next(reader)
            except StopIteration:
                return
            except csv.Error as exc:
                yield reader.line_num, RowError(self.where(reader.line_num, "-"), f"CSV 문법 오류: {exc}", fatal=True)
                return
            except UnicodeDecodeError as exc:
                at = self._prev_end + 1
                yield at, RowError(self.where(at, "-"), f"UTF-8이 아님 ({exc.reason}, 바이트 위치 {exc.start})",
                                   fatal=True)
                return
            except (gzip.BadGzipFile, EOFError, zlib.error, OSError) as exc:
                at = self._prev_end + 1
                yield at, RowError(self.where(at, "-"), f"압축 해제 또는 파일 읽기 오류: {exc}", fatal=True)
                return
            line = self._prev_end + 1
            self._prev_end = reader.line_num
            if not record or (len(record) == 1 and record[0].strip() == ""):
                continue      # 빈 줄
            if len(record) != width:
                yield line, RowError(self.where(line, "-"), f"값 개수 {len(record)}개가 머리글 {width}개와 다름",
                                     raw=record)
                continue
            yield line, record

    def where(self, at: int, column: str) -> str:
        return f"{self.name}:{at}:{column}"

    def progress(self) -> tuple[int, int | None]:
        """(읽은 바이트, 전체 바이트). 압축 파일은 압축된 크기 기준이다."""
        try:
            return self._raw_file.tell(), self._size
        except (OSError, ValueError, AttributeError):
            return 0, self._size

    @property
    def input_hash(self) -> str | None:
        return self._hasher.digest.hexdigest() if self._hasher else None

    def close(self) -> None:
        try:
            self._text.close()
        except Exception:
            pass


def _chain(first: str, rest: IO[str]) -> Iterator[str]:
    yield first
    yield from rest


def _cell(value: Any) -> str | None:
    """JSON 값 → 문자열. 빈 값은 ""(NULL). 객체·배열은 None(형식 오류)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, str):
        return value
    return None


class RowsParser(Parser):
    """JSON 행(직접 입력, 붙여넣기). 모든 행이 같은 열 집합이어야 한다(배치 하나의 머리글은 하나다)."""
    kind = "rows"

    def __init__(self, rows: Any, name: str = "rows"):
        self.name = name
        if not isinstance(rows, list) or not rows:
            raise IntakeError("invalid_rows", "rows는 비어 있지 않은 객체 목록이어야 합니다")
        if not all(isinstance(r, dict) for r in rows):
            raise IntakeError("invalid_rows", "rows의 항목은 객체여야 합니다")
        keys = list(rows[0].keys())
        bad = [f"rows[{i}]: 열 집합이 rows[0]과 다름" for i, r in enumerate(rows) if set(r.keys()) != set(keys)]
        if bad:
            raise IntakeError("invalid_rows", "한 번에 보내는 행들은 같은 열을 가져야 합니다", bad[:MAX_REPORTED])
        if not keys:
            raise IntakeError("invalid_rows", "열이 없는 행입니다")
        self._keys = [str(k) for k in keys]
        self._rows = rows

    def header(self) -> list[str]:
        return [k.strip() for k in self._keys]

    def rows(self) -> Iterator[tuple[int, "list[str] | RowError"]]:
        for i, row in enumerate(self._rows):
            values: list[str] = []
            bad: RowError | None = None
            for k in self._keys:
                cell = _cell(row[k])
                if cell is None:
                    bad = RowError(self.where(i, k), "값은 문자열·숫자·참거짓·null만 받음",
                                   raw=[_cell(row[x]) or "" for x in self._keys])
                    break
                values.append(cell)
            yield i, (bad if bad is not None else values)

    def where(self, at: int, column: str) -> str:
        return f"rows[{at}].{column}" if column != "-" else f"rows[{at}]"


PARSERS: dict[str, Callable[..., Parser]] = {
    "csv": CsvParser,
    "rows": RowsParser,
}


_SENSITIVE_COL_PATTERNS = (
    re.compile(r"^(?P<prefix>rows\[\d+\]\.(?P<col>[^:\s]+): )(?P<reason>.+)$", re.DOTALL),
    re.compile(r"^(?P<prefix>.+?:(?P<col>[^:\s]+): )(?P<reason>.+)$", re.DOTALL),
)
_QUOTED_REASON_RE = re.compile(
    r"^(?P<head>[^']*)'(?P<val>.*)'(?P<tail>\s+\((?:pattern [^']*|허용: [^']*)\)|가 없음)?$",
    re.DOTALL,
)


def _mask_quoted_reason(reason: str) -> str:
    m = _QUOTED_REASON_RE.match(reason)
    if not m:
        return reason
    return f"{m.group('head')}'***'{m.group('tail') or ''}"


def redact_sensitive_errors(table: Mapping[str, Any], errors: Sequence[str]) -> list[str]:
    """schema.yaml에서 sensitive: true가 설정된 열의 오류 메시지에서 원문 값을 '***'로 마스킹한다."""
    cols = table.get("columns") or {}
    sensitive = {c for c, spec in cols.items() if isinstance(spec, Mapping) and spec.get("sensitive")}
    if not sensitive:
        return list(errors)
    pk_sensitive = any(k in sensitive for k in (table.get("primary_key") or []))
    out: list[str] = []
    for err in errors:
        text = str(err)
        if "원래 칸:" in text:
            head, _, _tail = text.partition("원래 칸:")
            text = f"{head}원래 칸: ***"
        if pk_sensitive and "기본 키 중복 (" in text:
            text = re.sub(r"기본 키 중복 \(.*?(, 처음 나온 )", r"기본 키 중복 (***\1", text, flags=re.DOTALL)
        if pk_sensitive and ": 이미 있는 키 " in text:
            head, _, _tail = text.partition(": 이미 있는 키 ")
            text = f"{head}: 이미 있는 키 ***"
        for pat in _SENSITIVE_COL_PATTERNS:
            m = pat.match(text)
            if m and any(c in sensitive for c in m.group("col").split(",")):
                text = m.group("prefix") + _mask_quoted_reason(m.group("reason"))
                break
        out.append(text)
    return out


class RowError:
    """행 하나의 형식 오류(파서가 값 대신 내놓는다). fatal이면 그 뒤를 읽을 수 없다(CSV 문법 오류).
    raw는 읽을 수 있었던 원래 칸들(빠진 행 파일에 남겨 고쳐 다시 올릴 수 있게)."""

    def __init__(self, where: str, reason: str, *, fatal: bool = False, raw: list[str] | None = None):
        self.where = where
        self.reason = reason
        self.fatal = fatal
        self.raw = list(raw) if raw is not None else None


# ---------------------------------------------------------------- 머리글 맞추기

def _key(text: str) -> str:
    """대소문자·공백·_·- 차이를 무시한 비교 키."""
    return re.sub(r"[\s_\-]+", "", text).casefold()


def table_labels(dashboard: Mapping[str, Any], table: str) -> dict[str, str]:
    """그 테이블을 그대로 보는(`table:`) 데이터셋의 labels: {표시 이름: 열}. 두 데이터셋이 다르게 부르면 둘 다 받는다."""
    out: dict[str, str] = {}
    for ds in dashboard.get("datasets", {}).values():
        if ds.get("table") == table:
            for col, label in (ds.get("labels") or {}).items():
                if isinstance(label, str):
                    out.setdefault(label, col)
    return out


@dataclass
class HeaderMatch:
    columns: list[str | None]                 # 머리글 순서. None = 버리는 머리글
    matched: dict[str, str] = field(default_factory=dict)       # 이름이 그대로가 아닌 맞춤 {머리글: 열}
    unknown: list[dict[str, Any]] = field(default_factory=list)  # [{"header", "candidates"}]
    missing: list[str] = field(default_factory=list)            # 머리글에 없는 스키마 열
    dropped: list[str] = field(default_factory=list)            # 요청의 columns로 버린 머리글
    errors: list[str] = field(default_factory=list)


def match_header(table: Mapping[str, Any], header: Sequence[str], *, labels: Mapping[str, str] | None = None,
                 overrides: Mapping[str, Any] | None = None, strict: bool = False,
                 where: Callable[[str], str] = lambda h: f"머리글:{h}") -> HeaderMatch:
    """머리글 → 열. 차례로 ① 열 이름 ② aliases ③ 데이터셋 labels ④ 대소문자·공백·_·- 무시. 두 열에 맞으면 맞추지 않는다.

    overrides({머리글: 열 또는 None})가 있으면 그 머리글은 그대로 따른다(None은 버린다). strict면 ①만 본다.
    """
    cols: dict[str, Any] = dict(table["columns"])
    labels = dict(labels or {})
    overrides = dict(overrides or {})
    result = HeaderMatch(columns=[])
    steps: list[Callable[[str], set[str]]] = [lambda h: {h} & set(cols)]
    if not strict:
        steps += [
            lambda h: {c for c, spec in cols.items() if h in (spec.get("aliases") or [])},
            lambda h: {labels[h]} & set(cols) if h in labels else set(),
            lambda h: {c for c, spec in cols.items()
                       if _key(h) and _key(h) in {_key(c), *(_key(a) for a in spec.get("aliases") or []),
                                                  *(_key(lb) for lb, lc in labels.items() if lc == c)}},
        ]
    for h in header:
        if h == "" and not strict and h not in overrides:
            result.columns.append(None)       # 이름 없는 열(엑셀이 끝에 남기는 빈 열)은 버리고 알린다
            result.dropped.append("")
            continue
        if h in overrides:
            target = overrides[h]
            if target is None:
                result.columns.append(None)
                result.dropped.append(h)
                continue
            if not isinstance(target, str) or target not in cols:
                result.errors.append(f"{where(h)}: columns가 가리키는 열 '{target}'이 schema.yaml에 없음")
                result.columns.append(None)
                continue
            result.columns.append(target)
            if target != h:
                result.matched[h] = target
            continue
        found: set[str] = set()
        for step in steps:
            found = step(h)
            if found:
                break
        if len(found) == 1:
            col = found.pop()
            result.columns.append(col)
            if col != h:
                result.matched[h] = col
        else:
            result.columns.append(None)
            pool = sorted(found) if found else difflib.get_close_matches(h, list(cols), n=3, cutoff=0.5)
            result.unknown.append({"header": h, "candidates": pool})
    seen: dict[str, str] = {}
    for h, col in zip(header, result.columns):
        if col is None:
            continue
        if col in seen:
            result.errors.append(f"{where(h)}: 머리글 '{seen[col]}'와 '{h}'가 같은 열 '{col}'에 맞음")
        seen.setdefault(col, h)
    result.missing = [c for c in cols if c not in seen]
    for pk in table["primary_key"]:
        if pk in result.missing:
            result.errors.append(f"{where(pk)}: 기본 키 열이 머리글에 없음")
    return result


# ---------------------------------------------------------------- 한 줄씩 검증

class RowChecker:
    """정규형 머리글(스키마 순서의 열 부분집합)의 값 → 파이썬 값. 규칙은 로더의 CSV 검증과 같다."""

    def __init__(self, table: Mapping[str, Any], columns: Sequence[str]):
        self.specs = [table["columns"][c] for c in columns]
        self.columns = list(columns)

    def check(self, values: Sequence[str]) -> tuple[list[Any], list[tuple[str, str]]]:
        out: list[Any] = []
        problems: list[tuple[str, str]] = []
        for c, spec, raw in zip(self.columns, self.specs, values):
            if raw == "":
                if spec.get("required"):
                    problems.append((c, "값이 필요함"))
                out.append(None)
                continue
            try:
                v = _parse(raw, spec)
            except ValueError as exc:
                problems.append((c, str(exc)))
                out.append(None)
                continue
            for reason in _check_value(v, raw, spec):
                problems.append((c, reason))
            out.append(v)
        return out, problems


# ---------------------------------------------------------------- 접수 한 번

@dataclass
class Report:
    """접수 결과(미리보기·결과의 normalized·columns·errors에 싣는다)."""
    total: int = 0
    invalid: int = 0
    errors: list[str] = field(default_factory=list)
    error_count: int = 0
    normalized: dict[str, Any] = field(default_factory=dict)
    columns: dict[str, Any] = field(default_factory=dict)
    rejected: list[int] = field(default_factory=list)    # 빼고 넣은 행의 위치
    fatal: bool = False                                    # 더 읽을 수 없는 오류(CSV 문법 오류)

    def error(self, text: str) -> None:
        self.error_count += 1
        if len(self.errors) < MAX_REPORTED:
            self.errors.append(text)
        elif len(self.errors) == MAX_REPORTED:
            self.errors.append("…: 오류가 너무 많아 나머지는 생략함")


class Intake:
    """파서 하나를 정규형 행으로 바꾼다. header는 정규형 머리글(스키마 순서), rows()는 (위치, 정규형 값, 파이썬 값)."""

    def __init__(self, parser: Parser, table_name: str, table: Mapping[str, Any], *,
                 labels: Mapping[str, str] | None = None, overrides: Mapping[str, Any] | None = None,
                 strict: bool = False):
        self.parser = parser
        self.table_name = table_name
        self.table = table
        self.strict = strict
        self.report = Report()
        raw_header = parser.header()
        header_row = getattr(parser, "header_row", 1)
        is_file_like = parser.kind in ("csv", "xlsx", "cursor")
        if strict:
            dup = [h for i, h in enumerate(raw_header) if h in raw_header[:i]]
            errs = [parser.where(header_row, h) + ": 머리글에 같은 열이 두 번 있음" for h in dup] if is_file_like else []
        else:
            errs = []
        self.match = match_header(table, raw_header, labels=labels, overrides=overrides, strict=strict,
                                  where=lambda h: parser.where(header_row, h) if is_file_like else f"rows.{h}")
        if strict and self.match.unknown:
            errs += [parser.where(header_row, u["header"] or "(빈 이름)") + ": schema.yaml에 없는 열" for u in self.match.unknown]
        errs += self.match.errors
        if errs:
            detail = "invalid_csv" if parser.kind in ("csv", "xlsx") else "invalid_rows"
            raise IntakeError(detail, "머리글이 올바르지 않습니다", errs)
        if self.match.unknown:
            raise IntakeError("unknown_columns", "스키마의 열에 맞지 않는 머리글이 있습니다",
                              columns=self.match.unknown, table=table_name)
        order = list(table["columns"])
        present = {c: i for i, c in enumerate(self.match.columns) if c is not None}
        self.header = [c for c in order if c in present]
        self._take = [present[c] for c in self.header]
        self._types = [table["columns"][c]["type"] for c in self.header]
        self.checker = RowChecker(table, self.header)
        self.report.columns = {"matched": dict(self.match.matched), "missing": list(self.match.missing),
                               "unknown": [], "dropped": list(self.match.dropped)}
        self.report.normalized = {"encoding": parser.encoding, "delimiter": parser.delimiter}
        self._counts: dict[str, int] = {}
        self.last_problems: list[str] = []      # 바로 앞에 내놓은 오류 행의 이유들(빠진 행 파일에 쓴다)

    def rows(self) -> Iterator[tuple[int, list[str], list[Any] | None]]:
        """(위치, 정규형 값, 파이썬 값). 검증 오류가 있는 행은 파이썬 값이 None이고 오류는 report에 쌓인다."""
        where = self.parser.where
        report = self.report
        sensitive = {c for c, s in self.table["columns"].items() if isinstance(s, Mapping) and s.get("sensitive")}
        try:
            for at, record in self.parser.rows():
                report.total += 1
                if isinstance(record, RowError):
                    report.invalid += 1
                    redacted = redact_sensitive_errors(self.table, [f"{record.where}: {record.reason}"])[0]
                    report.error(redacted)
                    self.last_problems = [record.reason]
                    if record.fatal:
                        report.fatal = True
                        return
                    values = []
                    if record.raw is not None:
                        # 칸을 정규형 머리글 자리에 놓고, 개수가 달라 놓지 못한 칸은 원래 줄 그대로 이유에 덧붙인다
                        values = [record.raw[i] if i < len(record.raw) else "" for i in self._take]
                        if sensitive:
                            self.last_problems = [record.reason, "원래 칸: ***"]
                        else:
                            self.last_problems = [record.reason, "원래 칸: " + " | ".join(record.raw)]
                    yield at, values, None
                    continue
                values = [record[i] for i in self._take]
                long = [c for c, v in zip(self.header, values) if len(v) > MAX_CELL_CHARS]
                if long:
                    report.invalid += 1
                    self.last_problems = [f"{col}: 값이 너무 김(최대 {MAX_CELL_CHARS}자)" for col in long]
                    for col in long:
                        report.error(f"{where(at, col)}: 값이 너무 김(최대 {MAX_CELL_CHARS}자)")
                    yield at, values, None
                    continue
                if not self.strict:
                    values = [normalize.normalize(v, t, self._counts) for v, t in zip(values, self._types)]
                typed, problems = self.checker.check(values)
                if problems:
                    report.invalid += 1
                    self.last_problems = []
                    value_by_col = dict(zip(self.header, values))
                    for col, reason in problems:
                        if col in sensitive:
                            raw_val = value_by_col.get(col, "")
                            if raw_val and f"'{raw_val}'" in reason:
                                shown_reason = reason.replace(f"'{raw_val}'", "'***'", 1)
                            else:
                                shown_reason = _mask_quoted_reason(reason)
                        else:
                            shown_reason = reason
                        self.last_problems.append(f"{col}: {shown_reason}")
                        report.error(f"{where(at, col)}: {shown_reason}")
                    yield at, values, None
                    continue
                yield at, values, typed
        finally:
            for k, v in self._counts.items():
                report.normalized[k] = v

    def close(self) -> None:
        self.parser.close()


from .db_import import CursorParser  # noqa: E402
from .xlsx import XlsxParser  # noqa: E402

PARSERS["xlsx"] = XlsxParser
PARSERS["cursor"] = CursorParser

