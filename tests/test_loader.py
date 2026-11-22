"""로더(dashkit.loader). 운영 DB를 건드리지 않도록 임시 DB를 만들어 쓴다."""
from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg import sql

from dashkit import db
from dashkit.config import load_project
from dashkit.loader import LoadError, load

from conftest import BROKEN_CSV_ERRORS, PROJECTS, broken_csv_copy, copy_project, read_csv, write_csv

PROJECT = "devtracker"
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def test_db():
    name = f"dash_test_{uuid.uuid4().hex[:8]}"
    with db.owner_conn(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield name
    with db.owner_conn(autocommit=True) as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid() "
                     "AND backend_type = 'client backend'", (name,))
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def table_counts(dbname: str, tables) -> dict[str, int]:
    with db.owner_conn(dbname) as conn:
        return {t: conn.execute(sql.SQL("SELECT count(*) FROM raw.{}").format(sql.Identifier(t))).fetchone()[0] for t in tables}


def snapshot(dbname: str) -> list:
    """비교용: 이슈 테이블 전체 내용(정렬)."""
    with db.owner_conn(dbname) as conn:
        return conn.execute("SELECT * FROM raw.issues ORDER BY id").fetchall()


@pytest.fixture(scope="module")
def loaded(test_db):
    project = load_project(PROJECT, PROJECTS)
    counts = load(project, dbname=test_db)
    return project, counts


def test_row_counts_match_csv(loaded, test_db):
    project, counts = loaded
    csv_counts = {t: len(read_csv(project.data_dir / f"{t}.csv")[1]) for t in project.schema["tables"]}
    assert counts == csv_counts
    assert table_counts(test_db, csv_counts) == csv_counts


def test_broken_csv_reports_all_errors_and_keeps_db(loaded, test_db, tmp_path):
    before = snapshot(test_db)
    root = broken_csv_copy(tmp_path)
    with pytest.raises(LoadError) as exc:
        load(load_project(PROJECT, root), dbname=test_db)
    errs = exc.value.errors
    expected = BROKEN_CSV_ERRORS
    for prefix, value in expected:
        assert any(e.startswith(prefix) and value in e for e in errs), (prefix, errs)
    assert len(errs) == len(expected), errs
    assert snapshot(test_db) == before


def test_check_violation_rolls_back(loaded, test_db, tmp_path):
    before = snapshot(test_db)
    root = copy_project(PROJECT, tmp_path)
    path = root / PROJECT / "data" / "issues.csv"
    h, rows = read_csv(path)
    target = rows[2]
    target["created"], target["updated"] = "2026-11-18", "2026-11-01"  # created > updated
    write_csv(path, h, rows)
    with pytest.raises(LoadError) as exc:
        load(load_project(PROJECT, root), dbname=test_db)
    assert any(target["id"] in e and "위반 키" in e for e in exc.value.errors), exc.value.errors
    assert snapshot(test_db) == before


def test_reader_cannot_read_raw_or_write(loaded, test_db):
    with db.reader_conn(test_db, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM dash.issues").fetchone()[0] == 24  # dash는 읽힌다
        for stmt in (
            "SELECT count(*) FROM raw.issues",
            "INSERT INTO raw.members (id, name, role) VALUES ('x', 'x', 'x')",
            "UPDATE dash.milestones SET progress = 0",
            "DELETE FROM dash.kpi_snapshots",  # 단일 테이블 뷰(자동 갱신 가능)라 권한 검사까지 간다
            "CREATE TABLE dash.t_probe (x int)",
            "CREATE TABLE raw.t_probe (x int)",
            "CREATE TABLE public.t_probe (x int)",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt)


def test_declared_indexes_are_created_and_deterministic(loaded, test_db, tmp_path):
    from pathlib import Path

    from dashkit import paging
    from dashkit.loader import _index_name

    # 색인 이름 충돌 방지(구분자 모호성 및 63바이트 제한)
    n1 = _index_name("a", 0, ["b_0_c"])
    n2 = _index_name("a_0_b", 0, ["c"])
    n3 = _index_name("t", 0, ["a_b", "c"])
    n4 = _index_name("t", 0, ["a", "b_c"])
    long_name = _index_name("very_long_table_name_" * 3, 1, ["very_long_column_name_" * 3])
    assert n1 != n2 and n3 != n4
    assert len(n1) <= 63 and len(long_name) <= 63

    # 색인 없는 상태에서 조회 결과 기억
    proj_dir = Path(__file__).resolve().parent.parent / "projects"
    orig_proj = load_project(PROJECT, proj_dir)
    cols = [paging.Column(name=c, type=spec["type"]) for c, spec in orig_proj.schema["tables"]["issues"]["columns"].items()]
    q = paging.parse_query({"sort": [{"column": "status", "dir": "asc"}], "limit": 50}, cols)
    with db.inspect_conn(test_db) as conn, conn.cursor() as cur:
        before_page = paging.fetch_page(cur, sql.Identifier("raw", "issues"), cols, q,
                                        key_columns=["id"], version="v1", total_rows=None)

    # schema.yaml에 단일·복합 색인 선언 후 load
    root = copy_project(PROJECT, tmp_path)
    schema_path = root / PROJECT / "schema.yaml"
    import yaml
    doc = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    doc["tables"]["issues"]["indexes"] = [["status"], ["status", "created"]]
    schema_path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    load(load_project(PROJECT, root), dbname=test_db)

    with db.inspect_conn(test_db) as conn, conn.cursor() as cur:
        idx_rows = cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'raw' AND tablename = 'issues'"
        ).fetchall()
        names = {r[0] for r in idx_rows}
        assert _index_name("issues", 0, ["status"]) in names
        assert _index_name("issues", 1, ["status", "created"]) in names
        # 조회 결과 불변 및 EXPLAIN 구조 확인
        after_page = paging.fetch_page(cur, sql.Identifier("raw", "issues"), cols, q,
                                       key_columns=["id"], version="v1", total_rows=None)
        assert after_page.rows == before_page.rows
        plan = cur.execute(
            "EXPLAIN (FORMAT JSON) SELECT id FROM raw.issues WHERE status = 'open' ORDER BY created"
        ).fetchone()[0]
        assert plan is not None
