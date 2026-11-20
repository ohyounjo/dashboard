"""데이터 추가 적재의 DB 없는 부분(contracts/ingest.md): 값 다듬기, 머리글 맞추기, 파서, 배치 이름, 정규형 쓰기, 설정 검사."""
from __future__ import annotations

import gzip
import shutil
from pathlib import Path

import pytest

from dashkit import config
from dashkit.ingest import batches, normalize
from dashkit.ingest.intake import CsvParser, Intake, IntakeError, RowsParser, match_header, table_labels

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def project(tmp_path):
    shutil.copytree(FIXTURES / "ledger", tmp_path / "ledger")
    return config.load_project("ledger", tmp_path)


# ---------------------------------------------------------------- 값 다듬기

@pytest.mark.parametrize("ctype, raw, out, counted", [
    ("date", "2026/11/20", "2026-11-20", "dates"),
    ("date", "2026.1.5", "2026-01-05", "dates"),
    ("date", "20261120", "2026-11-20", "dates"),
    ("date", "2026-11-20", "2026-11-20", None),
    ("date", "11/20/2026", "11/20/2026", None),          # 순서가 모호한 날짜는 바꾸지 않는다(검증이 오류로 알린다)
    ("date", "20/11/2026", "20/11/2026", None),
    ("integer", "1,234,567", "1234567", "numbers"),
    ("number", "-1,234.50", "-1234.50", "numbers"),
    ("number", "1,23", "1,23", None),                    # 천 단위 쉼표가 아니면 그대로
    ("boolean", "Y", "true", "booleans"),
    ("boolean", "no", "false", "booleans"),
    ("boolean", "1", "true", "booleans"),
    ("boolean", "TRUE", "true", "booleans"),
    ("boolean", "maybe", "maybe", None),
    ("timestamp", "2026/11/20 10:30", "2026-11-20T10:30", "timestamps"),
    ("timestamp", "2026-11-20T10:30:00+09:00", "2026-11-20T10:30:00+09:00", None),
    ("text", "  a b  ", "a b", "trimmed"),
    ("text", "1,234", "1,234", None),                    # 글 열은 다듬지 않는다
])
def test_normalizers_change_only_listed_notations(ctype, raw, out, counted):
    counts: dict[str, int] = {}
    assert normalize.normalize(raw, ctype, counts) == out
    if counted:
        assert counts.get(counted) == 1, counts
    else:
        assert counts.get("dates", 0) + counts.get("numbers", 0) + counts.get("booleans", 0) == 0


def test_normalizer_registry_covers_every_column_type():
    schema = config.load_schema("schema.schema.json")
    types = schema["$defs"]["column"]["properties"]["type"]["enum"]
    assert set(types) <= set(normalize.NORMALIZERS), "유형을 더하면 NORMALIZERS에 한 줄을 더한다"


# ---------------------------------------------------------------- 머리글 맞추기

def test_header_matching_order_and_candidates(project):
    table = project.schema["tables"]["entries"]
    labels = table_labels(project.dashboard, "entries")
    m = match_header(table, ["id", "State Name", "금액", "On-Day", "비고", "QTY", "qtyy", "zzz"], labels=labels)
    assert m.columns == ["id", "state", "amount", "on_day", "memo", "qty", None, None]
    assert m.matched == {"State Name": "state", "금액": "amount", "On-Day": "on_day", "비고": "memo", "QTY": "qty"}
    assert [u["header"] for u in m.unknown] == ["qtyy", "zzz"]
    assert "qty" in m.unknown[0]["candidates"]
    assert m.missing == ["group_id", "flag"]


def test_header_matching_strict_takes_names_only(project):
    table = project.schema["tables"]["entries"]
    m = match_header(table, ["id", "State Name"], strict=True)
    assert m.columns == ["id", None] and m.unknown[0]["header"] == "State Name"


def test_header_matching_requires_primary_key_and_rejects_two_headers_for_one_column(project):
    table = project.schema["tables"]["entries"]
    m = match_header(table, ["memo", "note"])
    assert any("기본 키 열이 머리글에 없음" in e for e in m.errors)
    assert any("같은 열 'memo'" in e for e in m.errors)


def test_unnamed_columns_are_dropped_when_lenient(project):
    table = project.schema["tables"]["entries"]
    m = match_header(table, ["id", "", "memo"])
    assert m.columns == ["id", None, "memo"] and m.dropped == [""] and not m.unknown
    assert match_header(table, ["id", ""], strict=True).unknown[0]["header"] == ""


def test_header_overrides_map_or_drop(project):
    table = project.schema["tables"]["entries"]
    m = match_header(table, ["id", "x1", "x2"], overrides={"x1": "memo", "x2": None})
    assert m.columns == ["id", "memo", None] and m.dropped == ["x2"] and not m.unknown


def test_unknown_columns_raise_with_candidates(project, tmp_path):
    path = tmp_path / "in.csv"
    path.write_text("id,memmo\nE7,x\n", encoding="utf-8")
    with pytest.raises(IntakeError) as exc:
        Intake(CsvParser(path, "in.csv"), "entries", project.schema["tables"]["entries"])
    assert exc.value.detail == "unknown_columns"
    assert exc.value.extra["columns"][0]["header"] == "memmo" and "memo" in exc.value.extra["columns"][0]["candidates"]


# ---------------------------------------------------------------- 파서

def test_csv_parser_reads_cp949_and_sniffs_delimiter(tmp_path):
    path = tmp_path / "excel.csv"
    path.write_bytes("id;비고\r\nE1;가나다\r\n".encode("cp949"))
    p = CsvParser(path, "excel.csv")
    assert p.encoding == "cp949" and p.header() == ["id", "비고"]
    assert list(p.rows()) == [(2, ["E1", "가나다"])] and p.delimiter == ";"


def test_csv_parser_tab_and_bom(tmp_path):
    path = tmp_path / "t.csv"
    path.write_bytes("﻿id\tmemo\nE1\t\"a\tb\"\n".encode("utf-8"))
    p = CsvParser(path, "t.csv")
    assert p.header() == ["id", "memo"] and p.encoding == "utf-8" and p.delimiter == "\t"
    assert list(p.rows()) == [(2, ["E1", "a\tb"])]


def test_strict_parser_refuses_non_utf8(tmp_path):
    path = tmp_path / "x.csv"
    path.write_bytes("id,memo\nE1,가\n".encode("cp949"))
    p = CsvParser(path, "x.csv", strict=True)
    with pytest.raises(IntakeError):
        p.header()


def test_rows_parser_requires_same_columns_and_reports_positions(project):
    with pytest.raises(IntakeError) as exc:
        RowsParser([{"id": "E1"}, {"id": "E2", "memo": "x"}])
    assert exc.value.detail == "invalid_rows" and exc.value.errors == ["rows[1]: 열 집합이 rows[0]과 다름"]
    intake = Intake(RowsParser([{"id": "E1", "amount": "x"}, {"id": "bad", "amount": 1.5}]), "entries",
                    project.schema["tables"]["entries"])
    out = list(intake.rows())
    assert out[0][2] is None and out[1][2] is None
    assert intake.report.errors == ["rows[0].amount: 숫자가 아님 'x'", "rows[1].id: 형식 불일치 'bad' (pattern ^E\\d+$)"]


def test_json_values_become_canonical_text(project):
    intake = Intake(RowsParser([{"id": "E1", "amount": 2, "flag": True, "memo": None, "qty": "1,000"}]), "entries",
                    project.schema["tables"]["entries"])
    [(at, values, typed)] = list(intake.rows())
    assert intake.header == ["id", "amount", "flag", "qty", "memo"]
    assert values == ["E1", "2", "true", "1000", ""] and typed[4] is None


# ---------------------------------------------------------------- 배치 이름

def test_batch_names_are_read_in_numeric_order_and_bad_names_rejected(tmp_path):
    folder = tmp_path / "t"
    folder.mkdir()
    for name in ("10-b.csv", "2.csv.gz", "000001-20261120T0910Z.csv", "_removed", ".hidden.csv"):
        (folder / name).mkdir() if name == "_removed" else (folder / name).write_text("id\n")
    files, errors = batches.list_batches(tmp_path, "t")
    assert [f.name for f in files] == ["000001-20261120T0910Z.csv", "2.csv.gz", "10-b.csv"] and not errors
    (folder / "z.csv").write_text("id\n")
    (folder / "0002-again.csv").write_text("id\n")
    files, errors = batches.list_batches(tmp_path, "t")
    assert any("z.csv" in e and "이름 규칙" in e for e in errors)
    assert any("0002-again.csv" in e and "겹침" in e for e in errors)


def test_next_name_counts_removed_and_merged(tmp_path):
    folder = tmp_path / "t"
    (folder / "_removed").mkdir(parents=True)
    (folder / "_merged" / "20261120T0000Z").mkdir(parents=True)
    (folder / "000003-a.csv.gz").write_text("")
    (folder / "_removed" / "000007-b.csv.gz").write_text("")
    (folder / "_merged" / "20261120T0000Z" / "000009-c.csv.gz").write_text("")
    assert batches.next_name(tmp_path, "t").startswith("000010-")
    assert batches.stamp_of("000010-20261121T0102Z.csv.gz").isoformat() == "2026-11-21T01:02:00+00:00"
    assert batches.stamp_of("12-hand.csv") is None


def test_canonical_writer_is_deterministic_and_hash_is_of_content(tmp_path):
    outs = []
    for i in range(2):
        w = batches.CanonicalWriter(tmp_path / f"{i}.csv.gz")
        w.header(["id", "memo"])
        w.row(["E1", "a,b"])
        w.row(["E2", ""])
        outs.append((w.close(), (tmp_path / f"{i}.csv.gz").read_bytes()))
    assert outs[0] == outs[1]
    text = gzip.decompress(outs[0][1])
    assert text == b'id,memo\nE1,"a,b"\nE2,\n'
    plain = tmp_path / "p.csv"
    plain.write_bytes(text)
    assert batches.content_hash(tmp_path / "0.csv.gz") == batches.content_hash(plain) == outs[0][0]


# ---------------------------------------------------------------- 설정 검사

def _schema_with(tmp_path, edit) -> list[str]:
    import yaml

    root = tmp_path / "p"
    shutil.copytree(FIXTURES / "ledger", root / "ledger")
    path = root / "ledger" / "schema.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(doc)
    path.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    try:
        config.load_project("ledger", root)
    except config.ConfigError as exc:
        return exc.errors
    return []


def test_config_checks_new_keys(tmp_path):
    def bad_default(doc):
        doc["tables"]["entries"]["columns"]["qty"]["default"] = "many"
        doc["tables"]["entries"]["columns"]["state"]["default"] = "gone"
        doc["tables"]["entries"]["columns"]["id"]["default"] = "E0"
    errors = _schema_with(tmp_path, bad_default)
    assert any("columns.qty.default: 정수가 아님" in e for e in errors), errors
    assert any("columns.state.default: 허용되지 않은 값" in e for e in errors), errors
    assert any("columns.id.default: 기본 키 열에는" in e for e in errors), errors

    def bad_alias(doc):
        doc["tables"]["entries"]["columns"]["memo"]["aliases"] = ["qty"]
        doc["tables"]["entries"]["columns"]["flag"]["aliases"] = ["State Name"]
    errors = _schema_with(tmp_path / "2", bad_alias)
    assert any("aliases[0]: 'qty'는 같은 테이블의 열 이름" in e for e in errors), errors
    assert any("'State Name'가 열" in e and "aliases와 겹침" in e for e in errors), errors

    def conflict_on_replace(doc):
        doc["tables"]["groups"]["on_conflict"] = "skip"
    assert any("on_conflict는 mode: append" in e for e in _schema_with(tmp_path / "3", conflict_on_replace))

    def bad_mode(doc):
        doc["tables"]["groups"]["mode"] = "stream"
    assert any("tables.groups.mode" in e for e in _schema_with(tmp_path / "4", bad_mode))


def test_examples_keep_replace_mode():
    for name in ("devtracker", "sales-sample", "chart-gallery"):
        project = config.load_project(name, Path(__file__).parent.parent / "projects")
        assert all((t.get("mode") or "replace") == "replace" for t in project.schema["tables"].values())


def test_data_revision_ignores_tabs_but_not_rules(project):
    from dashkit.loader.record import data_revision
    import copy

    base = data_revision(project.schema, project.dashboard)
    dash = copy.deepcopy(project.dashboard)
    dash["tabs"][0]["label"] = "다른 이름"
    dash["datasets"]["entries"]["labels"]["amount"] = "합"
    assert data_revision(project.schema, dash) == base
    schema = copy.deepcopy(project.schema)
    schema["tables"]["entries"]["on_conflict"] = "skip"
    assert data_revision(schema, project.dashboard) != base
    schema = copy.deepcopy(project.schema)
    schema["tables"]["entries"]["columns"]["amount"]["min"] = 1
    assert data_revision(schema, project.dashboard) != base
