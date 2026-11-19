"""설정 + 뷰 열 정보 → Superset 6.1.0 asset 번들(파일 dict). 순수 함수만 둔다(DB·네트워크 없음).

기준: contracts/chart-shorthand.md(축약 → 파라미터. 유형별 대응은 dashkit/charts/), contracts/state-and-uuid.md(UUID),
contracts/superset-notes.md 2절(번들 형식 함정), contracts/superset-6.1.0-export/(키 구성).

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

from ..charts import build_params
from ..config import Project, charts, layout_rows, object_uuid

BUNDLE_ROOT = "bundle"              # zip 안 최상위 폴더(= .build/<프로젝트>/bundle/)
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

def chart_params(chart: Mapping[str, Any]) -> dict:
    """차트 설정 → Superset params. 축약 유형의 대응은 차트 유형 레지스트리(dashkit/charts/)가 맡는다."""
    if "viz_type" in chart:
        return {"viz_type": chart["viz_type"], **(chart.get("params") or {})}
    # params는 마지막에 얕게 덮어쓴다
    return {**build_params(chart), **(chart.get("params") or {})}


def chart_yaml(project: Project, chart: Mapping[str, Any]) -> dict:
    params = chart_params(chart)
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

def dashboard_position(project: Project, dkey: str) -> dict:
    d = project.dashboard["dashboards"][dkey]
    by_key = {c["key"]: c for c in d["charts"]}
    rows = layout_rows(d)
    row_ids = [f"ROW-{i}" for i in range(1, len(rows) + 1)]
    pos: dict[str, Any] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": row_ids, "parents": ["ROOT_ID"]},
    }
    for row_id, items in zip(row_ids, rows):
        chart_ids = [f"CHART-{i['chart']}" for i in items]
        pos[row_id] = {
            "type": "ROW",
            "id": row_id,
            "children": chart_ids,
            "parents": ["ROOT_ID", "GRID_ID"],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
        }
        for cid, item in zip(chart_ids, items):
            chart = by_key[item["chart"]]
            pos[cid] = {
                "type": "CHART",
                "id": cid,
                "children": [],
                "parents": ["ROOT_ID", "GRID_ID", row_id],
                "meta": {
                    "chartId": 0,  # 자리표시(필수). import가 실제 id로 바꾼다
                    "uuid": object_uuid(project.name, "chart", chart["key"]),
                    "sliceName": chart["title"],
                    "width": item["width"],
                    "height": item["height"],
                },
            }
    return pos


def native_filters(project: Project, dkey: str) -> list[dict]:
    out = []
    for flt in project.dashboard["dashboards"][dkey]["filters"]:
        out.append({
            "id": f"NATIVE_FILTER-{flt['key']}",
            "name": flt["label"],
            "filterType": "filter_select",
            "type": "NATIVE_FILTER",
            "targets": [{
                "column": {"name": flt["column"]},
                "datasetUuid": object_uuid(project.name, "dataset", flt["dataset"]),
            }],
            "controlValues": {
                "multiSelect": bool(flt.get("multi", True)),
                "enableEmptyFilter": False,
                "defaultToFirstItem": False,
                "inverseSelection": False,
                "searchAllOptions": False,
            },
            "defaultDataMask": {"extraFormData": {}, "filterState": {}, "ownState": {}},
            "cascadeParentIds": [],
            "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
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
            "cross_filters_enabled": False,
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
    env: DATA_DB_* 기본값을 덮을 환경(기본 os.environ).
    """
    env = os.environ if env is None else env
    missing = [k for k in project.dashboard["datasets"] if k not in view_columns]
    if missing:
        raise KeyError(f"뷰 열 정보가 없는 데이터셋: {', '.join(missing)}")

    ts = (timestamp or datetime.now(timezone.utc)).astimezone(timezone.utc)
    files: dict[str, str] = {
        "metadata.yaml": dump_yaml({"version": EXPORT_VERSION, "type": "assets", "timestamp": ts.isoformat()}),
        DATABASE_FILE: dump_yaml(database_yaml(project, env)),
    }
    for key in project.dashboard["datasets"]:
        files[f"{DATASET_DIR}/{key}.yaml"] = dump_yaml(dataset_yaml(project, key, view_columns[key], env))
    for _dkey, chart in charts(project):
        files[f"charts/{chart['key']}.yaml"] = dump_yaml(chart_yaml(project, chart))
    for dkey in project.dashboard["dashboards"]:
        files[f"dashboards/{dkey}.yaml"] = dump_yaml(dashboard_yaml(project, dkey))
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


def write_build(files: Mapping[str, str], zip_bytes: bytes, out_dir: Path) -> Path:
    """<out_dir>/bundle.zip과 풀어 둔 <out_dir>/bundle/을 쓴다(이전 내용은 지운다). zip 경로를 돌려준다."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tree = out_dir / BUNDLE_ROOT
    if tree.exists():
        shutil.rmtree(tree)
    for path, text in files.items():
        target = tree / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    zip_path = out_dir / "bundle.zip"
    tmp = zip_path.with_suffix(".zip.tmp")
    tmp.write_bytes(zip_bytes)
    os.replace(tmp, zip_path)
    return zip_path
