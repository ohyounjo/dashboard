"""차트 유형 레지스트리(보강설계서 4-1). 축약 유형 하나 = dashkit/charts/<viz>.yaml 하나.

이 파일들에서 나오는 것:
1. dashboard.schema.json의 viz enum과 유형별 정의(extend_schema, 로딩 때 합성)
2. Superset params(build_params)
3. 열·지표 참조 수집(column_refs, metric_refs)
4. 한 쿼리 안 열·지표 라벨 중복 검사(label_conflicts, contracts/superset-notes.md 14절)
5. contracts/chart-shorthand.md의 유형별 대응표(shorthand_table)

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
    query:                       # Superset 화면이 이 유형으로 보내는 쿼리의 모양(중복 라벨 검사가 쓴다)
      groups: [[<필드>, …], …]   # 한 쿼리에 함께 들어가는 열·지표 필드. 생략하면 kind가 column·columns·metric·metrics인
                                 # 필드 전부가 쿼리 하나
      merge_columns: <bool>      # true면 화면이 같은 열을 하나로 합쳐 보낸다(겹쳐도 오류가 아님). 기본 false

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
_TYPE_KEYS = {"viz", "viz_type", "label", "fields", "defaults", "hook", "schema", "notes", "query"}
_QUERY_KEYS = {"groups", "merge_columns"}
# Superset 쿼리에 열·지표 라벨로 들어가는 kind(order_by는 정렬이라 라벨이 아니다)
LABEL_KINDS = ("column", "columns", "metric", "metrics")


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
    values: Mapping[Any, Any] | None = None   # bool: 참·거짓 → 파라미터 값. enum: 허용 값 목록, 또는 값 → 파라미터 값 대응표
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
    query_groups: tuple[tuple[str, ...], ...] = ()   # 한 쿼리에 함께 들어가는 필드 이름(_parse_type이 채운다)
    merge_columns: bool = False                      # Superset 화면이 같은 열을 하나로 합쳐 보내는지

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
    groups, merge_columns = _parse_query(doc.get("query"), fields, source)
    return ChartType(
        viz=doc["viz"], viz_type=doc["viz_type"], label=doc["label"], fields=tuple(fields),
        defaults=dict(doc.get("defaults") or {}), hook=_load_hook(hook_ref, source) if hook_ref else None,
        hook_ref=hook_ref, extra_schema=dict(doc.get("schema") or {}), notes=doc.get("notes"),
        query_groups=groups, merge_columns=merge_columns,
    )


def _parse_query(spec: Any, fields: list[Field], source: str) -> tuple[tuple[tuple[str, ...], ...], bool]:
    """query 선언 → (쿼리별 필드 이름, merge_columns). groups를 생략하면 라벨 kind 필드 전부가 쿼리 하나다."""
    spec = {} if spec is None else spec
    if not isinstance(spec, dict):
        raise RegistryError(f"{source}: query는 매핑이어야 함")
    bad = set(spec) - _QUERY_KEYS
    if bad:
        raise RegistryError(f"{source}: query: 알 수 없는 키 {sorted(bad)}")
    merge = spec.get("merge_columns", False)
    if not isinstance(merge, bool):
        raise RegistryError(f"{source}: query.merge_columns는 true 또는 false여야 함")
    if "groups" not in spec:
        default = tuple(f.name for f in fields if f.kind in LABEL_KINDS)
        return ((default,) if default else ()), merge
    groups = spec["groups"]
    if not isinstance(groups, list) or not groups or not all(isinstance(g, list) and g for g in groups):
        raise RegistryError(f"{source}: query.groups는 필드 이름 목록의 목록이어야 함")
    kinds = {f.name: f.kind for f in fields}
    for g in groups:
        for name in g:
            if kinds.get(name) not in LABEL_KINDS:
                raise RegistryError(f"{source}: query.groups: '{name}'은 kind가 {list(LABEL_KINDS)} 중 하나인 필드가 아님")
    return tuple(tuple(g) for g in groups), merge


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


# ---------------------------------------------------------------- 중복 라벨(contracts/superset-notes.md 14절)

DUPLICATE_LABEL_ERROR = "Duplicate column/metric labels"


def _fmt_path(parts: list) -> str:
    out = ""
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else ("." if out else "") + str(p)
    return out


def _raw_types() -> dict[str, ChartType]:
    """원본(viz_type) 차트를 판단할 수 있는 유형: viz_type이 레지스트리에 하나뿐이고 hook이 없는 유형."""
    seen: dict[str, list[ChartType]] = {}
    for t in registry().values():
        seen.setdefault(t.viz_type, []).append(t)
    return {vt: ts[0] for vt, ts in seen.items() if len(ts) == 1 and ts[0].hook is None}


def _table_label_values(chart: Mapping[str, Any], params: Mapping[str, Any], original: ChartType | None) -> tuple[ChartType, dict[str, tuple[Any, list]]] | None:
    generated = build_params(chart) if original else {"viz_type": chart.get("viz_type")}
    final = {**generated, **params}
    mode = final.get("query_mode")
    if mode not in (None, "raw", "aggregate"):
        return None
    names = (("all_columns",) if mode == "raw" else
             ("groupby", "metrics", "percent_metrics") if mode == "aggregate" else
             ("groupby", "all_columns", "metrics", "percent_metrics"))
    fields = tuple(Field(name, "metrics" if name in ("metrics", "percent_metrics") else "columns") for name in names)
    t = ChartType("table-query", "table", "표", fields, query_groups=(names,), merge_columns=True)
    values = {}
    for name in names:
        value = final.get(name)
        if value is None:
            continue
        if not isinstance(value, list):
            return None
        if name in ("all_columns", "groupby"):
            if any(not isinstance(item, str) for item in value):
                return None
        elif any(not (isinstance(item, str) or (isinstance(item, Mapping)
                     and item.get("expressionType") == "SQL" and isinstance(item.get("sqlExpression"), str)
                     and isinstance(item.get("label"), str))) for item in value):
            return None
        if name in params:
            path = ["params", name]
        elif original and original.viz == "table":
            path = [{"all_columns": "columns"}.get(name, name)]
        elif original and original.hook is None:
            source = next((f for f in original.fields if f.emit and f.param_name == name and chart.get(f.name) is not None), None)
            if source is None:
                continue
            path = [source.name]
        else:
            continue
        values[name] = (value, path)
    return t, values


def _native_label_values(chart: Mapping[str, Any], params: Mapping[str, Any], original: ChartType | None) -> tuple[ChartType, dict[str, tuple[Any, list]]] | None:
    # 갤러리의 문자열 열과 단일 지표 구성만 실측했다.
    generated = build_params(chart) if original else {}
    final = {**generated, **params}
    shapes = {
        "graph_chart": (("source", "target"), ("metric",)),
        "tree_chart": (("id", "parent", "name"), ("metric",)),
        "gantt_chart": (("start_time", "end_time", "y_axis", "series"), ()),
    }
    columns, metrics = shapes[final.get("viz_type", chart.get("viz_type"))]
    if any(not isinstance(final.get(name), str) for name in columns):
        return None
    for name in metrics:
        metric = final.get(name)
        if isinstance(metric, str):
            continue
        if not (isinstance(metric, Mapping) and metric.get("expressionType") == "SQL"
                and isinstance(metric.get("sqlExpression"), str) and isinstance(metric.get("label"), str)):
            return None
    names = (*columns, *metrics)
    fields = tuple(Field(name, "column" if name in columns else "metric") for name in names)
    t = ChartType("native-query", "native-query", "원본", fields, query_groups=(names,), merge_columns=True)
    values = {}
    for name in names:
        if name in params:
            path = ["params", name]
        else:
            source = next((f for f in original.fields if f.emit and f.param_name == name and chart.get(f.name) is not None), None) if original and not original.hook else None
            if source is None:
                return None
            path = [source.name]
        values[name] = (final[name], path)
    return t, values


def _query_hooks() -> dict[str, Callable]:
    table = registry().get("table")
    hooks: dict[str, Callable] = {name: _native_label_values for name in ("graph_chart", "tree_chart", "gantt_chart")}
    if table:
        hooks[table.viz_type] = _table_label_values
    return hooks


def _label_values(chart: Mapping[str, Any]) -> tuple[ChartType, dict[str, tuple[Any, list]]] | None:
    """(유형, {필드: (값, 차트 기준 경로)}). 라벨 kind 필드만.

    최종 viz_type과 params의 얕은 덮어쓰기를 반영하고 원래 설정 경로를 보존한다.
    판단할 수 없는 원본 유형은 None.
    """
    params = chart.get("params") or {}
    original = registry().get(chart.get("viz"))
    actual_viz = params.get("viz_type", original.viz_type if original else chart.get("viz_type"))
    if not isinstance(actual_viz, str):
        return None
    hook = _query_hooks().get(actual_viz)
    if hook is not None:
        return hook(chart, params, original)
    if "viz" in chart:
        original = registry()[chart["viz"]]
        effective_type = params.get("viz_type", original.viz_type)
        t = original if effective_type == original.viz_type else _raw_types().get(effective_type)
        if t is None:
            return None
        if t is not original:
            generated = build_params(chart)
            merged = {**generated, **params}
            values = {}
            for f in t.fields:
                if f.kind in LABEL_KINDS and f.emit and merged.get(f.param_name) is not None:
                    # A generated field can only be traced when its source mapping is unambiguous.
                    if f.param_name in params:
                        values[f.name] = (merged[f.param_name], ["params", f.param_name])
                    else:
                        source = next((x for x in original.fields if x.emit and x.param_name == f.param_name), None)
                        if source and chart.get(source.name) is not None:
                            values[f.name] = (merged[f.param_name], [source.name])
            return t, values
        values: dict[str, tuple[Any, list]] = {}
        for f in t.fields:
            if f.kind not in LABEL_KINDS:
                continue
            if t.hook is None and f.emit and f.param_name in params:
                values[f.name] = (params[f.param_name], ["params", f.param_name])
            elif chart.get(f.name) is not None:
                values[f.name] = (chart[f.name], [f.name])
        return t, values
    raw_type = _raw_types().get(actual_viz)
    if raw_type is None:
        return None
    return raw_type, {f.name: (params[f.param_name], ["params", f.param_name])
                      for f in raw_type.fields if f.kind in LABEL_KINDS and f.emit and params.get(f.param_name) is not None}


def _column_label(item: Any) -> str | None:
    """Superset get_column_name: 문자열은 열 이름 그대로(표시 이름을 쓰지 않는다), adhoc 열은 label 또는 식."""
    if isinstance(item, str):
        return item or None
    if isinstance(item, Mapping):
        label = item.get("label") or item.get("sqlExpression")
        return label if isinstance(label, str) and label else None
    return None


def _metric_label(item: Any, verbose: Mapping[str, str]) -> tuple[str, str] | None:
    """(화면이 같은 지표로 보는 키, Superset 라벨). 데이터셋 지표 이름은 Superset이 표시 이름(verbose_name)으로 바꾼다."""
    if isinstance(item, str):
        return (item, verbose.get(item) or item) if item else None
    if not isinstance(item, Mapping):
        return None
    label = item.get("label")
    if not label and item.get("expressionType") == "SIMPLE":
        column = item.get("column")
        name = column.get("column_name") if isinstance(column, Mapping) else None
        label = f"{item['aggregate']}({name})" if name and item.get("aggregate") else name
    label = label or item.get("sqlExpression") or item.get("sql")
    return (label, label) if isinstance(label, str) and label else None


def label_conflicts(chart: Mapping[str, Any], metric_labels: Mapping[str, str] | None = None) -> list[tuple[list, str]]:
    """한 Superset 쿼리 안에서 겹치는 열·지표 라벨 (차트 기준 경로, 이유). 겹치면 Superset이 차트를 그리지 않는다.

    metric_labels: 데이터셋 지표 이름 → 표시 이름(dashboard.yaml의 datasets.<키>.labels).
    쿼리의 모양은 유형 파일의 query 선언이 정한다. Superset 화면이 하는 정리를 따른다: 같은 지표(이름 또는 adhoc 라벨이
    같음)는 하나로 합쳐 보내고, merge_columns인 유형은 같은 열도 하나로 합친다. 그러고도 남은 라벨이 겹치면 오류다.
    오류는 겹치는 라벨마다 하나, 경로는 마지막으로 나온 자리다.
    """
    found = _label_values(chart)
    if found is None:
        return []
    t, values = found
    kinds = {f.name: f.kind for f in t.fields}
    verbose = metric_labels or {}
    out: list[tuple[list, str]] = []
    for group in t.query_groups:
        entries: list[tuple[str, list, str | None]] = []   # (Superset 라벨, 경로, 표시 이름으로 바뀐 지표 이름)
        columns_seen: set[str] = set()
        metrics_seen: dict[str, tuple[Any, list]] = {}
        for name in group:
            if name not in values:
                continue
            value, base = values[name]
            items = list(enumerate(value)) if isinstance(value, list) else [(None, value)]
            for i, item in items:
                path = base + ([i] if i is not None else [])
                if kinds[name] in ("column", "columns"):
                    label = _column_label(item)
                    if label is None or (t.merge_columns and label in columns_seen):
                        continue
                    columns_seen.add(label)
                    entries.append((label, path, None))
                else:
                    pair = _metric_label(item, verbose)
                    if pair is None:
                        continue
                    previous = metrics_seen.get(pair[0])
                    if previous is not None:
                        first, first_path = previous
                        def definition(value: Any) -> Any:
                            if isinstance(value, str):
                                return ("stored", value)
                            if isinstance(value, Mapping) and set(value) == {"sql", "label"}:
                                return ("sql", value["sql"])
                            if isinstance(value, Mapping) and value.get("expressionType") == "SQL" and value.get("label") and isinstance(value.get("sqlExpression"), str):
                                return ("sql", value["sqlExpression"])
                            return None
                        left, right = definition(first), definition(item)
                        if left is not None and right is not None and left != right:
                            reason = (f"지표 라벨 '{pair[0]}'로 서로 다른 지표가 겹침({_fmt_path(first_path)}, {_fmt_path(path)}). "
                                      "Superset이 첫 지표만 보내므로 각 지표에 고유한 라벨을 쓰세요.")
                            conflict = (path, reason)
                            if conflict not in out:
                                out.append(conflict)
                        continue
                    metrics_seen[pair[0]] = (item, path)
                    entries.append((pair[1], path, pair[0] if pair[0] != pair[1] else None))
        by_label: dict[str, list[tuple[list, str | None]]] = {}
        for label, path, renamed in entries:
            by_label.setdefault(label, []).append((path, renamed))
        for label, hits in by_label.items():
            if len(hits) < 2:
                continue
            reason = (f"열·지표 라벨 '{label}'가 한 쿼리에서 겹침({', '.join(_fmt_path(p) for p, _ in hits)}). "
                      f"Superset이 차트 자리에 \"{DUPLICATE_LABEL_ERROR}\" 오류를 냄")
            renamed = [f"지표 '{r}'" for _, r in hits if r]
            if renamed:
                reason += f". {', '.join(renamed)}의 라벨은 데이터셋 labels의 표시 이름임"
            conflict = (hits[-1][0], reason)
            if conflict not in out:
                out.append(conflict)
    return out


# ---------------------------------------------------------------- 대응표

def _code(value: Any) -> str:
    import json
    return "`" + json.dumps(value, ensure_ascii=False) + "`"


def shorthand_table(types: Mapping[str, ChartType] | None = None) -> str:
    """contracts/chart-shorthand.md의 '유형별' 표(마크다운)."""
    types = registry() if types is None else types
    lines = [
        "| 축약 `viz` | 이름 | `viz_type` | 축약 필드 → 파라미터 | 함께 쓰는 파라미터 | 한 쿼리의 열·지표(중복 라벨 검사) |",
        "|---|---|---|---|---|---|",
    ]
    for viz, t in types.items():
        cells = []
        for f in t.fields:
            target = f"`{f.param_name}`" if f.emit and not t.hook else ("쓰지 않음" if not f.emit else "설명 참고")
            if f.kind == "bool" and f.values:
                target += " (" + ", ".join(f"{str(k).lower()} → {_code(v)}" for k, v in f.values.items()) + ")"
            if f.kind == "enum" and isinstance(f.values, Mapping):
                target += " (" + ", ".join(f"{k} → {_code(v)}" for k, v in f.values.items()) + ")"
            elif f.kind == "enum":
                target += " (" + ", ".join(f"`{v}`" for v in f.values or []) + ")"
            marks = [f.kind] + (["필수"] if f.required else []) + ([f"생략 시 {_code(f.default)}"] if f.has_default else [])
            cells.append(f"`{f.name}` → {target} ({', '.join(marks)})")
        extra = [f"`{k}: {_code(v)[1:-1]}`" for k, v in t.defaults.items()]
        if t.notes:
            cells.append(t.notes.strip().replace("\n", " ").replace("|", "\\|"))
        query = "<br>".join("·".join(f"`{n}`" for n in g) for g in t.query_groups) or "—"
        if t.merge_columns:
            query += "<br>같은 열은 합침"
        lines.append(f"| `{viz}` | {t.label} | `{t.viz_type}` | " + "<br>".join(cells) + " | " + (", ".join(extra) or "—")
                     + f" | {query} |")
    return "\n".join(lines) + "\n"
