"""이슈 #38: 자동 적재용 API 토큰 단위 및 통합 테스트."""
from __future__ import annotations

import io
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from dashkit.auth import sessions, store
from dashkit.features import KNOWN_MANAGE_FEATURES, has_manage_feature, manage_features
from dashkit.manage import auth as manage_auth
from studio_client import Client


def test_manage_features_includes_tokens_and_sql(monkeypatch):
    assert {"tokens", "sql"} <= KNOWN_MANAGE_FEATURES
    monkeypatch.setenv("MANAGE_FEATURES", "tokens, sql, unknown")
    assert manage_features() == frozenset({"tokens", "sql"})
    assert has_manage_feature("tokens")
    assert has_manage_feature("sql")


def test_api_token_format_and_status():
    raw = manage_auth.new_api_token()
    assert raw.startswith("dkt_")
    assert len(raw) == 47
    assert manage_auth.plausible_api_token(raw)
    assert not manage_auth.plausible_api_token(None)
    assert not manage_auth.plausible_api_token("bad_" + "a" * 40)
    assert not manage_auth.plausible_api_token("dkt_short")
    assert not manage_auth.plausible_api_token("dkt_" + "!" * 40)

    now = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
    active = store.ApiToken(
        id="1", name="ci", project="p", tables=("t1",), created_by=None, created_by_email=None,
        created_at=now - timedelta(days=1), expires_at=now + timedelta(days=1),
        last_used_at=None, revoked_at=None, revoked_by=None,
    )
    assert active.status(now) == "active"
    expired = store.ApiToken(
        id="2", name="ci", project="p", tables=("t1",), created_by=None, created_by_email=None,
        created_at=now - timedelta(days=2), expires_at=now,
        last_used_at=None, revoked_at=None, revoked_by=None,
    )
    assert expired.status(now) == "expired"
    revoked = store.ApiToken(
        id="3", name="ci", project="p", tables=("t1",), created_by=None, created_by_email=None,
        created_at=now - timedelta(days=1), expires_at=now + timedelta(days=10),
        last_used_at=None, revoked_at=now, revoked_by=None,
    )
    assert revoked.status(now) == "revoked"


def test_manage_guard_ambiguous_auth_and_duplicate_authorization():
    from dashkit.manage.app import app

    c = Client(app, host="localhost")
    r_ambig_get = c.get(
        "/api/manage/health",
        headers={"Authorization": "Bearer dkt_" + "a" * 43, "Cookie": "dashkit_session=abc"},
    )
    assert r_ambig_get.status == 400
    assert json.loads(r_ambig_get.body)["detail"] == "ambiguous_auth"

    r_ambig_post = c.post(
        "/api/manage/tables/events/batches",
        headers={
            "Authorization": "Bearer dkt_" + "a" * 43,
            "Cookie": "dashkit_session=abc",
            "Content-Type": "application/json",
        },
        body=b"{}",
    )
    assert r_ambig_post.status == 400
    assert json.loads(r_ambig_post.body)["detail"] == "ambiguous_auth"


def test_manage_guard_duplicate_auth_header_raw_asgi():
    import asyncio
    from dashkit.manage.app import app

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/manage/health",
        "headers": [
            (b"authorization", b"Bearer a"),
            (b"authorization", b"Bearer b"),
        ],
    }
    asyncio.run(app(scope, receive, send))
    assert sent[0]["status"] == 400
    assert json.loads(sent[1]["body"])["detail"] == "bad_request"


def _make_user_client(app, role: str):
    from dashkit.auth import passwords

    h = passwords.hash_password(secrets.token_urlsafe(12))
    user = store.create_user(
        f"tok-{role}-{secrets.token_hex(4)}@example.com",
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
    return user, Client(app, host="localhost"), {
        "cookie": f"dashkit_session={token}",
        "content-type": "application/json",
    }


@pytest.fixture(scope="module")
def integration_clients():
    from dashkit.manage.app import app

    admin_u, admin_c, admin_h = _make_user_client(app, "admin")
    op_u, op_c, op_h = _make_user_client(app, "operator")
    usr_u, usr_c, usr_h = _make_user_client(app, "user")
    yield (admin_u, admin_c, admin_h), (op_u, op_c, op_h), (usr_u, usr_c, usr_h), Client(app, host="localhost")
    for u in (admin_u, op_u, usr_u):
        store.delete_user(u.id)


@pytest.mark.integration
def test_tokens_disabled_returns_404_and_bearer_401(integration_clients, monkeypatch):
    from dashkit import config
    from dashkit.manage import context

    (_au, admin_c, admin_h), (_ou, op_c, op_h), (_uu, usr_c, usr_h), anon_c = integration_clients
    monkeypatch.delenv("MANAGE_FEATURES", raising=False)

    cfg = json.loads(admin_c.get("/api/manage/config", headers=admin_h).body)
    assert "tokens" not in cfg["features"]

    assert anon_c.get("/api/manage/tokens").status == 401
    assert usr_c.get("/api/manage/tokens", headers=usr_h).status == 403
    assert op_c.get("/api/manage/tokens", headers=op_h).status == 403
    r_dis = admin_c.get("/api/manage/tokens", headers=admin_h)
    assert r_dis.status == 404
    assert json.loads(r_dis.body)["detail"] == "not_found"

    table = next(iter(config.load_project(context.current_project()).schema["tables"]))
    r_bearer = anon_c.post(
        f"/api/manage/tables/{table}/batches",
        headers={"Authorization": f"Bearer {manage_auth.new_api_token()}", "Content-Type": "application/json"},
        body=json.dumps({"rows": []}).encode(),
    )
    assert r_bearer.status == 401
    assert json.loads(r_bearer.body)["detail"] == "unauthenticated"

    r_job = anon_c.get(
        "/api/manage/jobs/nonexistent",
        headers={"Authorization": f"Bearer {manage_auth.new_api_token()}"},
    )
    assert r_job.status == 401
    assert json.loads(r_job.body)["detail"] == "unauthenticated"


@pytest.mark.integration
def test_tokens_lifecycle_validation_and_audit(integration_clients, monkeypatch):
    from dashkit import config, db
    from dashkit.manage import context

    (_au, admin_c, admin_h), (_ou, op_c, op_h), (_uu, usr_c, usr_h), _anon = integration_clients
    monkeypatch.setenv("MANAGE_FEATURES", "tokens")

    project = config.load_project(context.current_project())
    append_tables = [k for k, v in project.schema["tables"].items() if (v.get("mode") or "replace") == "append"]
    replace_tables = [k for k, v in project.schema["tables"].items() if (v.get("mode") or "replace") != "append"]
    if not append_tables:
        pytest.skip("현재 프로젝트에 append 테이블이 없습니다")

    target_table = append_tables[0]
    future_date = (datetime.now(timezone.utc) + timedelta(days=10)).strftime("%Y-%m-%d")

    assert op_c.post("/api/manage/tokens", headers=op_h, body=b"{}").status == 403
    assert usr_c.post("/api/manage/tokens", headers=usr_h, body=b"{}").status == 403

    # 잘못된 입력 거절
    r_unk = admin_c.post(
        "/api/manage/tokens",
        headers=admin_h,
        body=json.dumps({"name": "t1", "tables": ["no_such_table"], "expires_at": future_date}).encode(),
    )
    assert r_unk.status == 422
    assert json.loads(r_unk.body)["detail"] == "unknown_table"

    if replace_tables:
        r_rep = admin_c.post(
            "/api/manage/tokens",
            headers=admin_h,
            body=json.dumps({"name": "t1", "tables": [replace_tables[0]], "expires_at": future_date}).encode(),
        )
        assert r_rep.status == 422
        assert json.loads(r_rep.body)["detail"] == "not_append"

    past_date = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    r_past = admin_c.post(
        "/api/manage/tokens",
        headers=admin_h,
        body=json.dumps({"name": "t1", "tables": [target_table], "expires_at": past_date}).encode(),
    )
    assert r_past.status == 422
    assert json.loads(r_past.body)["detail"] == "invalid_request"

    far_date = (datetime.now(timezone.utc) + timedelta(days=366)).strftime("%Y-%m-%d")
    r_far = admin_c.post(
        "/api/manage/tokens",
        headers=admin_h,
        body=json.dumps({"name": "t1", "tables": [target_table], "expires_at": far_date}).encode(),
    )
    assert r_far.status == 422

    # 발급 성공
    r_ok = admin_c.post(
        "/api/manage/tokens",
        headers=admin_h,
        body=json.dumps({"name": "ci-sync", "tables": [target_table], "expires_at": future_date}).encode(),
    )
    assert r_ok.status == 201, r_ok.body
    created = json.loads(r_ok.body)
    raw_token = created["token"]
    assert raw_token.startswith("dkt_")
    tok_id = created["id"]

    # 목록 및 DB/감사 로그에 원문 미노출 확인
    r_list = admin_c.get("/api/manage/tokens", headers=admin_h)
    assert r_list.status == 200
    assert raw_token not in r_list.body.decode("utf-8")

    with db.auth_conn() as conn:
        audit_rows = conn.execute(
            "SELECT action, target_type, target_id, target_label, detail::text AS d FROM dashkit_auth.audit_log WHERE target_id = %s",
            (tok_id,),
        ).fetchall()
    assert any(r[0] == "data.token.create" for r in audit_rows)
    for r in audit_rows:
        assert raw_token not in (r[4] or "")

    # 폐기 204 및 멱등성
    r_del1 = admin_c.delete(f"/api/manage/tokens/{tok_id}", headers=admin_h)
    assert r_del1.status == 204
    r_del2 = admin_c.delete(f"/api/manage/tokens/{tok_id}", headers=admin_h)
    assert r_del2.status == 204

    with db.auth_conn() as conn:
        revoke_count = conn.execute(
            "SELECT count(*) FROM dashkit_auth.audit_log WHERE target_id = %s AND action = 'data.token.revoke'",
            (tok_id,),
        ).fetchone()[0]
    assert revoke_count == 1


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("PROJECT") != "zz-ingest", reason="실제 적재 토큰 검증은 zz-ingest 단계에서 실행")
def test_token_ingest_security_boundaries_and_jobs(integration_clients, monkeypatch):
    from dashkit import config, db
    from dashkit.manage import context

    (_au, admin_c, admin_h), (_ou, op_c, op_h), _usr, anon_c = integration_clients
    monkeypatch.setenv("MANAGE_FEATURES", "tokens")

    project = config.load_project(context.current_project())
    append_tables = sorted(k for k, v in project.schema["tables"].items() if (v.get("mode") or "replace") == "append")
    assert len(append_tables) >= 2
    allowed_table, other_table = append_tables[0], append_tables[1]

    future_date = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%d")
    created = json.loads(
        admin_c.post(
            "/api/manage/tokens",
            headers=admin_h,
            body=json.dumps({"name": "ingest-bot", "tables": [allowed_table], "expires_at": future_date}).encode(),
        ).body
    )
    tok_id = created["id"]
    raw_token = created["token"]
    bearer = {"Authorization": f"Bearer {raw_token}"}

    # 1. 허용 테이블 JSON rows 적재 + cross-site Origin 허용(쿠키 없음)
    tdef = project.schema["tables"][allowed_table]
    sample_row = {}
    for cname, cspec in tdef["columns"].items():
        ctype = cspec.get("type", "text")
        if ctype == "integer":
            sample_row[cname] = 1
        elif ctype == "number":
            sample_row[cname] = 1.5
        elif ctype == "boolean":
            sample_row[cname] = True
        elif ctype == "date":
            sample_row[cname] = "2026-11-22"
        elif ctype == "enum":
            sample_row[cname] = cspec["values"][0]
        else:
            sample_row[cname] = f"tok-{secrets.token_hex(4)}"

    r_ing = anon_c.post(
        f"/api/manage/tables/{allowed_table}/batches",
        headers={
            **bearer,
            "Content-Type": "application/json",
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
        },
        body=json.dumps({"rows": [sample_row]}).encode(),
    )
    assert r_ing.status == 200, r_ing.body

    # last_used_at 갱신 및 감사 로그 확인
    with db.auth_conn() as conn:
        last_used = conn.execute("SELECT last_used_at FROM dashkit_auth.api_tokens WHERE id = %s", (tok_id,)).fetchone()[0]
        assert last_used is not None
        aud = conn.execute(
            "SELECT actor_id, actor_email, detail FROM dashkit_auth.audit_log WHERE action = 'data.batch.create' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert aud[0] is None
        assert aud[1] == "token:ingest-bot"
        assert aud[2]["auth_type"] == "api_token"
        assert aud[2]["api_key_id"] == tok_id

    # 2. 허용 외 테이블 403
    r_forb = anon_c.post(
        f"/api/manage/tables/{other_table}/batches",
        headers={**bearer, "Content-Type": "application/json"},
        body=json.dumps({"rows": []}).encode(),
    )
    assert r_forb.status == 403

    # 3. 다른 경로 Bearer 거절(401)
    for m, p in (
        ("GET", "/api/manage/tables"),
        ("GET", "/api/manage/jobs"),
        ("GET", "/api/manage/tokens"),
        ("POST", f"/api/manage/tables/{allowed_table}/rows/query"),
        ("POST", "/api/manage/sql"),
        ("PATCH", "/api/manage/config"),
    ):
        resp = anon_c.request(m, p, headers={**bearer, "Content-Type": "application/json"}, body=b"{}")
        assert resp.status == 401, f"{m} {p} -> {resp.status}"

    # 4. 다른 사용자의 작업 조회 403
    from dashkit.manage.runs_state import runs
    other_run = runs.start("ingest", lambda _ctx: {"ok": True}, table=allowed_table, actor_id="other", actor_email="o@e.com")
    assert anon_c.get(f"/api/manage/jobs/{other_run.id}", headers=bearer).status == 403

    # 5. 폐기 후 401
    assert admin_c.delete(f"/api/manage/tokens/{tok_id}", headers=admin_h).status == 204
    r_rev = anon_c.post(
        f"/api/manage/tables/{allowed_table}/batches",
        headers={**bearer, "Content-Type": "application/json"},
        body=json.dumps({"rows": [sample_row]}).encode(),
    )
    assert r_rev.status == 401
    assert json.loads(r_rev.body)["detail"] == "invalid_token"
