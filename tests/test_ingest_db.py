"""데이터 추가 적재(contracts/ingest.md)의 DB 동작. 운영 DB를 건드리지 않도록 임시 DB를 만들어 쓴다.

핵심 원칙(설계서 5-7): 하나씩 더한 결과와 빈 DB에 처음부터 다시 만든 결과가 같다. 실패한 것은 폴더에도 DB에도 남지 않는다.
"""
from __future__ import annotations

import gzip
import shutil
import threading
import uuid
from pathlib import Path

import psycopg
import pytest
import yaml
from psycopg import sql

from dashkit import db
from dashkit.config import load_project
from dashkit.ingest import IngestError, Item, ingest, operation, pending
from dashkit.loader import LoadError, load
from dashkit.project import locks

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures"
APPEND_TABLES = ("entries", "events", "marks")


@pytest.fixture()
def scratch_db():
    name = f"dash_ingest_{uuid.uuid4().hex[:8]}"
    with db.owner_conn(autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield name
    with db.owner_conn(autocommit=True) as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                     (name,))
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def make_project(tmp_path: Path, *, batches: bool = True, edit=None) -> Path:
    """ledger 픽스처 사본(projects 폴더)을 만든다. batches=False면 배치 폴더의 파일을 지운다."""
    root = tmp_path / "projects"
    shutil.copytree(FIXTURES / "ledger", root / "ledger")
    if not batches:
        for t in APPEND_TABLES:
            shutil.rmtree(root / "ledger" / "data" / t, ignore_errors=True)
    if edit is not None:
        path = root / "ledger" / "schema.yaml"
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        edit(doc)
        path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return root


def rows(dbname: str, table: str) -> list[tuple]:
    with db.owner_conn(dbname) as conn:
        return conn.execute(sql.SQL("SELECT * FROM raw.{} ORDER BY 1, 2").format(sql.Identifier(table))).fetchall()


def all_rows(dbname: str) -> dict[str, list[tuple]]:
    return {t: rows(dbname, t) for t in ("groups", *APPEND_TABLES)}


def ledger_rows(dbname: str) -> list[tuple]:
    with db.owner_conn(dbname) as conn:
        return conn.execute("SELECT table_name, file, sha256, rows FROM dashkit_meta.batch ORDER BY 1, 2").fetchall()


def folder(root: Path, table: str) -> list[str]:
    path = root / "ledger" / "data" / table
    return sorted(p.name for p in path.iterdir()) if path.is_dir() else []


def leftovers(root: Path) -> list[str]:
    path = root / "ledger" / "data" / "_pending"
    return sorted(str(p.relative_to(path)) for p in path.rglob("*") if p.is_file()) if path.is_dir() else []


def add(root: Path, dbname: str, table: str, data, **kw):
    if isinstance(data, Path):
        source = {"kind": "csv", "path": data, "name": data.name}
    else:
        source = {"kind": "rows", "rows": data}
    return ingest("ledger", [Item(table, source, kw.pop("columns", None))], projects_dir=root, dbname=dbname, **kw)


def expect_error(detail: str, fn, *args, **kw) -> IngestError:
    with pytest.raises(IngestError) as exc:
        fn(*args, **kw)
    assert exc.value.detail == detail, (exc.value.detail, exc.value.message, exc.value.errors)
    return exc.value


def rebuilt_equals_current(root: Path, dbname: str) -> None:
    """지금 DB의 행 = 폴더의 파일로 처음부터 다시 만든 행(5-7)."""
    before = all_rows(dbname)
    load(load_project("ledger", root), dbname=dbname)
    assert all_rows(dbname) == before


# ---------------------------------------------------------------- 로더

def test_load_reads_batches_in_number_order_with_defaults_and_records(tmp_path, scratch_db):
    root = make_project(tmp_path)
    counts = load(load_project("ledger", root), dbname=scratch_db)
    assert counts == {"groups": 2, "entries": 5, "events": 2, "marks": 0}
    by_id = {r[0]: r for r in rows(scratch_db, "entries")}
    assert by_id["E2"][2:4] == ("open", 25)                  # 000001 배치가 바꾼 열(update 정책)
    assert by_id["E2"][7] is None and by_id["E2"][6] == 2    # 머리글에 없던 열은 그대로
    assert by_id["E4"][6] == 0 and by_id["E5"][2] == "open"  # 새 행의 빠진 열은 default
    files = {(t, f): n for t, f, _h, n in ledger_rows(scratch_db)}
    assert files == {("groups", "groups.csv"): 2, ("entries", "entries.csv"): 3,
                     ("entries", "000001-20261120T0910Z.csv"): 2, ("entries", "000010-20261121T0102Z.csv"): 1,
                     ("events", "000001-20261120T0911Z.csv"): 2}
    with db.owner_conn(scratch_db) as conn:
        snap = conn.execute("SELECT snapshot FROM dashkit_meta.load_snapshot").fetchone()[0]
        n = conn.execute("SELECT count(*) FROM dashkit_meta.row_fingerprint WHERE table_name = 'entries'").fetchone()[0]
    assert snap["tables"]["entries"]["mode"] == "append" and snap["tables"]["groups"]["mode"] == "replace"
    assert len(snap["data_revision"]) == 64
    assert n == 0, "누적 테이블은 행 지문을 남기지 않는다"


def test_load_rejects_bad_batch_names_and_keeps_db(tmp_path, scratch_db):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    before = all_rows(scratch_db)
    (root / "ledger" / "data" / "entries" / "z.csv").write_text("id\nE9\n", encoding="utf-8")
    (root / "ledger" / "data" / "entries" / "10-again.csv").write_text("id\nE8\n", encoding="utf-8")
    with pytest.raises(LoadError) as exc:
        load(load_project("ledger", root), dbname=scratch_db)
    assert any("z.csv" in e and "이름 규칙" in e for e in exc.value.errors), exc.value.errors
    assert any("10-again.csv" in e and "겹침" in e for e in exc.value.errors), exc.value.errors
    assert all_rows(scratch_db) == before


def test_load_reports_batch_errors_with_file_and_line(tmp_path, scratch_db):
    root = make_project(tmp_path)
    (root / "ledger" / "data" / "entries" / "000011-x.csv").write_text("id,amount,zz\nE9,1,\n", encoding="utf-8")
    (root / "ledger" / "data" / "events" / "000002-x.csv").write_text("id,entry_id\n9,E1\n9,E2\n", encoding="utf-8")
    with pytest.raises(LoadError) as exc:
        load(load_project("ledger", root), dbname=scratch_db)
    assert "data/entries/000011-x.csv:1:zz: schema.yaml에 없는 열" in exc.value.errors
    assert any(e.startswith("data/events/000002-x.csv:3:id: 기본 키 중복") for e in exc.value.errors), exc.value.errors


# ---------------------------------------------------------------- 하나씩 더한 결과 = 다시 만든 결과

@pytest.mark.parametrize("policy", ["update", "skip", "error"])
def test_incremental_equals_rebuild_for_each_policy(tmp_path, scratch_db, policy):
    root = make_project(tmp_path, batches=False, edit=lambda d: d["tables"]["entries"].__setitem__("on_conflict", policy))
    load(load_project("ledger", root), dbname=scratch_db)
    add(root, scratch_db, "entries", [{"id": "E4", "state": "done", "amount": "4"}, {"id": "E5", "state": "open", "amount": None}])
    overlap = [{"id": "E1", "state": "done", "memo": ""}, {"id": "E6", "state": "open", "memo": "새"}]
    if policy == "error":
        e = expect_error("key_conflict", add, root, scratch_db, "entries", overlap)
        assert e.extra["keys"] == ["E1"]
        add(root, scratch_db, "entries", overlap[1:])
    else:
        r = add(root, scratch_db, "entries", overlap)
        b = r["batches"][0]["rows"]
        assert (b["inserted"], b["updated"] + b["skipped"]) == (1, 1)
    csv = tmp_path / "upload.csv"
    csv.write_bytes("ID;State Name;금액;on_day\r\nE7;done;1,234;2026/11/07\r\n".encode("cp949"))
    r = add(root, scratch_db, "entries", csv)
    assert r["batches"][0]["normalized"]["encoding"] == "cp949" and r["batches"][0]["normalized"]["dates"] == 1
    add(root, scratch_db, "events", [{"id": 1, "entry_id": "E4", "seen_at": "2026-11-20 10:00:00+09:00"}])
    add(root, scratch_db, "marks", [{"entry_id": "E4", "kind": "a", "points": 1}])
    add(root, scratch_db, "marks", [{"entry_id": "E4", "kind": "a", "points": 5}, {"entry_id": "E5", "kind": "a", "points": 2}])
    assert len(folder(root, "entries")) == 3
    assert leftovers(root) == []
    rebuilt_equals_current(root, scratch_db)
    marks = rows(scratch_db, "marks")
    assert marks == [("E4", "a", 1), ("E5", "a", 2)], "skip 정책: 먼저 들어온 행을 둔다"


def test_partial_update_keeps_unsent_columns_and_rebuilds_same(tmp_path, scratch_db):
    root = make_project(tmp_path, batches=False)
    load(load_project("ledger", root), dbname=scratch_db)
    long_text = "가" * 3000
    add(root, scratch_db, "entries", [{"id": "E1", "memo": long_text}])
    before = {r[0]: r for r in rows(scratch_db, "entries")}["E1"]
    r = add(root, scratch_db, "entries", [{"id": "E1", "amount": "11"}])
    assert r["batches"][0]["rows"]["updated"] == 1
    assert r["batches"][0]["changes"] == [{"key": "E1", "before": {"amount": "10"}, "after": {"amount": "11"}}]
    after = {r[0]: r for r in rows(scratch_db, "entries")}["E1"]
    assert after[3] == 11 and after[:3] == before[:3] and after[4:] == before[4:] and after[7] == long_text
    r = add(root, scratch_db, "entries", [{"id": "E1", "amount": "11"}], force=True)
    assert r["batches"][0]["rows"]["unchanged"] == 1
    r = add(root, scratch_db, "entries", [{"id": "E1", "memo": None}])          # 일부러 비움 = NULL
    assert {r[0]: r for r in rows(scratch_db, "entries")}["E1"][7] is None
    rebuilt_equals_current(root, scratch_db)


def test_same_rows_by_form_paste_and_file_make_identical_batch_files(tmp_path):
    outs = []
    for i, how in enumerate(("form", "paste", "file")):
        dbname = f"dash_ingest_{uuid.uuid4().hex[:8]}"
        with db.owner_conn(autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
        try:
            root = make_project(tmp_path / str(i), batches=False)
            load(load_project("ledger", root), dbname=dbname)
            if how == "file":
                csv = tmp_path / f"in{i}.csv"
                csv.write_bytes("id\t금액\tState Name\t비고\nE8\t1,000\tdone\t\"a, b\"\nE9\t2\topen\t\n".encode("cp949"))
                r = add(root, dbname, "entries", csv)
            else:
                data = [{"id": "E8", "state": "done", "amount": 1000, "memo": "a, b"},
                        {"id": "E9", "state": "open", "amount": "2", "memo": None}]
                if how == "paste":
                    data = [{"memo": " a, b ", "amount": "1,000", "state": "done", "id": "E8"},
                            {"memo": "", "amount": "2", "state": "open", "id": "E9"}]
                r = add(root, dbname, "entries", data)
            name = r["batches"][0]["file"]
            outs.append((root / "ledger" / "data" / "entries" / name).read_bytes())
        finally:
            with db.owner_conn(autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))
    assert outs[0] == outs[1] == outs[2]
    assert gzip.decompress(outs[0]) == b'id,state,amount,memo\nE8,done,1000,"a, b"\nE9,open,2,\n'


# ---------------------------------------------------------------- 실패는 아무것도 남기지 않는다

def test_failed_batches_leave_table_and_folder_as_they_were(tmp_path, scratch_db):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    before, files, led = all_rows(scratch_db), folder(root, "entries"), ledger_rows(scratch_db)
    e = expect_error("invalid_rows", add, root, scratch_db, "entries", [{"id": "E9", "on_day": "11/20/2026"}])
    assert e.errors == ["rows[0].on_day: 날짜 형식(YYYY-MM-DD)이 아님 '11/20/2026'"]
    e = expect_error("check_failed", add, root, scratch_db, "entries", [{"id": "E9", "qty": "-1"}])
    assert e.errors == ["수량이 음수: 위반 키 E9"]
    e = expect_error("invalid_rows", add, root, scratch_db, "events", [{"id": 7, "entry_id": "E99"}])
    assert e.errors == ["rows[0].entry_id: 참조 대상 entries.id에 값 'E99'가 없음"]
    e = expect_error("invalid_rows", add, root, scratch_db, "events", [{"id": 7}])
    assert "새 행" in e.errors[0]
    expect_error("key_conflict", add, root, scratch_db, "events", [{"id": 1, "entry_id": "E1"}])
    expect_error("not_append", add, root, scratch_db, "groups", [{"id": "g3", "label": "x"}])
    expect_error("unknown_columns", add, root, scratch_db, "entries", [{"id": "E9", "nope": 1}])
    assert all_rows(scratch_db) == before and folder(root, "entries") == files and ledger_rows(scratch_db) == led
    assert leftovers(root) == []


def test_preview_rolls_back(tmp_path, scratch_db):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    before, files = all_rows(scratch_db), folder(root, "entries")
    r = add(root, scratch_db, "entries", [{"id": "E1", "state": "done"}, {"id": "E9", "state": "open"}], dry_run=True)
    assert r["dry_run"] and r["batches"][0]["rows"]["inserted"] == 1 and r["batches"][0]["file"] is None
    r = add(root, scratch_db, "entries", [{"id": "E1", "on_day": "x"}], dry_run=True)
    assert r["ok"] is False and r["batches"][0]["errors"]
    assert all_rows(scratch_db) == before and folder(root, "entries") == files and leftovers(root) == []


def test_same_content_twice_is_rejected_even_when_sent_together(tmp_path, scratch_db):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    data = [{"id": "E20", "state": "open"}]
    first = add(root, scratch_db, "entries", data)["batches"][0]["file"]
    e = expect_error("duplicate_file", add, root, scratch_db, "entries", [{"state": "open", "id": "E20"}])
    assert e.extra["duplicates"] == [{"table": "entries", "file": first}]
    results: list = []

    def send():
        try:
            results.append(add(root, scratch_db, "entries", [{"id": "E21", "state": "open"}], wait_ms=20000)["ok"])
        except IngestError as exc:
            results.append(exc.detail)

    threads = [threading.Thread(target=send) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(map(str, results)) == ["True", "duplicate_file"], results


# ---------------------------------------------------------------- 공통 순서의 확인

def test_lock_busy_and_load_waits(tmp_path, scratch_db):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    holder = db.owner_conn(scratch_db)
    try:
        locks.acquire(holder, 0)
        expect_error("apply_running", add, root, scratch_db, "entries", [{"id": "E30"}], wait_ms=100)
        with pytest.raises(LoadError) as exc:
            load(load_project("ledger", root), dbname=scratch_db, lock_wait_ms=100)
        assert "쓰기 잠금" in exc.value.errors[0]
    finally:
        holder.close()
    assert add(root, scratch_db, "entries", [{"id": "E30"}])["ok"]


def test_unapplied_config_change_is_rejected_but_tab_change_is_not(tmp_path, scratch_db):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    dash = root / "ledger" / "dashboard.yaml"
    dash.write_text(dash.read_text(encoding="utf-8").replace("label: 메인", "label: 다른 탭"), encoding="utf-8")
    assert add(root, scratch_db, "entries", [{"id": "E31"}])["ok"]
    schema = root / "ledger" / "schema.yaml"
    schema.write_text(schema.read_text(encoding="utf-8").replace("on_conflict: update", "on_conflict: skip"), encoding="utf-8")
    expect_error("rebuild_required", add, root, scratch_db, "entries", [{"id": "E32"}])


def test_other_project_loaded_is_rejected(tmp_path, scratch_db):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    shutil.copytree(root / "ledger", root / "ledger2")
    load(load_project("ledger2", root), dbname=scratch_db)
    expect_error("project_mismatch", add, root, scratch_db, "entries", [{"id": "E33"}])


def test_folder_must_match_the_ledger(tmp_path, scratch_db):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    data = root / "ledger" / "data"
    # 뒤에 놓인 손 파일은 먼저 반영한다
    (data / "entries" / "000050-hand.csv").write_text("id,memo\nE40,손\n", encoding="utf-8")
    r = add(root, scratch_db, "entries", [{"id": "E41"}])
    assert r["applied_first"] == [{"table": "entries", "file": "000050-hand.csv", "rows": 1}]
    assert r["batches"][0]["file"].startswith("000051-")
    rebuilt_equals_current(root, scratch_db)
    # 반영한 배치보다 앞에 놓인 새 파일, 바뀐 파일, 없어진 파일, 다른 테이블(replace CSV 포함)의 차이는 거절한다
    cases = [
        lambda: (data / "entries" / "000005-early.csv").write_text("id\nE42\n", encoding="utf-8"),
        lambda: (data / "entries" / "000050-hand.csv").write_text("id,memo\nE40,바뀜\n", encoding="utf-8"),
        lambda: (data / "entries" / "000050-hand.csv").unlink(),
        lambda: (data / "groups.csv").write_text("id,label\ng1,하나\ng2,둘\ng3,셋\n", encoding="utf-8"),
        lambda: (data / "events" / "000009-x.csv").write_text("id,entry_id\n9,E1\n", encoding="utf-8"),
    ]
    for i, change in enumerate(cases):
        snapshot = {p: p.read_bytes() for p in data.rglob("*.csv")}
        change()
        e = expect_error("rebuild_required", add, root, scratch_db, "entries", [{"id": f"E5{i}"}])
        assert e.errors, i
        for p in list(data.rglob("*.csv")):
            if p not in snapshot:
                p.unlink()
        for p, content in snapshot.items():
            p.write_bytes(content)


def test_rules_narrowed_while_waiting_for_the_lock_are_used(tmp_path, scratch_db):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    holder = db.owner_conn(scratch_db)
    locks.acquire(holder, 0)
    out: list = []
    t = threading.Thread(target=lambda: out.append(_try(add, root, scratch_db, "entries",
                                                         [{"id": "E60", "amount": "5"}], wait_ms=20000)))
    t.start()
    try:
        import time
        deadline = time.monotonic() + 10
        while not locks.held_by_other(db.owner_conn(scratch_db, autocommit=True)) and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)     # 접수가 끝나고 잠금을 기다리는 중
        schema = root / "ledger" / "schema.yaml"
        schema.write_text(schema.read_text(encoding="utf-8").replace("amount:   { type: number, min: 0 }",
                                                                      "amount:   { type: number, min: 10 }"), encoding="utf-8")
    finally:
        holder.close()
    t.join()
    assert isinstance(out[0], IngestError) and out[0].detail == "invalid_rows", out
    assert "최솟값 10보다 작음" in out[0].errors[0]


def _try(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except IngestError as exc:
        return exc


def test_cross_table_check_is_not_missed_by_concurrent_adds(tmp_path, scratch_db):
    def edit(doc):
        doc["tables"]["marks"]["checks"] = [{"name": "합계 초과", "sql": "SELECT 'x' WHERE (SELECT count(*) FROM raw.entries) + (SELECT count(*) FROM raw.marks) > 6"}]
    root = make_project(tmp_path, edit=edit)
    load(load_project("ledger", root), dbname=scratch_db)       # entries 5 + marks 0
    out: list = []
    ts = [threading.Thread(target=lambda: out.append(_try(add, root, scratch_db, "entries", [{"id": "E70"}], wait_ms=20000))),
          threading.Thread(target=lambda: out.append(_try(add, root, scratch_db, "marks",
                                                          [{"entry_id": "E1", "kind": "k"}], wait_ms=20000)))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    failed = [o for o in out if isinstance(o, IngestError)]
    assert len(failed) == 1 and failed[0].detail == "check_failed", out
    rebuilt_equals_current(root, scratch_db)


# ---------------------------------------------------------------- 끊김 복구

@pytest.fixture()
def fault():
    def set_fault(point: str, exc: BaseException) -> None:
        def raise_it():
            raise exc
        operation.FAULTS[point] = raise_it
    yield set_fault
    operation.FAULTS.clear()


def test_crash_after_commit_keeps_files_and_next_run_clears_record(tmp_path, scratch_db, fault):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    fault("after_commit", operation.SimulatedCrash())
    with pytest.raises(operation.SimulatedCrash):
        add(root, scratch_db, "entries", [{"id": "E80"}])
    operation.FAULTS.clear()
    assert len(pending.list_records(root / "ledger" / "data")) == 1
    r = add(root, scratch_db, "entries", [{"id": "E81"}])
    assert r["recovered"][0]["result"] == "committed"
    assert {"E80", "E81"} <= {x[0] for x in rows(scratch_db, "entries")} and leftovers(root) == []
    rebuilt_equals_current(root, scratch_db)


def test_crash_before_commit_with_all_files_moved_is_applied_on_restart(tmp_path, scratch_db, fault):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    fault("before_commit", operation.SimulatedCrash())
    with pytest.raises(operation.SimulatedCrash):
        add(root, scratch_db, "entries", [{"id": "E82"}])
    operation.FAULTS.clear()
    assert "E82" not in {x[0] for x in rows(scratch_db, "entries")}
    r = add(root, scratch_db, "entries", [{"id": "E83"}])
    assert r["recovered"][0]["result"] == "applied"
    assert {"E82", "E83"} <= {x[0] for x in rows(scratch_db, "entries")}
    rebuilt_equals_current(root, scratch_db)


def test_crash_with_part_of_a_group_moved_discards_the_group(tmp_path, scratch_db, fault):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    before, files = all_rows(scratch_db), (folder(root, "entries"), folder(root, "events"))
    fault("after_move", operation.SimulatedCrash())          # 첫 파일(부모)만 옮기고 죽는다
    with pytest.raises(operation.SimulatedCrash):
        ingest("ledger", [Item("events", {"kind": "rows", "rows": [{"id": 90, "entry_id": "E90"}]}),
                          Item("entries", {"kind": "rows", "rows": [{"id": "E90"}]})],
               projects_dir=root, dbname=scratch_db)
    operation.FAULTS.clear()
    assert len(folder(root, "entries")) == len(files[0]) + 1
    load(load_project("ledger", root), dbname=scratch_db)     # 로더도 같은 판정으로 정리한다
    assert all_rows(scratch_db) == before and (folder(root, "entries"), folder(root, "events")) == files
    assert leftovers(root) == []


def test_unknown_commit_result_is_answered_by_recovery(tmp_path, scratch_db, fault):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    fault("after_commit", RuntimeError("응답을 받지 못함"))
    r = add(root, scratch_db, "entries", [{"id": "E84"}])        # 커밋은 됐다: 파일을 지우지 않고 성공으로 답한다
    assert r["ok"] and (root / "ledger" / "data" / "entries" / r["batches"][0]["file"]).is_file()
    # 파일을 모두 옮긴 뒤 커밋이 실패하면 복구가 묶음 전체를 반영하고(②) 성공으로 답한다
    fault("before_commit", RuntimeError("커밋 실패"))
    r = add(root, scratch_db, "entries", [{"id": "E85"}])
    assert r["ok"] and "E85" in {x[0] for x in rows(scratch_db, "entries")}
    # 아무 파일도 옮기기 전에 실패하면 복구가 치우고(③) 실패로 답한다
    files = folder(root, "entries")
    fault("after_record", RuntimeError("디스크 오류"))
    e = expect_error("commit_failed", add, root, scratch_db, "entries", [{"id": "E86"}])
    assert e.status == 500 and folder(root, "entries") == files and leftovers(root) == []
    operation.FAULTS.clear()
    assert "E86" not in {x[0] for x in rows(scratch_db, "entries")}
    rebuilt_equals_current(root, scratch_db)


# ---------------------------------------------------------------- DB 계정 경계

def test_reader_cannot_read_raw_and_inspect_reads_only(tmp_path, scratch_db):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    with db.reader_conn(scratch_db) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM raw.entries").fetchall()
    with db.inspect_conn(scratch_db) as conn:
        assert conn.execute("SELECT count(*) FROM raw.entries").fetchone()[0] == 5
        assert conn.execute("SELECT count(*) FROM dash.entries").fetchone()[0] == 5
        assert conn.execute("SELECT count(*) FROM dashkit_meta.batch").fetchone()[0] == 5
        conn.rollback()
        for stmt in ("INSERT INTO raw.entries (id, state) VALUES ('E99', 'open')",
                     "UPDATE raw.entries SET qty = 1", "DELETE FROM dashkit_meta.batch",
                     "CREATE TABLE raw.zz (a int)"):
            with pytest.raises((psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction)):
                conn.execute(stmt)
            conn.rollback()
        conn.execute("SET TRANSACTION READ WRITE")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO raw.entries (id, state) VALUES ('E99', 'open')")
        conn.rollback()


def test_inspect_cannot_read_auth_tables():
    with db.inspect_conn() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM dashkit_auth.users").fetchall()
