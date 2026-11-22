"""읽기 전용 SQL 콘솔 사전 검사 및 래핑(#35, contracts/manage.md).

PostgreSQL 렉서(문자열·이중 따옴표·달러 인용·줄/중첩 블록 주석)로 토큰을 분해해
단일 조회 문장만 허용하고, 부작용/DoS 함수를 차단하며, `%` 이스케이프 및 줄바꿈으로 감싼다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..ingest import IngestError

MAX_SQL_CHARS = 20_000

_ALLOWED_START = frozenset({"select", "with", "values", "table", "("})
_BLOCKED_FUNCTIONS = frozenset({
    "pg_sleep",
    "pg_terminate_backend",
    "pg_cancel_backend",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "lo_import",
    "lo_export",
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
})
_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


@dataclass(frozen=True)
class _Token:
    kind: str       # word | ident | string | punct
    value: str
    start: int
    end: int


def _invalid(message: str) -> IngestError:
    return IngestError(422, "invalid_query", message)


def _tokenize(sql: str) -> list[_Token]:
    tokens: list[_Token] = []
    n = len(sql)
    i = 0
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        # 줄 주석
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            nl = sql.find("\n", i + 2)
            i = n if nl == -1 else nl + 1
            continue
        # 중첩 블록 주석
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            depth = 1
            i += 2
            while i < n and depth > 0:
                if sql[i] == "/" and i + 1 < n and sql[i + 1] == "*":
                    depth += 1
                    i += 2
                elif sql[i] == "*" and i + 1 < n and sql[i + 1] == "/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth > 0:
                raise _invalid("블록 주석(/* ... */)이 닫히지 않았습니다")
            continue
        # E'...' 확장 문자열
        if ch in ("E", "e") and i + 1 < n and sql[i + 1] == "'":
            start = i
            i += 2
            closed = False
            while i < n:
                if sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                i += 1
            if not closed:
                raise _invalid("문자열 리터럴이 닫히지 않았습니다")
            tokens.append(_Token("string", sql[start:i], start, i))
            continue
        # 일반 문자열 '...'
        if ch == "'":
            start = i
            i += 1
            closed = False
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                i += 1
            if not closed:
                raise _invalid("문자열 리터럴이 닫히지 않았습니다")
            tokens.append(_Token("string", sql[start:i], start, i))
            continue
        # 이중 따옴표 식별자 "..."
        if ch == '"':
            start = i
            i += 1
            buf: list[str] = []
            closed = False
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        buf.append('"')
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                buf.append(sql[i])
                i += 1
            if not closed:
                raise _invalid("식별자 따옴표(\")가 닫히지 않았습니다")
            tokens.append(_Token("ident", "".join(buf), start, i))
            continue
        # 달러 인용 $tag$...$tag$
        if ch == "$":
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m:
                tag = m.group(0)
                start = i
                end_pos = sql.find(tag, m.end())
                if end_pos == -1:
                    raise _invalid("달러 인용 문자열($tag$...$tag$)이 닫히지 않았습니다")
                i = end_pos + len(tag)
                tokens.append(_Token("string", sql[start:i], start, i))
                continue
        # 단어/식별자
        if ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < n and (sql[i].isalnum() or sql[i] in ("_", "$")):
                i += 1
            tokens.append(_Token("word", sql[start:i], start, i))
            continue
        # 숫자
        if ch.isdigit():
            start = i
            i += 1
            while i < n and (sql[i].isalnum() or sql[i] in (".", "_")):
                i += 1
            tokens.append(_Token("number", sql[start:i], start, i))
            continue
        # 구두점
        tokens.append(_Token("punct", ch, i, i + 1))
        i += 1
    return tokens


def _is_blocked_fn(name: str) -> bool:
    low = name.lower()
    return low in _BLOCKED_FUNCTIONS or low.startswith("dblink")


def prepare_sql(raw_sql: str) -> tuple[str, str]:
    """사용자 SQL을 검사하고 래핑된 SQL과 정제된 원문(세미콜론 제거본)을 반환한다."""
    if not isinstance(raw_sql, str):
        raise _invalid("sql은 문자열이어야 합니다")
    trimmed = raw_sql.strip()
    if not trimmed:
        raise _invalid("실행할 SQL을 입력해 주세요")
    if len(trimmed) > MAX_SQL_CHARS:
        raise _invalid(f"SQL은 최대 {MAX_SQL_CHARS:,}자까지 입력할 수 있습니다")

    tokens = _tokenize(trimmed)
    if not tokens:
        raise _invalid("실행할 SQL을 입력해 주세요")

    cleaned = trimmed
    if tokens[-1].kind == "punct" and tokens[-1].value == ";":
        semi = tokens.pop()
        cleaned = (trimmed[:semi.start] + trimmed[semi.end:]).strip()
        if not tokens:
            raise _invalid("실행할 SQL을 입력해 주세요")

    for tok in tokens:
        if tok.kind == "punct" and tok.value == ";":
            raise _invalid("한 번에 하나의 조회 문장만 실행할 수 있습니다")

    depth = 0
    for tok in tokens:
        if tok.kind == "punct":
            if tok.value == "(":
                depth += 1
            elif tok.value == ")":
                depth -= 1
                if depth < 0:
                    raise _invalid("괄호 짝이 맞지 않습니다")
    if depth != 0:
        raise _invalid("괄호가 닫히지 않았습니다")

    first = tokens[0]
    first_val = first.value.lower() if first.kind in ("word", "punct") else ""
    if first_val not in _ALLOWED_START:
        raise _invalid("읽기 전용 SELECT 문장만 실행할 수 있습니다")

    for idx, tok in enumerate(tokens[:-1]):
        if tok.kind in ("word", "ident"):
            nxt = tokens[idx + 1]
            if nxt.kind == "punct" and nxt.value == "(" and _is_blocked_fn(tok.value):
                raise _invalid(f"허용되지 않는 함수입니다: {tok.value}")

    escaped = cleaned.replace("%", "%%")
    guarded_sql = f"SELECT * FROM (\n{escaped}\n) AS _dashkit_q LIMIT %s OFFSET %s"
    return guarded_sql, cleaned
