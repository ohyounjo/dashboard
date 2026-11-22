"""이슈 #32(profile), #33(export), #36(freshness) 통합 테스트.

관리 서비스·백엔드 API를 ASGI 프로세스 안에서 호출해 권한, 비활성 404, dash_inspect/dash_reader 격리,
열 통계, CSV 스트리밍 내보내기, 민감 열 보호, 감사 로그, 신선도 계산을 검증한다.
"""
from __future__ import annotations

import csv
import io
import json
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import psycopg
import pytest

from studio_client import Client

pytestmark = pytest.mark.integration


def _make_user_client(app, role: str):
    from dashkit.auth import passwords, sessions, store

    h = passwords.hash_password(secrets.token_urlsafe(12))
    user = store.create_user(
        f"feat-{role}-{secrets.token_hex(4)}@example.com",
        role=role,
        status="active",
        source="invite",
        password_hash=h,
        email_verified=True,
    )
    token = sessions.new_token()
    assert store.start_session(
        user.id,
        h,
        sessions.token_hash(token),
        datetime.now(timezone.utc) + timedelta(hours=1),
        None,
        None,
    )
    return user, Client(app, host="localhost"), {"cookie": f"dashkit_session={token}"}


@pytest.fixture(scope="module")
def clients():
    from dashkit.auth import store
    from dashkit.manage.app import app

    admin_u, admin_c, admin_h = _make_user_client(app, "admin")
    op_u, op_c, op_h = _make_user_client(app, "operator")
    usr_u, usr_c, usr_h = _make_user_client(app, "user")
    yield (admin_c, admin_h), (op_c, op_h), (usr_c, usr_h), Client(app, host="localhost")
    for u in (admin_u, op_u, usr_u):
        store.delete_user(u.id)


def test_manage_features_config_and_disabled_404(clients, monkeypatch):
    from dashkit import config
    from dashkit.manage import context

    (admin_c, admin_h), (_op_c, op_h), (usr_c, usr_h), anon_c = clients
    table = next(iter(config.load_project(context.current_project()).schema["tables"]))

    monkeypatch.delenv("MANAGE_FEATURES", raising=False)
    r = admin_c.get("/api/manage/config", headers=admin_h)
    assert r.status == 200
    assert json.loads(r.body)["features"] == []

    for path in (f"/api/manage/tables/{table}/profile", f"/api/manage/tables/{table}/export"):
        assert anon_c.get(path).status == 401
        assert usr_c.get(path, headers=usr_h).status == 403
        r_disabled = admin_c.get(path, headers=admin_h)
        assert r_disabled.status == 404
        assert json.loads(r_disabled.body)["detail"] == "not_found"
        assert admin_c.get(path, headers=op_h).status == 404

    monkeypatch.setenv("MANAGE_FEATURES", "freshness, profile, export, unknown")
    r2 = admin_c.get("/api/manage/config", headers=admin_h)
    assert r2.status == 200
    assert json.loads(r2.body)["features"] == ["export", "freshness", "profile"]


def test_profile_api_types_edge_cases_sensitive_and_timeout(clients, monkeypatch):
    from dashkit import config, db
    from dashkit.manage import context
    from dashkit.manage.routes import profile as profile_mod

    (admin_c, admin_h), (op_c, op_h), _usr, _anon = clients
    monkeypatch.setenv("MANAGE_FEATURES", "profile")
    monkeypatch.setattr(db, "owner_conn", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("owner_conn 금지")))

    project = config.load_project(context.current_project())
    table = next(iter(project.schema["tables"]))

    # 실제 DB로 기본 테이블 통계 확인
    r = op_c.get(f"/api/manage/tables/{table}/profile", headers=op_h)
    assert r.status == 200, r.body
    body = json.loads(r.body)
    assert body["table"] == table and body["total_rows"] > 0
    assert len(body["columns"]) == len(project.schema["tables"][table]["columns"])

    # 모든 유형(boolean 포함), NULL/전부 NULL/빈 테이블/긴 텍스트 절단/민감 열 SQL 제외 검증
    fake_schema = {
        "tables": {
            "sample": {
                "primary_key": ["id"],
                "columns": {
                    "id": {"type": "integer"},
                    "amount": {"type": "number"},
                    "flag": {"type": "boolean"},
                    "d": {"type": "date"},
                    "ts": {"type": "timestamp"},
                    "kind": {"type": "enum", "values": ["a", "b"]},
                    "note": {"type": "text"},
                    "all_null": {"type": "text"},
                    "secret": {"type": "text", "sensitive": True},
                },
            }
        }
    }
    monkeypatch.setattr(profile_mod, "_project", lambda: SimpleNamespace(name="p", schema=fake_schema))
    monkeypatch.setattr(profile_mod, "_table", lambda _p, t: t)

    executed_sql: list[str] = []

    class FakeCur:
        def __init__(self, row):
            self._row = row

        def execute(self, stmt, _params=None):
            executed_sql.append(stmt.as_string(None) if hasattr(stmt, "as_string") else str(stmt))

        def fetchone(self):
            return self._row

    long_text = "x" * 2500
    # 9개 열 중 secret 제외 8개. boolean(flag)은 MIN/MAX 제외 → 4*7 + 2 = 30 + COUNT(*) = 31개 값
    row_data = (
        4,                                              # total_rows
        4, 4, 1, 4,                                     # id
        3, 3, 10.5, 99.5,                               # amount (1 NULL)
        4, 2,                                           # flag (boolean: no MIN/MAX)
        4, 2, datetime(2026, 1, 1).date(), datetime(2026, 1, 2).date(),
        4, 2, datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc),
        4, 2, "a", "b",                                 # kind
        4, 2, "aaa", long_text,                         # note
        0, 0, None, None,                               # all_null
    )
    orig_run = profile_mod.rows._run
    monkeypatch.setattr(profile_mod.rows, "_run", lambda work: work(FakeCur(row_data)))
    r2 = admin_c.get("/api/manage/tables/sample/profile", headers=admin_h)
    assert r2.status == 200, r2.body
    res = json.loads(r2.body)
    assert "secret" not in executed_sql[-1], "sensitive 열은 SQL에서 제외되어야 한다"
    by_name = {c["name"]: c for c in res["columns"]}
    assert by_name["flag"]["min"] is None and by_name["flag"]["max"] is None
    assert by_name["amount"]["null_count"] == 1 and by_name["amount"]["null_ratio"] == 0.25
    assert by_name["all_null"]["null_count"] == 4 and by_name["all_null"]["null_ratio"] == 1.0
    assert by_name["all_null"]["distinct_count"] == 0 and by_name["all_null"]["min"] is None
    assert len(by_name["note"]["max"]) == 2000
    assert by_name["secret"] == {
        "name": "secret", "type": "text", "sensitive": True,
        "null_count": None, "null_ratio": None, "distinct_count": None, "min": None, "max": None,
    }

    # 빈 테이블
    empty_row = (0,) + (0, 0, None, None) * 4 + (0, 0) + (0, 0, None, None) * 3
    empty_row = (0, 0, 0, None, None, 0, 0, None, None, 0, 0, 0, 0, None, None, 0, 0, None, None, 0, 0, None, None, 0, 0, None, None, 0, 0, None, None)
    monkeypatch.setattr(profile_mod.rows, "_run", lambda work: work(FakeCur(empty_row)))
    r3 = admin_c.get("/api/manage/tables/sample/profile", headers=admin_h)
    assert r3.status == 200
    assert json.loads(r3.body)["columns"][0]["null_ratio"] is None

    # 504 query_timeout
    class TimeoutConn:
        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            pass

        def execute(self, *_a):
            raise psycopg.errors.QueryCanceled("canceling statement due to statement timeout")

        def close(self):
            pass

    monkeypatch.setattr(profile_mod.rows, "_connect", lambda: TimeoutConn())
    monkeypatch.setattr(profile_mod.rows, "_run", orig_run)
    r4 = admin_c.get("/api/manage/tables/sample/profile", headers=admin_h)
    assert r4.status == 504 and json.loads(r4.body)["detail"] == "query_timeout"


def test_export_api_streaming_limits_sensitive_and_audit(clients, monkeypatch):
    from dashkit import config, db
    from dashkit.auth import store as auth_store
    from dashkit.manage import context
    from dashkit.manage.routes import export as export_mod

    (admin_c, admin_h), (op_c, op_h), _usr, _anon = clients
    monkeypatch.setenv("MANAGE_FEATURES", "export")
    monkeypatch.setattr(db, "owner_conn", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("owner_conn 금지")))

    project = config.load_project(context.current_project())
    table = next(iter(project.schema["tables"]))
    cols = list(project.schema["tables"][table]["columns"])

    # 잘못된 format / SQL 인젝션 시도 거절
    assert op_c.get(f"/api/manage/tables/{table}/export?format=tsv", headers=op_h).status == 422
    inj = op_c.get(
        f"/api/manage/tables/{table}/export?filters=" + json.dumps([{"column": "x; DROP TABLE raw.y", "op": "eq", "value": 1}]),
        headers=op_h,
    )
    assert inj.status == 422 and json.loads(inj.body)["detail"] == "invalid_filter"

    # EXPORT_MAX_ROWS=2 절단 및 UTF-8 BOM + 감사 로그 검증
    monkeypatch.setenv("EXPORT_MAX_ROWS", "2")
    audit_calls: list[dict] = []
    orig_record_audit = auth_store.record_audit

    def spy_audit(action, **kwargs):
        audit_calls.append({"action": action, **kwargs})
        return orig_record_audit(action, **kwargs)

    monkeypatch.setattr(export_mod.auth_store, "record_audit", spy_audit)
    r = op_c.get(
        f"/api/manage/tables/{table}/export?format=csv&filters=" + json.dumps([{"column": cols[0], "op": "not_null"}]),
        headers=op_h,
    )
    assert r.status == 200, r.body
    assert r.headers.get("x-export-max-rows") == "2"
    assert r.body.startswith(b"\xef\xbb\xbf")
    parsed = list(csv.reader(io.StringIO(r.body.decode("utf-8-sig"))))
    assert parsed[0] == cols
    assert len(parsed) == 3  # header + 2 rows

    assert audit_calls and audit_calls[-1]["action"] == "data.export"
    detail = audit_calls[-1]["detail"]
    assert detail["rows"] == 2 and detail["truncated"] is True and detail["filter_count"] == 1
    assert "filters" not in detail

    # sensitive: true 열 마스킹 및 필터/정렬 거절
    fake_schema = {
        "tables": {
            "secret_tbl": {
                "primary_key": ["id"],
                "columns": {"id": {"type": "text"}, "secret": {"type": "text", "sensitive": True}},
            }
        }
    }
    monkeypatch.setattr(export_mod, "_project", lambda: SimpleNamespace(name="p", schema=fake_schema))
    monkeypatch.setattr(export_mod, "_table", lambda _p, t: t)
    assert admin_c.get(
        "/api/manage/tables/secret_tbl/export?filters=" + json.dumps([{"column": "secret", "op": "eq", "value": "x"}]),
        headers=admin_h,
    ).status == 422
    assert admin_c.get(
        "/api/manage/tables/secret_tbl/export?sort=" + json.dumps([{"column": "secret", "dir": "asc"}]),
        headers=admin_h,
    ).status == 422

    class FakeExportCur:
        def __init__(self):
            self.itersize = 0
            self.sql = ""
            self._batches = [[("id-1",)], []]

        def execute(self, stmt, _params=None):
            self.sql = stmt.as_string(None) if hasattr(stmt, "as_string") else str(stmt)

        def fetchmany(self, _n):
            return self._batches.pop(0) if self._batches else []

        def close(self):
            pass

    class FakeExportConn:
        def __init__(self):
            self.cur = FakeExportCur()

        def cursor(self, name=None):
            return self.cur

        def close(self):
            pass

    fake_conn = FakeExportConn()
    monkeypatch.setattr(export_mod.rows, "_connect", lambda: fake_conn)
    r_sec = admin_c.get("/api/manage/tables/secret_tbl/export", headers=admin_h)
    assert r_sec.status == 200
    assert '"secret"' not in fake_conn.cur.sql
    sec_rows = list(csv.reader(io.StringIO(r_sec.body.decode("utf-8-sig"))))
    assert sec_rows == [["id", "secret"], ["id-1", "***"]]

    # rows/query에서도 sensitive: true 열 마스킹 및 필터/정렬 거절 검증
    from dashkit.manage.routes import rows as rows_mod

    monkeypatch.setattr(rows_mod, "_project", lambda: SimpleNamespace(name="p", schema=fake_schema))
    monkeypatch.setattr(rows_mod, "_table", lambda _p, t: t)
    assert admin_c.post(
        "/api/manage/tables/secret_tbl/rows/query",
        json={"filters": [{"column": "secret", "op": "eq", "value": "x"}]},
        headers=admin_h,
    ).status == 422
    assert admin_c.post(
        "/api/manage/tables/secret_tbl/rows/query",
        json={"sort": [{"column": "secret", "dir": "asc"}]},
        headers=admin_h,
    ).status == 422
    monkeypatch.setattr(rows_mod, "_versions", lambda _cur: (datetime.now(timezone.utc), {}))
    monkeypatch.setattr(
        rows_mod.paging,
        "fetch_page",
        lambda _cur, _rel, cols, _q, **_kw: SimpleNamespace(
            public=lambda: {
                "columns": [{"name": c.name, "type": c.type} for c in cols],
                "rows": [{"id": "id-1"}],
                "next_cursor": None,
                "total": 1,
                "changed": False,
                "truncated_cells": [],
            }
        ),
    )
    monkeypatch.setattr(rows_mod, "_run", lambda work: work(None))
    r_rows = admin_c.post("/api/manage/tables/secret_tbl/rows/query", json={"limit": 10}, headers=admin_h)
    assert r_rows.status == 200
    assert json.loads(r_rows.body)["rows"] == [{"id": "id-1", "secret": "***"}]

    # 민감 열이 기본 키인 테이블은 key_columns=None(오프셋 커서)으로 조회해 커서에 기본 키가 실리지 않는다
    pk_secret_schema = {
        "tables": {
            "secret_pk_tbl": {
                "primary_key": ["secret_id"],
                "columns": {"secret_id": {"type": "text", "sensitive": True}, "note": {"type": "text"}},
            }
        }
    }
    monkeypatch.setattr(rows_mod, "_project", lambda: SimpleNamespace(name="p", schema=pk_secret_schema))
    seen_key_cols: list[Any] = []

    def spy_fetch_page(_cur, _rel, cols, _q, **kw):
        seen_key_cols.append(kw.get("key_columns"))
        return SimpleNamespace(
            public=lambda: {
                "columns": [{"name": c.name, "type": c.type} for c in cols],
                "rows": [{"note": "ok"}],
                "next_cursor": None,
                "total": 1,
                "changed": False,
                "truncated_cells": [],
            }
        )

    monkeypatch.setattr(rows_mod.paging, "fetch_page", spy_fetch_page)
    r_pk = admin_c.post("/api/manage/tables/secret_pk_tbl/rows/query", json={"limit": 10}, headers=admin_h)
    assert r_pk.status == 200
    assert seen_key_cols[-1] is None
    assert json.loads(r_pk.body)["rows"] == [{"secret_id": "***", "note": "ok"}]


def test_freshness_api_app_and_manage_tables(clients, monkeypatch):
    from dashkit import db
    from dashkit.api import app as api_app_mod
    from dashkit.api.app import app as backend_app
    from dashkit.auth import sessions, store
    from dashkit.auth.routes import Principal

    (admin_c, admin_h), _op, _usr, _anon = clients
    api_c = Client(backend_app, host="localhost")

    # /api/app 인증 세션 생성
    from dashkit.auth import passwords
    h = passwords.hash_password(secrets.token_urlsafe(12))
    user = store.create_user(
        f"fresh-{secrets.token_hex(4)}@example.com",
        role="admin",
        status="active",
        source="invite",
        password_hash=h,
        email_verified=True,
    )
    token = sessions.new_token()
    store.start_session(user.id, h, sessions.token_hash(token), datetime.now(timezone.utc) + timedelta(hours=1), None, None)
    api_h = {"cookie": f"dashkit_session={token}"}

    try:
        # 비활성 시 data_as_of·freshness·stale 키 미포함
        monkeypatch.delenv("MANAGE_FEATURES", raising=False)
        api_app_mod._load()
        r_off = api_c.get("/api/app", headers=api_h)
        assert r_off.status == 200
        assert all("data_as_of" not in t for t in json.loads(r_off.body)["tabs"])

        r_tbl_off = admin_c.get("/api/manage/tables", headers=admin_h)
        assert r_tbl_off.status == 200
        assert all("freshness" not in t and "stale" not in t for t in json.loads(r_tbl_off.body)["items"])

        # 활성 시 /api/app은 dash_reader만 사용하며 data_as_of 포함
        monkeypatch.setenv("MANAGE_FEATURES", "freshness")
        with monkeypatch.context() as m:
            m.setattr(db, "owner_conn", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("owner_conn 금지")))
            m.setattr(db, "inspect_conn", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("inspect_conn 금지")))
            r_on = api_c.get("/api/app", headers=api_h)
            assert r_on.status == 200
            tabs_on = json.loads(r_on.body)["tabs"]
            assert all("data_as_of" in t for t in tabs_on)

        # /api/manage/tables에는 freshness·stale 포함
        r_tbl_on = admin_c.get("/api/manage/tables", headers=admin_h)
        assert r_tbl_on.status == 200
        assert all("freshness" in t and "stale" in t for t in json.loads(r_tbl_on.body)["items"])
    finally:
        store.delete_user(user.id)
