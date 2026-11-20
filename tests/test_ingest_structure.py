"""구조 변경·새 테이블·설정 이력·합치기·계보(contracts/ingest.md "구조 변경", "합치기", "계보"). 임시 DB를 쓴다.

원칙: 설정 파일을 고치고 apply다. 여러 파일(두 YAML, replace 테이블의 CSV, 새 테이블의 첫 파일)을 함께 바꾸고, 실패하거나
도중에 죽으면 모든 파일이 변경 전으로 돌아간다. 합친 뒤 다시 만든 결과는 합치기 전과 같다(5-7).
"""
from __future__ import annotations

import gzip

import pytest

from dashkit.config import load_project
from dashkit.ingest import IngestError, ledger, lineage, newtable, pending, structure
from dashkit.ingest.compact import compact
from dashkit.ingest.faults import SimulatedCrash
from dashkit.loader import load
from dashkit.project import store

from test_ingest_db import (add, all_rows, fault, leftovers, make_project, rebuilt_equals_current, rows,  # noqa: F401
                            scratch_db)

pytestmark = pytest.mark.integration


@pytest.fixture()
def root(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    monkeypatch.setenv("PROJECTS_DIR", str(root))
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    return root


def folder(root, table):
    """배치 파일(`_`로 시작하는 폴더는 뺀다)."""
    path = root / "ledger" / "data" / table
    return sorted(p.name for p in path.iterdir() if not p.name.startswith("_")) if path.is_dir() else []


def files(root):
    p = root / "ledger"
    return {rel: (p / rel).read_bytes() for rel in ("schema.yaml", "dashboard.yaml", "data/groups.csv")}


def change(root, dbname, op_list, **kw):
    kw.setdefault("action", "test")
    return structure.change_config("ledger", op_list, store.revision(root / "ledger"), run_sync=False, dbname=dbname,
                                   **kw)


def columns_of(dbname, table):
    from dashkit import db

    with db.owner_conn(dbname) as conn, conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = 'raw' AND table_name = %s "
                    "ORDER BY ordinal_position", (table,))
        return [r[0] for r in cur.fetchall()]


def snapshot(dbname):
    from dashkit import db

    with db.inspect_conn(dbname) as conn, conn.cursor() as cur:
        return ledger.loaded(cur).snapshot


# ---------------------------------------------------------------- 열 추가·변경

def test_column_add_on_replace_table_changes_csv_too_and_history_restores_both(root, scratch_db):
    load(load_project("ledger", root), dbname=scratch_db)
    before = files(root)
    result = change(root, scratch_db, [{"op": "column.add", "table": "groups", "column": "note",
                                        "value": {"type": "text", "default": "-"}}])
    assert result["changed"] == ["data/groups.csv", "schema.yaml"]
    assert (root / "ledger/data/groups.csv").read_text(encoding="utf-8") == "id,label,note\ng1,하나,-\ng2,둘,-\n"
    assert columns_of(scratch_db, "groups")[-1] == "note"
    assert [r[-1] for r in rows(scratch_db, "groups")] == ["-", "-"]
    assert leftovers(root) == []
    rebuilt_equals_current(root, scratch_db)
    history = structure.list_history("ledger")
    assert history[0]["id"] == result["history"] and {f["path"] for f in history[0]["files"]} == {
        "schema.yaml", "data/groups.csv"}

    back = structure.history_files("ledger", result["history"])
    change(root, scratch_db, None, files=back, action="restore")
    assert files(root) == before
    assert "note" not in columns_of(scratch_db, "groups")


def test_history_restore_refuses_a_file_changed_afterwards(root, scratch_db):
    load(load_project("ledger", root), dbname=scratch_db)
    result = change(root, scratch_db, [{"op": "column.add", "table": "groups", "column": "note",
                                        "value": {"type": "text"}}])
    csv_path = root / "ledger/data/groups.csv"
    csv_path.write_text(csv_path.read_text(encoding="utf-8") + "g3,셋,\n", encoding="utf-8")
    err = pytest.raises(IngestError, structure.history_files, "ledger", result["history"]).value
    assert (err.status, err.detail) == (409, "stale_config")


def test_required_column_without_default_and_type_narrowing_are_rejected(root, scratch_db):
    load(load_project("ledger", root), dbname=scratch_db)
    before = files(root)
    err = pytest.raises(IngestError, change, root, scratch_db, [
        {"op": "column.add", "table": "entries", "column": "x", "value": {"type": "text", "required": True}}]).value
    assert (err.status, err.detail) == (400, "invalid_op")
    err = pytest.raises(IngestError, change, root, scratch_db, [
        {"op": "column.update", "table": "entries", "column": "amount", "set": {"type": "integer"}}]).value
    assert (err.status, err.detail) == (400, "invalid_op")
    assert files(root) == before and structure.list_history("ledger") == []


def test_column_add_on_append_table_fills_old_batches_with_default(root, scratch_db):
    load(load_project("ledger", root), dbname=scratch_db)
    change(root, scratch_db, [{"op": "column.add", "table": "entries", "column": "tier",
                               "value": {"type": "integer", "default": 5}},
                              {"op": "column.update", "table": "entries", "column": "qty", "set": {"type": "number"}}])
    got = {r[0]: r for r in rows(scratch_db, "entries")}
    assert {r[-1] for r in got.values()} == {5}
    add(root, scratch_db, "entries", [{"id": "E300", "tier": 9, "qty": 1.5}])       # 새 열이 든 배치
    add(root, scratch_db, "entries", [{"id": "E301"}])                             # 옛 모양의 배치
    got = {r[0]: r for r in rows(scratch_db, "entries")}
    assert got["E300"][-1] == 9 and got["E301"][-1] == 5
    rebuilt_equals_current(root, scratch_db)


def test_narrowed_rule_fails_load_and_puts_every_file_back(root, scratch_db):
    load(load_project("ledger", root), dbname=scratch_db)
    before, data = files(root), all_rows(scratch_db)
    err = pytest.raises(IngestError, change, root, scratch_db, [
        {"op": "column.update", "table": "entries", "column": "amount", "set": {"max": 6}}]).value
    assert (err.status, err.detail) == (422, "load_failed")
    assert any("000001-20261120T0910Z.csv" in e for e in err.errors)          # 옛 배치의 파일·줄로 알린다
    assert files(root) == before and all_rows(scratch_db) == data and leftovers(root) == []
    assert structure.list_history("ledger") == []           # 되돌려진 변경은 이력에 남지 않는다


@pytest.mark.parametrize("point,kept", [("after_move", False), ("before_commit", False), ("after_commit", True)])
def test_crash_during_structure_change_is_recovered_before_reading(root, scratch_db, fault, point, kept):
    load(load_project("ledger", root), dbname=scratch_db)
    before = files(root)
    fault(point, SimulatedCrash())
    with pytest.raises(SimulatedCrash):
        change(root, scratch_db, [{"op": "column.add", "table": "groups", "column": "note", "value": {"type": "text"}}])
    assert pending.list_records(root / "ledger" / "data")           # 묶음 기록이 남았다
    from dashkit.ingest import operation

    operation.FAULTS.clear()
    outcomes = structure.recover_before_read("ledger", root, dbname=scratch_db)
    assert [o.result for o in outcomes] == ["committed" if kept else "discarded"]
    assert (files(root) != before) is kept and leftovers(root) == []
    # 커밋 뒤 이력을 쓰기 전에 끊겼으면 복구가 기록에서 이력을 쓴다. 되돌린 변경은 이력이 없다
    assert [h["action"] for h in structure.list_history("ledger")] == (["test"] if kept else [])
    rebuilt_equals_current(root, scratch_db)                         # 파일과 DB가 같은 판이다


# ---------------------------------------------------------------- 새 테이블

CSV = "Item Code,Group,Count,Seen On\nA1,g1,3,2026-11-01\nA2,g2,4,2026-11-02\n".encode()


@pytest.mark.parametrize("mode", ["append", "replace"])
def test_new_table_from_csv_is_saved_validated_and_applied(root, scratch_db, mode):
    load(load_project("ledger", root), dbname=scratch_db)
    project = load_project("ledger", root)
    inferred = newtable.infer_one(project, "items.csv", CSV)
    assert inferred["name"] == "items" and [c["name"] for c in inferred["columns"]][:1] == ["item_code"]
    op_list, first = newtable.build(project, inferred, CSV, "items.csv", mode=mode, dataset=True, suggest=True)
    assert [o["op"] for o in op_list] == ["table.add", "dataset.add", "dashboard.add", "tab.add", "section.add"]
    structure.validate("ledger", op_list, first)
    result = change(root, scratch_db, op_list, extra=first, action="table-create")
    assert sorted(rows(scratch_db, "items")) == [("A1", "g1", 3, __import__("datetime").date(2026, 11, 1)),
                                                 ("A2", "g2", 4, __import__("datetime").date(2026, 11, 2))]
    spec = load_project("ledger", root).schema["tables"]["items"]
    assert spec["columns"]["item_code"]["aliases"] == ["Item Code"]       # 원래 머리글
    assert (spec.get("mode") or "replace") == mode
    if mode == "append":
        (name,) = folder(root, "items")
        assert name.startswith("000001-") and gzip.decompress((root / "ledger/data/items" / name).read_bytes()) \
            .decode().startswith("item_code,group_col,count_col,seen_on\n")
        # 같은 모양(원래 머리글)의 행을 다음에 올리면 aliases로 저절로 맞는다
        add(root, scratch_db, "items", [{"Item Code": "A3", "Group": "g1", "Count": 1, "Seen On": "2026-11-03"}])
    else:
        assert (root / "ledger/data/items.csv").read_text(encoding="utf-8").startswith("item_code,group_col,count_col,seen_on\n")
    rebuilt_equals_current(root, scratch_db)
    # 되돌리면 그 작업이 만든 첫 파일도 함께 지운다. 그 뒤에 더한 배치는 이력에 없어 그대로 남는다(데이터를 지우지 않는다)
    later = folder(root, "items")[1:]
    change(root, scratch_db, None, files=structure.history_files("ledger", result["history"]), action="restore")
    assert "items" not in load_project("ledger", root).schema["tables"]
    assert not (root / "ledger/data/items.csv").exists() and folder(root, "items") == later


def test_new_table_rows_that_fail_the_rules_are_reported(root, scratch_db):
    project = load_project("ledger", root)
    inferred = newtable.infer_one(project, "items.csv", CSV)
    inferred["columns"][2]["type"] = "boolean"
    err = pytest.raises(IngestError, newtable.build, project, inferred, CSV, "items.csv").value
    assert (err.status, err.detail) == (422, "invalid_rows")
    inferred["name"] = "groups"
    err = pytest.raises(IngestError, newtable.build, project, inferred, CSV, "items.csv").value
    assert (err.status, err.detail) == (409, "table_exists")


# ---------------------------------------------------------------- 계보

def test_lineage_follows_views_to_tables_and_charts(root, scratch_db):
    load(load_project("ledger", root), dbname=scratch_db)
    project = load_project("ledger", root)
    graph = lineage.build(project, snapshot(scratch_db))
    assert graph["datasets"]["summary"]["sources"] == {"entries": ["amount", "group_id"]}
    assert graph["datasets"]["states"]["sources"] == {"entries": ["group_id", "state"]}
    used = lineage.used_by(graph, "entries")
    assert used["datasets"] == ["entries", "summary", "states"] and [c["key"] for c in used["charts"]] == ["c_total"]
    assert used["tabs"] == ["main"]
    assert lineage.used_by(graph, "groups")["datasets"] == []
    assert lineage.without_column(graph, "entries", "memo") == ["summary", "states"]


# ---------------------------------------------------------------- 합치기

def test_compact_equals_rebuild_and_merged_folder_goes_after_next_load(root, scratch_db):
    load(load_project("ledger", root), dbname=scratch_db)
    for i in range(3):
        add(root, scratch_db, "entries", [{"id": f"E40{i}", "qty": i}])
    data = all_rows(scratch_db)
    result = compact("ledger", "entries", projects_dir=root, dbname=scratch_db)
    assert result["merged"] == 6 and result["rows"] == len(rows(scratch_db, "entries"))
    assert folder(root, "entries") == [result["file"]] and not (root / "ledger/data/entries.csv").exists()
    assert all_rows(scratch_db) == data and leftovers(root) == []
    with __import__("dashkit").db.owner_conn(scratch_db) as conn, conn.cursor() as cur:
        assert list(ledger.read(cur)["entries"]) == [result["file"]]
    rebuilt_equals_current(root, scratch_db)
    assert not (root / "ledger/data/entries/_merged").exists()     # load가 성공했으니 지웠다


def test_compact_refuses_unapplied_files(root, scratch_db):
    load(load_project("ledger", root), dbname=scratch_db)
    (root / "ledger/data/entries/000020-20261121T0300Z.csv").write_text("id\nE500\n", encoding="utf-8")
    err = pytest.raises(IngestError, compact, "ledger", "entries", projects_dir=root, dbname=scratch_db).value
    assert (err.status, err.detail) == (409, "rebuild_required")


@pytest.mark.parametrize("lose_new_file", [False, True])
def test_compact_crash_is_finished_or_rolled_back(root, scratch_db, fault, lose_new_file):
    load(load_project("ledger", root), dbname=scratch_db)
    add(root, scratch_db, "entries", [{"id": "E600"}])
    data = all_rows(scratch_db)
    before = folder(root, "entries")
    fault("after_move", SimulatedCrash())         # 옛 파일 하나를 옮긴 뒤 죽는다
    with pytest.raises(SimulatedCrash):
        compact("ledger", "entries", projects_dir=root, dbname=scratch_db)
    from dashkit.ingest import operation

    operation.FAULTS.clear()
    if lose_new_file:
        for p in (root / "ledger/data/_pending/incoming").iterdir():
            p.unlink()
    load(load_project("ledger", root), dbname=scratch_db)          # 로더가 배치를 읽기 전에 복구한다
    assert all_rows(scratch_db) == data and leftovers(root) == []
    if lose_new_file:
        assert folder(root, "entries") == before and (root / "ledger/data/entries.csv").exists()
    else:
        assert len(folder(root, "entries")) == 1
