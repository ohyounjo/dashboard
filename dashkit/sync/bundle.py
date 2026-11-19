"""설정 + 뷰 열 정보 → Superset 6.1.0 asset 번들(파일 dict). 순수 함수만 둔다(DB·네트워크 없음).

기준: contracts/chart-shorthand.md(축약 → 파라미터. 유형별 대응은 dashkit/charts/), contracts/state-and-uuid.md(UUID, 표식),
contracts/superset-notes.md 2절(번들 형식 함정)·11절(표식)·12절(배치 노드와 필터), contracts/superset-6.1.0-export/(키 구성).

결정성: 모든 dict는 고정된 삽입 순서로 만들고 YAML은 sort_keys=False로 쓴다.
타임스탬프는 metadata.yaml에만 들어간다. zip 항목 시각은 1980-01-01로 고정한다.
"""
from __future__ import annotations

import io
import os
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from ..charts import build_params, column_refs
from ..config import Project, charts, layout_blocks, object_uuid

BUNDLE_ROOT = "bundle"              # zip 안 최상위 폴더(= .build/<프로젝트>/bundle/)
APPLIED_ROOT = "applied"            # 마지막으로 import에 성공한 번들(.build/<프로젝트>/applied/). plan의 비교 기준
DATABASE_FILE = "databases/dashkit.yaml"
DATASET_DIR = "datasets/dashkit"
EXPORT_VERSION = "1.0.0"
VIEW_SCHEMA = "dash"
MASKED_PASSWORD = "XXXXXXXXXX"
DATABASE_KEY = "main"

# 종류 순서: import 의존 순서(DB → 데이터셋 → 차트 → 대시보드). prune은 이 역순.
KINDS = ("database", "dataset", "chart", "dashboard")

ViewColumns = Mapping[str, "Iterable[tuple[str, str]] | Mapping[str, str]"]


# ---------------------------------------------------------------- 공통

def dump_yaml(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, default_flow_style=False, width=100000)


def _slug(text: str) -> str:
    return text.replace("_", "-")


def database_name(project: Project) -> str:
    return f"dashkit-{project.name}"


def managed_objects(project: Project) -> dict[str, list[str]]:
    """이번 설정이 관리하는 Superset 객체 UUID(종류 → 목록, 설정 순서)."""
    p = project.name
    dash = project.dashboard
    return {
        "database": [object_uuid(p, "database", DATABASE_KEY)],
        "dataset": [object_uuid(p, "dataset", k) for k in dash["datasets"]],
        "chart": [object_uuid(p, "chart", c["key"]) for _d, c in charts(project)],
        "dashboard": [object_uuid(p, "dashboard", k) for k in dash["dashboards"]],
    }


def _env(env: Mapping[str, str], name: str, default: str) -> str:
    return env.get(name) or default


# ---------------------------------------------------------------- 표식(외부에서 관리됨)

def lock_ui(env: Mapping[str, str]) -> bool:
    """Superset 화면에서 관리 객체의 편집을 막을지(기본 막음). SYNC_LOCK_UI=0이면 막지 않는다."""
    return (env.get("SYNC_LOCK_UI") or "1").strip().lower() not in ("0", "false", "no", "off")


def managed_mark(project: Project, kind: str, key: str, env: Mapping[str, str]) -> dict:
    """번들 YAML 끝에 붙이는 표식(contracts/state-and-uuid.md "표식").

    is_managed_externally는 항상 쓴다. 키를 빼면 import가 이전 값을 그대로 두므로, 잠금을 풀 때도 false를 명시해야 한다.
    """
    return {
        "is_managed_externally": lock_ui(env),
        "external_url": f"dashkit://{project.name}/{kind}/{key}",
    }


# ---------------------------------------------------------------- 데이터베이스

def database_yaml(project: Project, env: Mapping[str, str]) -> dict:
    user = _env(env, "DATA_DB_READER_USER", "dash_reader")
    host = _env(env, "DATA_DB_HOST", "data-db")
    port = _env(env, "DATA_DB_PORT", "5432")
    dbname = _env(env, "DATA_DB_NAME", "dash")
    return {
        "database_name": database_name(project),
        "sqlalchemy_uri": f"postgresql+psycopg2://{user}:{MASKED_PASSWORD}@{host}:{port}/{dbname}",
        "cache_timeout": None,
        "expose_in_sqllab": False,
        "allow_run_async": False,
        "allow_ctas": False,
        "allow_cvas": False,
        "allow_dml": False,
        "allow_file_upload": False,
        "extra": {
            "metadata_params": {},
            "engine_params": {},
            "metadata_cache_timeout": {},
            "schemas_allowed_for_file_upload": [],
        },
        "impersonate_user": False,
        "configuration_method": "sqlalchemy_form",
        "uuid": object_uuid(project.name, "database", DATABASE_KEY),
        "version": EXPORT_VERSION,
    }


# ---------------------------------------------------------------- 데이터셋

def _is_temporal(pg_type: str) -> bool:
    t = pg_type.lower()
    return t == "date" or t.startswith("timestamp")


def _column_pairs(cols: Any) -> list[tuple[str, str]]:
    if isinstance(cols, Mapping):
        return [(str(k), str(v)) for k, v in cols.items()]
    return [(str(c), str(t)) for c, t in cols]


def dataset_yaml(project: Project, key: str, cols: Any, env: Mapping[str, str]) -> dict:
    ds = project.dashboard["datasets"][key]
    pairs = _column_pairs(cols)
    labels = ds.get("labels") or {}
    main_dttm = next((c for c, t in pairs if _is_temporal(t)), None)
    metrics = [
        {
            "metric_name": name,
            "verbose_name": labels.get(name),
            "metric_type": None,
            "expression": expr,
            "description": None,
            "d3format": None,
            "currency": None,
            "extra": None,
            "warning_text": None,
        }
        for name, expr in (ds.get("metrics") or {}).items()
    ]
    columns = [
        {
            "column_name": c,
            "verbose_name": labels.get(c),
            "is_dttm": _is_temporal(t),
            "is_active": True,
            "type": t.upper(),
            "advanced_data_type": None,
            "groupby": True,
            "filterable": True,
            "expression": None,
            "description": None,
            "python_date_format": None,
            "datetime_format": None,
            "extra": None,
        }
        for c, t in pairs
    ]
    return {
        "table_name": key,
        "main_dttm_col": main_dttm,
        "currency_code_column": None,
        "description": ds.get("description"),
        "default_endpoint": None,
        "offset": 0,
        "cache_timeout": None,
        "catalog": _env(env, "DATA_DB_NAME", "dash"),
        "schema": VIEW_SCHEMA,
        "sql": None,
        "params": None,
        "template_params": None,
        "filter_select_enabled": True,
        "fetch_values_predicate": None,
        "extra": None,
        "normalize_columns": False,
        "always_filter_main_dttm": False,
        "folders": None,
        "uuid": object_uuid(project.name, "dataset", key),
        "metrics": metrics,
        "columns": columns,
        "version": EXPORT_VERSION,
        "database_uuid": object_uuid(project.name, "database", DATABASE_KEY),
    }


# ---------------------------------------------------------------- 차트

def chart_params(chart: Mapping[str, Any], time_column: str | None = None) -> dict:
    """차트 설정 → Superset params. 축약 유형의 대응은 차트 유형 레지스트리(dashkit/charts/)가 맡는다.

    time_column이 있으면(대시보드에 time_range 필터가 있는 축약 차트) 그 열의 "No filter" 시간 조건을 adhoc_filters에
    더한다. 대시보드의 시간 범위 필터는 차트에 이 조건이 있어야 적용된다(superset-notes.md 12절).
    """
    if "viz_type" in chart:
        return {"viz_type": chart["viz_type"], **(chart.get("params") or {})}
    params = build_params(chart)
    if time_column:
        params["adhoc_filters"] = [*params.get("adhoc_filters", []), {
            "expressionType": "SIMPLE",
            "clause": "WHERE",
            "subject": time_column,
            "operator": "TEMPORAL_RANGE",
            "comparator": "No filter",
        }]
    # params는 마지막에 얕게 덮어쓴다
    return {**params, **(chart.get("params") or {})}


def time_filter_columns(project: Project, view_columns: ViewColumns) -> dict[str, str]:
    """차트 키 → 대시보드의 time_range 필터가 걸릴 시간 열.

    time_range 필터가 있는 대시보드에서 그 필터의 적용 범위(charts, 생략하면 전부)에 든 축약 차트가 대상이다.
    열은 차트가 참조한 열 중 첫 date/timestamp 열, 없으면 데이터셋 뷰의 첫 date/timestamp 열이다(둘 다 없으면 대상 아님).
    """
    out: dict[str, str] = {}
    for d in project.dashboard["dashboards"].values():
        ranges = [flt for flt in d["filters"] if flt["type"] == "time_range"]
        if not ranges:
            continue
        for chart in d["charts"]:
            if "viz" not in chart or not any(chart["key"] in flt.get("charts", [chart["key"]]) for flt in ranges):
                continue
            temporal = [c for c, t in _column_pairs(view_columns[chart["dataset"]]) if _is_temporal(t)]
            column = next((c for _path, c in column_refs(chart) if c in temporal), temporal[0] if temporal else None)
            if column:
                out[chart["key"]] = column
    return out


def chart_yaml(project: Project, chart: Mapping[str, Any], time_column: str | None = None) -> dict:
    params = chart_params(chart, time_column)
    return {
        "slice_name": chart["title"],
        "description": chart.get("description"),
        "certified_by": None,
        "certification_details": None,
        "viz_type": params["viz_type"],
        "params": params,
        "query_context": None,
        "cache_timeout": None,
        "uuid": object_uuid(project.name, "chart", chart["key"]),
        "version": EXPORT_VERSION,
        "dataset_uuid": object_uuid(project.name, "dataset", chart["dataset"]),
    }


# ---------------------------------------------------------------- 대시보드

HEADER_SIZE = "MEDIUM_HEADER"
BACKGROUND = "BACKGROUND_TRANSPARENT"
ROOT_PATH = ["ROOT_ID", "GRID_ID"]

FILTER_TYPES = {
    "select": "filter_select",
    "time_range": "filter_time",
    "number_range": "filter_range",
    "time_grain": "filter_timegrain",
}


# 화면이 저장하는 filterState.label(필터 표시 문구)과 같은 이름
TIME_GRAIN_LABELS = {"P1D": "Day", "P1W": "Week", "P1M": "Month", "P3M": "Quarter", "P1Y": "Year"}


def _chart_numbers(d: Mapping[str, Any]) -> dict[str, int]:
    """차트 키 → position의 자리표시 chartId.

    필터에 `charts`(적용 범위)가 없으면 모두 0이다. 있으면 차트마다 다른 번호(1부터, charts 순서)를 준다.
    import가 scope.excluded를 "자리표시 id → 실제 id"로 바꾸므로 번호가 서로 달라야 한다(superset-notes.md 12절).
    """
    scoped = any("charts" in flt for flt in d["filters"])
    return {c["key"]: (n if scoped else 0) for n, c in enumerate(d["charts"], 1)}


def dashboard_position(project: Project, dkey: str) -> dict:
    d = project.dashboard["dashboards"][dkey]
    by_key = {c["key"]: c for c in d["charts"]}
    numbers = _chart_numbers(d)
    grid_children: list[str] = []
    pos: dict[str, Any] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": grid_children, "parents": ["ROOT_ID"]},
    }

    def add_blocks(blocks: list[dict], children: list[str], parents: list[str], prefix: str) -> None:
        """블록을 노드로 만든다. id는 배치 안 위치에서 나온다(<종류>-<layout 순번>[-<탭>-<탭 안 순번>])."""
        for n, block in enumerate(blocks, 1):
            suffix = f"{prefix}{n}"
            if block["kind"] == "header":
                node_id = f"HEADER-{suffix}"
                children.append(node_id)
                pos[node_id] = {
                    "type": "HEADER",
                    "id": node_id,
                    "children": [],
                    "parents": list(parents),
                    "meta": {"text": block["text"], "headerSize": HEADER_SIZE, "background": BACKGROUND},
                }
            elif block["kind"] == "tabs":
                tabs_id = f"TABS-{suffix}"
                tab_ids: list[str] = []
                children.append(tabs_id)
                pos[tabs_id] = {"type": "TABS", "id": tabs_id, "children": tab_ids, "parents": list(parents), "meta": {}}
                for t, tab in enumerate(block["tabs"], 1):
                    tab_id = f"TAB-{suffix}-{t}"
                    tab_children: list[str] = []
                    tab_ids.append(tab_id)
                    pos[tab_id] = {
                        "type": "TAB",
                        "id": tab_id,
                        "children": tab_children,
                        "parents": [*parents, tabs_id],
                        "meta": {"text": tab["label"]},
                    }
                    add_blocks(tab["blocks"], tab_children, [*parents, tabs_id, tab_id], f"{suffix}-{t}-")
            else:
                row_id = f"ROW-{suffix}"
                item_ids = [f"CHART-{i['chart']}" if "chart" in i else f"MARKDOWN-{suffix}-{c}"
                            for c, i in enumerate(block["items"], 1)]
                children.append(row_id)
                pos[row_id] = {
                    "type": "ROW",
                    "id": row_id,
                    "children": item_ids,
                    "parents": list(parents),
                    "meta": {"background": BACKGROUND},
                }
                for node_id, item in zip(item_ids, block["items"]):
                    if "chart" in item:
                        chart = by_key[item["chart"]]
                        pos[node_id] = {
                            "type": "CHART",
                            "id": node_id,
                            "children": [],
                            "parents": [*parents, row_id],
                            "meta": {
                                # 자리표시(필수). import가 실제 id로 바꾼다
                                "chartId": numbers[chart["key"]],
                                "uuid": object_uuid(project.name, "chart", chart["key"]),
                                "sliceName": chart["title"],
                                "width": item["width"],
                                "height": item["height"],
                            },
                        }
                    else:
                        pos[node_id] = {
                            "type": "MARKDOWN",
                            "id": node_id,
                            "children": [],
                            "parents": [*parents, row_id],
                            "meta": {"code": item["markdown"], "width": item["width"], "height": item["height"]},
                        }

    add_blocks(layout_blocks(d), grid_children, ROOT_PATH, "")
    return pos


def _default_mask(flt: Mapping[str, Any]) -> dict:
    """필터 기본값 → defaultDataMask. 형태는 화면에서 기본값을 저장한 대시보드의 export에서 확인했다(superset-notes.md 12절)."""
    if flt.get("default") is None:
        return {"extraFormData": {}, "filterState": {}, "ownState": {}}
    value = flt["default"]
    kind = flt["type"]
    if kind == "select":
        values = list(value) if isinstance(value, list) else [value]
        extra: dict[str, Any] = {"filters": [{"col": flt["column"], "op": "IN", "val": values}]}
        # 같은 목록 객체를 두 번 쓰면 YAML에 앵커(&id)가 생기므로 복사한다
        state: dict[str, Any] = {"label": ", ".join(str(v) for v in values), "value": list(values)}
    elif kind == "number_range":
        low, high = value
        extra = {"filters": [f for f in (
            {"col": flt["column"], "op": ">=", "val": low} if low is not None else None,
            {"col": flt["column"], "op": "<=", "val": high} if high is not None else None,
        ) if f]}
        label = f"{low} ≤ x ≤ {high}" if None not in (low, high) else f"x ≥ {low}" if high is None else f"x ≤ {high}"
        state = {"label": label, "value": [low, high]}
    elif kind == "time_range":
        extra = {"time_range": value}
        state = {"value": value}
    else:  # time_grain
        extra = {"time_grain_sqla": value}
        state = {"label": TIME_GRAIN_LABELS.get(value, value), "value": [value]}
    return {"extraFormData": extra, "filterState": state, "ownState": {}}


def native_filters(project: Project, dkey: str) -> list[dict]:
    d = project.dashboard["dashboards"][dkey]
    numbers = _chart_numbers(d)
    out = []
    for flt in d["filters"]:
        kind = flt["type"]
        target: dict[str, Any] = {}
        if flt.get("column"):
            target["column"] = {"name": flt["column"]}
        if flt.get("dataset"):
            target["datasetUuid"] = object_uuid(project.name, "dataset", flt["dataset"])
        if kind == "select":
            controls: dict[str, Any] = {
                "multiSelect": bool(flt.get("multi", True)),
                "enableEmptyFilter": False,
                "defaultToFirstItem": False,
                "inverseSelection": False,
                "searchAllOptions": False,
            }
        else:
            controls = {"enableEmptyFilter": False}
        excluded = [numbers[k] for k in numbers if k not in flt["charts"]] if "charts" in flt else []
        out.append({
            "id": f"NATIVE_FILTER-{flt['key']}",
            "name": flt["label"],
            "filterType": FILTER_TYPES[kind],
            "type": "NATIVE_FILTER",
            "targets": [target],
            "controlValues": controls,
            "defaultDataMask": _default_mask(flt),
            "cascadeParentIds": [f"NATIVE_FILTER-{flt['parent']}"] if flt.get("parent") else [],
            "scope": {"rootPath": ["ROOT_ID"], "excluded": excluded},
            "description": "",
        })
    return out


def dashboard_yaml(project: Project, dkey: str) -> dict:
    d = project.dashboard["dashboards"][dkey]
    return {
        "dashboard_title": d["title"],
        "description": None,
        "css": None,
        "slug": _slug(f"{project.name}-{dkey}"),
        "certified_by": None,
        "certification_details": None,
        "published": True,
        "uuid": object_uuid(project.name, "dashboard", dkey),
        "position": dashboard_position(project, dkey),
        "metadata": {
            "native_filter_configuration": native_filters(project, dkey),
            "color_scheme": "",
            "cross_filters_enabled": bool(d.get("cross_filters", False)),
            "refresh_frequency": 0,
        },
        "theme_uuid": None,
        "version": EXPORT_VERSION,
    }


# ---------------------------------------------------------------- 번들

def build_bundle(
    project: Project,
    view_columns: ViewColumns,
    *,
    env: Mapping[str, str] | None = None,
    timestamp: datetime | None = None,
) -> dict[str, str]:
    """번들 파일 dict(zip 최상위 폴더를 뺀 상대 경로 → YAML 텍스트, 경로 정렬).

    view_columns: {데이터셋 키: [(열 이름, Postgres data_type), …]} (ordinal 순서) 또는 {열: 타입}.
    env: DATA_DB_* 기본값과 SYNC_LOCK_UI를 읽을 환경(기본 os.environ).
    """
    env = os.environ if env is None else env
    missing = [k for k in project.dashboard["datasets"] if k not in view_columns]
    if missing:
        raise KeyError(f"뷰 열 정보가 없는 데이터셋: {', '.join(missing)}")

    ts = (timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc)

    def marked(doc: dict, kind: str, key: str) -> str:
        return dump_yaml({**doc, **managed_mark(project, kind, key, env)})

    files: dict[str, str] = {
        "metadata.yaml": dump_yaml({"version": EXPORT_VERSION, "type": "assets", "timestamp": ts.isoformat()}),
        DATABASE_FILE: marked(database_yaml(project, env), "database", DATABASE_KEY),
    }
    for key in project.dashboard["datasets"]:
        files[f"{DATASET_DIR}/{key}.yaml"] = marked(dataset_yaml(project, key, view_columns[key], env), "dataset", key)
    time_columns = time_filter_columns(project, view_columns)
    for _dkey, chart in charts(project):
        doc = chart_yaml(project, chart, time_columns.get(chart["key"]))
        files[f"charts/{chart['key']}.yaml"] = marked(doc, "chart", chart["key"])
    for dkey in project.dashboard["dashboards"]:
        files[f"dashboards/{dkey}.yaml"] = marked(dashboard_yaml(project, dkey), "dashboard", dkey)
    return dict(sorted(files.items()))


def zip_bundle(files: Mapping[str, str], root: str = BUNDLE_ROOT) -> bytes:
    """결정적 zip: 경로 정렬, 항목 시각·권한 고정."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(files):
            info = zipfile.ZipInfo(f"{root}/{path}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[path].encode("utf-8"))
    return buf.getvalue()


def _write_tree(files: Mapping[str, str], tree: Path) -> None:
    if tree.exists():
        shutil.rmtree(tree)
    for path, text in files.items():
        target = tree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def write_applied(files: Mapping[str, str], out_dir: Path) -> Path:
    """import에 성공한 번들을 <out_dir>/applied/에 남긴다. 새 폴더에 다 쓴 뒤 바꿔 끼워, 중간에 끊겨도 반쯤 쓴 기준이 남지 않는다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tree = out_dir / APPLIED_ROOT
    fresh = out_dir / f".{APPLIED_ROOT}.{os.getpid()}.tmp"
    _write_tree(files, fresh)
    if tree.exists():
        shutil.rmtree(tree)
    os.replace(fresh, tree)
    return tree


def write_build(files: Mapping[str, str], zip_bytes: bytes, out_dir: Path) -> Path:
    """<out_dir>/bundle.zip과 풀어 둔 <out_dir>/bundle/을 쓴다(이전 내용은 지운다). zip 경로를 돌려준다.

    이 폴더는 "이번에 만든 번들"이다(import 실패 원인을 볼 때 쓴다). 적용에 성공한 번들은 write_applied가 따로 남긴다.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_tree(files, out_dir / BUNDLE_ROOT)
    zip_path = out_dir / "bundle.zip"
    tmp = zip_path.with_suffix(".zip.tmp")
    tmp.write_bytes(zip_bytes)
    os.replace(tmp, zip_path)
    return zip_path
