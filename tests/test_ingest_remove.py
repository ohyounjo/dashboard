"""배치 되돌리기·방금 추가 취소·되살리기(contracts/ingest.md "되돌리기와 되살리기"). 임시 DB를 쓴다."""
from __future__ import annotations

import pytest

from dashkit.config import load_project
from dashkit.ingest import operation, pending
from dashkit.ingest.remove import NeedsRebuild, remove_batch, restore_batch
from dashkit.loader import load

from test_ingest_db import (add, all_rows, expect_error, fault, folder, leftovers, make_project, rebuilt_equals_current,  # noqa: F401
                            rows, scratch_db)

pytestmark = pytest.mark.integration


def remove(root, dbname, table, name, **kw):
    return remove_batch("ledger", table, name, projects_dir=root, dbname=dbname, **kw)


def removed_dir(root, table):
    p = root / "ledger" / "data" / table / "_removed"
    return sorted(x.name for x in p.iterdir()) if p.is_dir() else []


def test_fast_cancel_equals_rebuild_without_the_file(tmp_path, scratch_db):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    before = all_rows(scratch_db)
    name = add(root, scratch_db, "entries", [{"id": "E200"}, {"id": "E201"}])["batches"][0]["file"]
    r = remove(root, scratch_db, "entries", name, fast_only=True)
    assert r["fast"] and r["rows"] == 2
    assert all_rows(scratch_db) == before and removed_dir(root, "entries") == [name] and leftovers(root) == []
    rebuilt_equals_current(root, scratch_db)


def test_cancel_keeps_rows_that_were_resent_unchanged(tmp_path, scratch_db):
    """같은 값으로 다시 보낸 기존 행(unchanged)이 든 배치는 빠른 길이 아니다. 원래 행이 남는다."""
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    before = all_rows(scratch_db)
    name = add(root, scratch_db, "entries", [{"id": "E1", "state": "open"}, {"id": "E202", "state": "open"}])["batches"][0]["file"]
    with pytest.raises(NeedsRebuild):
        remove(root, scratch_db, "entries", name, fast_only=True)
    r = remove(root, scratch_db, "entries", name)
    assert not r["fast"] and all_rows(scratch_db) == before
    rebuilt_equals_current(root, scratch_db)


def test_removing_an_older_batch_rebuilds_and_restore_brings_it_back(tmp_path, scratch_db):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    with_all = all_rows(scratch_db)
    r = remove(root, scratch_db, "entries", "000001-20261120T0910Z.csv")
    assert not r["fast"]
    by_id = {x[0]: x for x in rows(scratch_db, "entries")}
    assert "E4" not in by_id and by_id["E2"][2] == "done"         # 그 배치가 바꾼 값도 원래대로
    rebuilt_equals_current(root, scratch_db)
    restore_batch("ledger", "entries", "000001-20261120T0910Z.csv", projects_dir=root, dbname=scratch_db)
    assert all_rows(scratch_db) == with_all and removed_dir(root, "entries") == []


def test_cancel_that_breaks_references_or_checks_changes_nothing(tmp_path, scratch_db):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    name = add(root, scratch_db, "entries", [{"id": "E203"}])["batches"][0]["file"]
    add(root, scratch_db, "events", [{"id": 50, "entry_id": "E203"}, {"id": 51, "entry_id": "E4"}])
    before, files = all_rows(scratch_db), folder(root, "entries")
    # 가장 최근 배치이고 모두 새 키라 빠른 길이지만, 다른 테이블의 행이 참조한다
    e = expect_error("check_failed", remove, root, scratch_db, "entries", name, fast_only=True)
    assert e.errors
    assert all_rows(scratch_db) == before and folder(root, "entries") == files and leftovers(root) == []
    # 다시 만드는 길도 참조가 깨지면 거절한다(000001 배치가 넣은 E4를 events가 참조)
    expect_error("check_failed", remove, root, scratch_db, "entries", "000001-20261120T0910Z.csv")
    assert all_rows(scratch_db) == before and folder(root, "entries") == files


def test_unknown_batch_and_first_csv_cannot_be_removed(tmp_path, scratch_db):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    expect_error("unknown_batch", remove, root, scratch_db, "entries", "entries.csv")
    expect_error("unknown_batch", remove, root, scratch_db, "entries", "../schema.yaml")
    expect_error("unknown_batch", restore_batch, "ledger", "entries", "000001-x.csv.gz", projects_dir=root, dbname=scratch_db)


@pytest.mark.parametrize("point", ["after_record", "after_move", "before_commit", "after_commit"])
def test_crash_during_fast_cancel_is_recovered(tmp_path, scratch_db, fault, point):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    name = add(root, scratch_db, "entries", [{"id": "E204"}])["batches"][0]["file"]
    fault(point, operation.SimulatedCrash())
    with pytest.raises(operation.SimulatedCrash):
        remove(root, scratch_db, "entries", name, fast_only=True)
    operation.FAULTS.clear()
    assert len(pending.list_records(root / "ledger" / "data")) == 1
    add(root, scratch_db, "entries", [{"id": "E205"}])                 # 다음 작업이 복구한다
    ids = {x[0] for x in rows(scratch_db, "entries")}
    if point == "after_commit":
        assert "E204" not in ids and removed_dir(root, "entries") == [name]
    else:
        assert "E204" in ids and name in folder(root, "entries")
    assert leftovers(root) == []
    rebuilt_equals_current(root, scratch_db)


@pytest.mark.parametrize("point", ["after_record", "before_commit", "after_commit"])
def test_crash_during_rebuild_removal_is_recovered(tmp_path, scratch_db, fault, point):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    fault(point, operation.SimulatedCrash())
    with pytest.raises(operation.SimulatedCrash):
        remove(root, scratch_db, "entries", "000001-20261120T0910Z.csv")
    operation.FAULTS.clear()
    add(root, scratch_db, "entries", [{"id": "E206"}])
    ids = {x[0] for x in rows(scratch_db, "entries")}
    assert ("E4" in ids) == (point != "after_commit")
    assert leftovers(root) == []
    rebuilt_equals_current(root, scratch_db)
