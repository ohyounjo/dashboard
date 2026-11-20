"""DB 계정 경계(통합). 계약: contracts/auth.md "DB 계정", 설계서 7-5.

- dash_auth는 dashkit_auth 표의 SELECT·INSERT·UPDATE·DELETE만 한다. raw·dash·dashkit_meta를 읽지 못한다.
- dash_reader(와 로더 계정 dash_owner)는 dashkit_auth를 읽지 못한다.
- 로더의 재적재(DROP SCHEMA)는 raw·dash만 지운다. dashkit_auth는 남는다.
"""
from __future__ import annotations

import psycopg
import pytest

from dashkit import config, db
from dashkit.auth import cli
from dashkit.config import load_project

from conftest import PROJECTS, delete_user, live_project, make_user, random_email

pytestmark = pytest.mark.integration


def denied(connect, query: str) -> bool:
    with connect(autocommit=True) as conn:
        try:
            conn.execute(query)
        except psycopg.errors.InsufficientPrivilege:
            return True
    return False


@pytest.fixture(scope="module")
def project():
    return load_project(live_project(), PROJECTS)


def test_schema_is_at_latest_version():
    with db.auth_conn(autocommit=True) as conn:
        (v,), = conn.execute("SELECT max(version) FROM dashkit_auth.schema_version").fetchall()
    assert v == cli.latest_version()


def test_auth_account_cannot_read_dashboard_data(project):
    view = sorted(project.dashboard["datasets"])[0]
    table = sorted(project.schema["tables"])[0]
    assert not denied(db.reader_conn, f'SELECT 1 FROM dash."{view}" LIMIT 1')      # 비교 기준: reader는 읽는다
    for query in (f'SELECT 1 FROM dash."{view}" LIMIT 1', f'SELECT 1 FROM raw."{table}" LIMIT 1',
                  "SELECT 1 FROM dashkit_meta.load_snapshot LIMIT 1"):
        assert denied(db.auth_conn, query), query


def test_reader_and_owner_cannot_read_auth_schema():
    for connect in (db.reader_conn, db.owner_conn):
        for query in ("SELECT 1 FROM dashkit_auth.users LIMIT 1", "SELECT 1 FROM dashkit_auth.sessions LIMIT 1",
                      "SELECT 1 FROM dashkit_auth.schema_version LIMIT 1"):
            assert denied(connect, query), (connect.__name__, query)


def test_auth_account_has_only_row_privileges():
    assert not denied(db.auth_conn, "SELECT 1 FROM dashkit_auth.users LIMIT 1")
    for query in ("INSERT INTO dashkit_auth.schema_version (version) VALUES (999)",
                  "DELETE FROM dashkit_auth.schema_version",
                  "CREATE TABLE dashkit_auth.extra (x int)",
                  "CREATE TABLE public.extra (x int)",
                  "DROP TABLE dashkit_auth.users",
                  "ALTER TABLE dashkit_auth.users ADD COLUMN extra int",
                  "TRUNCATE dashkit_auth.users"):
        assert denied(db.auth_conn, query), query
    with db.auth_conn(autocommit=True) as conn:
        flags = conn.execute("SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user").fetchone()
    assert flags == (False, False, False)


def test_reload_keeps_auth_schema(project):
    """프로젝트를 다시 적재해도(raw·dash DROP SCHEMA) 사용자 계정은 남는다."""
    from dashkit.loader import load

    user = make_user(random_email("reload"), None, status="invited")
    try:
        load(project)
        with db.auth_conn(autocommit=True) as conn:
            assert conn.execute("SELECT 1 FROM dashkit_auth.users WHERE id = %s", (user.id,)).fetchone()
        assert config.widget_datasets(project)       # 재적재 뒤에도 프로젝트는 그대로다
    finally:
        delete_user(user.id)
