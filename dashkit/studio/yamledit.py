"""YAML 파일을 "고친 곳만" 다시 쓴다(주석·키 순서·인용 부호·들여쓰기·flow 스타일 보존).

파일을 통째로 다시 내보내지 않는다. PyYAML의 노드 위치(시작·끝 글자 위치)로 원문을 조각내고,
바뀌지 않은 조각은 **원문 글자 그대로** 다시 이어 붙인다. 바뀐 값·새 항목만 새로 쓴다.
그래서 건드리지 않은 줄은 바이트 단위로 같다.

- `Document(text).root`: 원문 위치를 기억하는 dict/list(TMap, TSeq). 연산(ops.py)이 이것을 고친다.
  객체를 **옮기면**(pop → insert) 주석과 원문이 따라간다. 새로 만든 dict/list는 새로 쓴다.
- `Document.render()`: 고친 결과의 글. 원문을 조각으로 다룰 수 없는 경우(앵커·별칭, 여러 줄 flow 안의 수정 등)는
  Unsupported를 던진다. 부르는 쪽(store.edit_yaml)이 ruamel.yaml round-trip으로 넘어간다.

조각 규칙(block 스타일):
- 항목(매핑의 `키: 값`, 시퀀스의 `- 값`)의 몸통은 키/`-`에서 값의 마지막 줄 끝까지다(같은 줄의 주석 포함).
- 앞 항목과의 사이에 있는 줄(빈 줄, 주석)은 뒤 항목의 것이다. 그 가운데 맨 앞의 빈 줄들은 "자리"에 남고
  (순서를 바꿔도 빈 줄 배치가 유지된다), 주석 줄부터는 항목을 따라 움직인다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import yaml

__all__ = ["Document", "Unsupported", "TMap", "TSeq", "plain", "strict_equal"]


class Unsupported(Exception):
    """이 방식으로 다룰 수 없는 원문 또는 변경. 부르는 쪽이 다른 방식으로 쓴다."""


class TMap(dict):
    """원문 위치를 기억하는 매핑."""
    origin: "Origin | None" = None


class TSeq(list):
    """원문 위치를 기억하는 시퀀스."""
    origin: "Origin | None" = None


@dataclass(eq=False)
class Origin:
    kind: str                      # map | seq | scalar
    flow: bool                     # flow 스타일({…}, […])이거나 스칼라
    start: int
    end: int                       # 내용의 끝(block 컬렉션은 마지막 항목 몸통의 끝)
    column: int
    snapshot: Any = None           # 읽었을 때의 값(보통 dict/list/스칼라)
    entries: list["Entry"] = field(default_factory=list)
    entry: "Entry | None" = None   # 이 노드를 값으로 가진 항목

    @property
    def block(self) -> bool:
        return self.kind != "scalar" and not self.flow


@dataclass(eq=False)
class Entry:
    parent: Origin
    key: Any                       # 매핑의 키 값(시퀀스는 None)
    head_start: int                # 키 또는 `-`의 위치(flow 시퀀스는 값의 위치)
    key_end: int                   # 키 또는 `-`의 끝
    value: Origin
    trivia: str = ""               # block: 이 항목 앞의 줄들(빈 줄, 주석). 줄바꿈으로 끝난다
    body_end: int = 0              # block: 몸통의 끝(줄 끝)
    own_line: bool = False         # 값이 block 컬렉션이고 다음 줄에서 시작한다(`키:` 줄 + 들여쓴 값)
    head_line_end: int = 0         # own_line일 때 `키:` 줄의 끝


def plain(value: Any) -> Any:
    """TMap/TSeq(와 그 밖의 dict/list)를 보통 dict/list로."""
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def strict_equal(a: Any, b: Any) -> bool:
    """값과 유형, 키 순서까지 같은가(True와 1, 1과 1.0을 구분한다)."""
    if isinstance(a, dict) and isinstance(b, dict):
        return list(a) == list(b) and all(type(x) is type(y) for x, y in zip(a, b)) \
            and all(strict_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(strict_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return False
    return type(a) is type(b) and a == b


def _is_scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list))


# ---------------------------------------------------------------- 새로 쓰는 값

_PLAIN_RE = re.compile(r"[A-Za-z가-힣_][A-Za-z0-9가-힣_./()%+-]*( [A-Za-z0-9가-힣_./()%+-]+)*")
_ESCAPES = {"\x85": "\\N", "\u2028": "\\L", "\u2029": "\\P", "\ufeff": "\\uFEFF", "\x7f": "\\x7F"}
INLINE_LIMIT = 80        # 새 컬렉션을 한 줄(flow)로 쓰는 길이 상한


def _quoted(text: str) -> str:
    out = json.dumps(text, ensure_ascii=False)
    for ch, esc in _ESCAPES.items():
        out = out.replace(ch, esc)
    return out


def scalar_text(value: Any) -> str:
    """스칼라 하나의 YAML 글. PyYAML이 다시 읽으면 같은 값이 된다."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = repr(value)
        if value != value or value in (float("inf"), float("-inf")):
            raise Unsupported("유한하지 않은 숫자")
        return text if "." in text or "e" in text or "E" in text else text + ".0"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if not isinstance(value, str):
        raise Unsupported(f"쓸 수 없는 값 유형 {type(value).__name__}")
    text = value if _PLAIN_RE.fullmatch(value) else _quoted(value)
    try:
        back = yaml.safe_load(text)
    except yaml.YAMLError:
        back = None
    if not (isinstance(back, str) and back == value):
        text = _quoted(value)
        try:
            back = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise Unsupported(f"문자열을 YAML로 쓸 수 없음: {exc}") from None
        if back != value:
            raise Unsupported("문자열을 YAML로 쓸 수 없음")
    return text


def _float_check(value: Any) -> None:
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        raise Unsupported("유한하지 않은 숫자")


def flow_text(value: Any) -> str:
    """새 값을 한 줄(flow)로. 중괄호 안쪽에 한 칸을 둔다(`{ a: b }`)."""
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "{ " + ", ".join(f"{scalar_text(k)}: {flow_text(v)}" for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(flow_text(v) for v in value) + "]"
    return scalar_text(value)


def _inline(value: Any) -> str | None:
    """컬렉션을 한 줄로 쓸 수 있으면 그 글, 길면 None."""
    text = flow_text(value)
    return text if len(text) <= INLINE_LIMIT and "\n" not in text else None


def block_text(value: Any, col: int) -> str:
    """새 컬렉션을 block 스타일로. 첫 줄은 들여쓰지 않고(부르는 쪽이 놓는다) 다음 줄부터 col만큼 들여쓴다."""
    pad = " " * col
    lines: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            lines.append(_fresh_entry(scalar_text(k) + ":", v, col, compact=False))
    else:
        for v in value:
            lines.append(_fresh_entry("-", v, col, compact=True))
    return ("\n" + pad).join(lines)


def _fresh_entry(head: str, value: Any, col: int, *, compact: bool, force_block: bool = False) -> str:
    """`키:` 또는 `-` 뒤에 새 값을 붙인 항목(첫 줄은 들여쓰지 않는다)."""
    if _is_scalar(value):
        return f"{head} {scalar_text(value)}"
    if not value:
        return f"{head} {flow_text(value)}"
    inline = None if force_block else _inline(value)
    if inline is not None:
        return f"{head} {inline}"
    if compact and isinstance(value, dict):          # `- 키: 값` (다음 키는 col + 2)
        return f"{head} {block_text(value, col + 2)}"
    return f"{head}\n{' ' * (col + 2)}{block_text(value, col + 2)}"


# ---------------------------------------------------------------- 줄 도구

def _shift(text: str, delta: int, *, first: bool = False) -> str:
    """여러 줄 글의 들여쓰기를 delta만큼 옮긴다(first가 아니면 첫 줄은 그대로)."""
    if delta == 0 or (not first and "\n" not in text):
        return text
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if (i == 0 and not first) or not line.strip():
            continue
        if delta > 0:
            lines[i] = " " * delta + line
        else:
            if line[:-delta].strip(" "):
                raise Unsupported("들여쓰기를 줄일 수 없는 줄")
            lines[i] = line[-delta:]
    return "\n".join(lines)


def _split_trivia(trivia: str) -> tuple[str, str]:
    """(자리에 남는 맨 앞 빈 줄들, 항목을 따라가는 나머지)."""
    lines = trivia.split("\n")[:-1] if trivia else []
    n = 0
    while n < len(lines) and not lines[n].strip():
        n += 1
    sep = "".join(line + "\n" for line in lines[:n])
    above = "".join(line + "\n" for line in lines[n:])
    return sep, above


# ---------------------------------------------------------------- 읽기

class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.loader = yaml.SafeLoader(text)

    def eol(self, index: int) -> int:
        i = self.text.find("\n", index)
        return len(self.text) if i < 0 else i

    def line_start(self, index: int) -> int:
        return self.text.rfind("\n", 0, index) + 1

    def root(self) -> tuple[TMap, Origin]:
        try:
            node = self.loader.get_single_node()
        except yaml.YAMLError as exc:
            raise Unsupported(f"YAML을 읽을 수 없음: {exc}") from None
        finally:
            self.loader.dispose()
        if not isinstance(node, yaml.MappingNode) or node.flow_style or node.start_mark.column != 0:
            raise Unsupported("최상위가 block 매핑이 아님")
        value, origin = self.build(node)
        return value, origin

    def build(self, node: yaml.Node) -> tuple[Any, Origin]:
        text = self.text
        if isinstance(node, yaml.ScalarNode):
            if node.tag == "tag:yaml.org,2002:merge":
                raise Unsupported("병합 키(<<)")
            value = self.loader.construct_object(node, deep=True)
            start = node.start_mark.index
            end = start + len(text[start:node.end_mark.index].rstrip())
            return value, Origin("scalar", True, start, end, node.start_mark.column, value)
        is_map = isinstance(node, yaml.MappingNode)
        container: Any = TMap() if is_map else TSeq()
        origin = Origin("map" if is_map else "seq", bool(node.flow_style), node.start_mark.index,
                        node.end_mark.index, node.start_mark.column)
        container.origin = origin
        prev_end = -1
        for i, item in enumerate(node.value):
            key_node, value_node = item if is_map else (None, item)
            key = None
            if is_map:
                if not isinstance(key_node, yaml.ScalarNode) or key_node.tag == "tag:yaml.org,2002:merge":
                    raise Unsupported("스칼라가 아닌 키 또는 병합 키")
                key = self.loader.construct_object(key_node, deep=True)
                try:
                    duplicate = key in container
                except TypeError:
                    raise Unsupported("키로 쓸 수 없는 값") from None
                if duplicate:
                    raise Unsupported(f"같은 키가 두 번 있음: {key!r}")
            value, vo = self.build(value_node)
            if origin.flow:
                head = key_node.start_mark.index if is_map else vo.start
                entry = Entry(origin, key, head, key_node.end_mark.index if is_map else head, vo)
            else:
                entry = self._block_entry(origin, i, key, key_node, vo, value_node, prev_end)
                prev_end = entry.body_end
            vo.entry = entry
            origin.entries.append(entry)
            if is_map:
                container[key] = value
            else:
                container.append(value)
        if not origin.flow:
            if not origin.entries:
                raise Unsupported("빈 block 컬렉션")
            origin.end = origin.entries[-1].body_end
        origin.snapshot = plain(container)
        return container, origin

    def _block_entry(self, origin: Origin, i: int, key: Any, key_node: yaml.Node | None, vo: Origin,
                     value_node: yaml.Node, prev_end: int) -> Entry:
        text = self.text
        col = origin.column
        if key_node is not None:
            head = key_node.start_mark.index
            head_end = key_node.end_mark.index
        elif i == 0:
            head = origin.start
            head_end = head + 1
        else:
            head = self._find_dash(prev_end + 1, vo.start, col)
            head_end = head + 1
        if key_node is None and text[head:head + 1] != "-":
            raise Unsupported("시퀀스 항목의 `-`를 찾지 못함")
        entry = Entry(origin, key, head, head_end, vo, body_end=self.eol(vo.end))
        if i > 0:
            ls = self.line_start(head)
            if text[ls:head].strip(" ") or head - ls != col or prev_end + 1 > ls:
                raise Unsupported("항목이 줄 머리에서 시작하지 않음")
            entry.trivia = text[prev_end + 1:ls]
        if vo.block and text.count("\n", head_end, vo.start):
            # `키:` 줄과 값 사이의 줄(빈 줄, 주석)은 값의 첫 항목 앞의 줄로 본다
            entry.own_line = True
            entry.head_line_end = self.eol(head_end)
            first = vo.entries[0]
            fls = self.line_start(first.head_start)
            if text[fls:first.head_start].strip(" ") or entry.head_line_end + 1 > fls:
                raise Unsupported("값이 줄 머리에서 시작하지 않음")
            first.trivia = text[entry.head_line_end + 1:fls]
        return entry

    def _find_dash(self, begin: int, limit: int, col: int) -> int:
        text = self.text
        ls = begin
        while ls <= limit:
            if text[ls:ls + col].strip(" ") == "" and len(text[ls:ls + col]) == col and text[ls + col:ls + col + 1] == "-" \
                    and text[ls + col + 1:ls + col + 2] in ("", " ", "\n"):
                return ls + col
            nl = text.find("\n", ls)
            if nl < 0:
                break
            ls = nl + 1
        raise Unsupported("시퀀스 항목의 `-`를 찾지 못함")


# ---------------------------------------------------------------- 쓰기

class _Renderer:
    def __init__(self, text: str, force: bool = False):
        self.text = text
        self.force = force           # 바뀐 것이 없어도 조각을 다시 이어 붙인다(조각내기가 빠짐없는지 확인하는 용도)
        self.used: set[int] = set()

    def _claim(self, entry: Entry) -> bool:
        if id(entry) in self.used:
            return False
        self.used.add(id(entry))
        return True

    # ---- 항목 찾기

    def _match(self, value: Any, origin: Origin) -> list[tuple[Any, Any, Entry | None]]:
        """새 컬렉션의 (키, 값, 원문 항목 또는 None) 목록."""
        out: list[tuple[Any, Any, Entry | None]] = []
        if origin.kind == "map":
            by_key = {}
            for e in origin.entries:
                by_key[(type(e.key), e.key)] = e
            for k, v in value.items():
                e = by_key.get((type(k), k))
                if e is not None:
                    foreign = isinstance(v, (TMap, TSeq)) and v.origin is not None and v.origin is not e.value
                    if foreign or not self._claim(e):
                        e = None
                out.append((k, v, e))
            return out
        scalars = [e for e in origin.entries if e.value.kind == "scalar"]
        for i, v in enumerate(value):
            e = None
            if isinstance(v, (TMap, TSeq)):
                if v.origin is not None and v.origin.entry is not None and v.origin.entry.parent.kind == "seq":
                    e = v.origin.entry
            elif _is_scalar(v):
                same_slot = origin.entries[i] if i < len(origin.entries) else None
                candidates = ([same_slot] if same_slot is not None and same_slot.value.kind == "scalar" else []) + scalars
                e = next((c for c in candidates if id(c) not in self.used and strict_equal(v, c.value.snapshot)), None)
            if e is not None and not self._claim(e):
                e = None
            out.append((None, v, e))
        return out

    # ---- block

    def block(self, value: Any, origin: Origin, col: int) -> tuple[str, str]:
        """block 컬렉션 → (첫 항목 앞에 놓을 줄들, 첫 항목부터의 글). 둘째 줄부터는 col에 맞춘다."""
        text = self.text
        delta = col - origin.column
        if not self.force and strict_equal(plain(value), origin.snapshot):
            for e in origin.entries:
                self.used.add(id(e))
            return _shift(origin.entries[0].trivia, delta, first=True), _shift(text[origin.start:origin.end], delta)
        if not value:
            raise Unsupported("block 컬렉션이 비게 됨")
        seps = [_split_trivia(e.trivia)[0] for e in origin.entries]
        block_items = any(e.value.block for e in origin.entries)
        chunks: list[tuple[str, str]] = []
        for j, (key, val, e) in enumerate(self._match(value, origin)):
            sep = seps[j] if j < len(seps) else (seps[-1] if len(seps) > 1 else "")
            if e is None:
                head = (scalar_text(key) + ":") if origin.kind == "map" else "-"
                force_block = origin.kind == "seq" and block_items and isinstance(val, dict)
                chunks.append((sep, _fresh_entry(head, plain(val), col, compact=origin.kind == "seq",
                                                 force_block=force_block)))
                continue
            if e.parent.flow:        # flow 시퀀스에 있던 항목을 block 시퀀스로 옮김
                chunks.append((sep, "- " + self.inline(val, e.value)))
                continue
            shift = col - e.parent.column
            above = _shift(_split_trivia(e.trivia)[1], shift, first=True)
            chunks.append((sep + above, self.entry(val, e, shift)))
        pad = " " * col
        body = chunks[0][1] + "".join("\n" + above + pad + chunk for above, chunk in chunks[1:])
        return chunks[0][0], body

    def entry(self, val: Any, e: Entry, shift: int) -> str:
        """block 항목 하나의 몸통(첫 줄은 들여쓰지 않는다)."""
        text = self.text
        vo = e.value
        if strict_equal(plain(val), vo.snapshot) and not (self.force and vo.block):
            return _shift(text[e.head_start:e.body_end], shift)
        tracked = isinstance(val, (TMap, TSeq)) and val.origin is vo
        if tracked and vo.block and not val and e.own_line:
            # 항목이 모두 없어진 컬렉션: `키: []`. 키 줄의 주석은 남긴다
            key_text = text[e.head_start:e.key_end] + (":" if e.parent.kind == "map" else "")
            return key_text + (" {}" if vo.kind == "map" else " []") + text[e.head_start + len(key_text):e.head_line_end]
        if tracked and vo.block:
            child_col = vo.column + shift
            above, body = self.block(val, vo, child_col)
            if e.own_line:
                return text[e.head_start:e.head_line_end] + "\n" + above + " " * child_col + body
            if above:
                raise Unsupported("같은 줄에서 시작하는 컬렉션의 첫 항목 앞에 주석을 둘 수 없음")
            return text[e.head_start:vo.start] + body
        if "\n" in text[vo.start:vo.end] and tracked:
            raise Unsupported("여러 줄 flow 컬렉션 안의 수정")
        head = text[e.head_start:vo.start]
        tail = text[vo.end:e.body_end]
        key_text = text[e.head_start:e.key_end] + (":" if e.parent.kind == "map" else "")
        if tracked and vo.kind == "seq" and e.parent.kind == "map" and "\n" not in head:
            # 한 줄 시퀀스(`키: [a, b]`)에 block 항목이 들어오거나 새 항목으로 줄이 너무 길어지면 여러 줄로 푼다
            self._check_flow(vo)
            matched = self._match(val, vo)
            line = None
            if not any(m is not None and m.value.block for _k, _v, m in matched):
                line = self._join_flow(vo, matched)
                fresh = any(m is None and not _is_scalar(v) for _k, v, m in matched)
                if fresh and len(line) > INLINE_LIMIT:
                    line = None
            if line is not None:
                return head + line + tail
            return key_text + tail + self._unfold(matched, e.parent.column + shift + 2)
        if tracked:
            return head + self.inline(val, vo) + tail
        # 값을 새로 쓴다. 키(또는 `-`)는 원문 그대로 둔다
        if _is_scalar(val) and vo.kind == "scalar" and "\n" not in text[e.head_start:vo.end]:
            if vo.start == vo.end and not head.endswith(" "):
                head += " "
            return head + scalar_text(val) + tail          # 같은 줄의 주석은 남는다
        if _is_scalar(val):
            return f"{key_text} {scalar_text(val)}"
        # 값의 종류가 바뀜(스칼라 → 컬렉션 등)
        return _fresh_entry(key_text, plain(val), e.parent.column + shift, compact=e.parent.kind == "seq")

    # ---- flow

    def _unfold(self, matched: list[tuple[Any, Any, Entry | None]], col: int) -> str:
        """한 줄 시퀀스의 항목들을 block 시퀀스의 줄들로(각 줄 앞에 줄바꿈)."""
        pad = " " * col
        out = ""
        for _key, v, m in matched:
            if m is None:
                out += "\n" + pad + _fresh_entry("-", plain(v), col, compact=True)
            elif m.parent.block:
                shift = col - m.parent.column
                out += "\n" + _shift(_split_trivia(m.trivia)[1], shift, first=True) + pad + self.entry(v, m, shift)
            else:
                out += "\n" + pad + "- " + self.inline(v, m.value)
        return out

    def _check_flow(self, origin: Origin) -> None:
        whole = self.text[origin.start:origin.end]
        if origin.block:
            raise Unsupported("block 컬렉션을 flow 자리로 옮길 수 없음")
        if "\n" in whole:
            raise Unsupported("여러 줄 flow 컬렉션 안의 수정")
        if not (whole.startswith(("{", "[")) and whole.endswith(("}", "]"))):
            raise Unsupported("괄호 없는 flow 매핑 안의 수정")

    def inline(self, val: Any, origin: Origin) -> str:
        """flow 컬렉션 또는 스칼라 자리에 놓을 한 줄 글."""
        if strict_equal(plain(val), origin.snapshot):
            return self.text[origin.start:origin.end]
        if not (isinstance(val, (TMap, TSeq)) and val.origin is origin):
            return flow_text(plain(val))
        self._check_flow(origin)
        return self._join_flow(origin, self._match(val, origin))

    def _join_flow(self, origin: Origin, matched: list[tuple[Any, Any, Entry | None]]) -> str:
        text = self.text
        parts: list[str] = []
        for key, v, e in matched:
            if e is None:
                parts.append(flow_text(plain(v)) if origin.kind == "seq" else f"{scalar_text(key)}: {flow_text(plain(v))}")
            elif origin.kind == "seq":
                if e.value.block:
                    raise Unsupported("block 항목을 flow 시퀀스로 옮길 수 없음")
                parts.append(self.inline(v, e.value))
            else:
                head = text[e.head_start:e.value.start]
                if e.value.start == e.value.end and not head.endswith(" "):
                    head += " "
                parts.append(head + self.inline(v, e.value))
        if not parts:
            return "{}" if origin.kind == "map" else "[]"
        if origin.entries:
            first, last = origin.entries[0], origin.entries[-1]
            opening, closing = text[origin.start:first.head_start], text[last.value.end:origin.end]
            if "#" in opening or "#" in closing or closing.strip() not in ("}", "]"):
                raise Unsupported("flow 컬렉션의 괄호 주변을 해석하지 못함")
        else:
            opening, closing = ("{ ", " }") if origin.kind == "map" else ("[", "]")
        return opening + ", ".join(parts) + closing


class Document:
    """YAML 글 하나. `root`를 고친 뒤 `render()`로 새 글을 얻는다."""

    def __init__(self, text: str):
        if "\r" in text or text.startswith("\ufeff"):
            raise Unsupported("CR 줄바꿈 또는 BOM")
        for event in _events(text):
            if getattr(event, "anchor", None) is not None or isinstance(event, yaml.AliasEvent):
                raise Unsupported("앵커·별칭")
        self.text = text
        self.root, self._origin = _Parser(text).root()
        # 조각내기가 원문을 빠짐없이 덮는지 확인한다: 바꾸지 않고 다시 이어 붙이면 원문과 같아야 한다
        if self._assemble(force=True) != text:
            raise Unsupported("원문을 조각으로 나눌 수 없음")

    def _assemble(self, force: bool = False) -> str:
        if not isinstance(self.root, TMap) or self.root.origin is not self._origin:
            raise Unsupported("최상위가 바뀜")
        o = self._origin
        above, body = _Renderer(self.text, force).block(self.root, o, o.column)
        return self.text[:o.start] + above + body + self.text[o.end:]

    def render(self) -> str:
        return self._assemble()


def _events(text: str):
    try:
        yield from yaml.parse(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError as exc:
        raise Unsupported(f"YAML을 읽을 수 없음: {exc}") from None
