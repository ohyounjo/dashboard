"""위젯 매니페스트 레지스트리(보강설계서 4-2). 위젯 유형 하나 = web/widgets/<유형>/ 폴더 하나.

폴더 안의 widget.json(매니페스트)에서 나오는 것:
1. dashboard.schema.json의 섹션 유형 enum, 유형별 if/then, s_<유형> 정의(extend_schema, 로딩 때 합성)
2. 섹션 선택 필드의 기본값 채움(fill_defaults, 스키마의 default)
3. 열 참조 수집(column_refs, "x-column" 표시)
4. 유형별 검사(check, "x-unique"·"x-date-range" 표시)
5. /api/data 허용 목록에 들어갈 데이터셋(dataset 옵션), /api/app의 위젯 유형 목록(type, label)

매니페스트 형식과 표시의 정의는 contracts/widgets.md에 있다.
매니페스트 위치는 환경변수 WIDGETS_DIR, 없으면 저장소의 web/widgets다. 프로세스마다 처음 쓸 때 한 번 읽는다.

이 모듈은 dashkit.config를 import하지 않는다(config가 이 모듈을 쓴다).
"""
from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

MANIFEST_FILE = "widget.json"
_REF = "#/$defs/"
# 모든 섹션에 공통인 키(매니페스트의 options.properties에 쓸 수 없다)
COMMON_KEYS = ("type", "title")
# 위젯이 아닌 섹션 유형(계약 스키마에 직접 정의돼 있다)
RESERVED_TYPES = ("superset",)
_TYPE_RE = re.compile(r"[a-z][a-z0-9_]*")
_MANIFEST_KEYS = {"type", "label", "options"}
DEFAULT_ITEM_NAME = "항목"


class ManifestError(Exception):
    """위젯 매니페스트가 잘못됨(템플릿 코드의 오류)."""


def default_widgets_dir() -> Path:
    env = os.environ.get("WIDGETS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "web" / "widgets"


# ---------------------------------------------------------------- 스키마와 값을 함께 내려가기

def _children(schema: Mapping[str, Any], value: Any, path: list) -> Iterator[tuple[Mapping[str, Any], Any, list]]:
    """스키마 노드의 하위 노드와 그 자리에 있는 값. properties, additionalProperties, items만 따라간다
    ($ref 너머는 보지 않는다: 표시와 default는 매니페스트 안에 직접 적은 것만 유효하다)."""
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for k, sub in props.items():
            if isinstance(sub, dict) and k in value:
                yield sub, value[k], path + [k]
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            for k, v in value.items():
                if k not in props:
                    yield extra, v, path + [k]
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, v in enumerate(value):
            yield schema["items"], v, path + [i]


def _walk(schema: Mapping[str, Any], value: Any, path: list) -> Iterator[tuple[Mapping[str, Any], Any, list]]:
    yield schema, value, path
    for sub, v, p in _children(schema, value, path):
        yield from _walk(sub, v, p)


def _fill(schema: Mapping[str, Any], value: Any) -> None:
    if isinstance(value, dict):
        for k, sub in (schema.get("properties") or {}).items():
            if isinstance(sub, dict) and "default" in sub and k not in value:
                value[k] = copy.deepcopy(sub["default"])
    for sub, v, _ in _children(schema, value, []):
        _fill(sub, v)


def _schema_nodes(schema: Mapping[str, Any], path: str) -> Iterator[tuple[Mapping[str, Any], str]]:
    """매니페스트 검사용: 값 없이 스키마 노드만 훑는다."""
    yield schema, path
    for k, sub in (schema.get("properties") or {}).items():
        if isinstance(sub, dict):
            yield from _schema_nodes(sub, f"{path}.properties.{k}")
    for key in ("additionalProperties", "items"):
        if isinstance(schema.get(key), dict):
            yield from _schema_nodes(schema[key], f"{path}.{key}")


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _unique_spec(mark: Any) -> tuple[str, str]:
    """"x-unique" 값 → (키, 오류 문구에 쓸 항목 이름)."""
    if isinstance(mark, str):
        return mark, DEFAULT_ITEM_NAME
    return mark["key"], mark.get("name") or DEFAULT_ITEM_NAME


# ---------------------------------------------------------------- 유형

@dataclass(frozen=True)
class WidgetType:
    type: str
    label: str
    options: Mapping[str, Any]        # 매니페스트의 options(섹션 옵션 JSON Schema, type·title 제외)

    @property
    def has_dataset(self) -> bool:
        return "dataset" in self.options["properties"]

    def schema(self) -> dict:
        """섹션 스키마(s_<유형>): 공통 키(type, title)와 additionalProperties: false를 더한 것."""
        out = copy.deepcopy(dict(self.options))
        out["type"] = "object"
        out["additionalProperties"] = False
        out["required"] = ["type", *out.get("required", [])]
        out["properties"] = {
            "type": True,
            "title": {"type": "string", "default": None},
            **out["properties"],
        }
        return out

    def fill_defaults(self, section: dict) -> None:
        """설정에 없는 키를 스키마의 default로 채운다(section을 고친다)."""
        _fill(self.schema(), section)

    def column_refs(self, section: Mapping[str, Any]) -> list[tuple[list, str]]:
        """섹션이 참조한 열 (섹션 기준 경로, 열 이름). "x-column" 표시가 있는 자리의 문자열 값."""
        return [
            (path, value)
            for node, value, path in _walk(self.options, section, [])
            if node.get("x-column") and isinstance(value, str) and value
        ]

    def check(self, section: Mapping[str, Any]) -> list[tuple[list, str]]:
        """표시로 선언한 검사의 오류 (섹션 기준 경로, 이유)."""
        errors: list[tuple[list, str]] = []
        for node, value, path in _walk(self.options, section, []):
            if "x-unique" in node and isinstance(value, list):
                key, name = _unique_spec(node["x-unique"])
                seen = [str(item[key]) for item in value if isinstance(item, dict) and key in item]
                dup = {v for v in seen if seen.count(v) > 1}
                if dup:
                    errors.append((path, f"{name} {key}가 중복됨 ({', '.join(sorted(dup))})"))
            if "x-date-range" in node and isinstance(value, dict) and value:
                first, last = node["x-date-range"]
                a, b = _parse_date(value.get(first)), _parse_date(value.get(last))
                if a is None or b is None:
                    errors.append((path, "날짜 형식 오류"))
                elif a > b:
                    errors.append((path, f"{first}가 {last}보다 늦음"))
        return errors


# ---------------------------------------------------------------- 로딩

def _check_marks(options: Mapping[str, Any], source: str) -> bool:
    """표시의 형식을 확인하고, "x-column"이 하나라도 있으면 True."""
    has_column = False
    for node, path in _schema_nodes(options, "options"):
        if "x-column" in node:
            if node["x-column"] is not True:
                raise ManifestError(f"{source}: {path}: \"x-column\"은 true만 쓸 수 있음")
            has_column = True
        if "x-unique" in node:
            mark = node["x-unique"]
            ok = (isinstance(mark, str) and mark) or (
                isinstance(mark, dict) and isinstance(mark.get("key"), str) and mark["key"]
                and set(mark) <= {"key", "name"} and isinstance(mark.get("name", ""), str)
            )
            if not ok:
                raise ManifestError(f"{source}: {path}: \"x-unique\"는 키 이름 문자열이나 {{key, name}}이어야 함")
        if "x-date-range" in node:
            mark = node["x-date-range"]
            if not (isinstance(mark, list) and len(mark) == 2 and all(isinstance(k, str) and k for k in mark)):
                raise ManifestError(f"{source}: {path}: \"x-date-range\"는 [시작 키, 끝 키]여야 함")
    return has_column


def _parse_manifest(doc: Any, folder: str, source: str) -> WidgetType:
    if not isinstance(doc, dict):
        raise ManifestError(f"{source}: JSON 객체가 아님")
    unknown = set(doc) - _MANIFEST_KEYS
    if unknown:
        raise ManifestError(f"{source}: 알 수 없는 키 {sorted(unknown)}")
    for k in ("type", "label"):
        if not isinstance(doc.get(k), str) or not doc[k].strip():
            raise ManifestError(f"{source}: '{k}'가 필요함(비어 있지 않은 문자열)")
    wtype = doc["type"]
    if not _TYPE_RE.fullmatch(wtype):
        raise ManifestError(f"{source}: type 형식 오류 '{wtype}' (소문자로 시작, 소문자·숫자·_)")
    if wtype in RESERVED_TYPES:
        raise ManifestError(f"{source}: type '{wtype}'은 위젯 이름으로 쓸 수 없음")
    if wtype != folder:
        raise ManifestError(f"{source}: type '{wtype}'이 폴더 이름 '{folder}'와 다름")

    options = doc.get("options")
    if not isinstance(options, dict):
        raise ManifestError(f"{source}: 'options'가 필요함(JSON Schema 객체)")
    if options.get("type") != "object" or not isinstance(options.get("properties"), dict):
        raise ManifestError(f"{source}: options는 type이 \"object\"이고 properties가 있어야 함")
    try:
        Draft202012Validator.check_schema(options)
    except SchemaError as exc:
        raise ManifestError(f"{source}: options가 JSON Schema가 아님: {exc.message}") from exc
    props = options["properties"]
    required = options.get("required", [])
    for k in COMMON_KEYS:
        if k in props or k in required:
            raise ManifestError(f"{source}: options에 공통 키 '{k}'를 쓸 수 없음(셸이 더한다)")
    for k in required:
        if k not in props:
            raise ManifestError(f"{source}: options.required의 '{k}'가 properties에 없음")
    for k, sub in props.items():
        if k not in required and not (isinstance(sub, dict) and "default" in sub):
            raise ManifestError(f"{source}: 선택 옵션 '{k}'에 default가 필요함(설정에 없을 때 채울 값)")
    has_column = _check_marks(options, source)
    if "dataset" in props and "dataset" not in required:
        raise ManifestError(f"{source}: 'dataset' 옵션은 required여야 함")
    if has_column and "dataset" not in props:
        raise ManifestError(f"{source}: \"x-column\"을 쓰려면 'dataset' 옵션이 있어야 함")
    return WidgetType(type=wtype, label=doc["label"].strip(), options=options)


def load_registry(widgets_dir: Path) -> dict[str, WidgetType]:
    """widgets_dir/<유형>/widget.json을 모두 읽는다. 순서는 유형 이름순."""
    if not widgets_dir.is_dir():
        raise ManifestError(f"{widgets_dir}: 위젯 폴더가 없음(환경변수 WIDGETS_DIR로 위치를 바꿀 수 있음)")
    types: dict[str, WidgetType] = {}
    for folder in sorted(p for p in widgets_dir.iterdir() if p.is_dir() and not p.name.startswith(".")):
        path = folder / MANIFEST_FILE
        source = f"{folder.name}/{MANIFEST_FILE}"
        if not path.is_file():
            raise ManifestError(f"{source}: 파일이 없음 ({path})")
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except ValueError as exc:
            raise ManifestError(f"{source}: JSON 문법 오류: {exc}") from exc
        types[folder.name] = _parse_manifest(doc, folder.name, source)
    return types


@lru_cache(maxsize=8)
def _cached(widgets_dir: str) -> dict[str, WidgetType]:
    return load_registry(Path(widgets_dir))


def registry() -> dict[str, WidgetType]:
    """현재 위젯 폴더의 레지스트리. 폴더 경로별로 한 번만 읽는다(매니페스트를 바꾸면 프로세스를 다시 시작한다)."""
    return _cached(str(default_widgets_dir()))


def clear_cache() -> None:
    _cached.cache_clear()


def type_list() -> list[dict[str, str]]:
    """/api/app의 widgets: 셸이 import()를 허용할 유형 목록."""
    return [{"type": t.type, "label": t.label} for t in registry().values()]


# ---------------------------------------------------------------- 스키마 합성

def extend_schema(schema: dict, types: Mapping[str, WidgetType] | None = None) -> dict:
    """dashboard.schema.json의 section에 위젯 유형 enum과 유형별 if/then을, $defs에 s_<유형>을 채운다
    (schema를 고쳐서 돌려준다)."""
    types = registry() if types is None else types
    defs = schema["$defs"]
    section = defs["section"]
    type_prop = section["properties"]["type"]
    section["properties"]["type"] = {**type_prop, "enum": [*type_prop["enum"], *types]}
    section["allOf"] = list(section.get("allOf") or []) + [
        {"if": {"properties": {"type": {"const": t}}}, "then": {"$ref": f"{_REF}s_{t}"}}
        for t in types
    ]
    for t, widget in types.items():
        if f"s_{t}" in defs:
            raise ManifestError(f"{t}/{MANIFEST_FILE}: 계약 스키마에 이미 s_{t} 정의가 있음")
        defs[f"s_{t}"] = widget.schema()
    return schema
