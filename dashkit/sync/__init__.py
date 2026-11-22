"""Superset 반영(sync). 순서는 contracts/state-and-uuid.md의 "sync 순서"를 따른다.

1. 뷰 열 읽기(information_schema, dash_reader)  2. 번들 생성(.build/<프로젝트>/)
3. 관리자 로그인  4. asset import  5. 대시보드마다 임베드 upsert
6. prune((이전 managed.json ∪ 검증된 현재·이전 프로젝트 객체) − 이번 목록 − 수동 의존 보존, 대시보드 → 차트 → 데이터셋 → DB)
7. embed.json, managed.json 원자적 쓰기
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlsplit

from ..config import Project, check_view_columns, object_uuid
from .bundle import (DATABASE_FILE, DATABASE_KEY, KINDS, VIEW_SCHEMA, _column_pairs, build_bundle, database_name,
                     lock_ui, managed_objects, write_applied, write_build, zip_bundle)
from .client import SupersetClient, _rison_str
from .errors import SyncError

__all__ = [
    "sync",
    "prune_project",
    "plan_prune",
    "discover_project_objects",
    "fetch_snapshot",
    "find_marked",
    "read_switch_lineage",
    "read_view_columns",
    "SupersetClient",
    "SupersetSnapshot",
    "PreservedItem",
    "PrunePlan",
    "SyncError",
]

STATE_VERSION = 1
DEFAULT_WEB_ORIGINS = "http://localhost:8080,http://127.0.0.1:8080"
_PROJECT_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def _log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 뷰 열·적재 이력(dash_reader)

def read_view_columns(project: Project) -> dict[str, list[tuple[str, str]]]:
    """dash 스키마 뷰의 열을 ordinal 순서로 읽는다(dash_reader)."""
    from ..db import reader_conn

    keys = list(project.dashboard["datasets"])
    out: dict[str, list[tuple[str, str]]] = {}
    try:
        with reader_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name, data_type, udt_name FROM information_schema.columns "
                "WHERE table_schema = 'dash' AND table_name = ANY(%s) ORDER BY table_name, ordinal_position",
                (keys,),
            )
            for table, col, data_type, udt in cur.fetchall():
                t = udt if data_type in ("USER-DEFINED", "ARRAY") else data_type
                out.setdefault(table, []).append((col, t))
    except SyncError:
        raise
    except Exception as exc:  # psycopg 오류, 환경변수 누락
        raise SyncError(f"데이터 DB에서 뷰 열 정보를 읽지 못함: {exc}") from exc
    return out


def _valid_project_names(names: Iterable[object], *, exclude: str | None = None) -> list[str]:
    out: list[str] = []
    for name in names:
        if isinstance(name, str) and _PROJECT_NAME_RE.fullmatch(name) and name != exclude and name not in out:
            out.append(name)
    return out


def read_switch_lineage(project_name: str, dbname: str | None = None) -> list[str]:
    """data-db 적재 기록(dash_reader)이 현재 프로젝트로 적재될 때 대체한 이전 프로젝트 목록을 읽는다."""
    from ..loader import current

    try:
        with current.reader(dbname) as conn:
            snap = current.read_load_snapshot(conn)
    except current.CurrentError:
        return []
    if not isinstance(snap, Mapping) or snap.get("project") != project_name:
        return []
    raw = snap.get("replaced_projects")
    if not isinstance(raw, list):
        return []
    return _valid_project_names(raw, exclude=project_name)


def _read_active_db_project(dbname: str | None = None) -> str | None:
    from ..loader import current

    try:
        with current.reader(dbname) as conn:
            snap = current.read_load_snapshot(conn)
    except current.CurrentError:
        return None
    proj = snap.get("project") if isinstance(snap, Mapping) else None
    return proj if isinstance(proj, str) and _PROJECT_NAME_RE.fullmatch(proj) else None


# ---------------------------------------------------------------- 상태 파일

def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_managed(path: Path) -> dict[str, list[str]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        objects = data.get("objects") or {}
        return {k: [str(u) for u in objects.get(k) or []] for k in KINDS}
    except (ValueError, AttributeError, TypeError) as exc:
        raise SyncError(f"{path}를 읽지 못함({exc}). 내용을 확인하거나 지운 뒤 다시 실행하세요") from exc


def _read_embed_project(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    proj = data.get("project") if isinstance(data, dict) else None
    return proj if isinstance(proj, str) and _PROJECT_NAME_RE.fullmatch(proj) else None


def _managed_doc(project: Project, objects: Mapping[str, list[str]]) -> dict:
    return {
        "version": STATE_VERSION,
        "project": project.name,
        "synced_at": _now(),
        "objects": {k: list(objects.get(k) or []) for k in KINDS},
    }


def _union(a: Mapping[str, Iterable[str]], b: Mapping[str, Iterable[str]]) -> dict[str, list[str]]:
    out = {}
    for k in KINDS:
        seen = list(a.get(k) or [])
        seen += [u for u in b.get(k) or [] if u not in seen]
        out[k] = seen
    return out


# ---------------------------------------------------------------- DB 대상 비교·스냅샷

@dataclass(frozen=True)
class SupersetSnapshot:
    databases: list[dict[str, Any]]
    datasets: list[dict[str, Any]]
    charts: list[dict[str, Any]]
    dashboards: list[dict[str, Any]]


@dataclass(frozen=True)
class PreservedItem:
    kind: str
    uuid: str
    reason: str


@dataclass(frozen=True)
class PrunePlan:
    delete: dict[str, list[str]]
    preserved: list[PreservedItem]
    other_dashkit_dbs: list[str]


def _db_target_from_env(env: Mapping[str, str]) -> tuple[str, int, str]:
    host = (env.get("DATA_DB_HOST") or "data-db").strip().lower()
    port_raw = (env.get("DATA_DB_PORT") or "5432").strip()
    try:
        port = int(port_raw)
    except ValueError:
        port = 5432
    dbname = (env.get("DATA_DB_NAME") or "dash").strip()
    return host, port, dbname


def _db_target_from_connection(conn_info: Mapping[str, object] | None) -> tuple[str, int, str] | None:
    """Superset /database/<id>/connection 응답에서 (host, port, dbname)을 읽는다. 알 수 없으면 None."""
    if not isinstance(conn_info, Mapping):
        return None
    params = conn_info.get("parameters")
    if isinstance(params, Mapping):
        host = params.get("host")
        dbname = params.get("database")
        port_val = params.get("port")
        if isinstance(host, str) and host.strip() and isinstance(dbname, str) and dbname.strip():
            port: int | None = None
            if isinstance(port_val, int) and not isinstance(port_val, bool):
                port = port_val
            elif isinstance(port_val, str) and port_val.strip().isdigit():
                port = int(port_val.strip())
            elif port_val is None:
                port = 5432
            if port is not None:
                return host.strip().lower(), port, dbname.strip()
    uri = conn_info.get("sqlalchemy_uri")
    if isinstance(uri, str) and "://" in uri:
        try:
            parts = urlsplit(uri)
        except ValueError:
            return None
        host = parts.hostname
        dbname = parts.path.lstrip("/")
        if host and dbname:
            try:
                port = parts.port if parts.port is not None else 5432
            except ValueError:
                return None
            return host.lower(), int(port), dbname
    return None


def fetch_snapshot(client: SupersetClient) -> SupersetSnapshot:
    """Superset의 DB·데이터셋·차트·대시보드 목록을 읽어 스냅샷으로 묶는다."""
    return SupersetSnapshot(
        databases=client.list_objects("database", ["id", "uuid", "database_name"]),
        datasets=client.list_objects("dataset", ["id", "uuid", "table_name", "schema", "database.id"]),
        charts=client.list_objects(
            "chart", ["id", "uuid", "is_managed_externally", "datasource_id", "datasource_type", "dashboards.id"]
        ),
        dashboards=client.list_objects("dashboard", ["id", "uuid", "slug", "is_managed_externally"]),
    )


# ---------------------------------------------------------------- 표식·프로젝트별 객체 탐색

def _made_by_sync(row: Mapping[str, object]) -> bool:
    """sync가 만든 객체인지. 표식(is_managed_externally)이 있거나 UUID가 버전 5다.

    Superset 화면에서 만든 객체는 표식이 없고 UUID가 버전 4다. 버전 5는 SYNC_LOCK_UI=0으로 만든 객체(표식의
    is_managed_externally가 false)를 알아보기 위한 것이다(contracts/state-and-uuid.md "표식").
    """
    if row.get("is_managed_externally") is True:
        return True
    try:
        return uuid.UUID(str(row.get("uuid"))).version == 5
    except ValueError:
        return False


def _is_project_dashboard(row: Mapping[str, object], project_name: str) -> bool:
    """대시보드가 해당 프로젝트 소유인지 검증한다(_made_by_sync + slug/uuid5 일치).

    _made_by_sync만으로는 SYNC_LOCK_UI=0 환경에서 다른 프로젝트의 관리 대시보드에 이 프로젝트 차트가 추가된
    경우를 구분하지 못한다. 번들이 생성한 slug(<project>-<key>)에서 키 후보를 역산해 object_uuid와 대조하고,
    소유를 확인할 수 없으면 False를 돌려주어 보존한다.
    """
    if not _made_by_sync(row):
        return False
    slug = row.get("slug")
    prefix = f"{project_name.replace('_', '-')}-"
    if not isinstance(slug, str) or not slug.startswith(prefix):
        return False
    rest = slug[len(prefix):]
    if not rest:
        return False
    row_uuid = str(row.get("uuid") or "")
    candidates = (rest.replace("-", "_"), rest) if "-" in rest else (rest,)
    return any(row_uuid == object_uuid(project_name, "dashboard", k) for k in candidates)


def _find_project_database(snap: SupersetSnapshot, project_name: str) -> dict[str, Any] | None:
    expected_name = f"dashkit-{project_name}"
    expected_uuid = object_uuid(project_name, "database", DATABASE_KEY)
    for row in snap.databases:
        if row.get("database_name") == expected_name and str(row.get("uuid")) == expected_uuid:
            return row
    return None


def discover_project_objects(
    snap: SupersetSnapshot,
    project_name: str,
    *,
    include_database: bool = False,
) -> dict[str, list[str]]:
    """스냅샷에서 특정 프로젝트의 sync가 만든 객체를 찾는다(종류 → UUID 목록).

    DB는 `dashkit-<프로젝트>` 이름과 `object_uuid(project, "database", "main")`이 일치할 때만 인정하고,
    데이터셋은 그 DB 아래 `schema == "dash"` 이며 `object_uuid(project, "dataset", table_name)`과 일치하는 것만
    인정한다. 차트·대시보드는 검증된 데이터셋에서 따라가 `_made_by_sync`인 것만 고른다.
    """
    found: dict[str, list[str]] = {k: [] for k in KINDS}
    db_row = _find_project_database(snap, project_name)
    if db_row is None:
        return found
    db_id = db_row.get("id")
    if include_database:
        found["database"] = [str(db_row["uuid"])]

    managed_datasets = [
        ds for ds in snap.datasets
        if (ds.get("database") or {}).get("id") == db_id
        and ds.get("schema") == VIEW_SCHEMA
        and isinstance(ds.get("table_name"), str)
        and str(ds.get("uuid")) == object_uuid(project_name, "dataset", ds["table_name"])
    ]
    found["dataset"] = [str(ds["uuid"]) for ds in managed_datasets]
    dataset_ids = {ds.get("id") for ds in managed_datasets if ds.get("id") is not None}
    if not dataset_ids:
        return found

    managed_charts = [
        c for c in snap.charts
        if c.get("datasource_type") == "table" and c.get("datasource_id") in dataset_ids and _made_by_sync(c)
    ]
    found["chart"] = [str(c["uuid"]) for c in managed_charts]

    dashboard_ids = {
        d.get("id") for c in managed_charts for d in (c.get("dashboards") or [])
        if isinstance(d, Mapping) and d.get("id") is not None
    }
    if dashboard_ids:
        found["dashboard"] = [
            str(d["uuid"]) for d in snap.dashboards
            if d.get("id") in dashboard_ids and _is_project_dashboard(d, project_name)
        ]
    return found


def find_marked(client: SupersetClient, project: Project) -> dict[str, list[str]]:
    """Superset에서 이 프로젝트의 sync가 만든 객체를 찾는다(종류 → UUID 목록). 상태 파일 없이 prune 대상을 구하는 데 쓴다."""
    return discover_project_objects(fetch_snapshot(client), project.name, include_database=False)


# ---------------------------------------------------------------- prune 계획·실행(순수 + 역방향 불변식)

def _rows_by_uuid(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(r["uuid"]): r for r in rows if r.get("uuid") is not None}


def plan_prune(
    snap: SupersetSnapshot,
    current: Mapping[str, Iterable[str]],
    candidates: Mapping[str, Iterable[str]],
    *,
    current_project: str | None = None,
) -> PrunePlan:
    """수동·타 객체 의존성을 보존하면서 삭제 대상과 미식별 타 dashkit DB를 계산한다."""
    by_uuid = {
        "database": _rows_by_uuid(snap.databases),
        "dataset": _rows_by_uuid(snap.datasets),
        "chart": _rows_by_uuid(snap.charts),
        "dashboard": _rows_by_uuid(snap.dashboards),
    }

    raw_delete: dict[str, list[str]] = {}
    for kind in KINDS:
        keep = set(current.get(kind) or ())
        seen: list[str] = []
        for u in candidates.get(kind) or ():
            s = str(u)
            if s not in keep and s not in seen:
                seen.append(s)
        raw_delete[kind] = seen

    delete: dict[str, list[str]] = {k: [] for k in KINDS}
    preserved: list[PreservedItem] = []

    delete["dashboard"] = list(raw_delete["dashboard"])
    del_dash_ids = {
        by_uuid["dashboard"][u]["id"] for u in delete["dashboard"]
        if u in by_uuid["dashboard"] and by_uuid["dashboard"][u].get("id") is not None
    }

    del_chart_ids: set[Any] = set()
    for u in raw_delete["chart"]:
        row = by_uuid["chart"].get(u)
        if row is not None:
            dash_ids = {
                d.get("id") for d in (row.get("dashboards") or [])
                if isinstance(d, Mapping) and d.get("id") is not None
            }
            if dash_ids - del_dash_ids:
                preserved.append(PreservedItem("chart", u, "수동 또는 다른 대시보드가 참조 중"))
                continue
            if row.get("id") is not None:
                del_chart_ids.add(row["id"])
        delete["chart"].append(u)

    del_ds_ids: set[Any] = set()
    for u in raw_delete["dataset"]:
        row = by_uuid["dataset"].get(u)
        if row is not None:
            ds_id = row.get("id")
            if ds_id is not None and any(
                c.get("datasource_type") == "table"
                and c.get("datasource_id") == ds_id
                and c.get("id") not in del_chart_ids
                for c in snap.charts
            ):
                preserved.append(PreservedItem("dataset", u, "수동 또는 보존된 차트가 참조 중"))
                continue
            if ds_id is not None:
                del_ds_ids.add(ds_id)
        delete["dataset"].append(u)

    for u in raw_delete["database"]:
        row = by_uuid["database"].get(u)
        if row is not None:
            db_id = row.get("id")
            if db_id is not None and any(
                (ds.get("database") or {}).get("id") == db_id and ds.get("id") not in del_ds_ids
                for ds in snap.datasets
            ):
                preserved.append(PreservedItem("database", u, "수동 또는 보존된 데이터셋이 소속됨"))
                continue
        delete["database"].append(u)

    accounted_dbs = (
        set(map(str, current.get("database") or ()))
        | set(delete["database"])
        | {p.uuid for p in preserved if p.kind == "database"}
    )
    other_dbs: list[str] = []
    for row in snap.databases:
        dname = row.get("database_name")
        u = str(row.get("uuid") or "")
        if not isinstance(dname, str) or not dname.startswith("dashkit-"):
            continue
        proj_name = dname[len("dashkit-"):]
        if not _PROJECT_NAME_RE.fullmatch(proj_name) or proj_name == current_project:
            continue
        if u == object_uuid(proj_name, "database", DATABASE_KEY) and u not in accounted_dbs:
            other_dbs.append(proj_name)

    return PrunePlan(delete=delete, preserved=preserved, other_dashkit_dbs=sorted(set(other_dbs)))


def _blocked_by_failed_child(kind: str, row: Mapping[str, Any] | None, snap: SupersetSnapshot,
                             failed_ids: Mapping[str, set[Any]]) -> bool:
    if row is None:
        return False
    if kind == "chart":
        return any(
            isinstance(d, Mapping) and d.get("id") in failed_ids["dashboard"]
            for d in (row.get("dashboards") or [])
        )
    if kind == "dataset":
        ds_id = row.get("id")
        return ds_id is not None and any(
            c.get("datasource_type") == "table" and c.get("datasource_id") == ds_id and c.get("id") in failed_ids["chart"]
            for c in snap.charts
        )
    if kind == "database":
        db_id = row.get("id")
        return db_id is not None and any(
            (ds.get("database") or {}).get("id") == db_id and ds.get("id") in failed_ids["dataset"]
            for ds in snap.datasets
        )
    return False


def _execute_prune(
    client: SupersetClient,
    snap: SupersetSnapshot,
    plan: PrunePlan,
    *,
    log: Callable[[str], None] = _log,
) -> tuple[int, int, list[str], dict[str, list[str]]]:
    """대시보드 → 차트 → 데이터셋 → DB 순으로 삭제한다. 하위 삭제가 실패하면 상위 부모는 건너뛰어 계보를 유지한다."""
    by_uuid = {
        "database": _rows_by_uuid(snap.databases),
        "dataset": _rows_by_uuid(snap.datasets),
        "chart": _rows_by_uuid(snap.charts),
        "dashboard": _rows_by_uuid(snap.dashboards),
    }
    deleted, absent, failed = 0, 0, []
    leftovers: dict[str, list[str]] = {k: [] for k in KINDS}
    failed_ids: dict[str, set[Any]] = {k: set() for k in KINDS}

    for kind in reversed(KINDS):
        for u in plan.delete[kind]:
            row = by_uuid[kind].get(u)
            if _blocked_by_failed_child(kind, row, snap, failed_ids):
                failed.append(f"{kind} {u}: 하위 객체 삭제 실패로 건너뜀")
                leftovers[kind].append(u)
                if row is not None and row.get("id") is not None:
                    failed_ids[kind].add(row["id"])
                continue
            obj_id: int | None = None
            try:
                obj_id = client.find_id(kind, u)
                if obj_id is None or not client.delete_object(kind, obj_id):
                    absent += 1
                else:
                    deleted += 1
            except SyncError as exc:
                failed.append(f"{kind} {u}: {exc}")
                leftovers[kind].append(u)
                fail_id = obj_id if obj_id is not None else (row.get("id") if row is not None else None)
                if fail_id is not None:
                    failed_ids[kind].add(fail_id)

    preserved_note = f", 수동 참조 보존 {len(plan.preserved)}개" if plan.preserved else ""
    log(
        f"· prune: {deleted}개 삭제"
        + (f", 이미 없음 {absent}개" if absent else "")
        + preserved_note
        + (f", 실패 {len(failed)}개" if failed else "")
    )
    for item in plan.preserved:
        log(f"  · 보존: {item.kind} {item.uuid} ({item.reason})")
    return deleted, absent, failed, leftovers


# ---------------------------------------------------------------- sync

def _collect_previous_candidates(
    client: SupersetClient,
    snap: SupersetSnapshot,
    prev_projects: list[str],
    expected_target: tuple[str, int, str],
    candidates: dict[str, list[str]],
    *,
    log: Callable[[str], None],
) -> tuple[dict[str, list[str]], set[str]]:
    skipped: set[str] = set()
    for prev_name in prev_projects:
        db_row = _find_project_database(snap, prev_name)
        if db_row is None or db_row.get("id") is None:
            continue
        try:
            conn_info = client.get_database_connection(int(db_row["id"]))
        except SyncError as exc:
            log(f"! 안내: 이전 프로젝트 '{prev_name}' DB 연결을 확인하지 못해 자동 정리하지 않음({exc})")
            skipped.add(prev_name)
            continue
        actual_target = _db_target_from_connection(conn_info)
        if actual_target != expected_target:
            log(
                f"! 안내: 이전 프로젝트 '{prev_name}'의 DB 연결 대상({actual_target})이 "
                f"현재 데이터 DB({expected_target})와 달라 자동 정리하지 않음"
            )
            skipped.add(prev_name)
            continue
        candidates = _union(candidates, discover_project_objects(snap, prev_name, include_database=True))
    return candidates, skipped


def sync(
    project: Project,
    *,
    state_dir: Path | None = None,
    build_dir: Path | None = None,
    client: SupersetClient | None = None,
    view_columns: Mapping[str, object] | None = None,
    replaced_projects: Iterable[str] | None = None,
    log: Callable[[str], None] = _log,
) -> dict[str, list[str]]:
    """설정을 Superset에 반영한다. 종류 → 이번에 관리하는 UUID 목록을 돌려준다.

    client, view_columns, replaced_projects는 테스트용 주입 지점이다.
    """
    state_dir = Path(state_dir or os.environ.get("STATE_DIR") or "/app/.state")
    build_dir = Path(build_dir or os.environ.get("BUILD_DIR") or "/app/.build")
    managed_path = state_dir / "managed.json"
    embed_path = state_dir / "embed.json"

    # 1. 뷰 열 및 전환 이력
    if view_columns is None:
        view_columns = read_view_columns(project)
        lineage = read_switch_lineage(project.name) if replaced_projects is None else list(replaced_projects)
    else:
        lineage = [] if replaced_projects is None else list(replaced_projects)
    errors = [f"dashboard.yaml:datasets.{k}: 데이터셋 '{k}' 뷰가 없음"
              for k in project.dashboard["datasets"] if k not in view_columns]
    errors += check_view_columns(project, {k: {c for c, _t in _column_pairs(v)} for k, v in view_columns.items()})
    errors = list(dict.fromkeys(errors))
    if errors:
        raise SyncError("뷰 열이 설정과 맞지 않음(먼저 make load):\n  " + "\n  ".join(errors))

    # 2. 번들
    files = build_bundle(project, view_columns)
    zip_bytes = zip_bundle(files)
    zip_path = write_build(files, zip_bytes, build_dir / project.name)
    current = managed_objects(project)
    log(f"· 번들: {zip_path} (데이터셋 {len(current['dataset'])}, 차트 {len(current['chart'])}, "
        f"대시보드 {len(current['dashboard'])}, Superset 화면 편집 {'차단' if lock_ui(os.environ) else '허용(SYNC_LOCK_UI=0)'})")

    password = os.environ.get("DATA_DB_READER_PASSWORD")
    if not password:
        raise SyncError("환경변수 DATA_DB_READER_PASSWORD가 필요합니다(import passwords)")

    previous = _read_managed(managed_path)
    # 5단계에서 embed.json을 새 프로젝트로 덮어쓰기 전에 이전 프로젝트 힌트를 읽는다
    embed_prev = _read_embed_project(embed_path)
    prev_projects = _valid_project_names([embed_prev, *lineage], exclude=project.name)

    # 3. 로그인
    client = client or SupersetClient()
    client.wait_ready()
    client.login()

    # import가 중간에 실패해도 만들어졌을 수 있는 객체를 다음 실행이 prune할 수 있게 먼저 합집합을 기록한다
    _write_json_atomic(managed_path, _managed_doc(project, _union(previous, current)))

    # 4. import
    client.import_bundle(zip_bytes, {DATABASE_FILE: password})
    # import에 성공한 번들만 plan의 비교 기준으로 남긴다(실패한 sync의 번들이 '적용된 상태'로 보이지 않게)
    write_applied(files, build_dir / project.name)
    log(f"· import: Superset에 번들 가져오기 완료({client.base_url})")

    # 5. 임베드
    origins = [o.strip() for o in (os.environ.get("WEB_ORIGINS") or DEFAULT_WEB_ORIGINS).split(",") if o.strip()]
    embeds: dict[str, dict[str, str]] = {}
    for dkey in project.dashboard["dashboards"]:
        d_uuid = object_uuid(project.name, "dashboard", dkey)
        d_id = client.find_id("dashboard", d_uuid)
        if d_id is None:
            raise SyncError(f"import 뒤 대시보드 '{dkey}'({d_uuid})를 찾지 못함 — Superset 로그를 확인하세요(make logs)")
        embeds[dkey] = {"dashboard_uuid": d_uuid, "embed_uuid": client.set_embedded(d_id, origins)}
    log(f"· embed: 대시보드 {len(embeds)}개 임베드 설정(allowed_domains: {', '.join(origins)})")

    # embed.json은 import·임베드가 끝났으면 prune 결과와 관계없이 새 상태다
    _write_json_atomic(embed_path, {
        "version": STATE_VERSION,
        "project": project.name,
        "synced_at": _now(),
        "dashboards": embeds,
    })

    # 6. prune. 대상 = (managed.json의 이전 목록 ∪ 현재·검증된 이전 프로젝트 객체) − 이번 목록 − 수동 보존
    snap = fetch_snapshot(client)
    candidates = _union(previous, discover_project_objects(snap, project.name, include_database=False))
    candidates, skipped = _collect_previous_candidates(
        client, snap, prev_projects, _db_target_from_env(os.environ), candidates, log=log
    )
    prune_plan = plan_prune(snap, current, candidates, current_project=project.name)
    _deleted, _absent, failed, leftovers = _execute_prune(client, snap, prune_plan, log=log)

    unaccounted = [p for p in prune_plan.other_dashkit_dbs if p not in skipped]
    if unaccounted:
        joined = ", ".join(unaccounted)
        log(
            f"! 안내: 정리 이력에 없는 다른 dashkit 프로젝트 DB({joined})가 남아 있어 자동 삭제하지 않음. "
            f"필요하면 `python -m dashkit prune-project <이름> --dry-run`으로 확인하세요"
        )

    # 7. managed.json (prune에 실패한 객체는 남겨 다음 실행에서 다시 시도. 수동 보존 항목은 남기지 않는다)
    _write_json_atomic(managed_path, _managed_doc(project, _union(current, leftovers)))
    if failed:
        raise SyncError("import·임베드는 끝났지만 prune 일부 실패(다시 실행하면 재시도):\n  " + "\n  ".join(failed))
    return current


# ---------------------------------------------------------------- 명시적 이전 프로젝트 정리 CLI

def prune_project(
    target_name: str,
    *,
    dry_run: bool = False,
    state_dir: Path | None = None,
    client: SupersetClient | None = None,
    active_projects: Iterable[str] | None = None,
    log: Callable[[str], None] = _log,
) -> dict[str, Any]:
    """지정한 이전 프로젝트의 Superset 관리 객체를 정리한다(활성 프로젝트는 거부)."""
    if not _PROJECT_NAME_RE.fullmatch(target_name or ""):
        raise SyncError(f"프로젝트 이름 형식 오류 '{target_name}' (소문자, 숫자, _, -)")

    state_dir = Path(state_dir or os.environ.get("STATE_DIR") or "/app/.state")
    if active_projects is None:
        embed_proj = _read_embed_project(state_dir / "embed.json")
        db_proj = _read_active_db_project()
        active = {p for p in (embed_proj, db_proj) if p}
        if not active:
            env_proj = (os.environ.get("PROJECT") or "").strip()
            if _PROJECT_NAME_RE.fullmatch(env_proj):
                active.add(env_proj)
    else:
        active = {p for p in active_projects if isinstance(p, str) and p}

    if target_name in active:
        raise SyncError(f"현재 활성 프로젝트 '{target_name}'는 정리할 수 없습니다")

    client = client or SupersetClient()
    client.wait_ready()
    client.login()

    snap = fetch_snapshot(client)
    db_row = _find_project_database(snap, target_name)
    if db_row is None or db_row.get("id") is None:
        log(f"· prune-project: '{target_name}'의 관리 DB가 Superset에 없어 정리할 대상이 없음")
        return {
            "project": target_name,
            "dry_run": dry_run,
            "deleted": 0,
            "absent": 0,
            "preserved": [],
            "planned": {k: [] for k in KINDS},
        }

    try:
        conn_info = client.get_database_connection(int(db_row["id"]))
    except SyncError:
        conn_info = None
    target = _db_target_from_connection(conn_info)
    target_desc = f"{target[0]}:{target[1]}/{target[2]}" if target else "알 수 없음"
    log(f"· 대상 프로젝트: {target_name} (DB 연결: {target_desc})")

    candidates = discover_project_objects(snap, target_name, include_database=True)
    prune_plan = plan_prune(snap, {k: [] for k in KINDS}, candidates, current_project=None)
    counts = ", ".join(f"{k} {len(prune_plan.delete[k])}" for k in reversed(KINDS))
    if dry_run:
        log(f"· 미리보기(삭제하지 않음): {counts}"
            + (f", 수동 참조 보존 {len(prune_plan.preserved)}개" if prune_plan.preserved else ""))
        for kind in reversed(KINDS):
            for u in prune_plan.delete[kind]:
                log(f"  - 삭제 예정: {kind} {u}")
        for item in prune_plan.preserved:
            log(f"  - 보존 예정: {item.kind} {item.uuid} ({item.reason})")
        return {
            "project": target_name,
            "dry_run": True,
            "deleted": 0,
            "absent": 0,
            "preserved": [{"kind": p.kind, "uuid": p.uuid, "reason": p.reason} for p in prune_plan.preserved],
            "planned": prune_plan.delete,
        }

    deleted, absent, failed, _leftovers = _execute_prune(client, snap, prune_plan, log=log)
    if failed:
        raise SyncError("prune-project 일부 실패:\n  " + "\n  ".join(failed))
    return {
        "project": target_name,
        "dry_run": False,
        "deleted": deleted,
        "absent": absent,
        "preserved": [{"kind": p.kind, "uuid": p.uuid, "reason": p.reason} for p in prune_plan.preserved],
        "planned": prune_plan.delete,
    }
