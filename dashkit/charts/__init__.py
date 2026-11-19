"""차트 유형 레지스트리(보강설계서 4-1). 축약 유형 하나 = dashkit/charts/<viz>.yaml 하나.

이 파일들에서 나오는 것:
1. dashboard.schema.json의 viz enum과 유형별 정의(extend_schema, 로딩 때 합성)
2. Superset params(build_params)
3. 열·지표 참조 수집(column_refs, metric_refs)
4. contracts/chart-shorthand.md의 유형별 대응표(shorthand_table)

유형 파일 형식:

    viz: <dashboard.yaml에서 쓰는 이름>
    viz_type: <Superset 이름>
    label: <표시 이름>
    fields:                      # 선언 순서 = params에 쓰는 순서
      <필드>: { kind, required, param, default, values, emit, min, max, min_items, description }
    defaults: { <파라미터>: <값> }   # 항상 함께 쓰는 파라미터(필드 뒤에 쓴다)
    hook: <모듈>:<함수>           # 단순 대응이 안 될 때. 함수(chart) -> params가 필드 대응을 대신한다
    schema: { … }                # 유형 정의에 더할 JSON Schema 조각(예: oneOf)
    notes: <대응표에 덧붙일 설명>

params에 쓰는 순서: viz_type → 필드(d3format 제외, 선언 순서) 또는 hook 결과 → defaults → d3format 필드 → where.
dashboard.yaml의 params는 호출하는 쪽(bundle.chart_params)이 마지막에 덮어쓴다.

이 모듈은 dashkit.config를 import하지 않는다(config가 이 모듈을 쓴다).
"""
from __future__ import annotations

import copy
import importlib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

CHARTS_DIR = Path(__file__).resolve().parent

# kind → 값의 JSON Schema(계약의 $defs를 가리킨다)
_REF = "#/$defs/"
KIND_SCHEMAS: dict[str, dict] = {
    "column": {"$ref": _REF + "column"},
    "columns": {"type": "array", "items": {"$ref": _REF + "column"}},
    "metric": {"$ref": _REF + "metric_ref"},
    "metrics": {"type": "array", "minItems": 1, "items": {"$ref": _REF + "metric_ref"}},
    "time_grain": {"$ref": _REF + "time_grain"},
    "order_by": {"$ref": _REF + "order_by"},
    "d3format": {"$ref": _REF + "format"},
    "bool": {"type": "boolean"},
    "int": {"type": "integer"},
    "string": {"type": "string"},
    "enum": {},
}
COLUMN_KINDS = ("column", "columns", "order_by")
METRIC_KINDS = ("metric", "metrics")
# 모든 차트에 공통인 필드(유형 파일의 fields에 쓸 수 없다)
COMMON_FIELDS = ("key", "title", "dataset", "viz", "params", "description", "where")
_FIELD_KEYS = {"kind", "required", "param", "default", "values", "emit", "min", "max", "min_items", "description"}
_TYPE_KEYS = {"viz", "viz_type", "label", "fields", "defaults", "hook", "schema", "notes"}


class RegistryError(Exception):
    """유형 파일이 잘못됨(템플릿 코드의 오류)."""


@dataclass(frozen=True)
class Field:
    name: str
    kind: str
    required: bool = False
    param: str | None = None          # 생략하면 필드 이름
    has_default: bool = False
    default: Any = None
    values: Mapping[Any, Any] | None = None   # 값 → 파라미터 값(bool은 대응표, enum은 허용 값 목록)
    emit: bool = True                 # False면 스키마에서만 받고 params에는 쓰지 않는다
    min: int | None = None
    max: int | None = None
    min_items: int | None = None
    description: str | None = None

    @property
    def param_name(self) -> str:
        return self.param or self.name

    def schema(self) -> dict:
        s: dict[str, Any] = {}
        if self.description:
            s["description"] = self.description
        s.update(copy.deepcopy(KIND_SCHEMAS[self.kind]))
        if self.kind == "enum":
            s["enum"] = list(self.values or [])
        if self.min is not None:
            s["minimum"] = self.min
        if self.max is not None:
            s["maximum"] = self.max
        if self.min_items is not None:
            s["minItems"] = self.min_items
        return s


@dataclass(frozen=True)
class ChartType:
    viz: str
    viz_type: str
    label: str
    fields: tuple[Field, ...]
    defaults: Mapping[str, Any] = field(default_factory=dict)
    hook: Callable[[Mapping[str, Any]], dict] | None = None
    hook_ref: str | None = None
    extra_schema: Mapping[str, Any] = field(default_factory=dict)
    notes: str | None = None

    def schema(self) -> dict:
        props: dict[str, Any] = {k: True for k in COMMON_FIELDS if k != "where"}
        props["where"] = {"$ref": _REF + "where"}
        for f in self.fields:
            props[f.name] = f.schema()
        out: dict[str, Any] = {"type": "object"}
        required = [f.name for f in self.fields if f.required]
        if required:
            out["required"] = required
        out["additionalProperties"] = False
        out["properties"] = props
        out.update(copy.deepcopy(dict(self.extra_schema)))
        return out


# ---------------------------------------------------------------- 로딩

def _load_hook(ref: str, source: str) -> Callable[[Mapping[str, Any]], dict]:
    try:
        module, func = ref.split(":", 1)
        return getattr(importlib.import_module(module), func)
    except (ValueError, ImportError, AttributeError) as exc:
        raise RegistryError(f"{source}: hook '{ref}'를 불러오지 못함: {exc}") from exc


def _parse_type(doc: Any, source: str) -> ChartType:
    if not isinstance(doc, dict):
        raise RegistryError(f"{source}: 매핑이 아님")
    unknown = set(doc) - _TYPE_KEYS
    if unknown:
        raise RegistryError(f"{source}: 알 수 없는 키 {sorted(unknown)}")
    for k in ("viz", "viz_type", "label"):
        if not isinstance(doc.get(k), str) or not doc[k]:
            raise RegistryError(f"{source}: '{k}'가 필요함")
    fields = []
    for name, spec in (doc.get("fields") or {}).items():
        where = f"{source}: fields.{name}"
        if name in COMMON_FIELDS or name == "viz_type":
            raise RegistryError(f"{where}: 공통 필드 이름은 쓸 수 없음")
        if not isinstance(spec, dict) or spec.get("kind") not in KIND_SCHEMAS:
            raise RegistryError(f"{where}: kind는 {sorted(KIND_SCHEMAS)} 중 하나여야 함")
        bad = set(spec) - _FIELD_KEYS
        if bad:
            raise RegistryError(f"{where}: 알 수 없는 키 {sorted(bad)}")
        if spec["kind"] == "enum" and not spec.get("values"):
            raise RegistryError(f"{where}: enum은 values가 필요함")
        fields.append(Field(
            name=name, kind=spec["kind"], required=bool(spec.get("required", False)), param=spec.get("param"),
            has_default="default" in spec, default=spec.get("default"), values=spec.get("values"),
            emit=bool(spec.get("emit", True)), min=spec.get("min"), max=spec.get("max"),
            min_items=spec.get("min_items"), description=spec.get("description"),
        ))
    hook_ref = doc.get("hook")
    return ChartType(
        viz=doc["viz"], viz_type=doc["viz_type"], label=doc["label"], fields=tuple(fields),
        defaults=dict(doc.get("defaults") or {}), hook=_load_hook(hook_ref, source) if hook_ref else None,
        hook_ref=hook_ref, extra_schema=dict(doc.get("schema") or {}), notes=doc.get("notes"),
    )


def load_registry(directory: Path) -> dict[str, ChartType]:
    """directory/*.yaml을 읽는다(파일 이름순). 파일 이름은 viz와 같아야 한다."""
    out: dict[str, ChartType] = {}
    for path in sorted(directory.glob("*.yaml")):
        with open(path, encoding="utf-8") as f:
            t = _parse_type(yaml.safe_load(f), path.name)
        if t.viz != path.stem:
            raise RegistryError(f"{path.name}: viz '{t.viz}'가 파일 이름과 다름")
        out[t.viz] = t
    return out


@lru_cache(maxsize=1)
def registry() -> dict[str, ChartType]:
    return load_registry(CHARTS_DIR)


# ---------------------------------------------------------------- 스키마 합성

def extend_schema(schema: dict, types: Mapping[str, ChartType] | None = None) -> dict:
    """dashboard.schema.json에 viz enum, 유형별 if/then, v_<viz> 정의를 채운다(schema를 고쳐서 돌려준다)."""
    types = registry() if types is None else types
    defs = schema["$defs"]
    chart = defs["chart"]
    chart["properties"]["viz"] = {**chart["properties"]["viz"], "enum": list(types)}
    conditions = [
        {"if": {"required": ["viz"], "properties": {"viz": {"const": viz}}}, "then": {"$ref": f"{_REF}v_{viz}"}}
        for viz in types
    ]
    chart["allOf"] = conditions + list(chart.get("allOf") or [])
    for viz, t in types.items():
        defs[f"v_{viz}"] = t.schema()
    return schema


# ---------------------------------------------------------------- params

def metric_param(ref: Any) -> Any:
    """지표 참조: 문자열은 데이터셋 지표 이름 그대로, {sql, label}은 adhoc SQL 지표."""
    if isinstance(ref, str):
        return ref
    return {"expressionType": "SQL", "sqlExpression": ref["sql"], "label": ref["label"]}


def metric_label(ref: Any) -> str:
    return ref if isinstance(ref, str) else ref["label"]


def _convert(f: Field, value: Any) -> Any:
    if f.kind == "metric":
        return metric_param(value)
    if f.kind == "metrics":
        return [metric_param(m) for m in value]
    if f.kind == "columns":
        return list(value)
    if f.kind == "bool":
        value = bool(value)
        return f.values[value] if f.values else value
    if f.kind == "enum" and isinstance(f.values, Mapping):
        return f.values[value]
    return copy.deepcopy(value)


def _emit(f: Field, chart: Mapping[str, Any], params: dict) -> None:
    if not f.emit:
        return
    value = chart.get(f.name)
    if value is None:
        if not f.has_default:
            return
        value = f.default
    params[f.param_name] = _convert(f, value)


def build_params(chart: Mapping[str, Any]) -> dict:
    """축약 차트 → Superset params(dashboard.yaml의 params 덮어쓰기 전)."""
    t = registry()[chart["viz"]]
    params: dict[str, Any] = {"viz_type": t.viz_type}
    if t.hook:
        params.update(t.hook(chart))
    else:
        for f in t.fields:
            if f.kind != "d3format":
                _emit(f, chart, params)
    for key, value in t.defaults.items():
        params.setdefault(key, copy.deepcopy(value))
    for f in t.fields:
        if f.kind == "d3format":
            _emit(f, chart, params)
    if chart.get("where"):
        params["adhoc_filters"] = [{"expressionType": "SQL", "clause": "WHERE", "sqlExpression": chart["where"]}]
    return params


# ---------------------------------------------------------------- 참조 수집

def metric_refs(chart: Mapping[str, Any]) -> list[tuple[list, str]]:
    """차트가 이름으로 참조한 데이터셋 지표 (필드 경로, 이름)."""
    out: list[tuple[list, str]] = []
    for f in registry()[chart["viz"]].fields:
        value = chart.get(f.name)
        if f.kind == "metric" and isinstance(value, str):
            out.append(([f.name], value))
        elif f.kind == "metrics":
            out += [([f.name, i], m) for i, m in enumerate(value or []) if isinstance(m, str)]
    return out


def column_refs(chart: Mapping[str, Any]) -> list[tuple[list, str]]:
    """차트가 참조한 뷰 열 (필드 경로, 열). order_by에서 지표 라벨은 뺀다."""
    t = registry()[chart["viz"]]
    labels: set[str] = set()
    for f in t.fields:
        value = chart.get(f.name)
        if f.kind == "metric" and value is not None:
            labels.add(metric_label(value))
        elif f.kind == "metrics":
            labels |= {metric_label(m) for m in value or []}
    out: list[tuple[list, str]] = []
    for f in t.fields:
        value = chart.get(f.name)
        if value is None:
            continue
        if f.kind == "column":
            out.append(([f.name], value))
        elif f.kind == "columns":
            out += [([f.name, j], col) for j, col in enumerate(value)]
        elif f.kind == "order_by":
            out += [([f.name, j, 0], col) for j, (col, _direction) in enumerate(value) if col not in labels]
    return out


# ---------------------------------------------------------------- 대응표

def _code(value: Any) -> str:
    import json
    return "`" + json.dumps(value, ensure_ascii=False) + "`"


def shorthand_table(types: Mapping[str, ChartType] | None = None) -> str:
    """contracts/chart-shorthand.md의 '유형별' 표(마크다운)."""
    types = registry() if types is None else types
    lines = [
        "| 축약 `viz` | 이름 | `viz_type` | 축약 필드 → 파라미터 | 함께 쓰는 파라미터 |",
        "|---|---|---|---|---|",
    ]
    for viz, t in types.items():
        cells = []
        for f in t.fields:
            target = f"`{f.param_name}`" if f.emit and not t.hook else ("쓰지 않음" if not f.emit else "설명 참고")
            if f.kind == "bool" and f.values:
                target += " (" + ", ".join(f"{str(k).lower()} → {_code(v)}" for k, v in f.values.items()) + ")"
            marks = [f.kind] + (["필수"] if f.required else []) + ([f"생략 시 {_code(f.default)}"] if f.has_default else [])
            cells.append(f"`{f.name}` → {target} ({', '.join(marks)})")
        extra = [f"`{k}: {_code(v)[1:-1]}`" for k, v in t.defaults.items()]
        if t.notes:
            cells.append(t.notes.strip().replace("\n", " ").replace("|", "\\|"))
        lines.append(f"| `{viz}` | {t.label} | `{t.viz_type}` | " + "<br>".join(cells) + " | " + (", ".join(extra) or "—") + " |")
    return "\n".join(lines) + "\n"
