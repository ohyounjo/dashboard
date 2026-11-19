"""로더(dashkit.loader). 운영 DB를 건드리지 않도록 임시 DB를 만들어 쓴다."""
from __future__ import annotations

import uuid

import psycopg
import pytest
from psycopg import sql

from dashkit import db
from dashkit.config import load_project
from dashkit.loader import LoadError, load

from conftest import PROJECTS, copy_project, read_csv, write_csv

PROJECT = "devtracker"


@pytest.fixture(scope="module")
def test_db():
    name = f"dash_test_{uuid.uuid4().hex[:8]}"
    with db.owner_conn(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield name
    with db.owner_conn(autocommit=True) as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()", (name,))
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


def _broken_copy(tmp_path):
    root = copy_project(PROJECT, tmp_path)
    data = root / PROJECT / "data"

    h, rows = read_csv(data / "issues.csv")
    rows[0]["milestone_id"] = "ms99"          # 없는 참조
    rows[1]["severity"] = "blocker"           # 잘못된 enum
    write_csv(data / "issues.csv", h, rows)

    h, rows = read_csv(data / "milestones.csv")
    rows[2]["progress"] = "120"               # 범위 밖 숫자
    write_csv(data / "milestones.csv", h, rows)

    h, rows = read_csv(data / "docs.csv")
    rows[0]["version"] = "2.1"                # 패턴 불일치
    write_csv(data / "docs.csv", h, rows)

    h, rows = read_csv(data / "tasks.csv")
    rows.append(dict(rows[0]))                # 중복 키
    write_csv(data / "tasks.csv", h, rows)
    return root


def test_broken_csv_reports_all_errors_and_keeps_db(loaded, test_db, tmp_path):
    before = snapshot(test_db)
    root = _broken_copy(tmp_path)
    with pytest.raises(LoadError) as exc:
        load(load_project(PROJECT, root), dbname=test_db)
    errs = exc.value.errors
    expected = [
        ("data/issues.csv:2:milestone_id:", "ms99"),
        ("data/issues.csv:3:severity:", "blocker"),
        ("data/milestones.csv:4:progress:", "120"),
        ("data/docs.csv:2:version:", "2.1"),
        ("data/tasks.csv:18:id:", "T1"),
    ]
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
