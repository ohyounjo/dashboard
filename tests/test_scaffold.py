"""CSV 폴더에서 프로젝트 초안 만들기(dashkit/scaffold.py, 보강설계서 5-1). 스택 없이 돈다."""
from __future__ import annotations

import csv
import io
import json
import textwrap

import pytest
import yaml

from dashkit import scaffold
from dashkit.__main__ import main
from dashkit.config import load_project, table_order
from dashkit.loader.validate import validate_csv
from dashkit.scaffold import MARK, ScaffoldError

from conftest import PROJECTS

EXAMPLES = ["devtracker", "sales-sample", "chart-gallery"]


def b(text: str) -> bytes:
    return textwrap.dedent(text).lstrip("\n").encode("utf-8")


def table_of(inference: dict, name: str) -> dict:
    return next(t for t in inference["tables"] if t["name"] == name)


def col_of(inference: dict, table: str, name: str) -> dict:
    return next(c for c in table_of(inference, table)["columns"] if c["name"] == name)


def loaded(name: str, root):
    project = load_project(name, root)
    data, errors = validate_csv(project.schema, project.data_dir, table_order(project))
    assert errors == []
    return project, data


def all_charts(inference: dict) -> list[dict]:
    return [c for d in inference["dashboard"]["dashboards"].values() for c in d["charts"]]


def all_sections(inference: dict) -> list[dict]:
    return [s for tab in inference["dashboard"]["tabs"] for s in tab["sections"]]


# 규칙 표의 모든 차트·섹션이 나오는 표 하나
ORDERS = b("""
    order_id,customer_id,ordered_on,ship_start,ship_end,amount,channel,ship_status,memo
    1,10,2026-01-05,2026-01-06,2026-01-08,120.5,web,sent,first
    2,11,2026-01-06,2026-01-07,2026-01-09,80,web,sent,
    3,10,2026-01-12,2026-01-13,2026-01-15,45.25,shop,packing,gift wrap
    4,12,2026-01-13,2026-01-14,,300,shop,packing,
    5,11,2026-01-20,2026-01-21,2026-01-25,15,web,sent,call first
    6,10,2026-01-21,2026-01-22,2026-01-23,60,phone,returned,
""")
CUSTOMERS = b("""
    id,full_name,joined
    10,Kim,2025-03-01
    11,Lee,2025-04-11
    12,Park,2025-05-21
""")


# ---------------------------------------------------------------- 완료 조건(단위 수준): 예시 CSV 폴더

@pytest.mark.parametrize("example", EXAMPLES)
def test_example_data_folder_makes_valid_project(example, tmp_path):
    sources = scaffold.read_folder(PROJECTS / example / "data")
    root, inference = scaffold.create_project("made", sources, tmp_path)

    assert root == tmp_path / "made"
    project, data = loaded("made", tmp_path)
    assert sorted(project.schema["tables"]) == sorted(name[:-4] for name in sources)
    for source, raw in sources.items():
        table = source[:-4]
        assert (root / "data" / source).read_bytes() == raw          # 이름을 바꾸지 않았으면 원본 그대로
        assert len(data[table].rows) == table_of(inference, table)["rows"] > 0
    for file in ("schema.yaml", "dashboard.yaml"):
        text = (root / file).read_text(encoding="utf-8")
        assert MARK in text
        assert yaml.safe_load(text)["version"] == 1
    assert inference["problems"] == []
    # 테이블마다 데이터셋·row_count·건수·원본 행 표·탭
    for table in project.schema["tables"]:
        assert project.dashboard["datasets"][table]["metrics"]["row_count"] == "COUNT(*)"
        vizzes = [c["viz"] for c in project.dashboard["dashboards"][table]["charts"]]
        assert vizzes.count("big_number_total") == 1 and vizzes.count("table") == 1
        assert len(vizzes) <= 6
    assert [t["key"] for t in project.dashboard["tabs"]] == sorted(project.schema["tables"])


def test_examples_reproduce_hand_written_schema_where_rules_apply(tmp_path):
    inference = scaffold.infer_folder(PROJECTS / "devtracker" / "data")
    hand = load_project("devtracker", PROJECTS).schema["tables"]
    for table in inference["tables"]:
        assert table["primary_key"] == hand[table["name"]]["primary_key"]
        for col in table["columns"]:
            want = hand[table["name"]]["columns"][col["name"]]
            if want["type"] in ("integer", "number", "date", "boolean"):
                assert col["type"] == want["type"], (table["name"], col["name"])
            if col.get("ref"):
                assert col["ref"] == want["ref"]
    assert col_of(inference, "issues", "milestone_id")["ref"] == "milestones.id"
    assert col_of(inference, "issues", "severity")["type"] == "enum"


# ---------------------------------------------------------------- 유형

def test_type_inference_matches_validator_notation():
    inference = scaffold.infer({"things.csv": b("""
        code,qty,price,made_on,active,note,zip,seen_at,blank,huge,odd_date,grouped,flag
        a1,1,1.5,2026-01-01,true,x,00123,2026-01-01 08:00:00,,99999999999999999999,2026-02-30,"1,200",yes
        a2,-2,2,2026-01-02,FALSE,,04510,2026-01-02T09:30:00,,1,2026-03-01,"3,400",no
        a3,+3,.5,2026-12-31,True,z,10000,2026-01-03 10:00,,2,2026-03-02,"12,000",yes
    """)})
    types = {c["name"]: c["type"] for c in table_of(inference, "things")["columns"]}
    assert types == {
        "code": "text", "qty": "integer", "price": "number", "made_on": "date", "active": "boolean",
        "note": "text", "zip": "text", "seen_at": "timestamp", "blank": "text", "huge": "number",
        "odd_date": "text", "grouped": "text", "flag": "text",
    }
    assert col_of(inference, "things", "note")["required"] is False       # 빈 값 = NULL
    assert col_of(inference, "things", "qty")["required"] is True
    assert col_of(inference, "things", "blank")["required"] is False
    assert col_of(inference, "things", "blank")["confidence"] == "low"
    assert "0으로 시작" in col_of(inference, "things", "zip")["basis"]
    assert any("쉼표" in n for n in col_of(inference, "things", "grouped")["notes"])
    for col in table_of(inference, "things")["columns"]:
        assert col["basis"] and col["confidence"] in ("high", "medium", "low")


@pytest.mark.parametrize("values", [["1", " 2"], ["1_000", "2"], ["inf", "1"], ["NaN", "1"], ["1", "t"], ["2026-1-5"]])
def test_values_the_validator_would_not_take_stay_text(values, tmp_path):
    body = "k,v\n" + "".join(f"r{i},{v}\n" for i, v in enumerate(values))
    sources = {"t1.csv": body.encode()}
    assert col_of(scaffold.infer(sources), "t1", "v")["type"] == "text"
    scaffold.create_project("p", sources, tmp_path)
    loaded("p", tmp_path)


def test_all_inferred_types_load_and_validate(tmp_path):
    sources = {"things.csv": b("""
        code,qty,price,made_on,active,seen_at,blank
        a1,1,1.5,2026-01-01,true,2026-01-01 08:00:00,
        a2,-2,2e3,2026-01-02,FALSE,2026-01-02T09:30:00+09:00,
        a3,+3,.5,2026-12-31,True,2026-01-03 10:00,
    """)}
    scaffold.create_project("p", sources, tmp_path)
    project, data = loaded("p", tmp_path)
    assert len(data["things"].rows) == 3
    assert project.schema["tables"]["things"]["columns"]["seen_at"]["type"] == "timestamp"


# ---------------------------------------------------------------- enum

def test_enum_candidates_and_threshold():
    rows = "".join(f"{i},{'abc'[i % 3]},u{i},{'xy'[i % 2] if i else ''},{'L' * 41 if i % 2 else 'S'}\n" for i in range(12))
    inference = scaffold.infer({"t1.csv": ("id,kind,uniq,partial,long\n" + rows).encode()})
    kind = col_of(inference, "t1", "kind")
    assert kind["type"] == "enum" and kind["values"] == ["a", "b", "c"]      # 처음 나온 순서
    assert "3개" in kind["basis"]
    assert col_of(inference, "t1", "uniq")["type"] == "text"                   # 고유값이 많음
    partial = col_of(inference, "t1", "partial")
    assert partial["type"] == "enum" and partial["values"] == ["y", "x"] and partial["required"] is False
    assert col_of(inference, "t1", "long")["type"] == "text"                   # 값이 너무 김


def test_enum_limit_is_twelve_values():
    def source(n: int) -> dict:
        return {"t1.csv": ("id,kind\n" + "".join(f"{i},v{i % n}\n" for i in range(4 * n))).encode()}

    assert col_of(scaffold.infer(source(12)), "t1", "kind")["type"] == "enum"
    assert col_of(scaffold.infer(source(13)), "t1", "kind")["type"] == "text"


def test_state_named_column_needs_less_repetition():
    body = "id,run_status,color\n1,a,red\n2,b,blue\n3,c,red\n4,c,green\n"       # 고유값 3 > 4행의 절반
    inference = scaffold.infer({"t1.csv": body.encode()})
    assert col_of(inference, "t1", "run_status")["type"] == "enum"
    assert col_of(inference, "t1", "run_status")["confidence"] == "low"
    assert col_of(inference, "t1", "color")["type"] == "text"
    unique = scaffold.infer({"t1.csv": b"id,run_status\n1,a\n2,b\n3,c\n"})
    assert col_of(unique, "t1", "run_status")["type"] == "text"                # 반복이 전혀 없으면 enum이 아님


def test_enum_values_that_look_like_yaml_keywords_survive(tmp_path):
    tricky = ["yes", "no", "null", "on", "a: b", "#tag", "1.5x", "~", "it's", 'say "hi"', "[x]", "한 글", "-"]
    # 13개라 enum 상한을 넘으므로 둘로 나눠 확인한다
    for part in (tricky[:7], tricky[7:]):
        rows = "".join(f'{i},"{v.replace(chr(34), chr(34) * 2)}"\n' for i, v in enumerate(part * 2))
        sources = {"t1.csv": ("id,kind\n" + rows).encode()}
        root = tmp_path / f"n{len(part)}"
        scaffold.create_project("p", sources, root)
        project, _ = loaded("p", root)
        assert project.schema["tables"]["t1"]["columns"]["kind"]["values"] == part


# ---------------------------------------------------------------- 기본 키

def test_primary_key_is_first_unique_non_empty_column():
    inference = scaffold.infer({"t1.csv": b("""
        grp,ratio,maybe,code,other
        a,0.1,,k1,o1
        a,0.2,x,k2,o2
        b,0.3,y,k3,o3
    """)})
    table = table_of(inference, "t1")
    # grp는 중복, ratio는 소수(키로 쓰지 않음), maybe는 빈 값 → code
    assert table["primary_key"] == ["code"]
    assert "유일" in table["primary_key_basis"]
    assert inference["problems"] == []


def test_integer_key_uniqueness_uses_parsed_values(tmp_path):
    sources = {"t1.csv": b"n,code\n1,a\n+1,b\n2,c\n"}           # 1과 +1은 같은 정수
    assert table_of(scaffold.infer(sources), "t1")["primary_key"] == ["code"]
    scaffold.create_project("p", sources, tmp_path)
    loaded("p", tmp_path)


def test_composite_key_when_no_single_column(tmp_path):
    sources = {"t1.csv": b("""
        sold_on,shop,qty
        2026-01-01,a,1
        2026-01-01,b,1
        2026-01-02,a,1
        2026-01-02,b,1
    """)}
    table = table_of(scaffold.infer(sources), "t1")
    assert table["primary_key"] == ["sold_on", "shop"]
    assert table["primary_key_confidence"] == "low"
    scaffold.create_project("p", sources, tmp_path)
    project, _ = loaded("p", tmp_path)
    assert project.schema["tables"]["t1"]["primary_key"] == ["sold_on", "shop"]
    assert "primary_key: [sold_on, shop]" in (tmp_path / "p" / "schema.yaml").read_text(encoding="utf-8")


def test_no_key_candidate_is_reported_and_nothing_is_written(tmp_path):
    sources = {"good.csv": b"id,v\n1,a\n2,b\n", "dupes.csv": b"a,b\nx,1\nx,1\n"}
    inference = scaffold.infer(sources)                          # 추론은 돌려준다(Studio가 고칠 수 있게)
    assert table_of(inference, "dupes")["primary_key"] == []
    assert len(inference["problems"]) == 1 and inference["problems"][0].startswith("dupes.csv: 기본 키")
    with pytest.raises(ScaffoldError) as exc:
        scaffold.create_project("p", sources, tmp_path)
    assert "dupes.csv" in exc.value.errors[0]
    with pytest.raises(ScaffoldError):
        scaffold.render("p", inference, sources)
    assert list(tmp_path.iterdir()) == []

    # 사람이 키를 정해 주면(여기서는 중복 행을 고친 CSV와 함께) 만들 수 있다
    fixed = {**sources, "dupes.csv": b"a,b\nx,1\ny,1\n"}
    scaffold.create_project("p", fixed, tmp_path)
    loaded("p", tmp_path)


def test_header_only_csv_is_a_problem(tmp_path):
    sources = {"empty.csv": b"id,name\n"}
    inference = scaffold.infer(sources)
    assert table_of(inference, "empty")["rows"] == 0
    assert "데이터 행이 없음" in inference["problems"][0]
    with pytest.raises(ScaffoldError):
        scaffold.create_project("p", sources, tmp_path)
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------- ref

def test_ref_from_table_id_name_with_plural_table():
    inference = scaffold.infer({"orders.csv": ORDERS, "customers.csv": CUSTOMERS})
    ref = col_of(inference, "orders", "customer_id")
    assert ref["ref"] == "customers.id" and ref["type"] == "integer"
    assert ref["ref_confidence"] == "high" and "_id" in ref["ref_basis"]
    assert "ref" not in col_of(inference, "orders", "order_id")      # 자기 테이블의 키는 ref가 아님


@pytest.mark.parametrize("table,column", [("categories", "category_id"), ("boxes", "box_id"), ("item", "item_id"),
                                          ("classes", "class_id"), ("users", "users_id")])
def test_ref_singular_plural_forms(table, column, tmp_path):
    sources = {f"{table}.csv": b"id,name\n1,a\n2,b\n", "facts.csv": f"fact_no,{column}\n1,1\n2,2\n3,1\n".encode()}
    assert col_of(scaffold.infer(sources), "facts", column)["ref"] == f"{table}.id"
    scaffold.create_project("p", sources, tmp_path)
    project, _ = loaded("p", tmp_path)
    assert table_order(project)[0] == table


def test_ref_not_made_when_values_missing_but_comment_stays(tmp_path):
    sources = {"orders.csv": ORDERS, "customers.csv": b"id,full_name\n10,Kim\n11,Lee\n"}     # 12가 없음
    col = col_of(scaffold.infer(sources), "orders", "customer_id")
    assert "ref" not in col
    assert any("ref 후보였으나" in n and "대상에 없는 값" in n for n in col["notes"])
    scaffold.create_project("p", sources, tmp_path)
    project, _ = loaded("p", tmp_path)
    assert "ref" not in project.schema["tables"]["orders"]["columns"]["customer_id"]
    text = (tmp_path / "p" / "schema.yaml").read_text(encoding="utf-8")
    assert f"{MARK} - ref 후보였으나 만들지 않음. customers.id: 대상에 없는 값이 1건(예: '12')" in text


def test_ref_not_made_when_types_differ_or_target_key_is_composite():
    other_type = {"orders.csv": ORDERS, "customers.csv": b"id,full_name\nc10,Kim\nc11,Lee\n"}
    col = col_of(scaffold.infer(other_type), "orders", "customer_id")
    assert "ref" not in col and any("type이 다름" in n for n in col["notes"])

    composite = {"orders.csv": ORDERS, "customers.csv": b"id,part\n10,a\n10,b\n11,a\n12,a\n"}
    col = col_of(scaffold.infer(composite), "orders", "customer_id")
    assert "ref" not in col and any("단일 열 기본 키가 없음" in n for n in col["notes"])


def test_ref_from_same_name_as_other_tables_key(tmp_path):
    sources = {"shops.csv": b"shop_code,city\ns1,a\ns2,b\n", "sales.csv": b"sale_no,shop_code,qty\n1,s1,3\n2,s1,4\n3,s2,5\n"}
    col = col_of(scaffold.infer(sources), "sales", "shop_code")
    assert col["ref"] == "shops.shop_code" and col["ref_confidence"] == "medium" and col["type"] == "text"
    scaffold.create_project("p", sources, tmp_path)
    loaded("p", tmp_path)


def test_ref_cycle_is_not_created(tmp_path):
    sources = {"a.csv": b"id,b_id\n1,1\n2,2\n", "b.csv": b"id,a_id\n1,1\n2,2\n"}
    inference = scaffold.infer(sources)
    refs = [c.get("ref") for t in inference["tables"] for c in t["columns"] if c.get("ref")]
    assert refs == ["b.id"]
    assert any("순환" in n for n in col_of(inference, "b", "a_id")["notes"])
    scaffold.create_project("p", sources, tmp_path)
    loaded("p", tmp_path)


# ---------------------------------------------------------------- 이름 바꾸기와 CSV 사본

def test_to_ident_rules():
    taken: set[str] = set()
    cases = [("Order ID", "order_id"), ("orderDate", "order_date"), ("HTTPCode", "http_code"), ("select", "select_col"),
             ("2024 Sales", "c_2024_sales"), ("금액", "column_6"), ("매출 total", "total"), ("Order-ID", "order_id_2"),
             ("  ", "column_9"), ("x" * 80, "x" * 63), ("ok_name", "ok_name")]
    for position, (raw, want) in enumerate(cases, 1):
        name, why = scaffold.to_ident(raw, "column", position, taken)
        assert name == want
        assert (why is None) == (raw == want)
    assert scaffold.to_ident("Order", "table", 1, set())[0] == "order_tbl"
    assert scaffold.to_ident("2024", "table", 1, set())[0] == "t_2024"
    assert scaffold.to_ident("자료", "table", 3, set())[0] == "table_3"


def test_renamed_headers_get_a_csv_copy_and_labels(tmp_path):
    sources = {"Sales Orders 2024.csv": "﻿Order ID, Total Amount ,select,금액,Order Date\n"
                                        "1,10.5,a,\"1,000\",2026-01-01\n\n2,20,b,\"multi\nline\",2026-01-02\n".encode("utf-8")}
    inference = scaffold.infer(sources)
    table = inference["tables"][0]
    assert table["name"] == "sales_orders_2024" and table["label"] == "Sales Orders 2024"
    assert [c["name"] for c in table["columns"]] == ["order_id", "total_amount", "select_col", "column_4", "order_date"]
    assert [c["source"] for c in table["columns"]] == ["Order ID", "Total Amount", "select", "금액", "Order Date"]
    assert all(any("열 이름을" in n for n in c["notes"]) for c in table["columns"])

    files = scaffold.render("p", inference, sources)
    copy = files["data/sales_orders_2024.csv"].decode("utf-8")
    rows = list(csv.reader(io.StringIO(copy, newline="")))
    assert rows == [["order_id", "total_amount", "select_col", "column_4", "order_date"],
                    ["1", "10.5", "a", "1,000", "2026-01-01"], ["2", "20", "b", "multi\nline", "2026-01-02"]]

    root = scaffold.write_project("p", files, tmp_path)
    project, data = loaded("p", tmp_path)
    assert len(data["sales_orders_2024"].rows) == 2
    labels = project.dashboard["datasets"]["sales_orders_2024"]["labels"]
    assert labels["column_4"] == "금액" and labels["order_id"] == "Order ID"
    assert project.dashboard["tabs"][0]["label"] == "Sales Orders 2024"
    assert "열 이름을 '금액'에서 바꿈" in (root / "schema.yaml").read_text(encoding="utf-8")


def test_cp949_csv_is_read_and_rewritten_as_utf8(tmp_path):
    sources = {"t1.csv": "id,name\n1,가나다\n2,라마바\n".encode("cp949")}
    inference = scaffold.infer(sources)
    assert any("CP949" in n for n in inference["tables"][0]["notes"])
    files = scaffold.render("p", inference, sources)
    assert files["data/t1.csv"].decode("utf-8").splitlines()[1] == "1,가나다"
    scaffold.write_project("p", files, tmp_path)
    loaded("p", tmp_path)


def test_colliding_table_names_are_numbered(tmp_path):
    sources = {"My Data.csv": b"id\n1\n", "my_data.csv": b"id\n1\n"}
    inference = scaffold.infer(sources)
    assert [(t["source"], t["name"]) for t in inference["tables"]] == [("My Data.csv", "my_data"), ("my_data.csv", "my_data_2")]
    scaffold.create_project("p", sources, tmp_path)
    loaded("p", tmp_path)


# ---------------------------------------------------------------- 차트 규칙 표

def test_chart_rules_produce_every_chart_and_section(tmp_path):
    sources = {"orders.csv": ORDERS, "customers.csv": CUSTOMERS}
    inference = scaffold.infer(sources)
    dash = inference["dashboard"]

    assert dash["datasets"]["orders"]["table"] == "orders"
    assert dash["datasets"]["orders"]["metrics"] == {"row_count": "COUNT(*)", "sum_amount": "SUM(amount)"}
    charts = {c["viz"]: c for c in dash["dashboards"]["orders"]["charts"] if c["viz"] != "bar"}
    bars = [c for c in dash["dashboards"]["orders"]["charts"] if c["viz"] == "bar"]
    assert set(charts) == {"big_number_total", "line", "pie", "table"}
    assert charts["big_number_total"]["metric"] == "row_count"
    assert (charts["line"]["x"], charts["line"]["time_grain"], charts["line"]["metrics"]) == ("ordered_on", "P1W", ["sum_amount"])
    assert [(c["x"], c["metrics"]) for c in bars] == [("channel", ["row_count"]), ("ship_status", ["row_count"])]
    assert (charts["pie"]["groupby"], charts["pie"]["metric"]) == (["channel"], "sum_amount")
    assert charts["table"]["columns"][:2] == ["order_id", "customer_id"] and charts["table"]["order_by"] == [["order_id", "asc"]]
    assert all(c["note"] for c in all_charts(inference))

    layout = dash["dashboards"]["orders"]["layout"]
    assert layout[0] == [{"chart": "orders_count", "width": 3}, {"chart": "orders_ordered_on_weekly", "width": 9}]
    placed = [i if isinstance(i, str) else i["chart"] for row in layout for i in row]
    assert sorted(placed) == sorted(c["key"] for c in dash["dashboards"]["orders"]["charts"])

    tab = next(t for t in dash["tabs"] if t["key"] == "orders")
    kinds = [s["type"] for s in tab["sections"]]
    assert kinds == ["superset", "gantt", "kanban"]
    gantt, kanban = tab["sections"][1], tab["sections"][2]
    assert gantt["fields"] == {"label": "memo", "start": "ship_start", "end": "ship_end", "status": "ship_status"}
    assert kanban["group_by"] == "ship_status"
    assert kanban["columns"] == [{"value": v, "label": v} for v in ["sent", "packing", "returned"]]
    assert kanban["card"] == {"title": "memo", "chips": ["channel"]}

    # 건수와 원본 행 표만 있는 테이블
    simple = dash["dashboards"]["customers"]
    assert [c["viz"] for c in simple["charts"]] == ["big_number_total", "table"]
    assert simple["layout"] == [[{"chart": "customers_count", "width": 3}, {"chart": "customers_rows", "width": 9}]]
    assert [s["type"] for s in next(t for t in dash["tabs"] if t["key"] == "customers")["sections"]] == ["superset"]

    scaffold.create_project("p", sources, tmp_path)
    project, _ = loaded("p", tmp_path)
    sections = project.dashboard["tabs"][1]["sections"]
    assert sections[1]["type"] == "gantt" and sections[2]["type"] == "kanban"


def test_gantt_picks_matching_begin_finish_pair_and_percent_column():
    inference = scaffold.infer({"jobs.csv": b("""
        job_code,job_name,created_on,plan_start,plan_end,real_start,done_pct,big_pct
        j1,A,2026-01-01,2026-01-02,2026-01-05,2026-01-03,10,150
        j2,B,2026-01-01,2026-01-03,2026-01-09,2026-01-04,100,20
    """)})
    gantt = next(s for s in all_sections(inference) if s["type"] == "gantt")
    assert gantt["fields"] == {"label": "job_name", "start": "plan_start", "end": "plan_end", "progress": "done_pct"}
    assert not [s for s in all_sections(inference) if s["type"] == "kanban"]


def test_no_gantt_without_begin_and_finish_named_dates():
    inference = scaffold.infer({"t1.csv": b"id,created_on,updated_on\n1,2026-01-01,2026-01-02\n2,2026-01-03,2026-01-04\n"})
    assert [s["type"] for s in all_sections(inference)] == ["superset"]


def test_chart_count_is_capped():
    head = "id,d,n," + ",".join(f"e{i}" for i in range(6)) + "," + ",".join(f"x{i}" for i in range(10))
    rows = "".join(f"{r},2026-01-{r + 1:02d},{r}," + ",".join("ab"[(r >> (i % 3)) % 2] for i in range(6)) + ","
                   + ",".join(f"v{r}" for _ in range(10)) + "\n" for r in range(8))
    inference = scaffold.infer({"wide.csv": (head + "\n" + rows).encode()})
    charts = all_charts(inference)
    assert len([c for c in charts if c["viz"] == "bar"]) == scaffold.MAX_BARS
    assert len(charts) == 6
    listing = next(c for c in charts if c["viz"] == "table")
    assert len(listing["columns"]) == scaffold.MAX_LIST_COLUMNS and "앞의" in listing["note"]


def test_kanban_skipped_for_large_tables():
    rows = "".join(f"{i},{'ab'[i % 2]}\n" for i in range(scaffold.KANBAN_MAX_ROWS + 1))
    inference = scaffold.infer({"t1.csv": ("id,status\n" + rows).encode()})
    assert col_of(inference, "t1", "status")["type"] == "enum"
    assert [s["type"] for s in all_sections(inference)] == ["superset"]


def test_generated_charts_use_registry_and_manifest_fields(tmp_path):
    """차트 유형과 위젯 옵션이 레지스트리·매니페스트의 필수 필드를 채운다(load_project의 스키마 검사)."""
    from dashkit import charts as registry
    from dashkit import widgets

    inference = scaffold.infer({"orders.csv": ORDERS, "customers.csv": CUSTOMERS})
    known = registry.registry() if hasattr(registry, "registry") else None
    for chart in all_charts(inference):
        if known is not None:
            assert chart["viz"] in known
    for sec in all_sections(inference):
        assert sec["type"] == "superset" or sec["type"] in widgets.registry()
    scaffold.create_project("p", {"orders.csv": ORDERS, "customers.csv": CUSTOMERS}, tmp_path)
    loaded("p", tmp_path)


# ---------------------------------------------------------------- 주석

def test_mark_comment_on_every_inferred_place(tmp_path):
    sources = {"orders.csv": ORDERS, "customers.csv": CUSTOMERS}
    root, inference = scaffold.create_project("p", sources, tmp_path)
    schema_lines = (root / "schema.yaml").read_text(encoding="utf-8").splitlines()
    for table in inference["tables"]:
        for col in table["columns"]:
            line = next(l for l in schema_lines if l.strip().startswith(f"{col['name']}:"))
            assert MARK in line and col["basis"] in line
    assert sum("primary_key:" in l and MARK in l for l in schema_lines) == 2

    dash_text = (root / "dashboard.yaml").read_text(encoding="utf-8")
    widgets_n = len([s for s in all_sections(inference) if s["type"] != "superset"])
    assert dash_text.count(MARK) == len(all_charts(inference)) + widgets_n + 1     # +1: 파일 머리의 안내
    assert "note" not in yaml.safe_load(dash_text)["dashboards"]["orders"]["charts"][0]


# ---------------------------------------------------------------- 경계 사례

def test_existing_project_is_not_overwritten(tmp_path):
    sources = {"t1.csv": b"id,v\n1,a\n2,b\n"}
    root, _ = scaffold.create_project("p", sources, tmp_path)
    before = {p.name: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(ScaffoldError) as exc:
        scaffold.create_project("p", {"other.csv": b"id\n1\n"}, tmp_path)
    assert "이미 있음" in exc.value.errors[0]
    with pytest.raises(ScaffoldError):
        scaffold.write_project("p", scaffold.render("p", scaffold.infer(sources), sources), tmp_path)
    assert {p.name: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before

    (tmp_path / "bare").mkdir()                                    # 빈 폴더만 있어도 거부
    with pytest.raises(ScaffoldError):
        scaffold.create_project("bare", sources, tmp_path)
    assert list((tmp_path / "bare").iterdir()) == []


@pytest.mark.parametrize("name", ["", "Bad", "has space", "../up", "a/b", "-lead", "_x", "한글", "a.b"])
def test_invalid_project_name(name, tmp_path):
    with pytest.raises(ScaffoldError) as exc:
        scaffold.create_project(name, {"t1.csv": b"id\n1\n"}, tmp_path)
    assert "프로젝트 이름 형식 오류" in exc.value.errors[0]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["a", "0a", "my-project_2"])
def test_valid_project_names_match_load_project(name, tmp_path):
    scaffold.create_project(name, {"t1.csv": b"id\n1\n2\n"}, tmp_path)
    loaded(name, tmp_path)


def test_empty_or_missing_folder(tmp_path):
    with pytest.raises(ScaffoldError) as exc:
        scaffold.read_folder(tmp_path)
    assert "CSV 파일" in exc.value.errors[0]
    with pytest.raises(ScaffoldError) as exc:
        scaffold.read_folder(tmp_path / "nope")
    assert "폴더가 없음" in exc.value.errors[0]
    with pytest.raises(ScaffoldError):
        scaffold.infer({})
    (tmp_path / "note.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.csv").write_text("id\n1\n")
    with pytest.raises(ScaffoldError):
        scaffold.read_folder(tmp_path)                             # 하위 폴더와 다른 확장자는 보지 않는다
    (tmp_path / "UP.CSV").write_text("id\n1\n")
    assert list(scaffold.read_folder(tmp_path)) == ["UP.CSV"]


@pytest.mark.parametrize("raw,expect", [
    (b"", "머리글이 없음"),
    (b"\n\n", "머리글이 없음"),
    (b" , \n1,2\n", "머리글이 없음"),
    (b"id,v\n1,a\n2\n3,c,d\n", "t1.csv:3: 값 개수 1개가 머리글 2개와 다름"),
    (b"id,v\n1,\xff\xfe\x81\n", "읽을 수 없음"),
])
def test_unreadable_csv_is_an_error(raw, expect):
    with pytest.raises(ScaffoldError) as exc:
        scaffold.infer({"t1.csv": raw})
    assert any(expect in e for e in exc.value.errors)


def test_all_bad_files_are_reported_together():
    with pytest.raises(ScaffoldError) as exc:
        scaffold.infer({"a.csv": b"", "b.csv": b"id,v\n1\n", "../c.csv": b"id\n1\n", "ok.csv": b"id\n1\n"})
    assert len(exc.value.errors) == 3


def test_blank_lines_and_trailing_spaces_follow_validator(tmp_path):
    sources = {"t1.csv": b"id , v\n1,a\n\n2, \n3,b\n"}              # 머리글은 다듬고, 값 ' '은 NULL이 아닌 글자
    inference = scaffold.infer(sources)
    assert table_of(inference, "t1")["rows"] == 3
    assert col_of(inference, "t1", "v")["required"] is True
    scaffold.create_project("p", sources, tmp_path)
    _, data = loaded("p", tmp_path)
    assert [r[1] for r in data["t1"].rows] == ["a", " ", "b"]


# ---------------------------------------------------------------- JSON 왕복과 사람이 고친 추론 결과

@pytest.mark.parametrize("example", EXAMPLES)
def test_inference_round_trips_through_json(example):
    sources = scaffold.read_folder(PROJECTS / example / "data")
    inference = scaffold.infer(sources)
    again = json.loads(json.dumps(inference, ensure_ascii=False))
    assert again == inference
    assert scaffold.render("p", again, sources) == scaffold.render("p", inference, sources)
    assert set(inference) == {"format", "tables", "dashboard", "problems"} and inference["format"] == 1


def test_infer_and_render_write_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    sources = {"t1.csv": b"id,v\n1,a\n2,b\n"}
    files = scaffold.render("p", scaffold.infer(sources), sources)
    assert set(files) == {"schema.yaml", "dashboard.yaml", "data/t1.csv"}
    scaffold.verify("p", files)
    assert list(tmp_path.iterdir()) == []


def test_edited_inference_is_used(tmp_path):
    sources = {"orders.csv": ORDERS, "customers.csv": CUSTOMERS}
    inference = json.loads(json.dumps(scaffold.infer(sources)))
    channel = col_of(inference, "orders", "channel")
    channel["type"] = "text"
    del channel["values"]
    col_of(inference, "orders", "amount").update({"min": 0, "description": "주문 금액"})
    del col_of(inference, "orders", "customer_id")["ref"]
    table_of(inference, "customers")["label"] = "고객"
    inference["dashboard"] = scaffold.suggest_dashboard(inference["tables"])      # 고친 표로 차트를 다시 고른다
    inference["dashboard"]["app"] = {"title": "주문 현황", "as_of": "2026-02-01"}

    root = scaffold.write_project("p", scaffold.render("p", inference, sources), tmp_path)
    project, _ = loaded("p", tmp_path)
    cols = project.schema["tables"]["orders"]["columns"]
    assert cols["channel"]["type"] == "text" and "ref" not in cols["customer_id"]
    assert cols["amount"]["min"] == 0 and cols["amount"]["description"] == "주문 금액"
    assert project.dashboard["app"]["title"] == "주문 현황" and project.dashboard["app"]["as_of"] == "2026-02-01"
    assert [t["label"] for t in project.dashboard["tabs"]] == ["고객", "orders"]
    bars = [c["x"] for c in project.dashboard["dashboards"]["orders"]["charts"] if c["viz"] == "bar"]
    assert bars == ["ship_status"]
    assert root.is_dir()


@pytest.mark.parametrize("break_it,expect", [
    (lambda inf: col_of(inf, "orders", "customer_id").update(ref="nowhere.id"), "테이블 'nowhere'이 없음"),
    (lambda inf: col_of(inf, "orders", "amount").update(type="integer"), "정수가 아님"),
    (lambda inf: col_of(inf, "orders", "channel").update(values=["web"]), "허용되지 않은 값"),
    (lambda inf: inf["dashboard"]["dashboards"]["orders"]["charts"][0].update(metric="nope"), "지표 'nope'이 없음"),
    (lambda inf: inf["dashboard"]["dashboards"]["orders"]["charts"][0].update(viz="nope"), "dashboard.yaml"),
    (lambda inf: table_of(inf, "orders").update(name="Orders"), "이름 형식 오류"),
    (lambda inf: table_of(inf, "orders").update(primary_key=[]), "기본 키"),
    (lambda inf: table_of(inf, "orders")["columns"].pop(), "열 수가 추론 결과와 다름"),
    (lambda inf: table_of(inf, "orders").update(source="gone.csv"), "원본 CSV"),
    (lambda inf: inf.update(format=99), "format"),
    (lambda inf: inf.pop("dashboard"), "형식이 잘못됨"),
    (lambda inf: inf.update(tables="x"), "형식이 잘못됨"),
])
def test_bad_edit_fails_with_reason_and_leaves_no_files(break_it, expect, tmp_path):
    sources = {"orders.csv": ORDERS, "customers.csv": CUSTOMERS}
    inference = scaffold.infer(sources)
    break_it(inference)
    with pytest.raises(ScaffoldError) as exc:
        scaffold.write_project("p", scaffold.render("p", inference, sources), tmp_path)
    assert any(expect in e for e in exc.value.errors), exc.value.errors
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------- CLI

@pytest.mark.parametrize("example", EXAMPLES)
def test_cli_new_then_check(example, tmp_path, monkeypatch, capsys):
    """통합 테스트가 쓰는 흐름: 예시 CSV 폴더 → 임시 프로젝트 → check. CLI만으로 된다."""
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    assert main(["new", "from-cli", "--from", str(PROJECTS / example / "data")]) == 0
    out = capsys.readouterr().out
    assert "✔ new:" in out and "PROJECT=from-cli make check" in out
    monkeypatch.setenv("PROJECT", "from-cli")
    assert main(["check"]) == 0
    assert sorted(p.name for p in (tmp_path / "from-cli").iterdir()) == ["dashboard.yaml", "data", "schema.yaml"]

    assert main(["new", "from-cli", "--from", str(PROJECTS / example / "data")]) == 1      # 두 번째는 거부
    assert "이미 있음" in capsys.readouterr().err


def test_cli_failures_exit_1_and_write_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "projects"))
    src = tmp_path / "src"
    src.mkdir()
    assert main(["new", "p", "--from", str(src)]) == 1                      # 빈 폴더
    (src / "t1.csv").write_bytes(b"a,b\nx,1\nx,1\n")
    assert main(["new", "p", "--from", str(src)]) == 1                      # 기본 키 후보 없음
    assert main(["new", "Bad Name", "--from", str(src)]) == 1
    err = capsys.readouterr().err
    assert "CSV 파일" in err and "기본 키를 정할 수 없음" in err and "프로젝트 이름 형식 오류" in err
    assert not (tmp_path / "projects").exists()


def test_cli_dry_run_prints_inference_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path / "projects"))
    assert main(["new", "p", "--from", str(PROJECTS / "sales-sample" / "data"), "--dry-run"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == scaffold.infer_folder(PROJECTS / "sales-sample" / "data")
    assert not (tmp_path / "projects").exists()
