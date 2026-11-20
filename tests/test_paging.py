"""행 조회의 페이지 나눔(dashkit/paging.py, 설계서 8-1·8-2). 임시 DB에 누적 테이블 프로젝트를 적재해 쓴다."""
from __future__ import annotations

import pytest
from psycopg import sql

from dashkit import db, paging
from dashkit.config import load_project
from dashkit.ingest import Item, ingest
from dashkit.loader import load

from test_ingest_db import make_project, scratch_db  # noqa: F401

pytestmark = pytest.mark.integration

ENTRY_COLS = [paging.Column("id", "text"), paging.Column("group_id", "text"), paging.Column("state", "text"),
              paging.Column("amount", "number"), paging.Column("on_day", "date"), paging.Column("flag", "boolean"),
              paging.Column("qty", "integer"), paging.Column("memo", "text")]
RAW = sql.SQL("raw.entries")


@pytest.fixture()
def big(tmp_path, scratch_db):
    """entries 5 + 120행(상태·금액에 같은 값과 NULL이 많다)."""
    root = make_project(tmp_path)
    load(load_project("ledger", root), dbname=scratch_db)
    rows = [{"id": f"E{100 + i}", "state": "open" if i % 3 else "done", "amount": None if i % 4 == 0 else str(i % 5),
             "memo": "가" * 2500 if i == 7 else f"m{i}"} for i in range(120)]
    ingest("ledger", [Item("entries", {"kind": "rows", "rows": rows})], projects_dir=root, dbname=scratch_db)
    return root, scratch_db


def walk(cur, body, *, key_columns=("id",), relation=RAW, columns=ENTRY_COLS, version="v1"):
    out, cursor, pages = [], None, 0
    while True:
        q = paging.parse_query({**body, "cursor": cursor}, columns)
        page = paging.fetch_page(cur, relation, columns, q, key_columns=list(key_columns) if key_columns else None,
                                 version=version, total_rows=None)
        out += page.rows
        pages += 1
        if not page.next_cursor:
            return out, pages
        cursor = page.next_cursor


def test_key_cursor_visits_every_row_once_in_any_sort(big):
    _root, dbname = big
    with db.owner_conn(dbname) as conn, conn.cursor() as cur:
        rows, pages = walk(cur, {"limit": 7})
        ids = [r["id"] for r in rows]
        assert len(ids) == 125 == len(set(ids)) and ids == sorted(ids) and pages == 18
        for sort in ([{"column": "amount", "dir": "desc"}], [{"column": "state"}, {"column": "amount"}],
                     [{"column": "on_day", "dir": "desc"}, {"column": "memo"}]):
            rows, _p = walk(cur, {"limit": 9, "sort": sort, "columns": ["id", "amount", "state"]})
            assert len(rows) == 125 and len({r["id"] for r in rows}) == 125, sort
            assert set(rows[0]) == {"id", "amount", "state"}


def test_offset_cursor_for_views_without_a_key_keeps_duplicates(big):
    _root, dbname = big
    cols = [paging.Column("state", "text"), paging.Column("group_id", "text")]
    with db.owner_conn(dbname) as conn, conn.cursor() as cur:
        rows, _p = walk(cur, {"limit": 11}, key_columns=None, relation=sql.SQL("dash.states"), columns=cols)
        assert len(rows) == 125                         # 같은 값의 행이 많아도 모두 한 번씩
        done = sum(1 for r in rows if r["state"] == "done")
        assert done == conn.execute("SELECT count(*) FROM dash.states WHERE state = 'done'").fetchone()[0]


def test_filters_and_types(big):
    _root, dbname = big
    with db.owner_conn(dbname) as conn, conn.cursor() as cur:
        def ids(filters):
            return {r["id"] for r in walk(cur, {"filters": filters, "limit": 200})[0]}
        assert ids([{"column": "state", "op": "eq", "value": "done"}]) == {
            r[0] for r in conn.execute("SELECT id FROM raw.entries WHERE state = 'done'")}
        assert ids([{"column": "amount", "op": "is_null"}]) == {r[0] for r in conn.execute(
            "SELECT id FROM raw.entries WHERE amount IS NULL")}
        assert ids([{"column": "amount", "op": "ge", "value": 3}, {"column": "id", "op": "starts_with", "value": "E1"}]) \
            == {r[0] for r in conn.execute("SELECT id FROM raw.entries WHERE amount >= 3 AND id LIKE 'E1%'")}
        assert ids([{"column": "id", "op": "in", "value": ["E1", "E2", "nope"]}]) == {"E1", "E2"}
        assert ids([{"column": "memo", "op": "contains", "value": "%"}]) == set()        # 와일드카드가 아니라 글자
        assert ids([{"column": "memo", "op": "eq", "value": "'; DROP TABLE raw.entries; --"}]) == set()
        assert conn.execute("SELECT count(*) FROM raw.entries").fetchone()[0] == 125
    bad = [{"filters": [{"column": "id; DROP TABLE x", "op": "eq", "value": 1}]},
           {"filters": [{"column": "flag", "op": "lt", "value": True}]},
           {"filters": [{"column": "qty", "op": "eq", "value": "many"}]},
           {"filters": [{"column": "on_day", "op": "eq", "value": "11/20/2026"}]},
           {"filters": [{"column": "id", "op": "like", "value": "x"}]},
           {"sort": [{"column": "id) DESC; --"}]}, {"limit": 201}, {"columns": ["nope"]}]
    for body in bad:
        with pytest.raises(paging.QueryError):
            paging.parse_query(body, ENTRY_COLS)


def test_long_values_are_cut_in_lists_but_not_in_one_row_reads(big):
    _root, dbname = big
    with db.owner_conn(dbname) as conn, conn.cursor() as cur:
        q = paging.parse_query({"filters": [{"column": "id", "op": "eq", "value": "E107"}]}, ENTRY_COLS)
        page = paging.fetch_page(cur, RAW, ENTRY_COLS, q, key_columns=["id"], version="v", total_rows=None)
        assert len(page.rows[0]["memo"]) == paging.CELL_CHARS and page.truncated_cells == [[0, "memo"]]
        one = paging.fetch_row(cur, RAW, ENTRY_COLS, {"id": "E107"})
        assert len(one["row"]["memo"]) == 2500 and one["truncated_cells"] == []


def test_cursor_is_bound_to_its_query_and_version(big):
    _root, dbname = big
    with db.owner_conn(dbname) as conn, conn.cursor() as cur:
        q = paging.parse_query({"limit": 10}, ENTRY_COLS)
        first = paging.fetch_page(cur, RAW, ENTRY_COLS, q, key_columns=["id"], version="v1", total_rows=125)
        assert first.total == 125
        q2 = paging.parse_query({"limit": 10, "cursor": first.next_cursor}, ENTRY_COLS)
        assert not paging.fetch_page(cur, RAW, ENTRY_COLS, q2, key_columns=["id"], version="v1", total_rows=125).changed
        assert paging.fetch_page(cur, RAW, ENTRY_COLS, q2, key_columns=["id"], version="v2", total_rows=125).changed
        q3 = paging.parse_query({"limit": 10, "cursor": first.next_cursor,
                                 "filters": [{"column": "state", "op": "eq", "value": "open"}]}, ENTRY_COLS)
        with pytest.raises(paging.QueryError):
            paging.fetch_page(cur, RAW, ENTRY_COLS, q3, key_columns=["id"], version="v1", total_rows=None)
        with pytest.raises(paging.QueryError):
            paging.decode_cursor(first.next_cursor[:-3] + "AAA")


def test_count_and_totals(big):
    _root, dbname = big
    cols = [paging.Column("group_id", "text"), paging.Column("n", "integer"), paging.Column("total", "number")]
    with db.owner_conn(dbname) as conn, conn.cursor() as cur:
        q = paging.parse_query({"limit": 5}, cols)
        page = paging.fetch_page(cur, sql.SQL("dash.summary"), cols, q, key_columns=None, version="v", total_rows=None)
        assert page.total is None                               # sql: 데이터셋은 행 수를 모른다
        q = paging.parse_query({"limit": 5, "count": True}, cols)
        page = paging.fetch_page(cur, sql.SQL("dash.summary"), cols, q, key_columns=None, version="v", total_rows=None)
        assert page.total == conn.execute("SELECT count(*) FROM dash.summary").fetchone()[0]
