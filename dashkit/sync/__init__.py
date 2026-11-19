"""Superset 반영(sync). 순서는 contracts/state-and-uuid.md의 "sync 순서"를 따른다.

1. 뷰 열 읽기(information_schema, dash_reader)  2. 번들 생성(.build/<프로젝트>/)
3. 관리자 로그인  4. asset import  5. 대시보드마다 임베드 upsert
6. prune((이전 managed.json ∪ Superset에서 찾은 이 프로젝트의 표식 객체) − 이번 목록, 대시보드 → 차트 → 데이터셋 → DB)
7. embed.json, managed.json 원자적 쓰기
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from ..config import Project, check_view_columns, object_uuid
from .bundle import (DATABASE_FILE, KINDS, _column_pairs, build_bundle, database_name, lock_ui, managed_objects,
                     write_applied, write_build, zip_bundle)
from .client import SupersetClient, _rison_str
from .errors import SyncError

__all__ = ["sync", "SyncError", "SupersetClient", "read_view_columns"]

STATE_VERSION = 1
DEFAULT_WEB_ORIGINS = "http://localhost:8080,http://127.0.0.1:8080"


def _log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 뷰 열

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


def _managed_doc(project: Project, objects: Mapping[str, list[str]]) -> dict:
    return {
        "version": STATE_VERSION,
        "project": project.name,
        "synced_at": _now(),
        "objects": {k: list(objects.get(k) or []) for k in KINDS},
    }


def _union(a: Mapping[str, list[str]], b: Mapping[str, list[str]]) -> dict[str, list[str]]:
    out = {}
    for k in KINDS:
        seen = list(a.get(k) or [])
        seen += [u for u in b.get(k) or [] if u not in seen]
        out[k] = seen
    return out


# ---------------------------------------------------------------- 표식으로 찾기

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


def find_marked(client: SupersetClient, project: Project) -> dict[str, list[str]]:
    """Superset에서 이 프로젝트의 sync가 만든 객체를 찾는다(종류 → UUID 목록). 상태 파일 없이 prune 대상을 구하는 데 쓴다.

    프로젝트는 데이터베이스 이름(`dashkit-<프로젝트>`)에서 따라간다: DB → 그 DB의 데이터셋 → 그 데이터셋의 차트 →
    그 차트가 놓인 대시보드. 그 안에서 `_made_by_sync`인 것만 고른다. 다른 DB의 객체와 사람이 만든 객체는 들어가지 않는다.
    """
    found: dict[str, list[str]] = {k: [] for k in KINDS}
    name = database_name(project)
    db_uuid = object_uuid(project.name, "database", "main")
    dbs = [r for r in client.list_objects("database", ["id", "uuid", "database_name"],
                                          f"(col:database_name,opr:eq,value:{_rison_str(name)})")
           if r.get("database_name") == name and str(r.get("uuid")) == db_uuid]
    if not dbs:
        return found
    db_id = dbs[0]["id"]

    datasets = [r for r in client.list_objects("dataset", ["id", "uuid", "database.id"],
                                               f"(col:database,opr:rel_o_m,value:{int(db_id)})")
                if (r.get("database") or {}).get("id") == db_id]
    dataset_ids = {r["id"] for r in datasets}
    for r in datasets:
        # 데이터셋 목록에는 is_managed_externally 열이 없어 UUID 버전으로 먼저 보고, 아니면 한 건 조회로 확인한다
        if _made_by_sync(r) or _made_by_sync(client.get_object("dataset", r["id"]) or {}):
            found["dataset"].append(str(r["uuid"]))

    charts = [r for r in client.list_objects(
                  "chart", ["id", "uuid", "is_managed_externally", "datasource_id", "datasource_type", "dashboards.id"])
              if r.get("datasource_type") == "table" and r.get("datasource_id") in dataset_ids]
    found["chart"] = [str(r["uuid"]) for r in charts if _made_by_sync(r)]

    dashboard_ids = {d.get("id") for r in charts for d in r.get("dashboards") or []}
    if dashboard_ids:
        found["dashboard"] = [str(r["uuid"]) for r in client.list_objects("dashboard", ["id", "uuid", "is_managed_externally"])
                              if r.get("id") in dashboard_ids and _made_by_sync(r)]
    return found


# ---------------------------------------------------------------- sync

def sync(
    project: Project,
    *,
    state_dir: Path | None = None,
    build_dir: Path | None = None,
    client: SupersetClient | None = None,
    view_columns: Mapping[str, object] | None = None,
    log: Callable[[str], None] = _log,
) -> dict[str, list[str]]:
    """설정을 Superset에 반영한다. 종류 → 이번에 관리하는 UUID 목록을 돌려준다.

    client, view_columns는 테스트용 주입 지점이다(생략하면 env로 만든 클라이언트, DB에서 읽은 뷰 열).
    """
    state_dir = Path(state_dir or os.environ.get("STATE_DIR") or "/app/.state")
    build_dir = Path(build_dir or os.environ.get("BUILD_DIR") or "/app/.build")
    managed_path = state_dir / "managed.json"
    embed_path = state_dir / "embed.json"

    # 1. 뷰 열
    if view_columns is None:
        view_columns = read_view_columns(project)
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

    # 6. prune. 대상 = (managed.json의 이전 목록 ∪ Superset에서 찾은 이 프로젝트의 표식 객체) − 이번 목록.
    # managed.json의 이전 목록은 project가 달라도 지운다(프로젝트 전환). 표식으로 찾는 쪽은 managed.json이 없어도 동작한다
    candidates = _union(previous, find_marked(client, project))
    deleted, absent, failed = 0, 0, []
    leftovers: dict[str, list[str]] = {k: [] for k in KINDS}
    for kind in reversed(KINDS):
        keep = set(current[kind])
        for u in candidates.get(kind, []):
            if u in keep:
                continue
            try:
                obj_id = client.find_id(kind, u)
                if obj_id is None or not client.delete_object(kind, obj_id):
                    absent += 1
                else:
                    deleted += 1
            except SyncError as exc:
                failed.append(f"{kind} {u}: {exc}")
                leftovers[kind].append(u)
    log(f"· prune: {deleted}개 삭제" + (f", 이미 없음 {absent}개" if absent else "")
        + (f", 실패 {len(failed)}개" if failed else ""))

    # 7. managed.json (prune에 실패한 객체는 남겨 다음 실행에서 다시 시도)
    _write_json_atomic(managed_path, _managed_doc(project, _union(current, leftovers)))
    if failed:
        raise SyncError("import·임베드는 끝났지만 prune 일부 실패(다시 실행하면 재시도):\n  " + "\n  ".join(failed))
    return current

