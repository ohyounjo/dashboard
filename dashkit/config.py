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
from .reserved_words import is_reserved

NAMESPACE = uuid.UUID("867a0be0-cbd2-5438-8e90-40ddf6d53a0d")
OBJECT_KINDS = ("database", "dataset", "chart", "dashboard")
WIDGET_TYPES = ("gantt", "kanban")
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
    """계약의 JSON Schema. dashboard.schema.json에는 차트 유형 레지스트리의 유형별 정의를 합성한다."""
    with open(_contracts_dir() / name, encoding="utf-8") as f:
        schema = json.load(f)
    if name == "dashboard.schema.json":
        chart_registry.extend_schema(schema)
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


_SECTION_DEFAULTS = {
    "superset": {"title": None, "height": None},
    "gantt": {"title": None, "statuses": None, "range": None, "sort": None},
    "kanban": {"title": None, "filter": None},
}


def _normalize_dashboard(dash: dict) -> dict:
    dash = copy.deepcopy(dash)
    app = dash["app"]
    app.setdefault("logo_text", None)
    app.setdefault("theme", None)
    for ds in dash["datasets"].values():
        ds.setdefault("metrics", {})
    for d in dash["dashboards"].values():
        d.setdefault("filters", [])
        for flt in d["filters"]:
            flt.setdefault("multi", True)
    for tab in dash["tabs"]:
        for sec in tab["sections"]:
            for k, v in _SECTION_DEFAULTS[sec["type"]].items():
                sec.setdefault(k, v)
            if sec["type"] == "gantt":
                sec["fields"].setdefault("progress", None)
                sec["fields"].setdefault("status", None)
            if sec["type"] == "kanban":
                for col in sec["columns"]:
                    col.setdefault("icon", None)
                    col.setdefault("tone", None)
                sec["card"].setdefault("chips", [])
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


def _layout_widths(row: list) -> list[tuple[str, int | None]]:
    out = []
    for item in row:
        if isinstance(item, str):
            out.append((item, None))
        else:
            out.append((item["chart"], item.get("width")))
    return out


def layout_rows(dashboard: dict) -> list[list[dict]]:
    """layout을 [{chart, width, height}] 행 목록으로 푼다. 너비 없는 항목은 남은 칸을 균등 분할하고
    나머지는 앞 항목부터 1칸씩 더 준다(contracts/chart-shorthand.md)."""
    rows = []
    for row in dashboard["layout"]:
        items = [{"chart": i, "width": None, "height": None} if isinstance(i, str)
                 else {"chart": i["chart"], "width": i.get("width"), "height": i.get("height")} for i in row]
        fixed = sum(i["width"] for i in items if i["width"])
        free = [i for i in items if not i["width"]]
        if free:
            share, extra = divmod(GRID_COLUMNS - fixed, len(free))
            for n, i in enumerate(free):
                i["width"] = share + (1 if n < extra else 0)
        for i in items:
            i["height"] = i["height"] or 50
        rows.append(items)
    return rows


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

        placed: dict[str, int] = {}
        for r, row in enumerate(d["layout"]):
            rpath = dpath + ["layout", r]
            fixed = 0
            free = 0
            for c, (ckey, width) in enumerate(_layout_widths(row)):
                if ckey not in keys_here:
                    errors.append(f"{f}:{fmt_path(rpath + [c])}: 차트 '{ckey}'가 이 대시보드의 charts에 없음")
                placed[ckey] = placed.get(ckey, 0) + 1
                if width:
                    fixed += width
                else:
                    free += 1
            if fixed > GRID_COLUMNS or (free and GRID_COLUMNS - fixed < free):
                errors.append(f"{f}:{fmt_path(rpath)}: 행 너비가 {GRID_COLUMNS}칸을 넘음")
        for key in keys_here:
            n = placed.get(key, 0)
            if n == 0:
                errors.append(f"{f}:{fmt_path(dpath + ['layout'])}: 차트 '{key}'가 layout에 배치되지 않음")
            elif n > 1:
                errors.append(f"{f}:{fmt_path(dpath + ['layout'])}: 차트 '{key}'가 layout에 {n}번 배치됨")

        fkeys = set()
        for i, flt in enumerate(d["filters"]):
            fpath = dpath + ["filters", i]
            if flt["key"] in fkeys:
                errors.append(f"{f}:{fmt_path(fpath + ['key'])}: 필터 키 '{flt['key']}'가 중복됨")
            fkeys.add(flt["key"])
            if flt["dataset"] not in datasets:
                errors.append(f"{f}:{fmt_path(fpath + ['dataset'])}: 데이터셋 '{flt['dataset']}'이 없음")

    tab_keys = set()
    for t, tab in enumerate(dash["tabs"]):
        tpath = ["tabs", t]
        if tab["key"] in tab_keys:
            errors.append(f"{f}:{fmt_path(tpath + ['key'])}: 탭 키 '{tab['key']}'가 중복됨")
        tab_keys.add(tab["key"])
        for s, sec in enumerate(tab["sections"]):
            spath = tpath + ["sections", s]
            if sec["type"] == "superset":
                if sec["dashboard"] not in dash["dashboards"]:
                    errors.append(f"{f}:{fmt_path(spath + ['dashboard'])}: 대시보드 '{sec['dashboard']}'가 없음")
                continue
            if sec["dataset"] not in datasets:
                errors.append(f"{f}:{fmt_path(spath + ['dataset'])}: 데이터셋 '{sec['dataset']}'이 없음")
            if sec["type"] == "kanban":
                values = [c["value"] for c in sec["columns"]]
                dup = {v for v in values if values.count(v) > 1}
                if dup:
                    errors.append(f"{f}:{fmt_path(spath + ['columns'])}: 칸 value가 중복됨 ({', '.join(sorted(dup))})")
            if sec["type"] == "gantt" and sec["range"]:
                a, b = _parse_date(sec["range"]["start"]), _parse_date(sec["range"]["end"])
                if a is None or b is None:
                    errors.append(f"{f}:{fmt_path(spath + ['range'])}: 날짜 형식 오류")
                elif a > b:
                    errors.append(f"{f}:{fmt_path(spath + ['range'])}: start가 end보다 늦음")

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
            add(flt["dataset"], flt["column"], ["dashboards", dkey, "filters", i, "column"])
    for t, tab in enumerate(dash["tabs"]):
        for s, sec in enumerate(tab["sections"]):
            base = ["tabs", t, "sections", s]
            if sec["type"] == "gantt":
                for k, col in sec["fields"].items():
                    add(sec["dataset"], col, base + ["fields", k])
                add(sec["dataset"], sec["sort"], base + ["sort"])
            elif sec["type"] == "kanban":
                ds = sec["dataset"]
                add(ds, sec["group_by"], base + ["group_by"])
                add(ds, sec["card"]["title"], base + ["card", "title"])
                for j, col in enumerate(sec["card"]["chips"] or []):
                    add(ds, col, base + ["card", "chips", j])
                if sec["filter"]:
                    add(ds, sec["filter"]["column"], base + ["filter", "column"])
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
    """gantt·kanban 섹션이 참조한 데이터셋. /api/data 허용 목록."""
    return {
        sec["dataset"]
        for tab in project.dashboard["tabs"]
        for sec in tab["sections"]
        if sec["type"] in WIDGET_TYPES
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
