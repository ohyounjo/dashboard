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
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid() "
                     "AND backend_type = 'client backend'",
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


# ---------------------------------------------------------------- 리뷰 지적 회귀(PR 24)

def test_recovery_commits_even_when_the_connection_has_an_open_transaction(tmp_path, scratch_db, fault):
    """복구의 반영은 저장점이 아니라 진짜 커밋이어야 한다(그 뒤 접속을 되돌려도 행과 배치 기록이 남는다)."""
    from dashkit.ingest import recover

    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    fault("before_commit", operation.SimulatedCrash())
    with pytest.raises(operation.SimulatedCrash):
        add(root, scratch_db, "entries", [{"id": "E87"}])
    operation.FAULTS.clear()
    conn = db.owner_conn(scratch_db)
    try:
        locks.acquire(conn, 0)
        conn.execute("SELECT 1")                 # 열린 트랜잭션
        outcomes = recover.recover(load_project("ledger", root), conn, mode="online")
        assert [o.result for o in outcomes] == ["applied"]
        conn.rollback()
    finally:
        conn.close()
    assert "E87" in {x[0] for x in rows(scratch_db, "entries")}
    assert any(f.startswith("000011-") for _t, f, _h, _n in ledger_rows(scratch_db))
    rebuilt_equals_current(root, scratch_db)


def test_skip_invalid_inserts_good_rows_but_not_duplicates(tmp_path, scratch_db):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    r = add(root, scratch_db, "entries", [{"id": "E88", "amount": "1"}, {"id": "E89", "amount": "x"}],
            skip_invalid=True)
    b = r["batches"][0]
    assert b["rows"]["inserted"] == 1 and b["rows"]["invalid"] == 1 and b["errors"] == ["rows[1].amount: 숫자가 아님 'x'"]
    assert "E88" in {x[0] for x in rows(scratch_db, "entries")} and "E89" not in {x[0] for x in rows(scratch_db, "entries")}
    expect_error("invalid_rows", add, root, scratch_db, "entries", [{"id": "E90"}, {"id": "E90"}], skip_invalid=True)
    rebuilt_equals_current(root, scratch_db)


def test_hand_placed_files_are_applied_parents_first(tmp_path, scratch_db):
    """자식 테이블이 부모보다 먼저 선언돼 있어도 손으로 넣은 파일은 참조 순서로 반영한다(load와 같다)."""
    def child_first(doc):
        t = doc["tables"]
        doc["tables"] = {"events": t["events"], **{k: v for k, v in t.items() if k != "events"}}
    root = make_project(tmp_path, edit=child_first)
    load(load_project("ledger", root), dbname=scratch_db)
    data = root / "ledger" / "data"
    (data / "entries" / "000050-hand.csv").write_text("id\nE91\n", encoding="utf-8")
    (data / "events" / "000050-hand.csv").write_text("id,entry_id\n91,E91\n", encoding="utf-8")
    r = ingest("ledger", [Item("events", {"kind": "rows", "rows": [{"id": 92, "entry_id": "E1"}]}),
                          Item("entries", {"kind": "rows", "rows": [{"id": "E92"}]})],
               projects_dir=root, dbname=scratch_db)
    assert [f["table"] for f in r["applied_first"]] == ["entries", "events"]
    rebuilt_equals_current(root, scratch_db)


def test_hand_placed_file_applied_first_is_recovered_with_the_group(tmp_path, scratch_db, fault):
    """먼저 반영한 손 파일도 묶음 기록에 들어가, 커밋 전에 끊기면 복구가 함께 다시 반영한다(지우지 않는다)."""
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    hand = root / "ledger" / "data" / "entries" / "000050-hand.csv"
    hand.write_text("id\nE93\n", encoding="utf-8")
    fault("before_commit", operation.SimulatedCrash())
    with pytest.raises(operation.SimulatedCrash):
        add(root, scratch_db, "entries", [{"id": "E94"}])
    operation.FAULTS.clear()
    r = add(root, scratch_db, "entries", [{"id": "E95"}])
    assert r["recovered"][0]["result"] == "applied"
    assert {"E93", "E94", "E95"} <= {x[0] for x in rows(scratch_db, "entries")} and hand.is_file()
    rebuilt_equals_current(root, scratch_db)
    # 일부만 옮긴 채 끊기면 새 파일만 치우고 손 파일은 남긴다
    hand2 = root / "ledger" / "data" / "entries" / "000080-hand.csv"
    hand2.write_text("id\nE96\n", encoding="utf-8")
    fault("after_record", operation.SimulatedCrash())
    with pytest.raises(operation.SimulatedCrash):
        add(root, scratch_db, "entries", [{"id": "E97"}])
    operation.FAULTS.clear()
    r = add(root, scratch_db, "entries", [{"id": "E98"}])
    assert r["recovered"][0]["result"] == "discarded" and hand2.is_file()
    assert r["applied_first"] == [{"table": "entries", "file": "000080-hand.csv", "rows": 1}]
    assert "E97" not in {x[0] for x in rows(scratch_db, "entries")}
    rebuilt_equals_current(root, scratch_db)


def test_long_text_batch_can_be_read_back(tmp_path, scratch_db):
    """csv 모듈 기본 한도(131072자)보다 긴 값도 저장한 뒤 다시 만들 수 있다."""
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    add(root, scratch_db, "entries", [{"id": "E99", "memo": "가" * 200_000}])
    rebuilt_equals_current(root, scratch_db)


def test_plan_counts_rows_added_since_the_last_load(tmp_path, scratch_db, monkeypatch):
    from dashkit import plan

    root = make_project(tmp_path, batches=False)
    load(load_project("ledger", root), dbname=scratch_db)      # entries 3행
    r = add(root, scratch_db, "entries", [{"id": f"E{100 + i}"} for i in range(4)])
    monkeypatch.setenv("BUILD_DIR", str(tmp_path / "build"))
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    entry = next(t for t in plan.make_plan(load_project("ledger", root), dbname=scratch_db)["data"] if t["table"] == "entries")
    assert entry["status"] == "unchanged" and entry["rows"] == 7
    (root / "ledger" / "data" / "entries" / r["batches"][0]["file"]).unlink()
    entry = next(t for t in plan.make_plan(load_project("ledger", root), dbname=scratch_db)["data"] if t["table"] == "entries")
    assert entry["batches"]["removed"] == [r["batches"][0]["file"]] and entry["rows"] == 3 and entry["removed"] == 4


def _crash_before_commit(root, scratch_db, fault, row_id):
    fault("before_commit", operation.SimulatedCrash())
    with pytest.raises(operation.SimulatedCrash):
        add(root, scratch_db, "entries", [{"id": row_id}])
    operation.FAULTS.clear()
    assert len(pending.list_records(root / "ledger" / "data")) == 1


def test_start_recovery_waits_when_the_config_is_not_applied(tmp_path, scratch_db, fault):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    _crash_before_commit(root, scratch_db, fault, "E120")
    schema = root / "ledger" / "schema.yaml"
    schema.write_text(schema.read_text(encoding="utf-8").replace(
        "      memo:     { type: text, aliases: [note, \"비고\"] }",
        "      memo:     { type: text, aliases: [note, \"비고\"] }\n      extra:    { type: text }"), encoding="utf-8")
    assert operation.recover_on_start(load_project("ledger", root), dbname=scratch_db) == []
    assert len(pending.list_records(root / "ledger" / "data")) == 1          # 파일과 기록을 그대로 둔다
    load(load_project("ledger", root), dbname=scratch_db)                     # load가 파일로 처리한다
    assert "E120" in {x[0] for x in rows(scratch_db, "entries")} and leftovers(root) == []


def test_recovery_keeps_files_when_its_own_commit_result_is_unknown(tmp_path, scratch_db, fault):
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    _crash_before_commit(root, scratch_db, fault, "E121")
    files = folder(root, "entries")
    fault("recover_before_commit", psycopg.OperationalError("접속 끊김"))
    e = expect_error("recovery_unknown", add, root, scratch_db, "entries", [{"id": "E122"}])
    assert e.status == 503 and folder(root, "entries") == files
    assert len(pending.list_records(root / "ledger" / "data")) == 1
    operation.FAULTS.clear()
    r = add(root, scratch_db, "entries", [{"id": "E122"}])
    assert r["recovered"][0]["result"] == "applied"
    assert {"E121", "E122"} <= {x[0] for x in rows(scratch_db, "entries")}
    rebuilt_equals_current(root, scratch_db)


def test_plan_reports_a_changed_conflict_policy(tmp_path, scratch_db, monkeypatch):
    from dashkit import plan

    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    monkeypatch.setenv("BUILD_DIR", str(tmp_path / "build"))
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))

    def entries():
        return next(t for t in plan.make_plan(load_project("ledger", root), dbname=scratch_db)["data"]
                    if t["table"] == "entries")
    assert entries()["status"] == "unchanged"
    schema = root / "ledger" / "schema.yaml"
    schema.write_text(schema.read_text(encoding="utf-8").replace("on_conflict: update", "on_conflict: skip"), encoding="utf-8")
    e = entries()
    assert e["status"] == "changed" and e["rules_changed"] and e["batches"] == {"added": [], "removed": [], "changed": []}


def test_crash_then_restart_applies_the_batch(tmp_path, scratch_db, fault):
    """확정 도중(파일을 다 옮기고 커밋 전)에 죽인 뒤 관리 서비스가 다시 뜨면 그 배치가 반영된다(2a 완료 조건)."""
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    _crash_before_commit(root, scratch_db, fault, "E130")
    outcomes = operation.recover_on_start(load_project("ledger", root), dbname=scratch_db)
    assert [o.result for o in outcomes] == ["applied"]
    assert "E130" in {x[0] for x in rows(scratch_db, "entries")} and leftovers(root) == []
    rebuilt_equals_current(root, scratch_db)


def test_skip_invalid_drops_reference_errors_and_keeps_only_inserted_rows_in_the_file(tmp_path, scratch_db):
    """오류 행 빼고 넣기: 값 오류와 참조 오류가 난 행을 빼고, 배치 파일에는 들어간 행만 쓰고, 빠진 행 파일을 남긴다.
    다시 만들어도 같다(설계서 5-4, 14절)."""
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    rejects = tmp_path / "rejects"
    rejects.mkdir()
    csv = tmp_path / "ev.csv"
    csv.write_text("id,entry_id,seen_at\n10,E1,\n11,E404,\n12,E2,nope\n13,E3,\n", encoding="utf-8")
    r = ingest("ledger", [Item("events", {"kind": "csv", "path": csv, "name": "ev.csv"})], projects_dir=root,
               dbname=scratch_db, skip_invalid=True, rejects_dir=rejects)
    b = r["batches"][0]
    assert b["rows"]["inserted"] == 2 and b["rejected"] == 2, b
    assert any("ev.csv:3:entry_id" in e for e in b["errors"]) and any("ev.csv:4:seen_at" in e for e in b["errors"])
    stored = gzip.decompress((root / "ledger" / "data" / "events" / b["file"]).read_bytes()).decode()
    assert stored == "id,entry_id,seen_at\n10,E1,\n13,E3,\n"
    lines = (rejects / "000.csv").read_text(encoding="utf-8-sig").splitlines()
    assert lines[0] == "id,entry_id,seen_at,오류" and len(lines) == 3
    assert any(l.startswith("11,E404,") and "entry_id" in l for l in lines[1:])
    assert any(l.startswith("12,E2,nope") for l in lines[1:])
    rebuilt_equals_current(root, scratch_db)


def test_ingest_and_apply_from_other_processes_keep_order(tmp_path, scratch_db):
    """서로 다른 프로세스의 적재와 apply(load)가 쓰기 잠금으로 순서를 지킨다(2b 완료 조건): 동시에 돌려도 하나씩 차례로
    되고, 끝난 뒤 DB는 폴더의 파일로 다시 만든 것과 같다."""
    import os
    import subprocess
    import sys

    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    env = {**os.environ, "PROJECTS_DIR": str(root), "PROJECT": "ledger", "DATA_DB_NAME": scratch_db}
    csvs = []
    for i in range(4):
        p = tmp_path / f"p{i}.csv"
        p.write_text("id,memo\n" + "".join(f"E{1000 + i * 100 + k},p{i}\n" for k in range(50)), encoding="utf-8")
        csvs.append(p)
    procs = [subprocess.Popen([sys.executable, "-m", "dashkit", "ingest", "--table", "entries", "--file", str(p)],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) for p in csvs]
    procs += [subprocess.Popen([sys.executable, "-m", "dashkit", "load"], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT) for _ in range(2)]
    outs = [(p.wait(timeout=120), p.stdout.read().decode()) for p in procs]
    assert all(code == 0 for code, _o in outs), outs
    assert len({x[0] for x in rows(scratch_db, "entries")} & {f"E{1000 + i * 100}" for i in range(4)}) == 4
    assert len(folder(root, "entries")) == 2 + 4
    rebuilt_equals_current(root, scratch_db)


def test_preview_with_row_errors_also_reports_reference_errors(tmp_path, scratch_db):
    """행 검증 오류가 있는 미리보기도 참조 대조까지 해, 모든 오류와 빼고 넣을 때 들어갈 행 수를 보인다(화면 확인 지적)."""
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    csv = tmp_path / "ev.csv"
    csv.write_text("id,entry_id,seen_at\n20,E1,\n21,E404,\n22,E2,nope\n", encoding="utf-8")
    item = [Item("events", {"kind": "csv", "path": csv, "name": "ev.csv"})]
    r = ingest("ledger", item, projects_dir=root, dbname=scratch_db, dry_run=True)
    b = r["batches"][0]
    assert r["ok"] is False and b["rows"]["inserted"] == 1 and b["rejected"] == 2 and not b["skip_invalid"]
    assert any("ev.csv:3:entry_id" in e for e in b["errors"]) and any("ev.csv:4:seen_at" in e for e in b["errors"])
    assert 20 not in {x[0] for x in rows(scratch_db, "events")}
    expect_error("invalid_rows", ingest, "ledger", item, projects_dir=root, dbname=scratch_db)


def test_skip_invalid_keeps_raw_cells_of_bad_rows_and_rechecks_duplicates(tmp_path, scratch_db):
    """리뷰 지적 회귀(PR 26): 값 개수가 틀린 행도 빠진 행 파일에 원래 칸을 남기고, 행을 뺀 뒤의 최종 내용으로 중복을 다시 본다
    (update 정책에서 같은 파일을 다시 보내 그 사이 고친 값을 되돌리지 않게)."""
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    csv = tmp_path / "en.csv"
    csv.write_text("id,group_id\nE30,g1\nE31,g404\nE32,g2,extra\n", encoding="utf-8")
    rejects = tmp_path / "r1"
    rejects.mkdir()
    item = [Item("entries", {"kind": "csv", "path": csv, "name": "en.csv"})]
    r = ingest("ledger", item, projects_dir=root, dbname=scratch_db, skip_invalid=True, rejects_dir=rejects)
    assert r["batches"][0]["rows"]["inserted"] == 1
    text = (rejects / "000.csv").read_text(encoding="utf-8-sig")
    assert "E32,g2," in text and "원래 칸: E32 | g2 | extra" in text
    # 같은 파일을 다시: 행을 뺀 뒤의 내용(E30,g1)이 이미 있는 배치와 같다
    e = expect_error("duplicate_file", ingest, "ledger", item, projects_dir=root, dbname=scratch_db, skip_invalid=True)
    assert e.extra["duplicates"][0]["file"] == r["batches"][0]["file"]
    rebuilt_equals_current(root, scratch_db)


def test_rows_recorded_match_the_file_after_dropping_new_rows_without_required_columns(tmp_path, scratch_db):
    """리뷰 지적 회귀(PR 26): 필수 열이 빠진 새 행을 빼고 넣으면 배치 기록의 rows도 배치 파일의 행 수와 같다."""
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    # events.entry_id는 필수이고 default가 없다. 머리글에 없으므로 새 행(id 40, 41)은 모두 빠진다
    r = ingest("ledger", [Item("events", {"kind": "rows", "rows": [{"id": 40}, {"id": 41}]})], projects_dir=root,
               dbname=scratch_db, skip_invalid=True)
    b = r["batches"][0]
    assert b["rows"]["inserted"] == 0 and b["rejected"] == 2
    with db.owner_conn(scratch_db) as conn:
        recorded = conn.execute("SELECT rows FROM dashkit_meta.batch WHERE file = %s", (b["file"],)).fetchone()[0]
    stored = gzip.decompress((root / "ledger" / "data" / "events" / b["file"]).read_bytes()).decode().splitlines()
    assert recorded == len(stored) - 1 == 0
    rebuilt_equals_current(root, scratch_db)


def _source_dsn(dbname: str) -> str:
    import os
    from urllib.parse import quote

    host = os.environ.get("DATA_DB_HOST", "data-db")
    port = os.environ.get("DATA_DB_PORT", "5432")
    user = quote(os.environ.get("DATA_DB_OWNER_USER", "dash_owner"), safe="")
    password = quote(os.environ.get("DATA_DB_OWNER_PASSWORD", ""), safe="")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


def test_additional_intake_xlsx_db_import_watch_equivalence_and_readonly(tmp_path, scratch_db, monkeypatch):
    """이슈 #42 통합 검증: xlsx, ingest-db(read_only=True), ingest-watch(.csv/.csv.gz/.xlsx) 적재 후
    정규형 sha256 동등성 및 rebuilt_equals_current가 100% 일치한다.
    """
    from test_additional_intake_unit import make_xlsx_bytes
    from dashkit.ingest.db_import import ingest_from_db
    from dashkit.ingest.watch import watch_once

    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)

    # 별도 외부 원본 스키마/테이블 준비
    with db.owner_conn(scratch_db) as conn:
        conn.execute("CREATE SCHEMA src_ext")
        conn.execute(
            "CREATE TABLE src_ext.ext_entries ("
            "id text, group_id text, state text, amount numeric, on_day date, flag boolean, qty integer, memo text)"
        )
        conn.execute(
            "INSERT INTO src_ext.ext_entries VALUES "
            "('E901', 'g1', 'done', 1234.50, '2026-11-20', true, 5, 'hello'),"
            "('E902', 'g2', 'open', 0, '2026-11-21', false, 10, NULL)"
        )
        conn.commit()

    dsn = _source_dsn(scratch_db)
    monkeypatch.setenv("TEST_SOURCE_DSN", dsn)

    # 1) read_only=True가 쓰기 쿼리/CTE를 차단하며 오류 메시지에 DSN·비밀번호가 남지 않는다
    bad_sql = tmp_path / "write.sql"
    bad_sql.write_text("WITH d AS (DELETE FROM src_ext.ext_entries RETURNING *) SELECT * FROM d", encoding="utf-8")
    with pytest.raises(IngestError) as wexc:
        ingest_from_db("ledger", "entries", dsn_env="TEST_SOURCE_DSN", query_file=bad_sql,
                       projects_dir=root, dbname=scratch_db)
    assert wexc.value.detail == "invalid_query"
    assert dsn not in wexc.value.message

    lit_sql = tmp_path / "lit.sql"
    lit_sql.write_text("SELECT 'customer-secret'::integer AS id", encoding="utf-8")
    with pytest.raises(IngestError) as lexc:
        ingest_from_db("ledger", "entries", dsn_env="TEST_SOURCE_DSN", query_file=lit_sql,
                       projects_dir=root, dbname=scratch_db)
    assert lexc.value.detail == "invalid_query"
    assert "customer-secret" not in lexc.value.message

    # 2) 원본 DB 가져오기 (dry_run → 확정)
    r_db = ingest_from_db("ledger", "entries", dsn_env="TEST_SOURCE_DSN", source_table="src_ext.ext_entries",
                          projects_dir=root, dbname=scratch_db, dry_run=True)
    assert r_db["ok"] and r_db["batches"][0]["rows"]["inserted"] == 2
    r_db = ingest_from_db("ledger", "entries", dsn_env="TEST_SOURCE_DSN", source_table="src_ext.ext_entries",
                          projects_dir=root, dbname=scratch_db)
    db_sha = r_db["batches"][0]["sha256"]

    # 3) 동일 데이터 xlsx 접수 (force=True로 sha256 비교)
    xlsx_bytes = make_xlsx_bytes(
        [
            {
                "name": "Entries",
                "rows": [
                    ["id", "group_id", "state", "amount", "on_day", "flag", "qty", "memo"],
                    ["E901", "g1", "done", {"t": "n", "v": "1234.50"}, {"t": "n", "s": 1, "v": "46346"}, {"t": "b", "v": "1"}, {"t": "n", "v": "5"}, "hello"],
                    ["E902", "g2", "open", {"t": "n", "v": "0"}, {"t": "n", "s": 1, "v": "46347"}, {"t": "b", "v": "0"}, {"t": "n", "v": "10"}, None],
                ],
            }
        ],
        num_fmts={164: "yyyy-mm-dd"},
        cell_xfs=[0, 164],
    )
    xlsx_file = tmp_path / "same.xlsx"
    xlsx_file.write_bytes(xlsx_bytes)
    r_xlsx = ingest("ledger", [Item("entries", {"kind": "xlsx", "path": xlsx_file, "name": "same.xlsx"})],
                    projects_dir=root, dbname=scratch_db, force=True)
    assert r_xlsx["batches"][0]["sha256"] == db_sha

    # 4) 감시 폴더 (.csv, .csv.gz, .xlsx + 무시 파일 + 실패 격리)
    inbox = root / "ledger" / "data" / "_inbox"
    ent_box = inbox / "entries"
    evt_box = inbox / "events"
    ent_box.mkdir(parents=True)
    evt_box.mkdir(parents=True)
    (ent_box / "01.csv").write_text("id,group_id,state,amount,on_day,flag,qty\nE910,g1,open,10,2026-11-22,Y,1\n", encoding="utf-8")
    (ent_box / "02.csv.gz").write_bytes(gzip.compress(b"id,group_id,state,amount,on_day,flag,qty\nE911,g1,done,20,2026-11-22,N,2\n"))
    (ent_box / "03.xlsx").write_bytes(
        make_xlsx_bytes([{"name": "S1", "rows": [["id", "group_id", "state", "amount", "on_day", "flag", "qty"],
                                                 ["E912", "g1", "open", "30", "2026-11-22", "true", "3"]]}])
    )
    (evt_box / "01.csv").write_text("id,entry_id,seen_at\n900,E910,2026-11-22T01:02:03\n", encoding="utf-8")
    # 무시 대상(.part, _hidden)
    (ent_box / "uploading.csv.part").write_text("id\nE999\n", encoding="utf-8")
    (ent_box / "_draft.csv").write_text("id\nE999\n", encoding="utf-8")
    # 실패 파일
    (ent_box / "99_bad.csv").write_text("id,amount\nE998,not_a_number\n", encoding="utf-8")

    st = watch_once("ledger", settle_ms=0, projects_dir=root, dbname=scratch_db)
    assert st["succeeded"] == 4 and st["failed"] == 1
    assert (ent_box / "uploading.csv.part").exists() and (ent_box / "_draft.csv").exists()
    assert len(list((ent_box / "_done").iterdir())) == 3
    failed_jsons = list((ent_box / "_failed").glob("*.error.json"))
    assert len(failed_jsons) == 1
    err_doc = __import__("json").loads(failed_jsons[0].read_text(encoding="utf-8"))
    assert err_doc["detail"] == "invalid_rows"
    # 재스캔 시 _failed 파일 재실행 안 함
    st2 = watch_once("ledger", settle_ms=0, projects_dir=root, dbname=scratch_db)
    assert st2["processed"] == 0

    rebuilt_equals_current(root, scratch_db)


def test_watch_processing_recovery_transient_retry_and_sensitive_redaction(tmp_path, scratch_db):
    """감시 폴더의 _processing/ 재시작 복구(커밋 전/커밋 직후 duplicate_file), 일시 잠금 재시도, 민감 열 마스킹."""
    import json as _json
    import os
    from datetime import datetime, timezone
    from dashkit.ingest.watch import watch_once

    def add_sensitive(doc):
        doc["tables"]["entries"]["columns"]["phone"] = {"type": "text", "sensitive": True, "pattern": r"^\d+$"}

    root = make_project(tmp_path, edit=add_sensitive)
    load(load_project("ledger", root), dbname=scratch_db)

    inbox = root / "ledger" / "data" / "_inbox" / "entries"
    proc = inbox / "_processing"
    proc.mkdir(parents=True)

    # 1) _processing/ 중단 파일(적재 전 중단) 회수 → 정상 적재 후 _done/ 이동
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pre_commit = proc / f"{stamp}-pre.csv"
    pre_commit.write_text("id,group_id,state\nE950,g1,open\n", encoding="utf-8")
    st = watch_once("ledger", settle_ms=0, projects_dir=root, dbname=scratch_db)
    assert st["succeeded"] == 1 and not pre_commit.exists()
    assert any(p.name.endswith("-pre.csv") for p in (inbox / "_done").iterdir())

    # 2) _processing/ 중단 파일(적재 커밋 직후 종료 → duplicate_file) → _done/ 정상 마감
    committed_src = tmp_path / "committed.csv"
    committed_src.write_text("id,group_id,state\nE951,g1,done\n", encoding="utf-8")
    ingest("ledger", [Item("entries", {"kind": "csv", "path": committed_src, "name": "committed.csv"})],
           projects_dir=root, dbname=scratch_db)
    after_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    post_commit = proc / f"{after_stamp}-committed.csv"
    post_commit.write_bytes(committed_src.read_bytes())
    st2 = watch_once("ledger", settle_ms=0, projects_dir=root, dbname=scratch_db)
    assert st2["succeeded"] == 1 and st2["failed"] == 0 and not post_commit.exists()
    assert any(p.name.endswith("-committed.csv") for p in (inbox / "_done").iterdir())

    # 3) 일시 잠금(409 apply_running) 시 _failed/로 버리지 않고 재시도 보존
    (inbox / "wait.csv").write_text("id,group_id,state\nE952,g1,open\n", encoding="utf-8")
    lock_conn = db.owner_conn(scratch_db)
    try:
        locks.acquire(lock_conn, 0)
        st_busy = watch_once("ledger", settle_ms=0, projects_dir=root, dbname=scratch_db, wait_ms=50)
        assert st_busy["retrying"] == 1 and st_busy["failed"] == 0
        assert any(p.name.endswith("-wait.csv") for p in proc.iterdir())
    finally:
        locks.release(lock_conn)
        lock_conn.close()
    st_retry = watch_once("ledger", settle_ms=0, projects_dir=root, dbname=scratch_db)
    assert st_retry["succeeded"] == 1 and not list(proc.iterdir())

    # 4) sensitive: true 열 검증 오류 & 열 개수 불일치 → .error.json에 민감 값 미노출
    (inbox / "sens_bad.csv").write_text("id,phone\nE960,010-secret-1234\n", encoding="utf-8")
    (inbox / "sens_cols.csv").write_text("id,phone\nE961,010-secret-5678,extra\n", encoding="utf-8")
    st_sens = watch_once("ledger", settle_ms=0, projects_dir=root, dbname=scratch_db)
    assert st_sens["failed"] == 2
    for ej in (inbox / "_failed").glob("*.error.json"):
        raw_txt = ej.read_text(encoding="utf-8")
        assert "010-secret-1234" not in raw_txt
        assert "010-secret-5678" not in raw_txt
        doc = _json.loads(raw_txt)
        assert doc["detail"] == "invalid_rows"

    # 5) 손상된 .csv.gz 및 CRC 손상 .xlsx 격리 후 다음 정상 파일 계속 처리
    import io as _io
    import zipfile as _zipfile
    from test_additional_intake_unit import make_xlsx_bytes as _make_xlsx

    crc_buf = bytearray(_make_xlsx([{"name": "S1", "rows": [["id", "group_id", "state"], ["E999", "g1", "open"]]}]))
    with _zipfile.ZipFile(_io.BytesIO(crc_buf), "r") as _zf:
        _sh = _zf.getinfo("xl/worksheets/sheet1.xml")
        _pos = _sh.header_offset + 30 + len(_sh.filename.encode("utf-8")) + max(_sh.compress_size - 2, 0)
    crc_buf[_pos] ^= 0xFF
    (inbox / "01_bad.csv.gz").write_bytes(b"not-a-valid-gzip-stream")
    (inbox / "01_bad_crc.xlsx").write_bytes(bytes(crc_buf))
    (inbox / "01_bad_date.xlsx").write_bytes(
        _make_xlsx(
            [{"name": "S1", "rows": [["id", "group_id", "state", "on_day"],
                                     ["E997", "g1", "open", {"t": "n", "s": 1, "v": "2958466"}]]}],
            num_fmts={164: "yyyy-mm-dd"},
            cell_xfs=[0, 164],
        )
    )
    (inbox / "02_ok.csv").write_text("id,group_id,state\nE962,g1,open\n", encoding="utf-8")
    st_corrupt = watch_once("ledger", settle_ms=0, projects_dir=root, dbname=scratch_db)
    assert st_corrupt["failed"] == 3 and st_corrupt["succeeded"] == 1
    assert not list(proc.iterdir())
    assert any(p.name.endswith("-01_bad.csv.gz.error.json") for p in (inbox / "_failed").iterdir())
    assert any(p.name.endswith("-01_bad_crc.xlsx.error.json") for p in (inbox / "_failed").iterdir())
    assert any(p.name.endswith("-01_bad_date.xlsx.error.json") for p in (inbox / "_failed").iterdir())
    assert any(p.name.endswith("-02_ok.csv") for p in (inbox / "_done").iterdir())

    rebuilt_equals_current(root, scratch_db)


def test_ingest_from_db_recreates_cursor_on_stale_config_retry(tmp_path, scratch_db, monkeypatch):
    """설정 변경(StaleConfig)으로 재시도할 때 원본 DB 커서를 새로 열어 전체 행을 다시 적재한다."""
    import dashkit.ingest.add as ingest_add
    from dashkit.ingest.db_import import ingest_from_db
    from dashkit.ingest.operation import StaleConfig

    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)

    with db.owner_conn(scratch_db) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS src_stale")
        conn.execute("DROP TABLE IF EXISTS src_stale.ext_entries")
        conn.execute("CREATE TABLE src_stale.ext_entries (id text, group_id text, state text, amount numeric)")
        conn.execute(
            "INSERT INTO src_stale.ext_entries VALUES "
            "('E970', 'g1', 'open', 11.5), ('E971', 'g2', 'done', 22.0)"
        )
        conn.commit()

    monkeypatch.setenv("TEST_STALE_DSN", _source_dsn(scratch_db))

    real_commit = ingest_add._commit
    attempts = 0

    def fail_once_then_commit(session, prepared, *, force, dry_run):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise StaleConfig("설정 변경 재시도 모의")
        return real_commit(session, prepared, force=force, dry_run=dry_run)

    monkeypatch.setattr(ingest_add, "_commit", fail_once_then_commit)

    res = ingest_from_db(
        "ledger",
        "entries",
        dsn_env="TEST_STALE_DSN",
        source_table="src_stale.ext_entries",
        projects_dir=root,
        dbname=scratch_db,
    )
    assert attempts == 2
    assert res["ok"] is True
    assert res["batches"][0]["rows"]["inserted"] == 2
    rebuilt_equals_current(root, scratch_db)
