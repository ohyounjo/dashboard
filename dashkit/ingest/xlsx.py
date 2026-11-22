"""엑셀 통합 파일(.xlsx) 스트리밍 파서(설계서 reference/additional-intake-resumable-upload-design.md 2절).

외부 서드파티 패키지 없이 Python 표준 라이브러리(zipfile, xml.etree.ElementTree.iterparse)로만 구현한다.
ZIP 폭탄(비압축 크기·압축률 상한), XML 엔티티 선언(<!DOCTYPE, <!ENTITY), 공유 문자열 메모리 상한을 통제하고
워크시트는 <row> 처리 직후 elem.clear()로 해제하며 스트리밍으로 읽는다.
"""
from __future__ import annotations

import io
import posixpath
import re
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
import zipfile
import zlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import IO, Any, Iterator

from .intake import MAX_CELL_CHARS, IntakeError, Parser, RowError

MAX_XLSX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_XLSX_COMPRESSION_RATIO = 100
MAX_SHARED_STRINGS_BYTES = 128 * 1024 * 1024
MAX_XLSX_COLUMNS = 16_384
MAX_XLSX_ROWS = 1_048_576

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_CELL_REF_RE = re.compile(r"([A-Za-z]+)\d+")
_BUILTIN_DATE_FMTS = frozenset(range(14, 18)) | frozenset(range(27, 32)) | frozenset(range(50, 59))
_BUILTIN_TIME_FMTS = frozenset(range(18, 23)) | frozenset(range(32, 37)) | frozenset(range(45, 48))
_BUILTIN_DATETIME_FMTS = _BUILTIN_DATE_FMTS | _BUILTIN_TIME_FMTS
_ZIP_CORRUPT_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, KeyError, OSError)


@dataclass(frozen=True)
class XlsxLimits:
    max_uncompressed_bytes: int = MAX_XLSX_UNCOMPRESSED_BYTES
    max_compression_ratio: int = MAX_XLSX_COMPRESSION_RATIO
    max_shared_strings_bytes: int = MAX_SHARED_STRINGS_BYTES
    max_cell_chars: int = MAX_CELL_CHARS
    max_columns: int = MAX_XLSX_COLUMNS
    max_rows: int = MAX_XLSX_ROWS


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(elem: ET.Element, local_name: str, default: str | None = None) -> str | None:
    if local_name in elem.attrib:
        return elem.attrib[local_name]
    for k, v in elem.attrib.items():
        if _local(k) == local_name:
            return v
    return default


def _col_index(ref: str) -> int | None:
    m = _CELL_REF_RE.fullmatch(ref.strip())
    if not m:
        return None
    letters = m.group(1).upper()
    if len(letters) > 4:
        return MAX_XLSX_COLUMNS
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _first_format_section(code: str) -> str:
    """사용자 정의 서식의 첫 번째 구역(양수 서식, 따옴표 밖의 첫 ';' 앞)을 돌려준다."""
    in_quote = False
    escaped = False
    for i, ch in enumerate(code):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_quote = not in_quote
            continue
        if ch == ";" and not in_quote:
            return code[:i]
    return code


def _classify_format_code(code: str) -> tuple[bool, bool]:
    r"""사용자 정의 Excel 서식 코드 → (날짜/시각 서식 여부, 시각 토큰 포함 여부).

    따옴표("...")·이스케이프(\x)·색상/로캘([Red], [$-...]) 및 초 뒤 소수 초 자리표시자(s.0+)를 먼저 제거한 뒤
    일반 숫자 자리표시자(0, #, ?) 없이 날짜·시각 토큰(y, m, d, h, s)이 있을 때만 날짜·시각으로 판정한다.
    """
    sec = _first_format_section(code)
    cleaned = re.sub(r'"[^"]*"', "", sec)
    cleaned = re.sub(r"\\.", "", cleaned)
    cleaned = re.sub(r"[_*].", "", cleaned)
    cleaned = re.sub(r"\[(?![hmsHMS]+\])[^\]]*\]", "", cleaned)
    cleaned = re.sub(r"(?i)(?<=s)\.0+", "", cleaned)
    if any(ch in cleaned for ch in ("0", "#", "?")):
        return False, False
    low = cleaned.lower()
    has_date = any(ch in low for ch in ("y", "d"))
    has_time = any(ch in low for ch in ("h", "s")) or "am/pm" in low or "a/p" in low
    has_month_or_min = "m" in low
    if not (has_date or has_time or has_month_or_min):
        return False, False
    return True, has_time


def _excel_serial_to_iso(raw: str, *, has_time: bool, date1904: bool) -> str | None:
    """엑셀 날짜·시각 일련번호 → ISO 8601 문자열(범위 초과 시 IntakeError, 숫자가 아니면 None)."""
    try:
        serial = Decimal(raw)
    except InvalidOperation:
        return None
    if not serial.is_finite() or serial < 0:
        return None
    # Python date.max(9999-12-31) 기준 최대 일수(1900 체계 2,958,465 / 1904 체계 2,957,003) + 반올림 여유 전 사전 차단
    if serial > 2_958_466:
        raise IntakeError(
            "invalid_csv",
            "엑셀 날짜 일련번호가 표현 가능한 범위를 초과했습니다",
            ["날짜 일련번호가 최대 날짜(9999-12-31)를 초과함"],
        )
    days_dec = serial // 1
    frac = serial - days_dec
    if frac < 0:
        days_dec -= 1
        frac += 1
    days = int(days_dec)
    total_us = int((frac * Decimal(86_400_000_000)).to_integral_value(rounding=ROUND_HALF_UP))
    if total_us >= 86_400_000_000:
        days += 1
        total_us -= 86_400_000_000
    if date1904:
        base = date(1904, 1, 1)
    elif days == 60:
        base = date(1899, 12, 30)
    else:
        base = date(1899, 12, 31) if days < 60 else date(1899, 12, 30)
    if days > (date.max - base).days:
        raise IntakeError(
            "invalid_csv",
            "엑셀 날짜 일련번호가 표현 가능한 범위를 초과했습니다",
            ["날짜 일련번호가 최대 날짜(9999-12-31)를 초과함"],
        )
    if not has_time and total_us == 0:
        if not date1904 and days == 60:
            return "1900-02-29"
        return (base + timedelta(days=days)).isoformat()
    sec, us = divmod(total_us, 1_000_000)
    hh, rem = divmod(sec, 3600)
    mm, ss = divmod(rem, 60)
    if not date1904 and days == 60:
        return f"1900-02-29T{hh:02d}:{mm:02d}:{ss:02d}" + (f".{us:06d}" if us else "")
    d = base + timedelta(days=days)
    dt = datetime(d.year, d.month, d.day, hh, mm, ss, us)
    return dt.isoformat()


def _decimal_fixed_length(d: Decimal) -> int:
    """Decimal을 format(d, 'f')로 펼쳤을 때의 예상 문자열 길이를 할당 없이 계산한다."""
    sign, digits, exp = d.as_tuple()
    assert isinstance(exp, int)
    if all(dig == 0 for dig in digits):
        return 1 + (1 if sign else 0)
    end = len(digits)
    while exp < 0 and end > 0 and digits[end - 1] == 0:
        end -= 1
        exp += 1
    n = max(end, 1)
    s_len = 1 if sign else 0
    if exp >= 0:
        return s_len + n + exp
    if n + exp > 0:
        return s_len + n + 1
    return s_len + 2 + (-exp)


def _format_number_cell(raw: str, *, max_chars: int = MAX_CELL_CHARS) -> str:
    s = raw.strip()
    if not s:
        return ""
    if "e" not in s.lower():
        if len(s) > max_chars:
            raise IntakeError("invalid_csv", f"숫자 셀 길이가 최대 허용 길이({max_chars}자)를 초과했습니다")
        return s
    try:
        d = Decimal(s)
    except InvalidOperation:
        return s
    if not d.is_finite():
        return s
    if _decimal_fixed_length(d) > max_chars:
        raise IntakeError(
            "invalid_csv",
            f"숫자 셀 길이가 최대 허용 길이({max_chars}자)를 초과했습니다",
        )
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


class _BoundedStream(io.RawIOBase):
    """ZIP 엔트리 스트림 래퍼: 실제 읽은 비압축 바이트·압축률 상한과 XML DOCTYPE/ENTITY 선언을 차단한다."""

    def __init__(self, raw: IO[bytes], name: str, compress_size: int, counter: list[int], limits: XlsxLimits):
        self._raw = raw
        self._name = name
        self._compress_size = compress_size
        self._counter = counter
        self._limits = limits
        self.entry_bytes = 0
        self._in_prolog = True
        self._expat: expat.XMLParserType | None = self._make_prolog_parser()

    def _make_prolog_parser(self) -> expat.XMLParserType:
        p = expat.ParserCreate()

        def _reject_dtd(*_args: Any) -> None:
            raise IntakeError("invalid_csv", "안전하지 않은 XML 선언이 포함되어 있습니다",
                              [f"{self._name}:1:-: XML 엔티티 선언(DOCTYPE/ENTITY)은 허용되지 않습니다"])

        def _reject_ext(*_args: Any) -> int:
            _reject_dtd()
            return 0

        def _on_root(*_args: Any) -> None:
            self._in_prolog = False

        p.StartDoctypeDeclHandler = _reject_dtd
        p.EntityDeclHandler = _reject_dtd
        p.UnparsedEntityDeclHandler = _reject_dtd
        p.ExternalEntityRefHandler = _reject_ext
        p.StartElementHandler = _on_root
        return p

    def readable(self) -> bool:
        return True

    def _check_prolog(self, data: bytes) -> None:
        if not self._in_prolog or not data or self._expat is None:
            return
        try:
            self._expat.Parse(data, False)
        except expat.ExpatError as exc:
            raise IntakeError("invalid_csv", "xlsx XML을 읽을 수 없습니다",
                              [f"{self._name}:1:-: XML 오류: {exc}"]) from None
        if not self._in_prolog:
            self._expat = None

    def read(self, size: int = -1) -> bytes:
        data = self._raw.read(size)
        if data:
            n = len(data)
            self.entry_bytes += n
            self._counter[0] += n
            if self._counter[0] > self._limits.max_uncompressed_bytes:
                raise IntakeError("invalid_csv", "xlsx 압축 해제 크기가 한도를 넘었습니다",
                                  [f"{self._name}:1:-: xlsx 압축 해제 크기가 한도를 넘음"])
            if self.entry_bytes > 1024 * 1024:
                if self._compress_size <= 0 or self.entry_bytes / self._compress_size > self._limits.max_compression_ratio:
                    raise IntakeError("invalid_csv", "xlsx 압축률이 허용 한도를 넘었습니다",
                                      [f"{self._name}:1:-: xlsx 압축 해제 크기가 한도를 넘음"])
            self._check_prolog(data)
        return data

    def readinto(self, b) -> int:  # type: ignore[override]
        data = self.read(len(b))
        n = len(data)
        b[:n] = data
        return n

    def close(self) -> None:
        try:
            self._raw.close()
        finally:
            super().close()


class XlsxParser(Parser):
    """OOXML(.xlsx) 스트리밍 파서(PARSERS['xlsx'])."""

    kind = "xlsx"
    encoding = "xlsx"
    delimiter = None

    def __init__(self, path: Path, name: str, *, strict: bool = False, sheet: str | int | None = None,
                 limits: XlsxLimits | None = None):
        self.path = Path(path)
        self.name = name
        self.strict = strict
        self.sheet = sheet
        self.limits = limits or XlsxLimits()
        self.header_row = 1
        self._zf: zipfile.ZipFile | None = None
        self._sheet_stream: _BoundedStream | None = None
        self._sheet_size: int | None = None
        self._total_read = [0]
        self._date1904 = False
        self._shared_strings: list[str] = []
        self._xf_formats: list[tuple[bool, bool]] = []
        self._sheet_target: str | None = None
        self._header: list[str] | None = None
        self._iter: Iterator[tuple[int, list[str] | RowError]] | None = None

    def _reject_xlsx(self, reason: str) -> IntakeError:
        return IntakeError("invalid_xlsx", "암호화되지 않은 .xlsx 파일만 지원합니다", [f"{self.name}:1:-: {reason}"])

    def _open_entry(self, entry: str) -> _BoundedStream:
        assert self._zf is not None
        try:
            zinfo = self._zf.getinfo(entry)
            raw = self._zf.open(zinfo, "r")
        except _ZIP_CORRUPT_ERRORS as exc:
            raise self._reject_xlsx(f"손상되었거나 읽을 수 없는 xlsx 엔트리({entry})입니다: {exc}") from None
        return _BoundedStream(raw, self.name, zinfo.compress_size, self._total_read, self.limits)

    def _check_archive(self) -> None:
        if self.name.lower().endswith(".xlsm") or self.path.name.lower().endswith(".xlsm"):
            raise self._reject_xlsx("매크로 포함 .xlsm 파일은 지원하지 않습니다")
        try:
            with open(self.path, "rb") as f:
                magic = f.read(8)
        except OSError as exc:
            raise IntakeError("invalid_csv", "xlsx 파일을 열 수 없습니다", [f"{self.name}:1:-: {exc}"]) from None
        if magic.startswith(_OLE2_MAGIC):
            raise self._reject_xlsx("구형 .xls 또는 암호화된 파일은 지원하지 않습니다")
        try:
            self._zf = zipfile.ZipFile(self.path, "r")
        except (zipfile.BadZipFile, OSError):
            raise self._reject_xlsx("올바른 .xlsx(ZIP) 파일이 아닙니다") from None

        names = set(self._zf.namelist())
        if "xl/vbaProject.bin" in names or "EncryptedPackage" in names or "EncryptionInfo" in names:
            self.close()
            raise self._reject_xlsx("암호화되었거나 매크로가 포함된 워크북입니다")

        total_declared = 0
        for zinfo in self._zf.infolist():
            total_declared += zinfo.file_size
            if total_declared > self.limits.max_uncompressed_bytes:
                self.close()
                raise IntakeError("invalid_csv", "xlsx 압축 해제 크기가 한도를 넘었습니다",
                                  [f"{self.name}:1:-: xlsx 압축 해제 크기가 한도를 넘음"])
            if zinfo.file_size > 1024 * 1024:
                if zinfo.compress_size <= 0 or zinfo.file_size / zinfo.compress_size > self.limits.max_compression_ratio:
                    self.close()
                    raise IntakeError("invalid_csv", "xlsx 압축률이 허용 한도를 넘었습니다",
                                      [f"{self.name}:1:-: xlsx 압축 해제 크기가 한도를 넘음"])

        if "xl/workbook.xml" not in names or "xl/_rels/workbook.xml.rels" not in names:
            self.close()
            raise self._reject_xlsx("xlsx 워크북 구조(xl/workbook.xml)가 없습니다")

    def _parse_workbook(self) -> None:
        assert self._zf is not None
        rels: dict[str, str] = {}
        try:
            with self._open_entry("xl/_rels/workbook.xml.rels") as stream:
                for event, elem in ET.iterparse(stream, events=("end",)):
                    if _local(elem.tag) == "Relationship":
                        rid = elem.attrib.get("Id")
                        target = elem.attrib.get("Target")
                        mode = elem.attrib.get("TargetMode", "")
                        if rid and target and mode.lower() != "external":
                            norm = target.lstrip("/") if target.startswith("/") else posixpath.normpath(
                                posixpath.join("xl", target))
                            if not norm.startswith("..") and "/.." not in norm:
                                rels[rid] = norm
                        elem.clear()
        except ET.ParseError as exc:
            raise IntakeError("invalid_csv", "xlsx XML을 읽을 수 없습니다", [f"{self.name}:1:-: XML 오류: {exc}"]) from None
        except _ZIP_CORRUPT_ERRORS as exc:
            raise self._reject_xlsx(f"손상된 xlsx ZIP 엔트리입니다 ({exc})") from None

        sheets: list[tuple[str, str, str]] = []
        try:
            with self._open_entry("xl/workbook.xml") as stream:
                for event, elem in ET.iterparse(stream, events=("end",)):
                    tag = _local(elem.tag)
                    if tag == "workbookPr":
                        self._date1904 = elem.attrib.get("date1904", "").lower() in ("1", "true")
                        elem.clear()
                    elif tag == "sheet":
                        sname = elem.attrib.get("name", "")
                        state = elem.attrib.get("state", "visible")
                        rid = _attr(elem, "id")
                        target = rels.get(rid or "")
                        if target:
                            sheets.append((sname, target, state))
                        elem.clear()
        except ET.ParseError as exc:
            raise IntakeError("invalid_csv", "xlsx XML을 읽을 수 없습니다", [f"{self.name}:1:-: XML 오류: {exc}"]) from None
        except _ZIP_CORRUPT_ERRORS as exc:
            raise self._reject_xlsx(f"손상된 xlsx ZIP 엔트리입니다 ({exc})") from None

        if not sheets:
            raise IntakeError("invalid_csv", "시트가 없습니다", [f"{self.name}:1:-: 워크북에 시트가 없음"])

        chosen: tuple[str, str, str] | None = None
        if self.sheet is None:
            chosen = next((s for s in sheets if s[2] not in ("hidden", "veryHidden")), sheets[0])
        elif isinstance(self.sheet, int) and not isinstance(self.sheet, bool):
            if 0 <= self.sheet < len(sheets):
                chosen = sheets[self.sheet]
        elif isinstance(self.sheet, str):
            chosen = next((s for s in sheets if s[0] == self.sheet), None)
        if chosen is None or chosen[1] not in set(self._zf.namelist()):
            raise IntakeError("invalid_csv", "시트를 찾을 수 없습니다", [f"{self.name}:1:-: 시트 '{self.sheet}'가 없음"])
        self._sheet_target = chosen[1]

    def _parse_shared_strings(self) -> None:
        assert self._zf is not None
        if "xl/sharedStrings.xml" not in set(self._zf.namelist()):
            return
        total_bytes = 0
        parts: list[str] = []
        in_si = False
        in_rph = 0
        try:
            with self._open_entry("xl/sharedStrings.xml") as stream:
                for event, elem in ET.iterparse(stream, events=("start", "end")):
                    tag = _local(elem.tag)
                    if event == "start":
                        if tag == "si":
                            in_si = True
                            parts = []
                        elif tag == "rPh" and in_si:
                            in_rph += 1
                    else:
                        if tag == "rPh" and in_si and in_rph > 0:
                            in_rph -= 1
                        elif tag == "t" and in_si and in_rph == 0:
                            if elem.text:
                                parts.append(elem.text)
                        elif tag == "si":
                            s = "".join(parts)
                            if len(s) > self.limits.max_cell_chars:
                                raise IntakeError("invalid_csv", "xlsx 공유 문자열이 너무 깁니다",
                                                  [f"{self.name}:1:-: 공유 문자열이 최대 글자 수({self.limits.max_cell_chars})를 넘음"])
                            total_bytes += len(s.encode("utf-8"))
                            if total_bytes > self.limits.max_shared_strings_bytes:
                                raise IntakeError("invalid_csv", "xlsx 공유 문자열 크기가 한도를 넘었습니다",
                                                  [f"{self.name}:1:-: 공유 문자열 크기가 한도를 넘음"])
                            self._shared_strings.append(s)
                            in_si = False
                            parts = []
                            elem.clear()
        except ET.ParseError as exc:
            raise IntakeError("invalid_csv", "xlsx 공유 문자열 XML 오류", [f"{self.name}:1:-: XML 오류: {exc}"]) from None
        except _ZIP_CORRUPT_ERRORS as exc:
            raise self._reject_xlsx(f"손상된 xlsx 공유 문자열 엔트리입니다 ({exc})") from None

    def _parse_styles(self) -> None:
        assert self._zf is not None
        if "xl/styles.xml" not in set(self._zf.namelist()):
            return
        custom_fmts: dict[int, str] = {}
        in_cell_xfs = False
        try:
            with self._open_entry("xl/styles.xml") as stream:
                for event, elem in ET.iterparse(stream, events=("start", "end")):
                    tag = _local(elem.tag)
                    if event == "start":
                        if tag == "cellXfs":
                            in_cell_xfs = True
                    else:
                        if tag == "numFmt":
                            try:
                                raw_fid = elem.attrib["numFmtId"]
                                fid = int(raw_fid) if len(raw_fid) <= 9 else -1
                            except (KeyError, ValueError):
                                fid = -1
                            if fid >= 0 and "formatCode" in elem.attrib:
                                custom_fmts[fid] = elem.attrib["formatCode"]
                            elem.clear()
                        elif tag == "xf" and in_cell_xfs:
                            try:
                                raw_num_fmt = elem.attrib.get("numFmtId", "0")
                                fid = int(raw_num_fmt) if len(raw_num_fmt) <= 9 else 0
                            except ValueError:
                                fid = 0
                            if fid in _BUILTIN_DATE_FMTS:
                                self._xf_formats.append((True, False))
                            elif fid in _BUILTIN_TIME_FMTS:
                                self._xf_formats.append((True, True))
                            elif fid in custom_fmts:
                                self._xf_formats.append(_classify_format_code(custom_fmts[fid]))
                            else:
                                self._xf_formats.append((False, False))
                            elem.clear()
                        elif tag == "cellXfs":
                            in_cell_xfs = False
                            elem.clear()
        except ET.ParseError as exc:
            raise IntakeError("invalid_csv", "xlsx 스타일 XML 오류", [f"{self.name}:1:-: XML 오류: {exc}"]) from None
        except _ZIP_CORRUPT_ERRORS as exc:
            raise self._reject_xlsx(f"손상된 xlsx 스타일 엔트리입니다 ({exc})") from None

    def _cell_value(self, ctype: str, style_idx: int | None, raw_v: str, inline_parts: list[str]) -> str:
        if ctype == "s":
            try:
                raw_idx = raw_v.strip()
                if len(raw_idx) > 12:
                    return ""
                idx = int(raw_idx)
                return self._shared_strings[idx]
            except (ValueError, IndexError):
                return ""
        if ctype == "inlineStr":
            return "".join(inline_parts)
        if ctype in ("str", "d", "e"):
            return raw_v
        if ctype == "b":
            low = raw_v.strip().lower()
            if low in ("1", "true"):
                return "true"
            if low in ("0", "false"):
                return "false"
            return raw_v
        # 숫자 또는 날짜 일련번호(t="n" 또는 생략)
        if not raw_v.strip():
            return ""
        if style_idx is not None and 0 <= style_idx < len(self._xf_formats):
            is_dt, has_time = self._xf_formats[style_idx]
            if is_dt:
                iso = _excel_serial_to_iso(raw_v.strip(), has_time=has_time, date1904=self._date1904)
                if iso is not None:
                    return iso
        return _format_number_cell(raw_v, max_chars=self.limits.max_cell_chars)

    def _iter_sheet_rows(self) -> Iterator[tuple[int, list[str]]]:
        assert self._zf is not None and self._sheet_target is not None
        zinfo = self._zf.getinfo(self._sheet_target)
        self._sheet_size = zinfo.file_size
        self._sheet_stream = self._open_entry(self._sheet_target)
        prev_row_num = 0
        cur_row_num = 0
        in_row = False
        in_c = False
        in_is = False
        in_rph = 0
        c_type = "n"
        c_style: int | None = None
        c_col: int | None = None
        prev_col = -1
        c_v = ""
        c_inline: list[str] = []
        row_cells: list[tuple[int, str]] = []

        try:
            for event, elem in ET.iterparse(self._sheet_stream, events=("start", "end")):
                tag = _local(elem.tag)
                if event == "start":
                    if tag == "row":
                        in_row = True
                        r_attr = elem.attrib.get("r")
                        if r_attr is not None:
                            if not r_attr or len(r_attr) > 10 or not r_attr.isdigit():
                                raise IntakeError(
                                    "invalid_csv",
                                    "xlsx 행 번호 형식이 올바르지 않습니다",
                                    [f"{self.name}:{max(prev_row_num, 1)}:-: 잘못된 행 번호 r='{r_attr[:32]}'"],
                                )
                            cur_row_num = int(r_attr)
                            if cur_row_num <= 0 or cur_row_num > self.limits.max_rows:
                                raise IntakeError(
                                    "invalid_csv",
                                    f"xlsx 행 번호가 허용 한도({self.limits.max_rows}행)를 벗어났습니다",
                                    [f"{self.name}:{max(prev_row_num, 1)}:-: 행 번호 {cur_row_num}이 허용 범위를 벗어남"],
                                )
                        else:
                            cur_row_num = prev_row_num + 1
                            if cur_row_num > self.limits.max_rows:
                                raise IntakeError(
                                    "invalid_csv",
                                    f"xlsx 행 수가 허용 한도({self.limits.max_rows}행)를 초과했습니다",
                                    [f"{self.name}:{cur_row_num}:-: 최대 행 수 초과"],
                                )
                        prev_row_num = cur_row_num
                        row_cells = []
                        prev_col = -1
                    elif tag == "c" and in_row:
                        in_c = True
                        c_type = elem.attrib.get("t", "n")
                        s_attr = elem.attrib.get("s")
                        c_style = int(s_attr) if s_attr and len(s_attr) <= 9 and s_attr.isdigit() else None
                        r_attr = elem.attrib.get("r")
                        parsed_col = _col_index(r_attr) if r_attr else None
                        c_col = parsed_col if parsed_col is not None else (prev_col + 1)
                        if c_col < 0 or c_col >= self.limits.max_columns:
                            raise IntakeError(
                                "invalid_csv",
                                f"xlsx 열 번호가 허용 한도({self.limits.max_columns}열)를 초과했습니다",
                                [f"{self.name}:{max(cur_row_num, 1)}:-: 열 참조 '{r_attr or c_col + 1}'가 허용 범위를 넘음"],
                            )
                        prev_col = c_col
                        c_v = ""
                        c_inline = []
                    elif tag == "is" and in_c:
                        in_is = True
                    elif tag == "rPh" and in_is:
                        in_rph += 1
                else:
                    if tag == "rPh" and in_is and in_rph > 0:
                        in_rph -= 1
                    elif tag == "t" and in_is and in_rph == 0:
                        if elem.text:
                            c_inline.append(elem.text)
                    elif tag == "is" and in_c:
                        in_is = False
                    elif tag == "v" and in_c:
                        c_v = elem.text or ""
                    elif tag == "c" and in_c:
                        val = self._cell_value(c_type, c_style, c_v, c_inline)
                        assert c_col is not None
                        row_cells.append((c_col, val))
                        in_c = False
                    elif tag == "row" and in_row:
                        if row_cells:
                            max_col = -1
                            for idx, val in row_cells:
                                if val != "" and idx > max_col:
                                    max_col = idx
                            if max_col >= 0:
                                dense = [""] * (max_col + 1)
                                for idx, val in row_cells:
                                    if idx <= max_col:
                                        dense[idx] = val
                                yield cur_row_num, dense
                        in_row = False
                        row_cells = []
                        elem.clear()
        except ET.ParseError as exc:
            raise IntakeError("invalid_csv", "xlsx 시트 XML을 읽을 수 없습니다",
                              [f"{self.name}:{max(cur_row_num, 1)}:-: XML 오류: {exc}"]) from None
        except _ZIP_CORRUPT_ERRORS as exc:
            raise IntakeError("invalid_xlsx", "손상된 xlsx 시트 ZIP 데이터를 읽을 수 없습니다",
                              [f"{self.name}:{max(cur_row_num, 1)}:-: ZIP 무결성/압축 오류 ({exc})"]) from None
        finally:
            if self._sheet_stream is not None:
                self._sheet_stream.close()
                self._sheet_stream = None

    def _start(self) -> None:
        if self._header is not None:
            return
        try:
            self._check_archive()
            self._parse_workbook()
            self._parse_shared_strings()
            self._parse_styles()
            raw_iter = self._iter_sheet_rows()
            header: list[str] | None = None
            for row_num, cells in raw_iter:
                if not cells or all(c.strip() == "" for c in cells):
                    continue
                trimmed = list(cells)
                while trimmed and trimmed[-1].strip() == "":
                    trimmed.pop()
                if not trimmed:
                    continue
                self.header_row = row_num
                header = [c.strip() for c in trimmed]
                break
        except BaseException:
            self.close()
            raise
        if not header or all(h == "" for h in header):
            self.close()
            raise IntakeError("invalid_csv", "시트에 머리글이 없습니다", [f"{self.name}:1:-: 머리글이 없음"])
        self._header = header
        width = len(header)

        def _data_rows() -> Iterator[tuple[int, list[str] | RowError]]:
            try:
                for row_num, cells in raw_iter:
                    if not cells or all(c.strip() == "" for c in cells):
                        continue
                    # 머리글 너비보다 오른쪽의 빈 셀은 무시한다. 값이 있는 셀이 있으면 오류다.
                    last_non_empty = len(cells) - 1
                    while last_non_empty >= width and cells[last_non_empty].strip() == "":
                        last_non_empty -= 1
                    if last_non_empty >= width:
                        record = cells[:last_non_empty + 1]
                        yield row_num, RowError(self.where(row_num, "-"),
                                                f"값 개수 {len(record)}개가 머리글 {width}개와 다름", raw=record)
                        continue
                    record = cells[:width]
                    if len(record) < width:
                        record = record + [""] * (width - len(record))
                    yield row_num, record
            except IntakeError as exc:
                msg = exc.errors[0] if exc.errors else exc.message
                yield max(self.header_row, 1), RowError(self.where(max(self.header_row, 1), "-"), msg, fatal=True)

        self._iter = _data_rows()

    def header(self) -> list[str]:
        self._start()
        assert self._header is not None
        return list(self._header)

    def rows(self) -> Iterator[tuple[int, list[str] | RowError]]:
        self._start()
        assert self._iter is not None
        return self._iter

    def where(self, at: int, column: str) -> str:
        return f"{self.name}:{at}:{column}"

    def progress(self) -> tuple[int, int | None]:
        done = self._sheet_stream.entry_bytes if self._sheet_stream is not None else (self._sheet_size or 0)
        return done, self._sheet_size

    def close(self) -> None:
        if self._sheet_stream is not None:
            try:
                self._sheet_stream.close()
            except Exception:
                pass
            self._sheet_stream = None
        if self._zf is not None:
            try:
                self._zf.close()
            except Exception:
                pass
            self._zf = None
