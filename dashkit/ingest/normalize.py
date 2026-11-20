"""값 다듬기(설계서 5-3, contracts/ingest.md "값 다듬기"). 받을 때는 너그럽게, 저장은 정규형으로.

유형마다의 규칙 목록을 등록 표 `NORMALIZERS`에 둔다. 규칙은 `(값) -> (새 값, 센 항목 | None)` 함수 하나다.
값을 바꾸지 않으면 센 항목은 None이다. 유형을 더하거나 표기를 더 받으려면 규칙 함수 하나와 표 한 줄을 더한다.

**뜻이 하나인 표기만** 바꾼다. 순서가 모호한 날짜(11/20/2026)는 바꾸지 않고 그대로 두어 검증이 오류로 알린다.
"""
from __future__ import annotations

import re
from typing import Callable

Rule = Callable[[str], "tuple[str, str | None]"]

_YMD_SEP = re.compile(r"(\d{4})[/.](\d{1,2})[/.](\d{1,2})")
_YMD_PACKED = re.compile(r"(\d{4})(\d{2})(\d{2})")
_THOUSANDS = re.compile(r"[+-]?\d{1,3}(,\d{3})+(\.\d+)?")
_BOOLEANS = {"y": "true", "yes": "true", "1": "true", "true": "true",
             "n": "false", "no": "false", "0": "false", "false": "false"}
_TS_SPLIT = re.compile(r"^(\S+?)(?:[ T]+)(\d{1,2}:\d{2}.*)$")


def _date_text(value: str) -> str | None:
    """연도가 앞에 오는 날짜 표기 → YYYY-MM-DD. 해당하지 않으면 None."""
    m = _YMD_SEP.fullmatch(value) or _YMD_PACKED.fullmatch(value)
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def trim(value: str) -> tuple[str, str | None]:
    stripped = value.strip()
    return (stripped, "trimmed") if stripped != value else (value, None)


def date_ymd(value: str) -> tuple[str, str | None]:
    out = _date_text(value)
    return (out, "dates") if out is not None and out != value else (value, None)


def thousands(value: str) -> tuple[str, str | None]:
    if _THOUSANDS.fullmatch(value):
        return value.replace(",", ""), "numbers"
    return value, None


def boolean_words(value: str) -> tuple[str, str | None]:
    out = _BOOLEANS.get(value.lower())
    return (out, "booleans") if out is not None and out != value else (value, None)


def timestamp_text(value: str) -> tuple[str, str | None]:
    """날짜와 시각 사이의 공백·T를 T 하나로, 날짜 부분은 date_ymd와 같은 표기를 받는다."""
    m = _TS_SPLIT.match(value)
    if not m:
        return date_ymd(value)
    day, clock = m.groups()
    out = f"{_date_text(day) or day}T{clock}"
    return (out, "timestamps") if out != value else (value, None)


NORMALIZERS: dict[str, list[Rule]] = {
    "*": [trim],                       # 모든 유형에 먼저
    "text": [],
    "enum": [],
    "integer": [thousands],
    "number": [thousands],
    "boolean": [boolean_words],
    "date": [date_ymd],
    "timestamp": [timestamp_text],
}


def normalize(value: str, column_type: str, counts: dict[str, int]) -> str:
    """값 하나를 다듬는다. 바꾼 항목을 counts에 센다. 빈 값은 그대로 둔다."""
    for rule in NORMALIZERS["*"] + NORMALIZERS.get(column_type, []):
        if value == "":
            return value
        value, counted = rule(value)
        if counted:
            counts[counted] = counts.get(counted, 0) + 1
    return value
