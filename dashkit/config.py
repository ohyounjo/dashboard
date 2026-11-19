"""설정 로딩과 검증. loader, sync, api가 모두 이 모듈만 쓴다.

형식의 기준은 contracts/schema.schema.json, contracts/dashboard.schema.json이고,
JSON Schema로 표현할 수 없는 참조 검사는 contracts/README.md의 '참조 검사' 표를 따른다.
오류 메시지 형식: "<파일>:<YAML 경로>: <이유>". 오류는 모두 모아 ConfigError 하나로 던진다.
"""
from __future__ import annotations

import copy
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from . import charts as chart_registry
from . import widgets as widget_registry
from .reserved_words import is_reserved

NAMESPACE = uuid.UUID("867a0be0-cbd2-5438-8e90-40ddf6d53a0d")
OBJECT_KINDS = ("database", "dataset", "chart", "dashboard")
EMBED_SECTION = "superset"   # 위젯이 아닌 섹션 유형. 나머지 유형은 위젯 매니페스트(dashkit/widgets.py)가 정한다
GRID_COLUMNS = 12

SCHEMA_FILE = "schema.yaml"
DASHBOARD_FILE = "dashboard.yaml"


def _contracts_dir() -> Path:
    env = os.environ.get("CONTRACTS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "contracts"


def default_projects_dir() -> Path:
    env = os.environ.get("PROJECTS_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "projects"


class ConfigError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


@dataclass(frozen=True)
class Project:
    name: str
    root: Path
    schema: dict = field(repr=False)
    dashboard: dict = field(repr=False)

    @property
    def data_dir(self) -> Path:
        return self.root / "data"


# ---------------------------------------------------------------- 공통 도구

def object_uuid(project: str, kind: str, key: str) -> str:
    if kind not in OBJECT_KINDS:
        raise ValueError(f"unknown kind: {kind}")
    return str(uuid.uuid5(NAMESPACE, f"{project}/{kind}/{key}"))


def fmt_path(parts) -> str:
    out = ""
    for p in parts:
        if isinstance(p, int):
            out += f"[{p}]"
        else:
            out += ("." if out else "") + str(p)
    return out or "(최상위)"


def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(name))


def load_schema(name: str) -> dict:
    """계약의 JSON Schema. dashboard.schema.json에는 차트 유형 레지스트리의 유형별 정의와
    위젯 매니페스트의 섹션 정의를 합성한다."""
    with open(_contracts_dir() / name, encoding="utf-8") as f:
        schema = json.load(f)
    if name == "dashboard.schema.json":
        chart_registry.extend_schema(schema)
        widget_registry.extend_schema(schema)
    return schema


def _schema_errors(validator: Draft202012Validator, doc: Any, file: str) -> list[str]:
    errors = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(map(str, e.absolute_path))):
        # oneOf/anyOf 안쪽 오류 중 가장 구체적인 것을 고른다
        if err.context:
            err = best_match(err.context) or err
        errors.append(f"{file}:{fmt_path(err.absolute_path)}: {err.message}")
    return errors


def _read_yaml(path: Path, errors: list[str]) -> Any:
    if not path.is_file():
        errors.append(f"{path.name}:(최상위): 파일이 없음 ({path})")
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"{mark.line + 1}행" if mark else "(최상위)"
        errors.append(f"{path.name}:{where}: YAML 문법 오류: {getattr(exc, 'problem', exc)}")
        return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


# ---------------------------------------------------------------- 로딩

def load_project(name: str, projects_dir: Path | None = None) -> Project:
    projects_dir = Path(projects_dir) if projects_dir else default_projects_dir()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name or ""):
        raise ConfigError([f"PROJECT:(최상위): 프로젝트 이름 형식 오류 '{name}' (소문자, 숫자, _, -)"])
    root = projects_dir / name
    if not root.is_dir():
        raise ConfigError([f"PROJECT:(최상위): 프로젝트 폴더가 없음 ({root})"])

    errors: list[str] = []
    schema = _read_yaml(root / SCHEMA_FILE, errors)
    dashboard = _read_yaml(root / DASHBOARD_FILE, errors)
    if errors:
        raise ConfigError(errors)
    try:
        widget_registry.registry()
    except widget_registry.ManifestError as exc:
        raise ConfigError([str(exc)]) from exc

    # YAML이 날짜를 date 객체로 읽으면 스키마의 문자열 패턴과 맞지 않으므로 문자열로 되돌린다
    schema = _dates_to_str(schema)
    dashboard = _dates_to_str(dashboard)

    errors += _schema_errors(_validator("schema.schema.json"), schema, SCHEMA_FILE)
    errors += _schema_errors(_validator("dashboard.schema.json"), dashboard, DASHBOARD_FILE)
    if errors:
        raise ConfigError(errors)

    schema = _normalize_schema(schema)
    dashboard = _normalize_dashboard(dashboard)
    project = Project(name=name, root=root, schema=schema, dashboard=dashboard)

    errors += _check_schema_refs(project)
    errors += _check_dashboard_refs(project)
    if errors:
        raise ConfigError(errors)
    return project


def _dates_to_str(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _dates_to_str(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_dates_to_str(v) for v in obj]
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return obj


def _normalize_schema(schema: dict) -> dict:
    schema = copy.deepcopy(schema)
    for table in schema["tables"].values():
        pk = table["primary_key"]
        table["primary_key"] = [pk] if isinstance(pk, str) else list(pk)
        table.setdefault("checks", [])
        for col_name, col in table["columns"].items():
            col.setdefault("required", False)
            if col_name in table["primary_key"]:
                col["required"] = True
    return schema


# superset 섹션의 선택 필드. 위젯 섹션의 기본값은 매니페스트 스키마의 default에서 나온다
_EMBED_SECTION_DEFAULTS = {"title": None, "height": None}


def _normalize_dashboard(dash: dict) -> dict:
    dash = copy.deepcopy(dash)
    app = dash["app"]
    app.setdefault("logo_text", None)
    app.setdefault("theme", None)
    for ds in dash["datasets"].values():
        ds.setdefault("metrics", {})
    for d in dash["dashboards"].values():
        d.setdefault("filters", [])
        d.setdefault("cross_filters", False)
        for flt in d["filters"]:
            flt.setdefault("type", "select")
            if flt["type"] == "select":
                flt.setdefault("multi", True)
    widget_types = widget_registry.registry()
    for tab in dash["tabs"]:
        for sec in tab["sections"]:
            if sec["type"] == EMBED_SECTION:
                for k, v in _EMBED_SECTION_DEFAULTS.items():
                    sec.setdefault(k, v)
            else:
                widget_types[sec["type"]].fill_defaults(sec)
    return dash


# ---------------------------------------------------------------- schema.yaml 참조 검사

def _check_schema_refs(project: Project) -> list[str]:
    errors: list[str] = []
    tables = project.schema["tables"]
    f = SCHEMA_FILE
    for tname, table in tables.items():
        base = ["tables", tname]
        if is_reserved(tname):
            errors.append(f"{f}:{fmt_path(base)}: 테이블 이름 '{tname}'은 SQL 예약어")
        cols = table["columns"]
        for pk in table["primary_key"]:
            if pk not in cols:
                errors.append(f"{f}:{fmt_path(base + ['primary_key'])}: 열 '{pk}'가 columns에 없음")
        for cname, col in cols.items():
            cpath = base + ["columns", cname]
            if is_reserved(cname):
                errors.append(f"{f}:{fmt_path(cpath)}: 열 이름 '{cname}'은 SQL 예약어")
            errors += _check_bounds(f, cpath, col)
            if "pattern" in col:
                try:
                    re.compile(col["pattern"])
                except re.error as exc:
                    errors.append(f"{f}:{fmt_path(cpath + ['pattern'])}: 정규식 오류: {exc}")
            if "ref" in col:
                rt, rc = col["ref"].split(".", 1)
                target = tables.get(rt)
                if target is None:
                    errors.append(f"{f}:{fmt_path(cpath + ['ref'])}: 테이블 '{rt}'이 없음")
                elif rc not in target["columns"]:
                    errors.append(f"{f}:{fmt_path(cpath + ['ref'])}: 테이블 '{rt}'에 열 '{rc}'가 없음")
                elif target["primary_key"] != [rc]:
                    errors.append(f"{f}:{fmt_path(cpath + ['ref'])}: '{rt}.{rc}'는 단일 열 primary_key가 아님")
                elif target["columns"][rc]["type"] != col["type"]:
                    errors.append(f"{f}:{fmt_path(cpath + ['ref'])}: 참조 열과 대상 열의 type이 다름 "
                                  f"({col['type']} ≠ {target['columns'][rc]['type']})")
    if not errors:
        try:
            table_order(project)
        except ConfigError as exc:
            errors += exc.errors
    return errors


def _check_bounds(f: str, cpath: list, col: dict) -> list[str]:
    errors = []
    t = col["type"]
    vals = {}
    for k in ("min", "max"):
        if k not in col:
            continue
        v = col[k]
        if t == "date":
            d = _parse_date(v) if isinstance(v, str) else None
            if d is None:
                errors.append(f"{f}:{fmt_path(cpath + [k])}: date 열의 {k}는 YYYY-MM-DD 문자열이어야 함")
            vals[k] = d
        elif t in ("integer", "number"):
            if isinstance(v, bool) or not isinstance(v, (int, float)) or (t == "integer" and not float(v).is_integer()):
                errors.append(f"{f}:{fmt_path(cpath + [k])}: {t} 열의 {k}는 숫자여야 함")
            vals[k] = v
        else:
            errors.append(f"{f}:{fmt_path(cpath + [k])}: {t} 열에는 {k}를 쓸 수 없음")
    if vals.get("min") is not None and vals.get("max") is not None and not errors and vals["min"] > vals["max"]:
        errors.append(f"{f}:{fmt_path(cpath)}: min이 max보다 큼")
    return errors


def table_order(project: Project) -> list[str]:
    """ref를 따라 참조되는 테이블이 먼저 오도록 정렬한다(같은 단계는 schema.yaml 순서)."""
    tables = project.schema["tables"]
    deps = {
        t: {c["ref"].split(".", 1)[0] for c in spec["columns"].values() if "ref" in c}
        for t, spec in tables.items()
    }
    order: list[str] = []
    done: set[str] = set()
    remaining = list(tables)
    while remaining:
        progressed = False
        for t in list(remaining):
            if deps[t] <= done:
                order.append(t)
                done.add(t)
                remaining.remove(t)
                progressed = True
        if not progressed:
            raise ConfigError([f"{SCHEMA_FILE}:tables: ref에 순환이 있음 ({', '.join(remaining)})"])
    return order


# ---------------------------------------------------------------- dashboard.yaml 참조 검사

def charts(project: Project) -> Iterator[tuple[str, dict]]:
    for dkey, d in project.dashboard["dashboards"].items():
        for chart in d["charts"]:
            yield dkey, chart


def _metric_names(chart: dict) -> list[tuple[list, str]]:
    """차트가 이름으로 참조한 데이터셋 지표 (경로, 이름). 원본(viz_type) 차트는 검사하지 않는다."""
    return chart_registry.metric_refs(chart) if "viz" in chart else []


DEFAULT_ITEM_HEIGHT = 50      # 차트 높이 기본(Superset 격자 단위)
DEFAULT_TEXT_HEIGHT = 20      # 차트 없는 행에 놓인 글(markdown)의 높이 기본
FILTER_TYPES = ("select", "time_range", "number_range", "time_grain")
# 필터 유형별로 쓸 수 있는 필드(key, label, type, default, charts는 공통). 필수 필드는 스키마가 본다
_FILTER_FIELDS = {
    "select": {"dataset", "column", "multi", "parent"},
    "number_range": {"dataset", "column"},
    "time_range": set(),
    "time_grain": {"dataset"},
}


def _is_header(item: Any) -> bool:
    return isinstance(item, dict) and "header" in item


def _layout_row_items(row: list) -> list[dict]:
    """행의 항목을 {chart|markdown, width, height}로 푼다(너비·높이는 지정한 값 또는 None)."""
    items = []
    for i in row:
        if isinstance(i, str):
            items.append({"chart": i, "width": None, "height": None})
        elif "markdown" in i:
            items.append({"markdown": i["markdown"], "width": i.get("width"), "height": i.get("height")})
        else:
            items.append({"chart": i["chart"], "width": i.get("width"), "height": i.get("height")})
    return items


def _fill_row(row: list) -> list[dict]:
    """너비 없는 항목은 남은 칸을 균등 분할하고 나머지는 앞 항목부터 1칸씩 더 준다. 높이 기본은 차트 50,
    글은 같은 행 차트의 가장 큰 높이(차트가 없으면 20)다(contracts/chart-shorthand.md)."""
    items = _layout_row_items(row)
    fixed = sum(i["width"] for i in items if i["width"])
    free = [i for i in items if not i["width"]]
    if free:
        share, extra = divmod(GRID_COLUMNS - fixed, len(free))
        for n, i in enumerate(free):
            i["width"] = share + (1 if n < extra else 0)
    for i in items:
        if "chart" in i:
            i["height"] = i["height"] or DEFAULT_ITEM_HEIGHT
    tallest = max((i["height"] for i in items if "chart" in i), default=DEFAULT_TEXT_HEIGHT)
    for i in items:
        i["height"] = i["height"] or tallest
    return items


def _layout_block(entry: Any) -> dict:
    if isinstance(entry, dict):   # { tabs: [{ label, rows }] }
        return {"kind": "tabs", "tabs": [{"label": t["label"], "blocks": [_layout_block(r) for r in t["rows"]]}
                                         for t in entry["tabs"]]}
    if len(entry) == 1 and _is_header(entry[0]):
        return {"kind": "header", "text": entry[0]["header"]}
    return {"kind": "row", "items": _fill_row(entry)}


def layout_blocks(dashboard: dict) -> list[dict]:
    """layout을 블록 목록으로 푼다(검증을 통과한 설정 기준).

    {"kind": "row", "items": [{chart|markdown, width, height}]} / {"kind": "header", "text"} /
    {"kind": "tabs", "tabs": [{"label", "blocks": [행·제목 블록]}]}
    """
    return [_layout_block(entry) for entry in dashboard["layout"]]


def layout_rows(dashboard: dict) -> list[list[dict]]:
    """layout 최상위의 행을 [{chart, width, height}] 목록으로 푼다(제목과 대시보드 안 탭은 빼고)."""
    return [b["items"] for b in layout_blocks(dashboard) if b["kind"] == "row"]


def _check_layout(f: str, dpath: list, d: dict, keys_here: list[str]) -> list[str]:
    errors: list[str] = []
    placed: dict[str, int] = {}

    def check_row(rpath: list, row: list) -> None:
        fixed = 0
        free = 0
        for c, item in enumerate(row):
            if _is_header(item):
                if len(row) > 1:
                    errors.append(f"{f}:{fmt_path(rpath + [c])}: 제목(header)은 한 행에 혼자 있어야 함")
                return
            if isinstance(item, dict) and "markdown" in item:
                width = item.get("width")
            else:
                ckey = item if isinstance(item, str) else item["chart"]
                width = None if isinstance(item, str) else item.get("width")
                if ckey not in keys_here:
                    errors.append(f"{f}:{fmt_path(rpath + [c])}: 차트 '{ckey}'가 이 대시보드의 charts에 없음")
                placed[ckey] = placed.get(ckey, 0) + 1
            if width:
                fixed += width
            else:
                free += 1
        if fixed > GRID_COLUMNS or (free and GRID_COLUMNS - fixed < free):
            errors.append(f"{f}:{fmt_path(rpath)}: 행 너비가 {GRID_COLUMNS}칸을 넘음")

    for r, entry in enumerate(d["layout"]):
        rpath = dpath + ["layout", r]
        if isinstance(entry, dict):
            for t, tab in enumerate(entry["tabs"]):
                for k, row in enumerate(tab["rows"]):
                    check_row(rpath + ["tabs", t, "rows", k], row)
        else:
            check_row(rpath, entry)
    for key in keys_here:
        n = placed.get(key, 0)
        if n == 0:
            errors.append(f"{f}:{fmt_path(dpath + ['layout'])}: 차트 '{key}'가 layout에 배치되지 않음")
        elif n > 1:
            errors.append(f"{f}:{fmt_path(dpath + ['layout'])}: 차트 '{key}'가 layout에 {n}번 배치됨")
    return errors


def _check_filter_default(flt: dict) -> str | None:
    """필터 기본값의 형식이 유형과 맞지 않으면 이유를 돌려준다."""
    value = flt["default"]
    kind = flt["type"]

    def scalar(v: Any) -> bool:
        return isinstance(v, (str, int, float)) and not isinstance(v, bool)

    def number(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    if kind == "select":
        values = value if isinstance(value, list) else [value]
        if not values or not all(scalar(v) for v in values):
            return "select 필터의 default는 값 또는 값 목록이어야 함"
        if not flt.get("multi", True) and len(values) > 1:
            return "multi: false인 필터의 default는 값 하나여야 함"
    elif kind == "number_range":
        if not (isinstance(value, list) and len(value) == 2 and all(v is None or number(v) for v in value)
                and any(v is not None for v in value)):
            return "number_range 필터의 default는 [최소, 최대]여야 함(한쪽은 null 가능)"
        if value[0] is not None and value[1] is not None and value[0] > value[1]:
            return "number_range 필터의 default는 최소가 최대보다 클 수 없음"
    elif kind == "time_range":
        if not isinstance(value, str) or not value.strip():
            return "time_range 필터의 default는 Superset 시간 범위 문자열이어야 함(예: \"Last quarter\")"
    else:
        grains = load_schema("dashboard.schema.json")["$defs"]["time_grain"]["enum"]
        if value not in grains:
            return f"time_grain 필터의 default는 {', '.join(grains)} 중 하나여야 함"
    return None


def _check_filters(f: str, dpath: list, d: dict, datasets: dict, keys_here: list[str]) -> list[str]:
    errors: list[str] = []
    filters = d["filters"]
    by_key: dict[str, dict] = {}
    for i, flt in enumerate(filters):
        fpath = dpath + ["filters", i]
        if flt["key"] in by_key:
            errors.append(f"{f}:{fmt_path(fpath + ['key'])}: 필터 키 '{flt['key']}'가 중복됨")
        by_key.setdefault(flt["key"], flt)
        kind = flt["type"]
        for name in ("dataset", "column", "multi", "parent"):
            if name in flt and name not in _FILTER_FIELDS[kind]:
                errors.append(f"{f}:{fmt_path(fpath + [name])}: {kind} 필터에는 {name}을 쓸 수 없음")
        if "dataset" in flt and flt["dataset"] not in datasets:
            errors.append(f"{f}:{fmt_path(fpath + ['dataset'])}: 데이터셋 '{flt['dataset']}'이 없음")
        if "default" in flt:
            reason = _check_filter_default(flt)
            if reason:
                errors.append(f"{f}:{fmt_path(fpath + ['default'])}: {reason}")
        for c, ckey in enumerate(flt.get("charts") or []):
            if ckey not in keys_here:
                errors.append(f"{f}:{fmt_path(fpath + ['charts', c])}: 차트 '{ckey}'가 이 대시보드의 charts에 없음")
    for i, flt in enumerate(filters):
        if "parent" not in flt:
            continue
        ppath = dpath + ["filters", i, "parent"]
        if flt["parent"] == flt["key"]:
            errors.append(f"{f}:{fmt_path(ppath)}: 필터가 자기 자신을 parent로 가리킴")
        elif flt["parent"] not in by_key:
            errors.append(f"{f}:{fmt_path(ppath)}: 필터 '{flt['parent']}'가 이 대시보드의 filters에 없음")
        else:
            seen = [flt["key"]]
            cur = flt
            while "parent" in cur and cur["parent"] in by_key and cur["parent"] not in seen:
                seen.append(cur["parent"])
                cur = by_key[cur["parent"]]
            if cur.get("parent") == flt["key"]:
                errors.append(f"{f}:{fmt_path(ppath)}: parent가 순환함 ({' → '.join(seen + [flt['key']])})")
    return errors


def _check_dashboard_refs(project: Project) -> list[str]:
    errors: list[str] = []
    f = DASHBOARD_FILE
    dash = project.dashboard
    datasets = dash["datasets"]
    tables = project.schema["tables"]

    for dskey, ds in datasets.items():
        path = ["datasets", dskey]
        if is_reserved(dskey):
            errors.append(f"{f}:{fmt_path(path)}: 데이터셋 이름 '{dskey}'은 SQL 예약어")
        if "table" in ds and ds["table"] not in tables:
            errors.append(f"{f}:{fmt_path(path + ['table'])}: schema.yaml에 테이블 '{ds['table']}'이 없음")

    seen_charts: dict[str, str] = {}
    for dkey, d in dash["dashboards"].items():
        dpath = ["dashboards", dkey]
        keys_here = []
        for i, chart in enumerate(d["charts"]):
            cpath = dpath + ["charts", i]
            key = chart["key"]
            if key in seen_charts:
                errors.append(f"{f}:{fmt_path(cpath + ['key'])}: 차트 키 '{key}'가 중복됨"
                              f"(대시보드 '{seen_charts[key]}'에도 있음)")
            seen_charts.setdefault(key, dkey)
            keys_here.append(key)
            ds = datasets.get(chart["dataset"])
            if ds is None:
                errors.append(f"{f}:{fmt_path(cpath + ['dataset'])}: 데이터셋 '{chart['dataset']}'이 없음")
                continue
            for mpath, name in _metric_names(chart):
                if name not in ds["metrics"]:
                    errors.append(f"{f}:{fmt_path(cpath + mpath)}: 데이터셋 '{chart['dataset']}'에 지표 '{name}'이 없음")

        errors += _check_layout(f, dpath, d, keys_here)
        errors += _check_filters(f, dpath, d, datasets, keys_here)

    tab_keys = set()
    for t, tab in enumerate(dash["tabs"]):
        tpath = ["tabs", t]
        if tab["key"] in tab_keys:
            errors.append(f"{f}:{fmt_path(tpath + ['key'])}: 탭 키 '{tab['key']}'가 중복됨")
        tab_keys.add(tab["key"])
        for s, sec in enumerate(tab["sections"]):
            spath = tpath + ["sections", s]
            if sec["type"] == EMBED_SECTION:
                if sec["dashboard"] not in dash["dashboards"]:
                    errors.append(f"{f}:{fmt_path(spath + ['dashboard'])}: 대시보드 '{sec['dashboard']}'가 없음")
                continue
            widget = widget_registry.registry()[sec["type"]]
            if widget.has_dataset and sec["dataset"] not in datasets:
                errors.append(f"{f}:{fmt_path(spath + ['dataset'])}: 데이터셋 '{sec['dataset']}'이 없음")
            # 매니페스트의 표시("x-unique", "x-date-range")로 선언한 검사
            for path, reason in widget.check(sec):
                errors.append(f"{f}:{fmt_path(spath + path)}: {reason}")

    if dash["app"]["as_of"] != "today" and _parse_date(dash["app"]["as_of"]) is None:
        errors.append(f"{f}:app.as_of: 날짜 형식 오류")

    # table: 데이터셋은 열을 schema.yaml로 바로 확인할 수 있다
    if not errors:
        static_cols = {
            k: set(tables[ds["table"]]["columns"]) for k, ds in datasets.items() if "table" in ds
        }
        errors += check_view_columns(project, static_cols, only_known=True)
    return errors


# ---------------------------------------------------------------- 열 참조

def referenced_columns(project: Project) -> dict[str, dict[str, list[str]]]:
    """{데이터셋: {열: [설정 경로, …]}}. viz_type 원본 차트의 params는 포함하지 않는다."""
    refs: dict[str, dict[str, list[str]]] = {}

    def add(ds: str, col: str | None, path: list) -> None:
        if col:
            refs.setdefault(ds, {}).setdefault(col, []).append(fmt_path(path))

    dash = project.dashboard
    for dskey, ds in dash["datasets"].items():
        for col in (ds.get("labels") or {}):
            if col not in ds["metrics"]:
                add(dskey, col, ["datasets", dskey, "labels", col])
    for dkey, d in dash["dashboards"].items():
        for i, chart in enumerate(d["charts"]):
            if "viz" not in chart:
                continue
            base = ["dashboards", dkey, "charts", i]
            ds = chart["dataset"]
            for path, col in chart_registry.column_refs(chart):
                add(ds, col, base + path)
        for i, flt in enumerate(d["filters"]):
            if flt.get("dataset") and flt.get("column"):   # select, number_range
                add(flt["dataset"], flt["column"], ["dashboards", dkey, "filters", i, "column"])
    for t, tab in enumerate(dash["tabs"]):
        for s, sec in enumerate(tab["sections"]):
            if sec["type"] == EMBED_SECTION:
                continue
            base = ["tabs", t, "sections", s]
            # 매니페스트에서 "x-column" 표시가 있는 옵션(순서는 매니페스트의 선언 순서)
            for path, col in widget_registry.registry()[sec["type"]].column_refs(sec):
                add(sec["dataset"], col, base + path)
    return refs


def check_view_columns(project: Project, view_columns: dict[str, set[str]], only_known: bool = False) -> list[str]:
    """설정이 참조한 열이 실제 뷰에 있는지 확인한다. only_known이면 view_columns에 없는 데이터셋은 건너뛴다."""
    errors = []
    for ds, cols in referenced_columns(project).items():
        if ds not in view_columns:
            if not only_known:
                errors.append(f"{DASHBOARD_FILE}:datasets.{ds}: 데이터셋 '{ds}' 뷰가 없음")
            continue
        actual = view_columns[ds]
        for col, paths in cols.items():
            if col not in actual:
                for p in paths:
                    errors.append(f"{DASHBOARD_FILE}:{p}: 데이터셋 '{ds}' 뷰에 열 '{col}'가 없음")
    return errors


# ---------------------------------------------------------------- 기타 조회

def widget_datasets(project: Project) -> set[str]:
    """위젯 섹션이 dataset으로 참조한 데이터셋. /api/data 허용 목록."""
    widget_types = widget_registry.registry()
    return {
        sec["dataset"]
        for tab in project.dashboard["tabs"]
        for sec in tab["sections"]
        if sec["type"] in widget_types and widget_types[sec["type"]].has_dataset
    }


def resolve_as_of(project: Project, today: date | None = None) -> date:
    value = project.dashboard["app"]["as_of"]
    if value != "today":
        return date.fromisoformat(value)
    if today is not None:
        return today
    tz = os.environ.get("TZ")
    try:
        return datetime.now(ZoneInfo(tz)).date() if tz else date.today()
    except Exception:
        return date.today()
