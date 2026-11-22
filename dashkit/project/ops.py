"""설정 변경 연산(`PATCH …/config`의 `ops`). 계약: contracts/studio.md "변경 연산".

연산 하나는 `(doc, 연산 dict) → None` 함수다. doc은 dashboard.yaml의 내용(dict/list처럼 쓰는 값)이고 함수가 그 자리에서 고친다.
같은 함수가 세 가지 doc에 쓰인다: 보통 dict(기대값 계산), 원문 위치를 기억하는 TMap/TSeq(yamledit), ruamel의 CommentedMap/Seq.
그래서 **항목을 옮길 때는 복사하지 않고 같은 객체를 pop → insert**한다(주석과 원문이 객체를 따라간다).

연산을 더하려면 함수 하나를 쓰고 `@op("이름")`을 붙인다. 그 밖에 고칠 곳은 없다.
`layout.set`처럼 값 전체를 받는 연산도 같은 원칙을 따른다: 받은 값으로 통째로 갈아 끼우지 않고, 같은 행·항목은
있던 객체를 다시 쓴다(그래야 바뀌지 않은 행의 글자가 남는다).
형식이 틀리거나 대상이 없으면 OpError를 던진다. 설정이 유효한지는 여기서 보지 않는다(저장 전에 load_project가 본다).
"""
from __future__ import annotations

import copy
from typing import Any, Callable, Mapping, MutableMapping, MutableSequence

from ..config import DASHBOARD_FILE, SCHEMA_FILE

__all__ = ["OPS", "OpError", "apply_ops", "op"]


class OpError(Exception):
    """연산의 형식이 틀리거나 대상이 없음. index는 ops 배열에서의 자리(apply_ops가 채운다)."""

    def __init__(self, reason: str, index: int | None = None, name: str | None = None):
        self.reason = reason
        self.index = index
        self.name = name
        super().__init__(reason)

    def __str__(self) -> str:
        where = f"ops[{self.index}]" if self.index is not None else "ops"
        return f"{where}{f' {self.name}' if self.name else ''}: {self.reason}"


OpFn = Callable[[MutableMapping, Mapping[str, Any]], None]
OPS: dict[str, OpFn] = {}
OP_FILES: dict[str, str] = {}


def op(name: str, file: str = DASHBOARD_FILE) -> Callable[[OpFn], OpFn]:
    """연산을 등록한다. file은 이 연산이 고치는 설정 파일이다."""
    def register(fn: OpFn) -> OpFn:
        if name in OPS:
            raise ValueError(f"연산 이름이 겹침: {name}")
        OPS[name] = fn
        OP_FILES[name] = file
        return fn
    return register


def check_ops(ops: Any) -> list[Mapping[str, Any]]:
    """요청의 ops 형식을 본다(배열, 항목마다 등록된 `op` 이름)."""
    if not isinstance(ops, list) or not ops:
        raise OpError("ops는 비어 있지 않은 배열이어야 함")
    for i, item in enumerate(ops):
        if not isinstance(item, dict) or not isinstance(item.get("op"), str):
            raise OpError("연산은 `op` 이름이 있는 객체여야 함", i)
        if item["op"] not in OPS:
            raise OpError(f"모르는 연산 '{item['op']}'", i)
    return ops


def apply_ops(doc: MutableMapping, ops: list[Mapping[str, Any]], file: str = DASHBOARD_FILE) -> None:
    """file을 고치는 연산을 순서대로 doc에 적용한다. 실패하면 몇 번째 연산인지 담은 OpError."""
    for i, item in enumerate(ops):
        name = item["op"]
        if OP_FILES[name] != file:
            continue
        try:
            OPS[name](doc, item)
        except OpError as exc:
            raise OpError(exc.reason, i, name) from None


def files_of(ops: list[Mapping[str, Any]]) -> list[str]:
    """연산들이 고치는 파일(나온 순서, 중복 없음)."""
    return list(dict.fromkeys(OP_FILES[item["op"]] for item in ops))


# ---------------------------------------------------------------- 인자 읽기

def _fields(item: Mapping[str, Any], required: tuple[str, ...], optional: tuple[str, ...] = ()) -> None:
    for name in required:
        if name not in item:
            raise OpError(f"`{name}`이 필요함")
    extra = sorted(set(item) - set(required) - set(optional) - {"op"})
    if extra:
        raise OpError(f"모르는 필드: {', '.join(extra)}")


def _text(item: Mapping[str, Any], name: str) -> str:
    value = item[name]
    if not isinstance(value, str) or not value:
        raise OpError(f"`{name}`은 비어 있지 않은 문자열이어야 함")
    return value


def _index(item: Mapping[str, Any], name: str, size: int, *, insert: bool = False) -> int:
    """0 이상의 정수. insert면 맨 뒤(size)도 된다."""
    value = item[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise OpError(f"`{name}`은 정수여야 함")
    limit = size if insert else size - 1
    if value < 0 or value > limit:
        raise OpError(f"`{name}` {value}가 범위(0~{limit})를 벗어남")
    return value


def _object(item: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = item[name]
    if not isinstance(value, dict):
        raise OpError(f"`{name}`은 객체여야 함")
    if any(not isinstance(k, str) for k in value):
        raise OpError(f"`{name}`의 키는 문자열이어야 함")
    return value


def _tabs(doc: MutableMapping) -> MutableSequence:
    tabs = doc.get("tabs")
    if not isinstance(tabs, list):
        raise OpError("dashboard.yaml에 tabs 목록이 없음")
    return tabs


def _tab_at(doc: MutableMapping, key: str) -> tuple[MutableSequence, int]:
    tabs = _tabs(doc)
    for i, tab in enumerate(tabs):
        if isinstance(tab, dict) and tab.get("key") == key:
            return tabs, i
    raise OpError(f"탭 '{key}'가 없음")


def _sections(doc: MutableMapping, key: str) -> MutableSequence:
    tabs, i = _tab_at(doc, key)
    sections = tabs[i].get("sections")
    if not isinstance(sections, list):
        raise OpError(f"탭 '{key}'에 sections 목록이 없음")
    return sections


def _set_values(target: MutableMapping, values: Mapping[str, Any], *, allow_remove: bool) -> None:
    """얕은 병합: 최상위 키만 바꾼다. null은 그 키를 지운다(allow_remove일 때)."""
    for k, v in values.items():
        if v is None:
            if not allow_remove:
                raise OpError(f"`{k}`는 지울 수 없음")
            if k in target:
                del target[k]
        else:
            target[k] = copy.deepcopy(v)


# ---------------------------------------------------------------- 5a: 앱

APP_KEYS = ("title", "logo_text", "as_of", "theme")


@op("app.update")
def app_update(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("values",))
    values = _object(item, "values")
    extra = sorted(set(values) - set(APP_KEYS))
    if extra:
        raise OpError(f"app에서 바꿀 수 없는 키: {', '.join(extra)}")
    app = doc.get("app")
    if not isinstance(app, dict):
        raise OpError("dashboard.yaml에 app이 없음")
    _set_values(app, values, allow_remove=True)


# ---------------------------------------------------------------- 5a: 탭

TAB_KEYS = ("key", "label", "icon")


@op("tab.add")
def tab_add(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("tab",), ("index",))
    tab = _object(item, "tab")
    extra = sorted(set(tab) - set(TAB_KEYS))
    missing = [k for k in TAB_KEYS if k not in tab]
    if extra or missing:
        raise OpError("`tab`은 key, label, icon만 가짐" + (f"(없음: {', '.join(missing)})" if missing else "")
                      + (f"(모르는 키: {', '.join(extra)})" if extra else ""))
    tabs = _tabs(doc)
    if any(isinstance(t, dict) and t.get("key") == tab["key"] for t in tabs):
        raise OpError(f"탭 '{tab['key']}'가 이미 있음")
    index = _index(item, "index", len(tabs), insert=True) if "index" in item else len(tabs)
    tabs.insert(index, {**{k: copy.deepcopy(tab[k]) for k in TAB_KEYS}, "sections": []})


@op("tab.update")
def tab_update(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("key", "values"))
    values = _object(item, "values")
    extra = sorted(set(values) - {"label", "icon"})
    if extra:
        raise OpError(f"탭에서 바꿀 수 없는 키: {', '.join(extra)}")
    tabs, i = _tab_at(doc, _text(item, "key"))
    _set_values(tabs[i], values, allow_remove=False)


@op("tab.move")
def tab_move(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("key", "index"))
    tabs, i = _tab_at(doc, _text(item, "key"))
    index = _index(item, "index", len(tabs))       # 옮긴 뒤의 자리(원래 자리에서 뺀 뒤 넣을 자리)
    if index != i:
        tabs.insert(index, tabs.pop(i))


@op("tab.remove")
def tab_remove(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("key",))
    tabs, i = _tab_at(doc, _text(item, "key"))
    tabs.pop(i)


# ---------------------------------------------------------------- 5a: 섹션

@op("section.add")
def section_add(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("tab", "section"), ("index",))
    section = _object(item, "section")
    if not isinstance(section.get("type"), str) or not section["type"]:
        raise OpError("`section.type`이 필요함")
    sections = _sections(doc, _text(item, "tab"))
    index = _index(item, "index", len(sections), insert=True) if "index" in item else len(sections)
    # type을 맨 앞에 두고, null인 옵션은 쓰지 않는다
    new = {"type": section["type"], **{k: copy.deepcopy(v) for k, v in section.items() if k != "type" and v is not None}}
    sections.insert(index, new)


@op("section.update")
def section_update(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("tab", "index", "values"))
    values = _object(item, "values")
    if "type" in values:
        raise OpError("섹션의 type은 바꿀 수 없음(지우고 새로 추가)")
    sections = _sections(doc, _text(item, "tab"))
    section = sections[_index(item, "index", len(sections))]
    if not isinstance(section, dict):
        raise OpError("섹션이 객체가 아님")
    _set_values(section, values, allow_remove=True)


@op("section.move")
def section_move(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("tab", "index", "to_index"), ("to_tab",))
    source = _sections(doc, _text(item, "tab"))
    index = _index(item, "index", len(source))
    target = _sections(doc, _text(item, "to_tab")) if item.get("to_tab") is not None else source
    if target is source:
        to_index = _index(item, "to_index", len(source))            # 옮긴 뒤의 자리
        if to_index != index:
            source.insert(to_index, source.pop(index))
    else:
        to_index = _index(item, "to_index", len(target), insert=True)
        target.insert(to_index, source.pop(index))


@op("section.remove")
def section_remove(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("tab", "index"))
    sections = _sections(doc, _text(item, "tab"))
    sections.pop(_index(item, "index", len(sections)))


# ---------------------------------------------------------------- 5b: 대시보드

DASHBOARD_KEYS = ("title", "cross_filters")


def _dashboard(doc: MutableMapping, item: Mapping[str, Any]) -> MutableMapping:
    name = _text(item, "dashboard")
    dashboards = doc.get("dashboards")
    if not isinstance(dashboards, dict):
        raise OpError("dashboard.yaml에 dashboards가 없음")
    found = dashboards.get(name)
    if not isinstance(found, dict):
        raise OpError(f"대시보드 '{name}'가 없음")
    return found


def _whole(item: Mapping[str, Any], name: str) -> int:
    value = item[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise OpError(f"`{name}`은 정수여야 함")
    return value


def _keyed(entries: Any, key: str) -> int | None:
    """목록에서 `key`가 같은 항목의 자리."""
    if isinstance(entries, list):
        for i, entry in enumerate(entries):
            if isinstance(entry, dict) and entry.get("key") == key:
                return i
    return None


def _new_keyed(value: Mapping[str, Any], first: tuple[str, ...]) -> dict[str, Any]:
    """새 항목: first의 키를 앞에 두고, null인 필드는 쓰지 않는다."""
    out = {k: copy.deepcopy(value[k]) for k in first if value.get(k) is not None}
    out.update({k: copy.deepcopy(v) for k, v in value.items() if k not in first and v is not None})
    return out


@op("dashboard.update")
def dashboard_update(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("dashboard", "values"))
    values = _object(item, "values")
    extra = sorted(set(values) - set(DASHBOARD_KEYS))
    if extra:
        raise OpError(f"대시보드에서 바꿀 수 없는 키: {', '.join(extra)}")
    _set_values(_dashboard(doc, item), values, allow_remove=True)


# ---------------------------------------------------------------- 5b: 차트

def _chart_at(doc: MutableMapping, key: str) -> tuple[MutableMapping, MutableSequence, int]:
    """차트 키는 프로젝트 전체에서 유일하다 → (대시보드, 그 charts, 자리)."""
    dashboards = doc.get("dashboards")
    for d in (dashboards.values() if isinstance(dashboards, dict) else ()):
        if isinstance(d, dict):
            i = _keyed(d.get("charts"), key)
            if i is not None:
                return d, d["charts"], i
    raise OpError(f"차트 '{key}'가 없음")


def _item_chart(entry: Any) -> Any:
    """layout 항목이 가리키는 차트 키(차트 항목이 아니면 None)."""
    if isinstance(entry, str):
        return entry
    return entry.get("chart") if isinstance(entry, dict) else None


@op("chart.add")
def chart_add(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("dashboard", "chart"), ("row", "index", "width"))
    chart = _object(item, "chart")
    key = chart.get("key")
    if not isinstance(key, str) or not key:
        raise OpError("`chart.key`가 필요함")
    d = _dashboard(doc, item)
    charts, layout = d.get("charts"), d.get("layout")
    if not isinstance(charts, list) or not isinstance(layout, list):
        raise OpError("대시보드에 charts·layout 목록이 없음")
    try:
        _chart_at(doc, key)
    except OpError:
        pass
    else:
        raise OpError(f"차트 '{key}'가 이미 있음")
    placed: Any = key
    if "width" in item:
        placed = {"chart": key, "width": _whole(item, "width")}
    if "row" in item:
        row = layout[_index(item, "row", len(layout))]
        if not isinstance(row, list):
            raise OpError(f"`row` {item['row']}는 행이 아님(대시보드 안 탭 묶음). 탭 속 행에는 layout.set을 쓴다")
        at = _index(item, "index", len(row), insert=True) if "index" in item else len(row)
        charts.append(_new_keyed(chart, ("key", "title")))
        row.insert(at, placed)
    else:
        if "index" in item:
            raise OpError("`index`는 `row`와 함께 써야 함")
        charts.append(_new_keyed(chart, ("key", "title")))
        layout.append([placed])


@op("chart.update")
def chart_update(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("key", "values"))
    values = _object(item, "values")
    if "key" in values:
        raise OpError("차트의 key는 바꿀 수 없음(지우고 새로 추가)")
    _d, charts, i = _chart_at(doc, _text(item, "key"))
    _set_values(charts[i], values, allow_remove=True)


def _drop_from_layout(entries: MutableSequence, key: str) -> None:
    """layout(또는 탭의 rows)에서 차트 항목을 뺀다. 그래서 비게 된 행, 행이 없어진 탭, 탭이 없어진 묶음도 지운다."""
    for r in range(len(entries) - 1, -1, -1):
        entry = entries[r]
        if isinstance(entry, list):
            hits = [c for c, placed in enumerate(entry) if _item_chart(placed) == key]
            for c in reversed(hits):
                entry.pop(c)
            if hits and not entry:
                entries.pop(r)
        elif isinstance(entry, dict) and isinstance(entry.get("tabs"), list) and entry["tabs"]:
            tabs = entry["tabs"]
            for t in range(len(tabs) - 1, -1, -1):
                rows = tabs[t].get("rows") if isinstance(tabs[t], dict) else None
                if isinstance(rows, list) and rows:
                    _drop_from_layout(rows, key)
                    if not rows:
                        tabs.pop(t)
            if not tabs:
                entries.pop(r)


@op("chart.remove")
def chart_remove(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("key",))
    key = _text(item, "key")
    d, charts, i = _chart_at(doc, key)
    charts.pop(i)
    if isinstance(d.get("layout"), list):
        _drop_from_layout(d["layout"], key)
    for flt in (d.get("filters") if isinstance(d.get("filters"), list) else ()):
        scope = flt.get("charts") if isinstance(flt, dict) else None
        if isinstance(scope, list) and key in scope:
            for c in range(len(scope) - 1, -1, -1):
                if scope[c] == key:
                    scope.pop(c)
            if not scope:
                del flt["charts"]


# ---------------------------------------------------------------- 5b: 배치

def _same(a: Any, b: Any) -> bool:
    """값이 같은가(키 순서 포함). 문자열·숫자의 하위 유형(ruamel)은 값으로 비교하고 true와 1, 1과 1.0은 구분한다."""
    if isinstance(a, dict) or isinstance(b, dict):
        return isinstance(a, dict) and isinstance(b, dict) and list(a) == list(b) and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) or isinstance(b, list):
        return isinstance(a, list) and isinstance(b, list) and len(a) == len(b) and all(map(_same, a, b))
    if isinstance(a, bool) != isinstance(b, bool) or isinstance(a, float) != isinstance(b, float):
        return False
    return bool(a == b)


def _is(a: Any, b: Any) -> bool:
    """같은 객체(컬렉션)이거나 같은 값(스칼라)인가."""
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a is b
    return _same(a, b)


def _set_items(seq: MutableSequence, targets: list) -> None:
    """seq의 내용을 targets로 바꾼다. 이미 제자리에 있는 것은 건드리지 않고, 있던 객체는 pop → insert로 옮긴다."""
    for i, target in enumerate(targets):
        if i < len(seq) and _is(seq[i], target):
            continue
        j = next((j for j in range(i + 1, len(seq)) if _is(seq[j], target)), None)
        seq.insert(i, seq.pop(j) if j is not None else target)
    while len(seq) > len(targets):
        seq.pop(len(seq) - 1)


def _sync_map(target: MutableMapping, new: Mapping[str, Any], skip: tuple[str, ...] = ()) -> None:
    """target을 new와 같은 내용으로(skip의 키는 부르는 쪽이 맞춘다). 있던 키의 순서는 그대로, 새 키는 뒤에 붙는다."""
    for k in [k for k in target if k not in new]:
        del target[k]
    for k, v in new.items():
        if k not in skip and not (k in target and _same(target[k], v)):
            target[k] = copy.deepcopy(v)


def _ident(placed: Any) -> tuple[str, Any]:
    """layout 항목이 "무엇"인지(너비·높이를 뺀 것). 같은 것끼리 짝지어 원문을 다시 쓴다."""
    if isinstance(placed, dict):
        for kind in ("chart", "header", "markdown"):
            if isinstance(placed.get(kind), str):
                return kind, str(placed[kind])
        return "other", None
    return "chart", str(placed) if isinstance(placed, str) else None


def _check_rows(rows: Any, where: str, *, nested: bool) -> None:
    if not isinstance(rows, list):
        raise OpError(f"{where}은 배열이어야 함")
    for r, entry in enumerate(rows):
        at = f"{where}[{r}]"
        if isinstance(entry, list):
            for c, placed in enumerate(entry):
                if not isinstance(placed, str) and not (isinstance(placed, dict) and all(isinstance(k, str) for k in placed)):
                    raise OpError(f"{at}[{c}]은 차트 키 문자열 또는 객체여야 함")
        elif isinstance(entry, dict) and not nested and isinstance(entry.get("tabs"), list):
            for t, tab in enumerate(entry["tabs"]):
                if not isinstance(tab, dict):
                    raise OpError(f"{at}.tabs[{t}]은 객체({{label, rows}})여야 함")
                _check_rows(tab.get("rows"), f"{at}.tabs[{t}].rows", nested=True)
        elif nested:
            raise OpError(f"{at}은 행(항목 배열)이어야 함(탭 안에 탭을 둘 수 없음)")
        else:
            raise OpError(f"{at}은 행(항목 배열) 또는 {{tabs: […]}}여야 함")


def _tab_groups(entries: Any) -> list:
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("tabs"), list)]


def _all_rows(entries: Any) -> list:
    """layout의 모든 행(최상위와 탭 속), 위에서 아래 순서."""
    out: list = []
    for entry in entries:
        if isinstance(entry, list):
            out.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("tabs"), list):
            for tab in entry["tabs"]:
                if isinstance(tab, dict) and isinstance(tab.get("rows"), list):
                    out += [row for row in tab["rows"] if isinstance(row, list)]
    return out


def _reconcile_layout(old: MutableSequence, new: list) -> None:
    """old(파일의 layout)를 new(요청의 layout)와 같은 내용으로 고친다. 같은 행·항목은 있던 객체를 다시 쓴다.

    1. 내용이 같은 행은 그 객체를 그대로 쓴다(자리만 옮긴다).
    2. 남은 행은 항목(차트·제목·글)을 가장 많이 공유하는 옛 행을 골라 그 안에서 항목만 고친다.
       차트가 없는 행(제목·글)은 같은 꼴의 옛 행과 차례로 짝짓는다(글자만 고친 경우).
    3. 짝이 없는 행·항목만 새로 쓴다.
    """
    old_rows, new_rows = _all_rows(old), _all_rows(new)
    old_idents = [[_ident(p) for p in row] for row in old_rows]
    new_idents = [[_ident(p) for p in row] for row in new_rows]
    pair: dict[int, int] = {}                 # 새 행 번호 → 옛 행 번호
    exact: set[int] = set()
    taken: set[int] = set()
    for n, row in enumerate(new_rows):
        o = next((o for o, cand in enumerate(old_rows) if o not in taken and _same(cand, row)), None)
        if o is not None:
            pair[n] = o
            exact.add(n)
            taken.add(o)
    scores = sorted(
        (-len(set(new_idents[n]) & set(old_idents[o])), n, o)
        for n in range(len(new_rows)) if n not in pair
        for o in range(len(old_rows)) if o not in taken)
    for score, n, o in scores:
        if score < 0 and n not in pair and o not in taken:
            pair[n] = o
            taken.add(o)
    for n in range(len(new_rows)):
        kinds = [kind for kind, _v in new_idents[n]]
        if n in pair or "chart" in kinds:
            continue
        o = next((o for o in range(len(old_rows)) if o not in taken and [k for k, _v in old_idents[o]] == kinds), None)
        if o is not None:
            pair[n] = o
            taken.add(o)

    # 옛 항목(객체) 가운데 다시 쓸 수 있는 것: 무엇인지 → 객체들. 내용이 같은 제목·글이 여러 행에 있을 수 있으므로
    # 객체를 하나로 합치지 않고, 그대로 두는 행(exact)의 객체는 후보에서 뺀다(다른 행이 가져가 고치면 안 된다).
    kept_rows = {pair[n] for n in exact}
    pool: dict[tuple[str, Any], list[Any]] = {}
    for o, row in enumerate(old_rows):
        if o in kept_rows:
            continue
        for placed in row:
            if isinstance(placed, dict):
                pool.setdefault(_ident(placed), []).append(placed)
    wanted = {ident for idents in new_idents for ident in idents}

    def take(ident: tuple[str, Any], own: list[Any]) -> Any:
        """ident인 옛 객체 하나를 후보에서 꺼낸다. 짝지은 옛 행(own)에 있던 객체를 먼저 쓴다. 객체는 한 번만 쓰인다."""
        candidates = pool.get(ident) or []
        found = next((c for c in candidates if any(c is x for x in own)), None)
        if found is None and candidates:
            found = candidates[0]
        if found is not None:
            pool[ident] = [c for c in candidates if c is not found]
        return found

    def item_for(placed: Any, own: list[Any]) -> Any:
        if not isinstance(placed, dict):
            return copy.deepcopy(placed)
        ident = _ident(placed)
        found = take(ident, own)
        if found is None and ident[0] in ("header", "markdown"):
            # 글자를 고친 제목·글: 같은 행에 있던 같은 종류의 항목(새 layout에는 없는 것)을 고쳐 쓴다
            stale = next((_ident(x) for x in own if isinstance(x, dict) and _ident(x)[0] == ident[0]
                          and _ident(x) not in wanted and any(x is c for c in pool.get(_ident(x)) or [])), None)
            found = take(stale, own) if stale is not None else None
        if found is None:
            return copy.deepcopy(placed)
        _sync_map(found, placed)
        return found

    row_no = {id(row): n for n, row in enumerate(new_rows)}

    def row_for(row: list) -> Any:
        n = row_no[id(row)]
        if n not in pair:
            return copy.deepcopy(row)
        kept = old_rows[pair[n]]
        if n not in exact:
            _set_items(kept, [item_for(placed, list(kept)) for placed in row])
        return kept

    def tab_for(tab: Mapping[str, Any], kept: Any) -> Any:
        rows = [row_for(row) for row in tab["rows"]]
        if kept is None or not isinstance(kept.get("rows"), list):
            return {**{k: copy.deepcopy(v) for k, v in tab.items() if k != "rows"}, "rows": rows}
        _sync_map(kept, tab, skip=("rows",))
        _set_items(kept["rows"], rows)
        return kept

    def group_for(group: Mapping[str, Any], kept: Any) -> Any:
        old_tabs = [t for t in kept["tabs"] if isinstance(t, dict)] if kept is not None else []
        labels = [t.get("label") for t in group["tabs"]]
        free = list(old_tabs)
        chosen: list[Any] = [None] * len(group["tabs"])
        for t, tab in enumerate(group["tabs"]):                 # 라벨이 같은 탭
            hit = next((x for x in free if _same(x.get("label"), tab.get("label"))), None)
            if hit is not None:
                chosen[t] = hit
                free = [x for x in free if x is not hit]
        for t in range(len(chosen)):                              # 같은 자리의 탭(라벨을 고친 경우)
            here = old_tabs[t] if t < len(old_tabs) else None
            if chosen[t] is None and here is not None and any(here is x for x in free) \
                    and not any(_same(here.get("label"), label) for label in labels):
                chosen[t] = here
                free = [x for x in free if x is not here]
        tabs = [tab_for(tab, chosen[t]) for t, tab in enumerate(group["tabs"])]
        if kept is None:
            return {**{k: copy.deepcopy(v) for k, v in group.items() if k != "tabs"}, "tabs": tabs}
        _sync_map(kept, group, skip=("tabs",))
        _set_items(kept["tabs"], tabs)
        return kept

    old_groups = _tab_groups(old)
    targets: list[Any] = []
    g = 0
    for entry in new:
        if isinstance(entry, list):
            targets.append(row_for(entry))
        else:
            targets.append(group_for(entry, old_groups[g] if g < len(old_groups) else None))
            g += 1
    _set_items(old, targets)


@op("layout.set")
def layout_set(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("dashboard", "layout"))
    d = _dashboard(doc, item)
    new = item["layout"]
    _check_rows(new, "`layout`", nested=False)
    if isinstance(d.get("layout"), list):
        _reconcile_layout(d["layout"], new)
    else:
        d["layout"] = copy.deepcopy(new)


# ---------------------------------------------------------------- 5b: 필터

def _filter_at(d: MutableMapping, key: str) -> tuple[MutableSequence, int]:
    filters = d.get("filters")
    i = _keyed(filters, key)
    if i is None:
        raise OpError(f"필터 '{key}'가 없음")
    return filters, i


@op("filter.add")
def filter_add(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("dashboard", "filter"), ("index",))
    flt = _object(item, "filter")
    key = flt.get("key")
    if not isinstance(key, str) or not key:
        raise OpError("`filter.key`가 필요함")
    d = _dashboard(doc, item)
    if "filters" in d and not isinstance(d["filters"], list):
        raise OpError("대시보드의 filters가 목록이 아님")
    filters = d["filters"] if "filters" in d else []
    if _keyed(filters, key) is not None:
        raise OpError(f"필터 '{key}'가 이미 있음")
    index = _index(item, "index", len(filters), insert=True) if "index" in item else len(filters)
    filters.insert(index, _new_keyed(flt, ("key", "label")))
    if "filters" not in d:
        d["filters"] = filters          # 필터가 없던 대시보드: `filters` 키를 새로 쓴다


@op("filter.update")
def filter_update(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("dashboard", "key", "values"))
    values = _object(item, "values")
    if "key" in values:
        raise OpError("필터의 key는 바꿀 수 없음(지우고 새로 추가)")
    filters, i = _filter_at(_dashboard(doc, item), _text(item, "key"))
    _set_values(filters[i], values, allow_remove=True)


@op("filter.move")
def filter_move(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("dashboard", "key", "index"))
    filters, i = _filter_at(_dashboard(doc, item), _text(item, "key"))
    index = _index(item, "index", len(filters))        # 옮긴 뒤의 자리
    if index != i:
        filters.insert(index, filters.pop(i))


@op("filter.remove")
def filter_remove(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("dashboard", "key"))
    key = _text(item, "key")
    d = _dashboard(doc, item)
    filters, i = _filter_at(d, key)
    children = [f.get("key") for f in filters if isinstance(f, dict) and f.get("parent") == key and f.get("key") != key]
    if children:
        raise OpError(f"필터 '{key}'를 parent로 가리키는 필터가 있음({', '.join(map(str, children))}). 먼저 그 parent를 지워야 함")
    filters.pop(i)
    if not filters:
        del d["filters"]                # 마지막 필터를 지우면 `filters` 키도 지운다


# ---------------------------------------------------------------- schema.yaml: 열(데이터 운용, contracts/ingest.md)

# 열 연산이 바꿀 수 있는 키(설계서 6-2 표). 유형은 넓히기만(integer → number, 무엇이든 → text) 받는다. 규칙을 좁히면 옛
# 배치가 검증에서 걸리고, 그것은 적용(load)이 파일·줄로 알린다
COLUMN_UPDATE_KEYS = ("aliases", "description", "type", "values", "min", "max", "pattern", "default", "required", "sensitive")
_WIDER = {("integer", "number"), ("integer", "text"), ("number", "text"), ("boolean", "text"), ("date", "text"),
          ("timestamp", "text"), ("enum", "text")}
TABLE_KEYS = ("description", "mode", "on_conflict", "primary_key", "columns", "checks", "freshness")
TABLE_UPDATE_KEYS = ("description", "mode", "on_conflict", "freshness")
COLUMN_KEYS = ("type", "required", "values", "ref", "min", "max", "pattern", "description", "default", "aliases", "sensitive")


def _schema_tables(doc: MutableMapping) -> MutableMapping:
    tables = doc.get("tables")
    if not isinstance(tables, dict):
        raise OpError("schema.yaml에 tables가 없음")
    return tables


def _schema_column(doc: MutableMapping, table: str, column: str) -> MutableMapping:
    tables = _schema_tables(doc)
    if not isinstance(tables.get(table), dict):
        raise OpError(f"테이블 '{table}'이 없음")
    columns = tables[table].get("columns")
    if not isinstance(columns, dict) or not isinstance(columns.get(column), dict):
        raise OpError(f"테이블 '{table}'에 열 '{column}'이 없음")
    return columns[column]


def _column_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    bad = sorted(set(value) - set(COLUMN_KEYS))
    if bad or not isinstance(value.get("type"), str):
        raise OpError("열 정의는 type이 있고 " + ", ".join(COLUMN_KEYS) + "만 가짐"
                      + (f"(모르는 키: {', '.join(bad)})" if bad else ""))
    # type을 맨 앞에, null은 쓰지 않는다
    return {"type": value["type"], **{k: copy.deepcopy(v) for k, v in value.items() if k != "type" and v is not None}}


@op("table.add", SCHEMA_FILE)
def table_add(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    """{op, table, value: {mode?, on_conflict?, primary_key, columns: {열: {type, …}}, description?, checks?}}."""
    _fields(item, ("table", "value"))
    name = _text(item, "table")
    value = _object(item, "value")
    bad = sorted(set(value) - set(TABLE_KEYS))
    if bad or "primary_key" not in value or not isinstance(value.get("columns"), dict) or not value["columns"]:
        raise OpError("`value`는 primary_key와 columns가 있고 " + ", ".join(TABLE_KEYS) + "만 가짐")
    tables = _schema_tables(doc)
    if name in tables:
        raise OpError(f"테이블 '{name}'이 이미 있음")
    new: dict[str, Any] = {}
    for k in ("description", "mode", "on_conflict", "freshness", "primary_key"):
        if value.get(k) is not None:
            new[k] = copy.deepcopy(value[k])
    new["columns"] = {str(c): _column_spec(_object({"c": spec}, "c")) for c, spec in value["columns"].items()}
    if value.get("checks"):
        new["checks"] = copy.deepcopy(value["checks"])
    tables[name] = new


@op("table.update", SCHEMA_FILE)
def table_update(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    """{op, table, set: {description?, mode?, on_conflict?}}. null은 그 키를 지운다(방식 변경 포함)."""
    _fields(item, ("table", "set"))
    values = _object(item, "set")
    bad = sorted(set(values) - set(TABLE_UPDATE_KEYS))
    if bad:
        raise OpError(f"바꿀 수 없는 키: {', '.join(bad)}")
    tables = _schema_tables(doc)
    name = _text(item, "table")
    if not isinstance(tables.get(name), dict):
        raise OpError(f"테이블 '{name}'이 없음")
    _set_values(tables[name], values, allow_remove=True)


@op("table.remove", SCHEMA_FILE)
def table_remove(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("table",))
    tables = _schema_tables(doc)
    name = _text(item, "table")
    if name not in tables:
        raise OpError(f"테이블 '{name}'이 없음")
    del tables[name]


@op("column.add", SCHEMA_FILE)
def column_add(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    """{op, table, column, value: {type, required?, default?, …}}. 맨 뒤에 더한다. replace 테이블의 CSV에 열을 더하는 일은
    구조 변경 작업이 함께 한다(dashkit/ingest/structure.py)."""
    _fields(item, ("table", "column", "value"))
    tables = _schema_tables(doc)
    name, col = _text(item, "table"), _text(item, "column")
    if not isinstance(tables.get(name), dict) or not isinstance(tables[name].get("columns"), dict):
        raise OpError(f"테이블 '{name}'이 없음")
    if col in tables[name]["columns"]:
        raise OpError(f"열 '{col}'이 이미 있음")
    spec = _column_spec(_object(item, "value"))
    if spec.get("required") and "default" not in spec:
        raise OpError("필수 열을 더할 때는 default가 있어야 함(이미 있는 행이 검증에서 걸린다)")
    tables[name]["columns"][col] = spec


@op("column.update", SCHEMA_FILE)
def column_update(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    """{op, table, column, set: {aliases?, description?, type?, values?, min?, max?, pattern?, default?, required?}}.
    null은 그 키를 지운다. 유형은 넓히기만 받는다."""
    _fields(item, ("table", "column", "set"))
    values = _object(item, "set")
    bad = sorted(set(values) - set(COLUMN_UPDATE_KEYS))
    if bad:
        raise OpError(f"바꿀 수 없는 키: {', '.join(bad)}")
    current = _schema_column(doc, _text(item, "table"), _text(item, "column"))
    if "type" in values and values["type"] != current.get("type") and \
            (current.get("type"), values["type"]) not in _WIDER:
        raise OpError(f"유형은 넓히기만 할 수 있음({current.get('type')} → {values['type']}는 안 됨)")
    if "aliases" in values and values["aliases"] is not None and (
            not isinstance(values["aliases"], list) or not all(isinstance(a, str) and a for a in values["aliases"])):
        raise OpError("`aliases`는 비어 있지 않은 문자열의 목록이어야 함")
    _set_values(_schema_column(doc, _text(item, "table"), _text(item, "column")), values, allow_remove=True)



# ---------------------------------------------------------------- dashboard.yaml: 데이터셋·대시보드(데이터 운용)

DATASET_KEYS = ("table", "sql", "labels", "metrics", "description")


def _datasets(doc: MutableMapping) -> MutableMapping:
    datasets = doc.get("datasets")
    if not isinstance(datasets, dict):
        raise OpError("dashboard.yaml에 datasets가 없음")
    return datasets


@op("dataset.add")
def dataset_add(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    """{op, key, value: {table | sql, labels?, metrics?, description?}}."""
    _fields(item, ("key", "value"))
    key = _text(item, "key")
    value = _object(item, "value")
    bad = sorted(set(value) - set(DATASET_KEYS))
    if bad or ("table" in value) == ("sql" in value):
        raise OpError("`value`는 table과 sql 가운데 하나를 갖고 " + ", ".join(DATASET_KEYS) + "만 가짐")
    datasets = _datasets(doc)
    if key in datasets:
        raise OpError(f"데이터셋 '{key}'가 이미 있음")
    datasets[key] = _new_keyed(value, ("table", "sql", "description", "labels", "metrics"))


@op("dataset.update")
def dataset_update(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    """{op, key, set: {sql?, labels?, metrics?, description?}}. null은 그 키를 지운다."""
    _fields(item, ("key", "set"))
    values = _object(item, "set")
    bad = sorted(set(values) - {"sql", "labels", "metrics", "description"})
    if bad:
        raise OpError(f"바꿀 수 없는 키: {', '.join(bad)}")
    datasets = _datasets(doc)
    key = _text(item, "key")
    if not isinstance(datasets.get(key), dict):
        raise OpError(f"데이터셋 '{key}'가 없음")
    _set_values(datasets[key], values, allow_remove=True)


@op("dataset.remove")
def dataset_remove(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    _fields(item, ("key",))
    datasets = _datasets(doc)
    key = _text(item, "key")
    if key not in datasets:
        raise OpError(f"데이터셋 '{key}'가 없음")
    del datasets[key]


@op("dashboard.add")
def dashboard_add(doc: MutableMapping, item: Mapping[str, Any]) -> None:
    """{op, key, value: {title, charts, layout, filters?, cross_filters?}}. 새 테이블의 초안 그래프(suggest_dashboard)가 쓴다."""
    _fields(item, ("key", "value"))
    key = _text(item, "key")
    value = _object(item, "value")
    if not isinstance(value.get("title"), str) or not isinstance(value.get("charts"), list) \
            or not isinstance(value.get("layout"), list):
        raise OpError("`value`는 title, charts, layout이 필요함")
    dashboards = doc.get("dashboards")
    if not isinstance(dashboards, dict):
        raise OpError("dashboard.yaml에 dashboards가 없음")
    if key in dashboards:
        raise OpError(f"대시보드 '{key}'가 이미 있음")
    dashboards[key] = _new_keyed(value, ("title", "charts", "layout", "filters", "cross_filters"))
