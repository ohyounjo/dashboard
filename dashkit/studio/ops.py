"""설정 변경 연산(`PATCH …/config`의 `ops`). 계약: contracts/studio.md "변경 연산".

연산 하나는 `(doc, 연산 dict) → None` 함수다. doc은 dashboard.yaml의 내용(dict/list처럼 쓰는 값)이고 함수가 그 자리에서 고친다.
같은 함수가 세 가지 doc에 쓰인다: 보통 dict(기대값 계산), 원문 위치를 기억하는 TMap/TSeq(yamledit), ruamel의 CommentedMap/Seq.
그래서 **항목을 옮길 때는 복사하지 않고 같은 객체를 pop → insert**한다(주석과 원문이 객체를 따라간다).

연산을 더하려면(5b: chart.*, layout.set, filter.*) 함수 하나를 쓰고 `@op("이름")`을 붙인다. 그 밖에 고칠 곳은 없다.
형식이 틀리거나 대상이 없으면 OpError를 던진다. 설정이 유효한지는 여기서 보지 않는다(저장 전에 load_project가 본다).
"""
from __future__ import annotations

import copy
from typing import Any, Callable, Mapping, MutableMapping, MutableSequence

from ..config import DASHBOARD_FILE

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
