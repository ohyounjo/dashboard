"""이슈 #35: 읽기 전용 SQL 콘솔 단위 및 통합 테스트."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from dashkit.auth import sessions, store
from dashkit.ingest import IngestError
from dashkit.manage import sql_guard
from studio_client import Client


def test_sql_guard_allows_valid_read_queries():
    w1, c1 = sql_guard.prepare_sql("SELECT 1;")
    assert c1 == "SELECT 1"
    assert w1 == "SELECT * FROM (\nSELECT 1\n) AS _dashkit_q LIMIT %s OFFSET %s"

    w2, _ = sql_guard.prepare_sql("WITH x AS (SELECT 1 AS a) SELECT * FROM x")
    assert "WITH x AS" in w2

    w3, _ = sql_guard.prepare_sql("SELECT 'a;b' AS s, 5 % 2 AS rem, 'abc' LIKE 'a%' AS ok, '%s' AS f, '%(x)s' AS n")
    assert "5 %% 2" in w3
    assert "'a%%'" in w3
    assert "'%%s'" in w3
    assert "'%%(x)s'" in w3

    w4, _ = sql_guard.prepare_sql("SELECT 1 -- trailing comment")
    assert w4.endswith("\n) AS _dashkit_q LIMIT %s OFFSET %s")

    w5, _ = sql_guard.prepare_sql("SELECT /* nested /* comment */ */ 1")
    assert "SELECT" in w5

    w6, _ = sql_guard.prepare_sql("SELECT $tag$a;b$tag$ AS s")
    assert "$tag$a;b$tag$" in w6


@pytest.mark.parametrize(
    "bad_sql",
    [
        "",
        "   ",
        "INSERT INTO raw.issues VALUES (1)",
        "UPDATE raw.issues SET status = 'x'",
        "DELETE FROM raw.issues",
        "CREATE TABLE raw.x (id int)",
        "DROP TABLE raw.issues",
        "ALTER TABLE raw.issues ADD COLUMN x text",
        "TRUNCATE raw.issues",
        "GRANT ALL ON raw.issues TO PUBLIC",
        "COPY raw.issues TO STDOUT",
        "DO $$ BEGIN PERFORM 1; END $$",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE raw.issues",
        "SELECT 1;;",
        "SELECT 1) AS q; SELECT 2 --",
        "SELECT 1) AS q UNION SELECT 2 --",
        "SELECT (1",
        "SELECT 'unclosed",
        "SELECT /* unclosed",
        "SELECT $tag$unclosed",
        "SELECT pg_sleep(10)",
        "SELECT pg_catalog.pg_sleep(1)",
        "SELECT pg_terminate_backend(123)",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT dblink_connect('host=127.0.0.1')",
    ],
)
def test_sql_guard_rejects_unsafe_or_malformed_queries(bad_sql):
    with pytest.raises(IngestError) as exc:
        sql_guard.prepare_sql(bad_sql)
    assert exc.value.status == 422
    assert exc.value.detail == "invalid_query"


def _make_user_client(app, role: str):
    from dashkit.auth import passwords

    h = passwords.hash_password(secrets.token_urlsafe(12))
    user = store.create_user(
        f"sql-{role}-{secrets.token_hex(4)}@example.com",
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
def sql_clients():
    from dashkit.manage.app import app

    admin_u, admin_c, admin_h = _make_user_client(app, "admin")
    op_u, op_c, op_h = _make_user_client(app, "operator")
    usr_u, usr_c, usr_h = _make_user_client(app, "user")
    yield (admin_u, admin_c, admin_h), (op_u, op_c, op_h), (usr_u, usr_c, usr_h), Client(app, host="localhost")
    for u in (admin_u, op_u, usr_u):
        store.delete_user(u.id)


@pytest.mark.integration
def test_sql_console_disabled_and_permissions(sql_clients, monkeypatch):
    (_au, admin_c, admin_h), (_ou, op_c, op_h), (_uu, usr_c, usr_h), anon_c = sql_clients
    monkeypatch.delenv("MANAGE_FEATURES", raising=False)

    assert anon_c.post("/api/manage/sql", headers={"Content-Type": "application/json"}, body=b'{"sql":"SELECT 1"}').status == 401
    assert usr_c.post("/api/manage/sql", headers=usr_h, body=b'{"sql":"SELECT 1"}').status == 403
    r_dis = admin_c.post("/api/manage/sql", headers=admin_h, body=b'{"sql":"SELECT 1"}')
    assert r_dis.status == 404
    assert json.loads(r_dis.body)["detail"] == "not_found"
    assert op_c.post("/api/manage/sql", headers=op_h, body=b'{"sql":"SELECT 1"}').status == 404


@pytest.mark.integration
def test_sql_console_execution_security_and_audit(sql_clients, monkeypatch):
    from dashkit import db
    from dashkit.manage import context

    (_au, admin_c, admin_h), (op_u, op_c, op_h), _usr, _anon = sql_clients
    monkeypatch.setenv("MANAGE_FEATURES", "sql")
    monkeypatch.setattr(db, "owner_conn", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("owner_conn 금지")))

    # 1. 정상 조회, % 이스케이프, 중복 열 이름 보존, 절단
    r_ok = op_c.post(
        "/api/manage/sql",
        headers=op_h,
        body=json.dumps({
            "sql": "SELECT 1 AS id, 2 AS id, 5 % 2 AS rem, 'abc' LIKE 'a%' AS m, '%s' AS fmt, repeat('x', 2005) AS long_col;",
            "limit": 50,
            "offset": 0,
        }).encode(),
    )
    assert r_ok.status == 200, r_ok.body
    data = json.loads(r_ok.body)
    assert [c["name"] for c in data["columns"]] == ["id", "id", "rem", "m", "fmt", "long_col"]
    assert len(data["rows"]) == 1
    assert data["rows"][0][0] == 1
    assert data["rows"][0][1] == 2
    assert data["rows"][0][2] == 1
    assert data["rows"][0][3] is True
    assert data["rows"][0][4] == "%s"
    assert len(data["rows"][0][5]) == 2000
    assert data["truncated_cells"] == [[0, 5]]
    assert data["has_more"] is False

    # 2. 페이지 나눔 및 경계
    r_page = admin_c.post(
        "/api/manage/sql",
        headers=admin_h,
        body=json.dumps({"sql": "SELECT generate_series(1, 3) AS n", "limit": 2, "offset": 0}).encode(),
    )
    assert r_page.status == 200
    p1 = json.loads(r_page.body)
    assert p1["rows"] == [[1], [2]]
    assert p1["has_more"] is True

    assert admin_c.post("/api/manage/sql", headers=admin_h, body=json.dumps({"sql": "SELECT 1", "limit": 201}).encode()).status == 422
    assert admin_c.post("/api/manage/sql", headers=admin_h, body=json.dumps({"sql": "SELECT 1", "offset": 100001}).encode()).status == 422

    # 3. 쓰기 CTE 차단 (422)
    from dashkit import config
    first_table = next(iter(config.load_project(context.current_project()).schema["tables"]))
    r_wcte = op_c.post(
        "/api/manage/sql",
        headers=op_h,
        body=json.dumps({"sql": f"WITH d AS (DELETE FROM raw.{first_table} RETURNING *) SELECT * FROM d"}).encode(),
    )
    assert r_wcte.status == 422

    # 4. dashkit_auth 스키마 조회 차단 (403 forbidden)
    r_auth = op_c.post(
        "/api/manage/sql",
        headers=op_h,
        body=json.dumps({"sql": "SELECT * FROM dashkit_auth.users"}).encode(),
    )
    assert r_auth.status == 403
    assert json.loads(r_auth.body)["detail"] == "forbidden"

    # 5. 타임아웃 504
    orig_limits = context.limits()
    monkeypatch.setattr(
        context,
        "limits",
        lambda: type(orig_limits)(
            orig_limits.inline_max_rows, orig_limits.max_upload_mb, orig_limits.max_rows,
            orig_limits.max_request_mb, orig_limits.compact_hint, 50,
            orig_limits.lock_wait_ms, orig_limits.job_lock_wait_ms, orig_limits.export_max_rows,
        ),
    )
    r_to = op_c.post(
        "/api/manage/sql",
        headers=op_h,
        body=json.dumps({"sql": "SELECT count(*) FROM generate_series(1, 50000000) AS a, generate_series(1, 10) AS b"}).encode(),
    )
    assert r_to.status == 504
    assert json.loads(r_to.body)["detail"] == "query_timeout"
    monkeypatch.undo()
    monkeypatch.setenv("MANAGE_FEATURES", "sql")

    # 6. 감사 로그 2,000자 절단 및 결과 값 미포함 검증
    long_sql = "SELECT md5('abc') AS v -- " + ("a" * 2100)
    r_long = op_c.post("/api/manage/sql", headers=op_h, body=json.dumps({"sql": long_sql}).encode())
    assert r_long.status == 200
    assert json.loads(r_long.body)["rows"] == [["900150983cd24fb0d6963f7d28e17f72"]]
    with db.auth_conn() as conn:
        aud = conn.execute(
            "SELECT actor_id, actor_email, detail FROM dashkit_auth.audit_log WHERE action = 'data.query.run' AND actor_id = %s ORDER BY id DESC LIMIT 1",
            (op_u.id,),
        ).fetchone()
    assert str(aud[0]) == op_u.id
    assert aud[1] == op_u.email
    assert len(aud[2]["sql"]) == 2000
    assert aud[2]["rows"] == 1
    assert "900150983cd24fb0d6963f7d28e17f72" not in json.dumps(aud[2])
