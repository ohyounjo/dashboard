"""상태 파일 없는 프로젝트 전환 정리·수동 의존성 보호·명시적 정리 CLI 단위 테스트.

DB나 Superset 컨테이너 없이 순수 함수와 가짜 클라이언트만으로 검증한다.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from dashkit import __main__ as cli
from dashkit import plan
from dashkit.config import load_project, object_uuid
from dashkit.loader import record
from dashkit.sync import (
    PreservedItem,
    SupersetSnapshot,
    SyncError,
    _db_target_from_connection,
    _db_target_from_env,
    _execute_prune,
    discover_project_objects,
    plan_prune,
    prune_project,
    sync,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _make_project(tmp_path: Path, name: str):
    root = tmp_path / "projects"
    shutil.copytree(FIXTURES / "basic", root / name)
    return load_project(name, root)


def _columns_for(project) -> dict[str, list[tuple[str, str]]]:
    base = {
        "owners": [("id", "text"), ("label", "text")],
        "things": [
            ("id", "text"),
            ("owner_id", "text"),
            ("phase", "text"),
            ("score", "bigint"),
            ("begins", "date"),
            ("ends", "date"),
            ("owner_label", "text"),
        ],
    }
    return {k: base[k] for k in project.dashboard["datasets"]}


def _db_row(proj: str, db_id: int, *, row_uuid: str | None = None) -> dict:
    return {
        "id": db_id,
        "uuid": row_uuid or object_uuid(proj, "database", "main"),
        "database_name": f"dashkit-{proj}",
    }


def _ds_row(proj: str, ds_id: int, db_id: int, table_name: str, *, schema: str = "dash", row_uuid: str | None = None) -> dict:
    return {
        "id": ds_id,
        "uuid": row_uuid or object_uuid(proj, "dataset", table_name),
        "table_name": table_name,
        "schema": schema,
        "database": {"id": db_id},
    }


def _chart_row(proj: str, cid: int, key: str, ds_id: int, dashboards: list[int], *,
               locked: bool = True, row_uuid: str | None = None, ds_type: str = "table") -> dict:
    return {
        "id": cid,
        "uuid": row_uuid or object_uuid(proj, "chart", key),
        "is_managed_externally": locked,
        "datasource_id": ds_id,
        "datasource_type": ds_type,
        "dashboards": [{"id": d} for d in dashboards],
    }


_UNSET = object()


def _dash_row(proj: str, did: int, key: str, *, locked: bool = True, row_uuid: str | None = None,
              slug: object = _UNSET) -> dict:
    return {
        "id": did,
        "uuid": row_uuid or object_uuid(proj, "dashboard", key),
        "slug": f"{proj}-{key}".replace("_", "-") if slug is _UNSET else slug,
        "is_managed_externally": locked,
    }


# ---------------------------------------------------------------- replaced_projects 계산

def test_next_replaced_projects_lifecycle(tmp_path):
    assert record.next_replaced_projects(None, "alpha") == []
    assert record.next_replaced_projects({"project": "alpha"}, "alpha") == []
    assert record.next_replaced_projects({"project": "alpha"}, "beta") == ["alpha"]
    # 동일 프로젝트 재적재는 기존 전환 이력을 지우지 않는다
    assert record.next_replaced_projects({"project": "beta", "replaced_projects": ["alpha"]}, "beta") == ["alpha"]
    # 연속 전환 누적
    assert record.next_replaced_projects({"project": "beta", "replaced_projects": ["alpha"]}, "gamma") == ["alpha", "beta"]
    # 왕복(alpha -> beta -> alpha): 현재 프로젝트(alpha)는 제외하고 직전 프로젝트(beta) 추가
    assert record.next_replaced_projects({"project": "beta", "replaced_projects": ["alpha"]}, "alpha") == ["beta"]
    # 잘못된 타입·중복 방어
    dirty = {"project": "beta", "replaced_projects": ["alpha", "", None, 123, "alpha"]}
    assert record.next_replaced_projects(dirty, "gamma") == ["alpha", "beta"]

    proj = _make_project(tmp_path, "alpha")
    s0 = record.snapshot("alpha", proj.schema, proj.dashboard, {"owners": 1, "things": 1})
    assert "replaced_projects" not in s0
    s1 = record.snapshot("alpha", proj.schema, proj.dashboard, {"owners": 1, "things": 1}, replaced_projects=["beta"])
    assert s1["replaced_projects"] == ["beta"]


# ---------------------------------------------------------------- DB 대상 비교

def test_db_target_parsing():
    assert _db_target_from_env({}) == ("data-db", 5432, "dash")
    assert _db_target_from_env({"DATA_DB_HOST": "DB.Internal", "DATA_DB_PORT": "5433", "DATA_DB_NAME": "prod"}) == (
        "db.internal", 5433, "prod"
    )
    assert _db_target_from_connection({"parameters": {"host": "Data-DB", "port": 5432, "database": "dash"}}) == (
        "data-db", 5432, "dash"
    )
    assert _db_target_from_connection({"parameters": {"host": "db2", "port": "5439", "database": "app"}}) == (
        "db2", 5439, "app"
    )
    assert _db_target_from_connection({
        "sqlalchemy_uri": "postgresql+psycopg2://dash_reader:XXXXXXXXXX@Other-Host:5435/analytics"
    }) == ("other-host", 5435, "analytics")
    assert _db_target_from_connection({"sqlalchemy_uri": "postgresql+psycopg2://u:p@db/dash"}) == ("db", 5432, "dash")
    assert _db_target_from_connection({"sqlalchemy_uri": "sqlite:////tmp/test.db"}) is None
    assert _db_target_from_connection(None) is None


# ---------------------------------------------------------------- 객체 탐색 경계

def test_discover_project_objects_strict_boundaries():
    u4_db = "11111111-1111-4111-8111-111111111111"
    u4_ds = "22222222-2222-4222-8222-222222222222"
    u4_ch = "33333333-3333-4333-8333-333333333333"
    u4_da = "44444444-4444-4444-8444-444444444444"
    other_ns_ds = object_uuid("other", "dataset", "things")

    snap = SupersetSnapshot(
        databases=[
            _db_row("alpha", 1),
            _db_row("alpha-beta", 2),
            {"id": 3, "uuid": u4_db, "database_name": "dashkit-manual"},
        ],
        datasets=[
            _ds_row("alpha", 10, 1, "things"),
            _ds_row("alpha", 11, 1, "owners", schema="public"),     # dash 스키마 아님 → 제외
            _ds_row("alpha", 12, 1, "manual_tbl", row_uuid=u4_ds),   # 수동 UUID v4 → 제외
            _ds_row("alpha", 13, 1, "other_tbl", row_uuid=other_ns_ds),  # 다른 프로젝트 UUID → 제외
            _ds_row("alpha-beta", 20, 2, "things"),
        ],
        charts=[
            _chart_row("alpha", 100, "c_bar", 10, [1000], locked=True),
            _chart_row("alpha", 101, "c_unlocked", 10, [1000], locked=False),  # SYNC_LOCK_UI=0 (UUID v5)
            _chart_row("alpha", 102, "c_manual", 10, [1000], locked=False, row_uuid=u4_ch),  # 수동 차트 제외
            _chart_row("alpha", 103, "c_on_manual_ds", 12, [1000], locked=True),  # 수동 데이터셋 차트 제외
            _chart_row("alpha-beta", 200, "c_bar", 20, [2000], locked=True),
        ],
        dashboards=[
            _dash_row("alpha", 1000, "main", locked=False),          # SYNC_LOCK_UI=0 (UUID v5)
            _dash_row("alpha", 1001, "manual", locked=False, row_uuid=u4_da),  # 수동 대시보드 제외
            _dash_row("alpha-beta", 2000, "main", locked=True),      # 접두사 유사 타 프로젝트 제외
        ],
    )

    found_alpha = discover_project_objects(snap, "alpha", include_database=True)
    assert found_alpha == {
        "database": [object_uuid("alpha", "database", "main")],
        "dataset": [object_uuid("alpha", "dataset", "things")],
        "chart": [object_uuid("alpha", "chart", "c_bar"), object_uuid("alpha", "chart", "c_unlocked")],
        "dashboard": [object_uuid("alpha", "dashboard", "main")],
    }
    assert discover_project_objects(snap, "manual", include_database=True) == {
        "database": [], "dataset": [], "chart": [], "dashboard": []
    }


def test_discover_finds_orphan_database_without_datasets():
    snap = SupersetSnapshot(
        databases=[_db_row("alpha", 1)],
        datasets=[],
        charts=[],
        dashboards=[],
    )
    assert discover_project_objects(snap, "alpha", include_database=True) == {
        "database": [object_uuid("alpha", "database", "main")],
        "dataset": [],
        "chart": [],
        "dashboard": [],
    }


def test_discover_and_prune_exclude_other_project_and_unverified_dashboards():
    # alpha-gamma(접두사 유사 타 프로젝트) 및 gamma(타 프로젝트), slug가 바뀐 미식별 대시보드가
    # SYNC_LOCK_UI=0 상태에서 alpha 차트를 참조해도 alpha 소유 대시보드로 오인하지 않아야 한다.
    snap = SupersetSnapshot(
        databases=[_db_row("alpha", 1), _db_row("gamma", 2), _db_row("alpha-gamma", 3), _db_row("beta", 4)],
        datasets=[
            _ds_row("alpha", 10, 1, "things"),
            _ds_row("alpha", 11, 1, "owners"),
            _ds_row("gamma", 20, 2, "things"),
        ],
        charts=[
            _chart_row("alpha", 100, "c_owned", 11, [1000], locked=False),
            _chart_row("alpha", 101, "c_shared_gamma", 10, [1000, 2000], locked=False),
            _chart_row("alpha", 102, "c_shared_prefix", 10, [3000], locked=False),
            _chart_row("alpha", 103, "c_unverified_slug", 10, [4000], locked=False),
            _chart_row("gamma", 200, "c_gamma", 20, [2000], locked=False),
        ],
        dashboards=[
            _dash_row("alpha", 1000, "sales_home", locked=False),  # _ 포함 키 → slug 역산 검증 통과
            _dash_row("gamma", 2000, "home_view", locked=False),
            _dash_row("alpha-gamma", 3000, "main", locked=False),
            _dash_row("alpha", 4000, "edited", locked=False, slug="custom-edited-slug"),
        ],
    )

    cand = discover_project_objects(snap, "alpha", include_database=True)
    assert cand["dashboard"] == [object_uuid("alpha", "dashboard", "sales_home")]

    p = plan_prune(snap, {"database": [_db_row("beta", 4)["uuid"]]}, cand, current_project="beta")
    assert p.delete == {
        "dashboard": [object_uuid("alpha", "dashboard", "sales_home")],
        "chart": [object_uuid("alpha", "chart", "c_owned")],
        "dataset": [object_uuid("alpha", "dataset", "owners")],
        "database": [],
    }
    assert [(x.kind, x.uuid) for x in p.preserved] == [
        ("chart", object_uuid("alpha", "chart", "c_shared_gamma")),
        ("chart", object_uuid("alpha", "chart", "c_shared_prefix")),
        ("chart", object_uuid("alpha", "chart", "c_unverified_slug")),
        ("dataset", object_uuid("alpha", "dataset", "things")),
        ("database", object_uuid("alpha", "database", "main")),
    ]


# ---------------------------------------------------------------- plan_prune 수동 의존성 보존

def test_plan_prune_preserves_parents_for_manual_children():
    u4_chart = "33333333-3333-4333-8333-333333333333"
    u4_dash = "44444444-4444-4444-8444-444444444444"
    u4_ds = "22222222-2222-4222-8222-222222222222"

    # Case 1: 수동 차트 -> 관리 데이터셋(things) + 관리 DB 보존; 형제 데이터셋(owners)·차트·대시보드는 삭제
    snap1 = SupersetSnapshot(
        databases=[_db_row("alpha", 1), _db_row("beta", 2), _db_row("gamma", 3)],
        datasets=[_ds_row("alpha", 10, 1, "things"), _ds_row("alpha", 11, 1, "owners")],
        charts=[
            _chart_row("alpha", 100, "c_things", 10, [1000]),
            _chart_row("alpha", 101, "c_owners", 11, [1000]),
            _chart_row("alpha", 102, "manual_c", 10, [], locked=False, row_uuid=u4_chart),
        ],
        dashboards=[_dash_row("alpha", 1000, "main")],
    )
    cand1 = discover_project_objects(snap1, "alpha", include_database=True)
    p1 = plan_prune(snap1, {"database": [_db_row("beta", 2)["uuid"]]}, cand1, current_project="beta")
    assert p1.delete == {
        "dashboard": [object_uuid("alpha", "dashboard", "main")],
        "chart": [object_uuid("alpha", "chart", "c_things"), object_uuid("alpha", "chart", "c_owners")],
        "dataset": [object_uuid("alpha", "dataset", "owners")],
        "database": [],
    }
    assert [(x.kind, x.uuid) for x in p1.preserved] == [
        ("dataset", object_uuid("alpha", "dataset", "things")),
        ("database", object_uuid("alpha", "database", "main")),
    ]
    assert p1.other_dashkit_dbs == ["gamma"]

    # Case 2: 수동 대시보드 -> 관리 차트 + 그 데이터셋 + DB 연쇄 보존
    snap2 = SupersetSnapshot(
        databases=[_db_row("alpha", 1)],
        datasets=[_ds_row("alpha", 10, 1, "things")],
        charts=[_chart_row("alpha", 100, "c_things", 10, [1000, 9999])],
        dashboards=[
            _dash_row("alpha", 1000, "main"),
            _dash_row("manual", 9999, "m", locked=False, row_uuid=u4_dash),
        ],
    )
    cand2 = discover_project_objects(snap2, "alpha", include_database=True)
    p2 = plan_prune(snap2, {}, cand2, current_project="beta")
    assert p2.delete == {
        "dashboard": [object_uuid("alpha", "dashboard", "main")],
        "chart": [],
        "dataset": [],
        "database": [],
    }
    assert [(x.kind, x.uuid) for x in p2.preserved] == [
        ("chart", object_uuid("alpha", "chart", "c_things")),
        ("dataset", object_uuid("alpha", "dataset", "things")),
        ("database", object_uuid("alpha", "database", "main")),
    ]

    # Case 3: 수동 데이터셋 -> 관리 DB 보존
    snap3 = SupersetSnapshot(
        databases=[_db_row("alpha", 1)],
        datasets=[_ds_row("alpha", 10, 1, "things"), _ds_row("alpha", 19, 1, "manual_ds", row_uuid=u4_ds)],
        charts=[],
        dashboards=[],
    )
    cand3 = discover_project_objects(snap3, "alpha", include_database=True)
    p3 = plan_prune(snap3, {}, cand3, current_project="beta")
    assert p3.delete["dataset"] == [object_uuid("alpha", "dataset", "things")]
    assert p3.delete["database"] == []
    assert p3.preserved == [PreservedItem("database", object_uuid("alpha", "database", "main"), "수동 또는 보존된 데이터셋이 소속됨")]


# ---------------------------------------------------------------- _execute_prune 역방향 불변식

class _FakePruneClient:
    def __init__(self, snap: SupersetSnapshot, *, fail_uuids: set[str] = (), conn_targets: dict[int, tuple[str, int, str]] | None = None):
        self.base_url = "http://fake"
        self.snap = snap
        self.fail_uuids = set(fail_uuids)
        self.conn_targets = dict(conn_targets or {})
        self.deleted: list[tuple[str, int]] = []
        self.imported = False

    def _rows(self, kind: str) -> list[dict]:
        return getattr(self.snap, f"{kind}s")

    def wait_ready(self, timeout: float = 0) -> None:
        pass

    def login(self) -> None:
        pass

    def import_bundle(self, zip_bytes: bytes, passwords: dict) -> None:
        self.imported = True

    def set_embedded(self, dashboard_id: int, allowed_domains: list[str]) -> str:
        return f"emb-{dashboard_id}"

    def list_objects(self, kind: str, columns, filters: str = "", page_size: int = 100) -> list[dict]:
        return list(self._rows(kind))

    def get_database_connection(self, db_id: int) -> dict | None:
        if db_id not in self.conn_targets:
            return None
        host, port, dbname = self.conn_targets[db_id]
        return {"parameters": {"host": host, "port": port, "database": dbname}}

    def find_id(self, kind: str, row_uuid: str) -> int | None:
        for r in self._rows(kind):
            if str(r.get("uuid")) == row_uuid:
                return int(r["id"])
        return None

    def delete_object(self, kind: str, obj_id: int) -> bool:
        for r in self._rows(kind):
            if int(r["id"]) == obj_id:
                if str(r.get("uuid")) in self.fail_uuids:
                    raise SyncError(f"{kind} 삭제 실패(시험)")
                self.deleted.append((kind, obj_id))
                self._rows(kind).remove(r)
                return True
        return False


def test_execute_prune_blocks_ancestors_when_child_delete_fails():
    snap = SupersetSnapshot(
        databases=[_db_row("alpha", 1)],
        datasets=[_ds_row("alpha", 10, 1, "things"), _ds_row("alpha", 11, 1, "owners")],
        charts=[_chart_row("alpha", 100, "c_things", 10, [1000]), _chart_row("alpha", 101, "c_owners", 11, [1001])],
        dashboards=[_dash_row("alpha", 1000, "d1"), _dash_row("alpha", 1001, "d2")],
    )
    cand = discover_project_objects(snap, "alpha", include_database=True)
    prune_plan = plan_prune(snap, {}, cand, current_project="beta")
    client = _FakePruneClient(snap, fail_uuids={object_uuid("alpha", "dashboard", "d1")})

    deleted, _absent, failed, leftovers = _execute_prune(client, snap, prune_plan, log=lambda _m: None)
    # d2 -> c_owners -> owners는 정상 삭제되고, d1 체인(d1 -> c_things -> things -> db)은 모두 보존·leftovers에 남는다
    assert deleted == 3
    assert client.deleted == [("dashboard", 1001), ("chart", 101), ("dataset", 11)]
    assert leftovers == {
        "dashboard": [object_uuid("alpha", "dashboard", "d1")],
        "chart": [object_uuid("alpha", "chart", "c_things")],
        "dataset": [object_uuid("alpha", "dataset", "things")],
        "database": [object_uuid("alpha", "database", "main")],
    }
    assert len(failed) == 4


# ---------------------------------------------------------------- sync 통합 흐름(가짜 클라이언트)

def _snap_with_two_projects() -> SupersetSnapshot:
    return SupersetSnapshot(
        databases=[_db_row("alpha", 1), _db_row("beta", 2)],
        datasets=[_ds_row("alpha", 10, 1, "things"), _ds_row("alpha", 11, 1, "owners"), _ds_row("beta", 20, 2, "things"), _ds_row("beta", 21, 2, "owners")],
        charts=[_chart_row("alpha", 100, "c_line", 10, [1000]), _chart_row("beta", 200, "c_line", 20, [2000])],
        dashboards=[_dash_row("alpha", 1000, "main"), _dash_row("beta", 2000, "main")],
    )


def test_sync_stateless_switch_with_embed_hint_and_lineage(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DB_READER_PASSWORD", "pw")
    monkeypatch.setenv("DATA_DB_HOST", "data-db")
    monkeypatch.setenv("DATA_DB_PORT", "5432")
    monkeypatch.setenv("DATA_DB_NAME", "dash")

    beta = _make_project(tmp_path, "beta")
    state, build = tmp_path / "state", tmp_path / "build"
    state.mkdir()

    # 1) managed.json 없이 embed.json만 alpha인 상태
    (state / "embed.json").write_text(json.dumps({"version": 1, "project": "alpha", "dashboards": {}}), encoding="utf-8")
    snap1 = _snap_with_two_projects()
    c1 = _FakePruneClient(snap1, conn_targets={1: ("data-db", 5432, "dash")})
    logs1: list[str] = []
    sync(beta, state_dir=state, build_dir=build, client=c1, view_columns=_columns_for(beta), replaced_projects=[], log=logs1.append)
    assert [r["database_name"] for r in snap1.databases] == ["dashkit-beta"]
    assert json.loads((state / "embed.json").read_text())["project"] == "beta"

    # 2) state 전체 비어 있고 lineage(replaced_projects=["alpha"])만 있으며 DB 대상 불일치 -> 비삭제 + 경고
    shutil.rmtree(state)
    snap2 = _snap_with_two_projects()
    c2 = _FakePruneClient(snap2, conn_targets={1: ("other-db", 5432, "dash")})
    logs2: list[str] = []
    sync(beta, state_dir=state, build_dir=build, client=c2, view_columns=_columns_for(beta), replaced_projects=["alpha"], log=logs2.append)
    assert [r["database_name"] for r in snap2.databases] == ["dashkit-alpha", "dashkit-beta"]
    assert any("자동 정리하지 않음" in m for m in logs2)

    # 3) state 전체 비어 있고 lineage 없고 타 dashkit DB 존재 -> 비삭제 + prune-project 안내
    shutil.rmtree(state)
    snap3 = _snap_with_two_projects()
    c3 = _FakePruneClient(snap3, conn_targets={1: ("data-db", 5432, "dash")})
    logs3: list[str] = []
    sync(beta, state_dir=state, build_dir=build, client=c3, view_columns=_columns_for(beta), replaced_projects=[], log=logs3.append)
    assert [r["database_name"] for r in snap3.databases] == ["dashkit-alpha", "dashkit-beta"]
    assert any("prune-project" in m for m in logs3)


def test_sync_retains_embed_prev_project_across_snapshot_failure(tmp_path, monkeypatch):
    """managed.json·적재 이력이 없고 embed.json만 있을 때 prune 목록 조회가 실패해도 재실행에서 이전 프로젝트를 정리한다."""
    monkeypatch.setenv("DATA_DB_READER_PASSWORD", "test-pw")
    beta = _make_project(tmp_path, "beta")
    state = tmp_path / "state"
    build = tmp_path / "build"
    state.mkdir()
    (state / "embed.json").write_text(json.dumps({"version": 1, "project": "alpha", "dashboards": {}}), encoding="utf-8")

    snap = _snap_with_two_projects()

    class _FlakySnapshotClient(_FakePruneClient):
        def __init__(self, s: SupersetSnapshot, **kw):
            super().__init__(s, **kw)
            self.fail_list = True

        def list_objects(self, kind: str, columns, filters: str = "", page_size: int = 100) -> list[dict]:
            if self.fail_list:
                raise SyncError("일시적 목록 조회 실패")
            return super().list_objects(kind, columns, filters=filters, page_size=page_size)

    client = _FlakySnapshotClient(snap, conn_targets={1: ("data-db", 5432, "dash")})
    with pytest.raises(SyncError, match="일시적 목록 조회 실패"):
        sync(beta, state_dir=state, build_dir=build, client=client, view_columns=_columns_for(beta),
             replaced_projects=[], log=lambda _m: None)

    # embed.json은 beta로 덮어써졌지만 managed.json에 pending_projects=["alpha"]가 남아 있어야 한다
    assert json.loads((state / "embed.json").read_text())["project"] == "beta"
    assert json.loads((state / "managed.json").read_text())["pending_projects"] == ["alpha"]

    # 복구 후 재실행 → alpha 전부 삭제 및 pending_projects 정리
    client.fail_list = False
    sync(beta, state_dir=state, build_dir=build, client=client, view_columns=_columns_for(beta),
         replaced_projects=[], log=lambda _m: None)
    assert [r["database_name"] for r in snap.databases] == ["dashkit-beta"]
    assert "pending_projects" not in json.loads((state / "managed.json").read_text())


def test_sync_retains_embed_prev_project_when_preserved_by_manual_ref(tmp_path, monkeypatch):
    """적재 이력 없이 embed.json만으로 전환 시 수동 참조로 이전 객체가 보존돼도 다음 sync에서 남은 부모를 정리한다."""
    monkeypatch.setenv("DATA_DB_READER_PASSWORD", "test-pw")
    beta = _make_project(tmp_path, "beta")
    state = tmp_path / "state"
    build = tmp_path / "build"
    state.mkdir()
    (state / "embed.json").write_text(json.dumps({"version": 1, "project": "alpha", "dashboards": {}}), encoding="utf-8")

    snap = _snap_with_two_projects()
    u4_chart = "33333333-3333-4333-8333-333333333333"
    manual_chart = _chart_row("alpha", 109, "manual_c", 10, [], locked=False, row_uuid=u4_chart)
    snap.charts.append(manual_chart)
    client = _FakePruneClient(snap, conn_targets={1: ("data-db", 5432, "dash")})

    # 1) 첫 전환 sync: 수동 차트 때문에 alpha의 things 데이터셋·DB는 보존되고 pending_projects=["alpha"]가 유지된다
    sync(beta, state_dir=state, build_dir=build, client=client, view_columns=_columns_for(beta),
         replaced_projects=[], log=lambda _m: None)
    assert [r["database_name"] for r in snap.databases] == ["dashkit-alpha", "dashkit-beta"]
    assert json.loads((state / "managed.json").read_text())["pending_projects"] == ["alpha"]

    # 2) 수동 차트 제거 후 beta 재-sync → 보존됐던 alpha 데이터셋·DB 자동 정리 및 pending_projects 제거
    snap.charts.remove(manual_chart)
    sync(beta, state_dir=state, build_dir=build, client=client, view_columns=_columns_for(beta),
         replaced_projects=[], log=lambda _m: None)
    assert [r["database_name"] for r in snap.databases] == ["dashkit-beta"]
    assert "pending_projects" not in json.loads((state / "managed.json").read_text())


def test_sync_retains_managed_prev_project_when_preserved_by_manual_ref(tmp_path, monkeypatch):
    """embed.json·적재 이력이 없고 alpha의 managed.json만 있을 때도 수동 참조 보존 후 재실행 시 남은 부모를 정리한다."""
    monkeypatch.setenv("DATA_DB_READER_PASSWORD", "test-pw")
    beta = _make_project(tmp_path, "beta")
    state = tmp_path / "state"
    build = tmp_path / "build"
    state.mkdir()
    (state / "managed.json").write_text(json.dumps({
        "version": 1,
        "project": "alpha",
        "objects": {
            "database": [object_uuid("alpha", "database", "main")],
            "dataset": [object_uuid("alpha", "dataset", "things")],
            "chart": [object_uuid("alpha", "chart", "c1")],
            "dashboard": [object_uuid("alpha", "dashboard", "d1")],
        },
    }), encoding="utf-8")

    snap = _snap_with_two_projects()
    u4_chart = "33333333-3333-4333-8333-333333333333"
    manual_chart = _chart_row("alpha", 109, "manual_c", 10, [], locked=False, row_uuid=u4_chart)
    snap.charts.append(manual_chart)
    client = _FakePruneClient(snap, conn_targets={1: ("data-db", 5432, "dash")})

    sync(beta, state_dir=state, build_dir=build, client=client, view_columns=_columns_for(beta),
         replaced_projects=[], log=lambda _m: None)
    assert [r["database_name"] for r in snap.databases] == ["dashkit-alpha", "dashkit-beta"]
    assert json.loads((state / "managed.json").read_text())["pending_projects"] == ["alpha"]

    snap.charts.remove(manual_chart)
    sync(beta, state_dir=state, build_dir=build, client=client, view_columns=_columns_for(beta),
         replaced_projects=[], log=lambda _m: None)
    assert [r["database_name"] for r in snap.databases] == ["dashkit-beta"]
    assert "pending_projects" not in json.loads((state / "managed.json").read_text())


# ---------------------------------------------------------------- prune_project + CLI + plan

def test_prune_project_safety_and_cli(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    state.mkdir()
    (state / "embed.json").write_text(json.dumps({"version": 1, "project": "beta", "dashboards": {}}), encoding="utf-8")

    # 잘못된 이름 거부
    with pytest.raises(SyncError, match="형식 오류"):
        prune_project("Bad_Name!", state_dir=state, client=_FakePruneClient(_snap_with_two_projects()), active_projects=())

    # 활성 프로젝트 거부 (embed.json / active_projects / env fallback)
    with pytest.raises(SyncError, match="활성 프로젝트"):
        prune_project("beta", state_dir=state, client=_FakePruneClient(_snap_with_two_projects()), active_projects=None)

    # embed.json이 beta이면 env PROJECT=alpha여도 alpha 정리를 허용한다
    monkeypatch.setenv("PROJECT", "alpha")
    monkeypatch.setattr("dashkit.sync._read_active_db_project", lambda dbname=None: None)
    snap = _snap_with_two_projects()
    client = _FakePruneClient(snap, conn_targets={1: ("data-db", 5432, "dash")})
    dry = prune_project("alpha", dry_run=True, state_dir=state, client=client, log=lambda _m: None)
    assert dry["dry_run"] is True and client.deleted == []
    assert len(dry["planned"]["database"]) == 1 and len(dry["planned"]["chart"]) == 1

    # 실제 실행: alpha만 삭제, beta 보존
    res = prune_project("alpha", dry_run=False, state_dir=state, client=client, log=lambda _m: None)
    assert res["deleted"] == 5
    assert [r["database_name"] for r in snap.databases] == ["dashkit-beta"]

    # 상태 파일·DB 모두 없을 때 env PROJECT fallback으로 활성 프로젝트 거부
    (state / "embed.json").unlink()
    monkeypatch.setenv("STATE_DIR", str(state))
    monkeypatch.setenv("PROJECT", "beta")
    assert cli.main(["prune-project", "beta"]) == 1
    assert "활성 프로젝트" in capsys.readouterr().err

    # CLI 성공 경로 (--dry-run)
    snap2 = _snap_with_two_projects()
    fake2 = _FakePruneClient(snap2, conn_targets={1: ("data-db", 5432, "dash")})
    monkeypatch.setattr("dashkit.sync.SupersetClient", lambda: fake2)
    assert cli.main(["prune-project", "alpha", "--dry-run"]) == 0
    assert "prune-project --dry-run: alpha" in capsys.readouterr().out


def test_plan_detects_other_project_from_embed_json(tmp_path):
    (tmp_path / "embed.json").write_text(json.dumps({"version": 1, "project": "alpha", "dashboards": {}}), encoding="utf-8")
    assert plan.read_other_project(tmp_path, "beta") == {"project": "alpha", "objects": None}
    assert plan.read_other_project(tmp_path, "alpha") is None

    proj = _make_project(tmp_path, "beta")
    from dashkit.sync.bundle import build_bundle

    text = plan.format_plan({
        "project": "beta",
        "changed": True,
        "load_record": {"exists": True, "project": "alpha"},
        "data": [{"table": "owners", "rows": 1, "status": "unchanged"}, {"table": "things", "rows": 1, "status": "unchanged"}],
        "views": [{"dataset": "owners", "status": "unchanged"}, {"dataset": "things", "status": "unchanged"}],
        "superset": {
            **plan.diff_bundles(build_bundle(proj, _columns_for(proj)), None),
            "replaces": {"project": "alpha", "objects": None},
            "assumed_columns": [],
            "pending_prune": 0,
        },
    })
    assert "지금 적용된 프로젝트 'alpha'의 관리 객체 삭제(상태 파일 없어 개수는 적용 시 확인)" in text
