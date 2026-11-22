"""상태 파일 없는 프로젝트 전환·왕복·중단 재실행·수동 객체 보존·권한 경계 통합 테스트.

`make test` 1단계에서 실행된다. 끝나면 테스트 스택을 깨끗한 devtracker 상태로 되돌린다.
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid

import jwt
import pytest
import requests
import yaml

from dashkit import config, db
from dashkit.__main__ import main as cli_main
from dashkit.config import load_project
from dashkit.loader import current, load
from dashkit.sync import SupersetClient, SyncError, prune_project, sync

from conftest import PROJECTS, api, copy_project, live_project

pytestmark = pytest.mark.integration
KINDS_DELETE = ("dashboard", "chart", "dataset", "database")


def _state_dir():
    from pathlib import Path

    return Path(os.environ.get("STATE_DIR") or "/app/.state")


def _build_dir():
    from pathlib import Path

    return Path(os.environ.get("BUILD_DIR") or "/app/.build")


def _delete_project_objects(superset: SupersetClient, proj_name: str, projects_dir=PROJECTS) -> None:
    proj = load_project(proj_name, projects_dir)
    expected = {
        "dashboard": [config.object_uuid(proj_name, "dashboard", k) for k in proj.dashboard["dashboards"]],
        "chart": [config.object_uuid(proj_name, "chart", c["key"]) for _, c in config.charts(proj)],
        "dataset": [config.object_uuid(proj_name, "dataset", k) for k in proj.dashboard["datasets"]],
        "database": [config.object_uuid(proj_name, "database", "main")],
    }
    for kind in KINDS_DELETE:
        for u in expected[kind]:
            oid = superset.find_id(kind, u)
            if oid is not None:
                superset.delete_object(kind, oid)


def _wipe_state() -> None:
    for name in ("managed.json", "embed.json"):
        p = _state_dir() / name
        if p.is_file():
            p.unlink()


def _exists_summary(superset: SupersetClient, proj_name: str, projects_dir=PROJECTS) -> dict[str, int]:
    proj = load_project(proj_name, projects_dir)
    expected = {
        "database": [config.object_uuid(proj_name, "database", "main")],
        "dataset": [config.object_uuid(proj_name, "dataset", k) for k in proj.dashboard["datasets"]],
        "chart": [config.object_uuid(proj_name, "chart", c["key"]) for _, c in config.charts(proj)],
        "dashboard": [config.object_uuid(proj_name, "dashboard", k) for k in proj.dashboard["dashboards"]],
    }
    return {
        kind: sum(1 for u in uuids if superset.find_id(kind, u) is not None)
        for kind, uuids in expected.items()
    }


def _apply_project(name: str, *, projects_dir=PROJECTS, sync_kwargs: dict | None = None) -> dict[str, list[str]]:
    proj = load_project(name, projects_dir)
    load(proj)
    kw: dict = {"log": lambda _m: None}
    if sync_kwargs:
        kw.update(sync_kwargs)
    return sync(proj, **kw)


def _mint_guest_token(embed_uuid: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "user": {"username": "embed-viewer", "first_name": "Embed", "last_name": "Viewer"},
            "resources": [{"type": "dashboard", "id": embed_uuid}],
            "rls_rules": [],
            "iat": now,
            "exp": now + 300,
            "aud": os.environ["GUEST_TOKEN_JWT_AUDIENCE"],
            "type": "guest",
        },
        os.environ["GUEST_TOKEN_JWT_SECRET"],
        algorithm="HS256",
    )


@pytest.fixture(scope="module", autouse=True)
def restore_live_devtracker(superset):
    """이 모듈이 끝난 뒤 테스트 스택을 원래 devtracker 상태로 정확히 복구한다."""
    original = live_project()
    yield original
    # 테스트 중 남은 수동/임시 객체 정리 뒤 devtracker 재적용
    for kind in ("dashboard", "chart", "dataset", "database"):
        for r in superset.list_objects(kind, ["id", "uuid"]):
            try:
                if uuid.UUID(str(r.get("uuid"))).version == 4:
                    superset.delete_object(kind, int(r["id"]))
            except (ValueError, SyncError):
                pass
    for extra in ("sales-sample",):
        if extra != original:
            _delete_project_objects(superset, extra)
    _wipe_state()
    _apply_project(original)


def test_stateless_switch_roundtrip_and_unlocked(superset, monkeypatch):
    """managed.json 단독 손실 -> .state 전체 손실 왕복 -> SYNC_LOCK_UI=0 전환 모두 이전 객체를 남기지 않는다."""
    dev_embed_before = json.loads((_state_dir() / "embed.json").read_text())
    old_embed_uuid = dev_embed_before["dashboards"]["overview"]["embed_uuid"]

    # 1) managed.json만 삭제 후 devtracker -> sales-sample 전환
    (_state_dir() / "managed.json").unlink()
    _apply_project("sales-sample")
    assert _exists_summary(superset, "devtracker") == {"database": 0, "dataset": 0, "chart": 0, "dashboard": 0}
    assert _exists_summary(superset, "sales-sample") == {"database": 1, "dataset": 3, "chart": 9, "dashboard": 2}
    # 삭제된 이전 대시보드 embed_uuid는 404, 새 프로젝트 guest token은 200
    referer = (os.environ.get("WEB_PUBLIC_URL") or "http://localhost:8080") + "/"
    r_old = requests.get(
        f"{superset.base_url}/embedded/{old_embed_uuid}",
        headers={"X-GuestToken": _mint_guest_token(old_embed_uuid), "Referer": referer},
        timeout=30,
    )
    assert r_old.status_code == 404
    code, tok = api("POST", "/api/guest-token", {"dashboard": "sales_home"})
    assert code == 200 and tok["token"]

    # 2) .state/ 전체 삭제 후 sales-sample -> devtracker 왕복
    _wipe_state()
    _apply_project("devtracker")
    assert _exists_summary(superset, "sales-sample") == {"database": 0, "dataset": 0, "chart": 0, "dashboard": 0}
    assert _exists_summary(superset, "devtracker") == {"database": 1, "dataset": 5, "chart": 11, "dashboard": 3}

    # 3) SYNC_LOCK_UI=0으로 devtracker 적용 -> .state/ 전체 삭제 -> sales-sample 전환
    monkeypatch.setenv("SYNC_LOCK_UI", "0")
    _apply_project("devtracker")
    assert superset.get_object("dashboard", superset.find_id("dashboard", config.object_uuid("devtracker", "dashboard", "overview")))[
        "is_managed_externally"
    ] is False
    _wipe_state()
    monkeypatch.delenv("SYNC_LOCK_UI")
    _apply_project("sales-sample")
    assert _exists_summary(superset, "devtracker") == {"database": 0, "dataset": 0, "chart": 0, "dashboard": 0}

    # 복구(devtracker)
    _wipe_state()
    _apply_project("devtracker")
    assert _exists_summary(superset, "sales-sample") == {"database": 0, "dataset": 0, "chart": 0, "dashboard": 0}


def test_stateless_switch_preserves_manual_objects_and_guest_boundary(superset, tmp_path):
    """수동 DB/차트/대시보드/데이터셋을 보존하고, 수동 객체 제거 후 재실행하면 부모가 마저 정리된다."""
    _apply_project("devtracker")
    dev_db_id = superset.find_id("database", config.object_uuid("devtracker", "database", "main"))
    ds_issues_id = superset.find_id("dataset", config.object_uuid("devtracker", "dataset", "issues"))
    chart_bar_id = superset.find_id("chart", config.object_uuid("devtracker", "chart", "ms_bar"))
    ds_milestones_id = superset.find_id("dataset", config.object_uuid("devtracker", "dataset", "milestones"))

    # 1) 이름 충돌 수동 DB (dashkit-zz-manual, UUID v4)
    r_mdb = superset.expect(superset.post("/api/v1/database/", json={
        "database_name": "dashkit-zz-manual",
        "sqlalchemy_uri": (
            f"postgresql+psycopg2://{os.environ.get('DATA_DB_READER_USER', 'dash_reader')}:"
            f"{os.environ['DATA_DB_READER_PASSWORD']}@{os.environ.get('DATA_DB_HOST', 'data-db')}:"
            f"{os.environ.get('DATA_DB_PORT', '5432')}/{os.environ.get('DATA_DB_NAME', 'dash')}"
        ),
        "expose_in_sqllab": False,
    }), "수동 DB 생성")
    m_db_id = int(r_mdb["id"])

    # 2) dashkit-devtracker의 issues 데이터셋 위 수동 차트
    r_mc = superset.expect(superset.post("/api/v1/chart/", json={
        "slice_name": "zz_manual_keep_chart",
        "viz_type": "table",
        "datasource_id": ds_issues_id,
        "datasource_type": "table",
        "params": "{}",
    }), "수동 차트 생성")
    m_chart_id = int(r_mc["id"])

    overview_dash_id = superset.find_id("dashboard", config.object_uuid("devtracker", "dashboard", "overview"))
    assert overview_dash_id is not None

    # 3) devtracker의 overview + ms_bar 차트를 함께 포함하는 수동 대시보드
    r_md = superset.expect(superset.post("/api/v1/dashboard/", json={
        "dashboard_title": "zz_manual_keep_dash",
        "published": True,
    }), "수동 대시보드 생성")
    m_dash_id = int(r_md["id"])
    superset.expect(
        superset.put(f"/api/v1/chart/{chart_bar_id}", json={"dashboards": [overview_dash_id, m_dash_id]}),
        "차트-대시보드 연결",
    )

    try:
        _wipe_state()
        logs: list[str] = []
        _apply_project("sales-sample", sync_kwargs={"log": logs.append})

        # 수동 객체 및 그 부모(issues, milestones, ms_bar, dashkit-devtracker DB) 보존, 미참조 devtracker 객체 삭제
        assert superset.get_object("database", m_db_id) is not None
        mc_after = superset.get_object("chart", m_chart_id)
        assert mc_after is not None and mc_after.get("datasource_id") == ds_issues_id
        md_charts = superset.get(f"/api/v1/dashboard/{m_dash_id}/charts").json()["result"]
        assert [c["id"] for c in md_charts] == [chart_bar_id]
        assert superset.get_object("dataset", ds_issues_id) is not None
        assert superset.get_object("chart", chart_bar_id) is not None
        assert superset.get_object("dataset", ds_milestones_id) is not None
        assert superset.get_object("database", dev_db_id) is not None
        # 미참조 devtracker 대시보드(3)와 나머지 데이터셋(3)은 삭제됨
        assert superset.find_id("dashboard", config.object_uuid("devtracker", "dashboard", "overview")) is None
        assert superset.find_id("dataset", config.object_uuid("devtracker", "dataset", "docs")) is None
        assert any("수동 참조 보존" in m for m in logs)

        # Guest 권한 경계: sales-sample guest token으로는 보존된 devtracker 데이터셋·전체 목록 API 접근 403
        sales_embed = json.loads((_state_dir() / "embed.json").read_text())["dashboards"]["sales_home"]["embed_uuid"]
        guest_hdrs = {"X-GuestToken": _mint_guest_token(sales_embed), "Referer": (os.environ.get("WEB_PUBLIC_URL") or "http://localhost:8080") + "/"}
        r_roles = requests.get(f"{superset.base_url}/api/v1/me/roles/", headers=guest_hdrs, timeout=30)
        assert r_roles.status_code == 200 and "EmbedGuest" in r_roles.text
        assert requests.get(f"{superset.base_url}/api/v1/dataset/", headers=guest_hdrs, timeout=30).status_code == 403
        assert requests.get(f"{superset.base_url}/api/v1/database/", headers=guest_hdrs, timeout=30).status_code == 403
        assert requests.get(f"{superset.base_url}/api/v1/time_range/?q=%27Last+week%27", headers=guest_hdrs, timeout=30).status_code == 200
        r_forbid = requests.post(
            f"{superset.base_url}/api/v1/chart/data",
            json={
                "datasource": {"id": ds_issues_id, "type": "table"},
                "queries": [{"columns": ["title"], "row_limit": 5}],
                "form_data": {"dashboardId": superset.find_id("dashboard", config.object_uuid("sales-sample", "dashboard", "sales_home"))},
            },
            headers=guest_hdrs,
            timeout=30,
        )
        assert r_forbid.status_code == 403
    finally:
        # 수동 차트·대시보드·DB 제거 후 재-sync 하면 보존되었던 devtracker 부모가 자동 정리된다
        superset.delete_object("dashboard", m_dash_id)
        superset.delete_object("chart", m_chart_id)
        superset.delete_object("database", m_db_id)

    sync(load_project("sales-sample", PROJECTS), log=lambda _m: None)
    assert _exists_summary(superset, "devtracker") == {"database": 0, "dataset": 0, "chart": 0, "dashboard": 0}

    # 4) 별도 임시 프로젝트(zz-mds)로 수동 데이터셋(M_ds) -> 관리 DB 보존 및 정리 검증
    root = copy_project("devtracker", tmp_path, "zz-mds")
    dash_file = root / "zz-mds" / "dashboard.yaml"
    doc = yaml.safe_load(dash_file.read_text(encoding="utf-8"))
    doc["datasets"].pop("docs", None)
    doc["dashboards"].pop("docs", None)
    doc["tabs"] = [t for t in doc["tabs"] if t["key"] != "docs"]
    dash_file.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")

    m_ds_id = None
    try:
        _apply_project("zz-mds", projects_dir=root)
        zz_db_id = superset.find_id("database", config.object_uuid("zz-mds", "database", "main"))
        zz_tasks_ds_id = superset.find_id("dataset", config.object_uuid("zz-mds", "dataset", "tasks"))
        superset.delete_object("dataset", zz_tasks_ds_id)
        r_mds = superset.expect(superset.post("/api/v1/dataset/", json={
            "database": zz_db_id,
            "schema": "dash",
            "table_name": "tasks",
        }), "수동 데이터셋 생성")
        m_ds_id = int(r_mds["id"])

        _wipe_state()
        _apply_project("sales-sample")
        # zz-mds의 나머지 관리 객체는 삭제되지만 M_ds와 zz-mds DB는 보존
        assert superset.get_object("dataset", m_ds_id) is not None
        assert superset.get_object("database", zz_db_id) is not None
        assert superset.find_id("dataset", config.object_uuid("zz-mds", "dataset", "issues")) is None
        # 수동 데이터셋 제거 후 재-sync 하면 보존됐던 zz-mds DB가 자동 정리된다
        superset.delete_object("dataset", m_ds_id)
        m_ds_id = None
        sync(load_project("sales-sample", PROJECTS), log=lambda _m: None)
        assert superset.find_id("database", config.object_uuid("zz-mds", "database", "main")) is None
    finally:
        if m_ds_id is not None:
            superset.delete_object("dataset", m_ds_id)
        _delete_project_objects(superset, "zz-mds", root)


def test_interrupted_prune_preserves_lineage_and_recovers_statelessly(superset):
    """대시보드 삭제 실패 주입 시 하위 차트·데이터셋·DB를 유지하고, 상태 파일 삭제 후 재실행에서 완전 수렴한다."""
    _apply_project("sales-sample")
    sales_dash_uuid = config.object_uuid("sales-sample", "dashboard", "sales_home")

    class FailingDashClient(SupersetClient):
        def delete_object(self, kind: str, obj_id: int) -> bool:
            if kind == "dashboard" and obj_id == self.find_id("dashboard", sales_dash_uuid):
                raise SyncError("주입된 대시보드 삭제 실패")
            return super().delete_object(kind, obj_id)

    fc = FailingDashClient()
    fc.wait_ready()
    fc.login()
    load(load_project("devtracker", PROJECTS))
    with pytest.raises(SyncError, match="prune 일부 실패"):
        sync(load_project("devtracker", PROJECTS), client=fc, log=lambda _m: None)

    # sales_home 대시보드와 그 하위 차트·데이터셋·DB는 보존(계보 유지)
    assert superset.find_id("dashboard", sales_dash_uuid) is not None
    assert superset.find_id("chart", config.object_uuid("sales-sample", "chart", "sum_won")) is not None
    assert superset.find_id("dataset", config.object_uuid("sales-sample", "dataset", "deal_pipeline")) is not None
    assert superset.find_id("database", config.object_uuid("sales-sample", "database", "main")) is not None

    # 상태 파일을 지운 뒤 정상 sync 재실행 → 완전 정리
    _wipe_state()
    sync(load_project("devtracker", PROJECTS), log=lambda _m: None)
    assert _exists_summary(superset, "sales-sample") == {"database": 0, "dataset": 0, "chart": 0, "dashboard": 0}


def test_coexisting_project_preserved_and_explicit_prune_project_cli(superset, tmp_path, capsys, monkeypatch):
    """이력 없는 타 프로젝트(SYNC_LOCK_UI=0 공유 차트 포함)는 전환 중 보존되고, prune-project CLI로 정리된다."""
    monkeypatch.setenv("SYNC_LOCK_UI", "0")
    _apply_project("devtracker")
    root = copy_project("devtracker", tmp_path, "zz-coexist")
    tmp_state = tmp_path / "coexist_state"
    tmp_build = tmp_path / "coexist_build"
    sync(load_project("zz-coexist", root), state_dir=tmp_state, build_dir=tmp_build, log=lambda _m: None)
    monkeypatch.delenv("SYNC_LOCK_UI")

    dev_overview_id = superset.find_id("dashboard", config.object_uuid("devtracker", "dashboard", "overview"))
    coexist_dash_id = superset.find_id("dashboard", config.object_uuid("zz-coexist", "dashboard", "overview"))
    chart_bar_id = superset.find_id("chart", config.object_uuid("devtracker", "chart", "ms_bar"))
    assert dev_overview_id is not None and coexist_dash_id is not None and chart_bar_id is not None
    superset.expect(
        superset.put(f"/api/v1/chart/{chart_bar_id}", json={"dashboards": [dev_overview_id, coexist_dash_id]}),
        "공존 대시보드에 이전 프로젝트 차트 연결",
    )

    try:
        _wipe_state()
        logs: list[str] = []
        _apply_project("sales-sample", sync_kwargs={"log": logs.append})
        # zz-coexist는 이력에 없으므로 대시보드와 공유된 devtracker 차트·부모 체인까지 함께 보존된다
        assert _exists_summary(superset, "zz-coexist", root) == {"database": 1, "dataset": 5, "chart": 11, "dashboard": 3}
        coexist_charts = superset.get(f"/api/v1/dashboard/{coexist_dash_id}/charts").json()["result"]
        assert chart_bar_id in [c["id"] for c in coexist_charts]
        assert superset.get_object("chart", chart_bar_id) is not None
        assert superset.find_id("dashboard", config.object_uuid("devtracker", "dashboard", "overview")) is None
        assert superset.find_id("dataset", config.object_uuid("devtracker", "dataset", "docs")) is None
        assert any("zz-coexist" in m and "prune-project" in m for m in logs)

        # 공존 대시보드에서 공유 차트를 떼고 재-sync 하면 남아 있던 devtracker 체인만 정리되고 zz-coexist는 무손상
        superset.expect(superset.put(f"/api/v1/chart/{chart_bar_id}", json={"dashboards": []}), "공유 차트 연결 해제")
        sync(load_project("sales-sample", PROJECTS), log=lambda _m: None)
        assert _exists_summary(superset, "devtracker") == {"database": 0, "dataset": 0, "chart": 0, "dashboard": 0}
        assert _exists_summary(superset, "zz-coexist", root) == {"database": 1, "dataset": 5, "chart": 11, "dashboard": 3}

        # 활성 프로젝트(sales-sample)는 거부
        assert cli_main(["prune-project", "sales-sample"]) == 1
        assert "활성 프로젝트" in capsys.readouterr().err

        # --dry-run은 삭제하지 않음
        assert cli_main(["prune-project", "zz-coexist", "--dry-run"]) == 0
        assert _exists_summary(superset, "zz-coexist", root) == {"database": 1, "dataset": 5, "chart": 11, "dashboard": 3}

        # 실제 prune-project 실행 → zz-coexist만 삭제, sales-sample 무손상
        assert cli_main(["prune-project", "zz-coexist"]) == 0
        assert _exists_summary(superset, "zz-coexist", root) == {"database": 0, "dataset": 0, "chart": 0, "dashboard": 0}
        assert _exists_summary(superset, "sales-sample") == {"database": 1, "dataset": 3, "chart": 9, "dashboard": 2}
    finally:
        _delete_project_objects(superset, "zz-coexist", root)


def test_sync_and_dashkit_meta_db_permission_boundaries(superset, monkeypatch):
    """상태 파일 없는 전환 sync도 owner 계정 없이 dash_reader만으로 동작하고, dash_reader는 dashkit_meta를 수정할 수 없다."""
    _apply_project("devtracker")
    load(load_project("sales-sample", PROJECTS))
    _wipe_state()

    # owner 자격 증명 제거 및 owner_conn 차단
    for var in ("DATA_DB_OWNER_USER", "DATA_DB_OWNER_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(db, "owner_conn", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("sync는 owner 계정을 열면 안 됨")))

    sync(load_project("sales-sample", PROJECTS), log=lambda _m: None)
    assert _exists_summary(superset, "devtracker") == {"database": 0, "dataset": 0, "chart": 0, "dashboard": 0}
    assert _exists_summary(superset, "sales-sample") == {"database": 1, "dataset": 3, "chart": 9, "dashboard": 2}

    # dash_reader 역할 자체에 dashkit_meta.load_snapshot 쓰기 권한이 없다
    import psycopg

    with db.reader_conn() as conn:
        try:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("DELETE FROM dashkit_meta.load_snapshot")
        finally:
            conn.rollback()
