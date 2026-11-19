"""plan의 차이 계산(dashkit/plan.py의 순수 함수)과 행 지문(dashkit/loader/record.py). DB 없이 돈다.
DB와 함께 보는 검사는 tests/test_plan.py."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from dashkit import plan
from dashkit.config import load_project
from dashkit.loader import record
from dashkit.loader.validate import _parse
from dashkit.sync.bundle import build_bundle

FIXTURES = Path(__file__).parent / "fixtures"

THINGS_NOW = [("id", "text"), ("owner_id", "text"), ("phase", "text"), ("score", "bigint"),
              ("begins", "date"), ("ends", "date")]
OWNERS_NOW = [("id", "text"), ("label", "text")]
THINGS_VIEW = THINGS_NOW + [("owner_label", "text")]


def project_copy(tmp_path: Path, mutate_dash=None, mutate_schema=None):
    root = tmp_path / "p"
    shutil.copytree(FIXTURES / "basic", root / "basic")
    for fname, fn in (("dashboard.yaml", mutate_dash), ("schema.yaml", mutate_schema)):
        if fn:
            p = root / "basic" / fname
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
            fn(doc)
            p.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return load_project("basic", root)


def table_of(columns: dict[str, str], pk: list[str]) -> dict:
    """schema.yaml의 정규화된 테이블 정의 모양."""
    return {"primary_key": pk, "columns": {c: {"type": t} for c, t in columns.items()}}


def loaded(table: dict, rows: list[list]) -> tuple[dict, list[tuple[str, str]]]:
    """그 정의와 행으로 적재했을 때 남는 기록(스냅샷의 테이블 항목, 행 지문)."""
    snap = record.snapshot("basic", {"tables": {"t": table}}, {"datasets": {}}, {"t": len(rows)})
    return snap["tables"]["t"], record.fingerprints(table, rows)


def snapshot_of(project, name: str | None = None) -> dict:
    counts = {t: 0 for t in project.schema["tables"]}
    return json.loads(json.dumps(record.snapshot(name or project.name, project.schema, project.dashboard, counts)))


# ---------------------------------------------------------------- 지문

@pytest.mark.parametrize("type_, a, b", [
    ("number", "1", "1.0"),
    ("number", "1.50", "1.5"),
    ("number", "0", "-0.00"),
    ("number", "100", "1E+2"),
    ("integer", "7", "+7"),
    ("boolean", "true", "TRUE"),
    ("timestamp", "2026-01-01T09:00:00+09:00", "2026-01-01T00:00:00+00:00"),
    ("date", "2026-01-05", "2026-01-05"),
])
def test_same_value_in_another_notation_has_the_same_fingerprint(type_, a, b):
    table = table_of({"id": "text", "v": type_}, ["id"])
    fa = record.fingerprints(table, [["k", _parse(a, {"type": type_})]])
    fb = record.fingerprints(table, [["k", _parse(b, {"type": type_})]])
    assert fa == fb


@pytest.mark.parametrize("type_, a, b", [
    ("number", "1", "1.01"),
    ("boolean", "true", "false"),
    ("date", "2026-01-05", "2026-01-06"),
    ("timestamp", "2026-01-01T09:00:00+09:00", "2026-01-01T09:00:00+00:00"),
    ("text", "1", "1.0"),
    ("text", "a", "A"),
])
def test_different_values_have_different_fingerprints(type_, a, b):
    table = table_of({"id": "text", "v": type_}, ["id"])
    fa = record.fingerprints(table, [["k", _parse(a, {"type": type_})]])
    fb = record.fingerprints(table, [["k", _parse(b, {"type": type_})]])
    assert fa[0][0] == fb[0][0] and fa[0][1] != fb[0][1]


def test_empty_value_differs_from_empty_looking_values():
    table = table_of({"id": "text", "v": "text", "w": "text"}, ["id"])
    hashes = {record.fingerprints(table, [["k", *vw]])[0][1] for vw in ([None, "x"], ["", "x"], ["x", None], ["null", "x"])}
    assert len(hashes) == 4


def test_fingerprint_ignores_column_order_but_not_column_names():
    a = table_of({"id": "text", "n": "integer", "memo": "text"}, ["id"])
    b = table_of({"memo": "text", "id": "text", "n": "integer"}, ["id"])
    renamed = table_of({"id": "text", "n": "integer", "note": "text"}, ["id"])
    assert record.fingerprints(a, [["k", 1, "x"]]) == record.fingerprints(b, [["x", "k", 1]])
    assert record.fingerprints(a, [["k", 1, "x"]])[0][1] != record.fingerprints(renamed, [["k", 1, "x"]])[0][1]


def test_fingerprint_holds_no_original_value():
    """기록에 들어가는 것은 해시 둘뿐이다. 기본 키 값도 원본으로 남지 않는다."""
    table = table_of({"id": "text", "secret": "text"}, ["id"])
    rows = [["someone@example.com", "아주 비밀스러운 값"]]
    (key, digest), = record.fingerprints(table, rows)
    assert re.fullmatch(r"[0-9a-f]{64}", key) and re.fullmatch(r"[0-9a-f]{64}", digest) and key != digest
    assert key == hashlib.sha256('["someone@example.com"]'.encode()).hexdigest()
    # 사람이 읽는 키는 CSV 쪽 행에서만 만들고 저장하지 않는다
    assert record.labels(table, rows) == {key: "someone@example.com"}
    composite = table_of({"region": "text", "day": "date", "n": "integer"}, ["region", "day"])
    assert list(record.labels(composite, [["east", date(2026, 1, 2), 3]]).values()) == ["east, 2026-01-02"]
    # 같은 키는 표기가 달라도 같은 해시다
    numeric = table_of({"n": "number"}, ["n"])
    assert record.fingerprints(numeric, [[Decimal("1.0")]])[0][0] == record.fingerprints(numeric, [[Decimal("1")]])[0][0]


def test_snapshot_shape(tmp_path):
    project = project_copy(tmp_path)
    snap = record.snapshot(project.name, project.schema, project.dashboard, {"owners": 2, "things": 5})
    assert json.loads(json.dumps(snap)) == snap
    assert snap["version"] == 1 and snap["project"] == "basic"
    assert snap["tables"]["owners"] == {"columns": [["id", "text"], ["label", "text"]], "primary_key": ["id"], "rows": 2}
    assert snap["tables"]["things"]["columns"] == [list(c) for c in THINGS_NOW]
    assert snap["datasets"]["owners"]["kind"] == "table" and snap["datasets"]["things"]["kind"] == "sql"
    assert len(snap["datasets"]["things"]["definition"]) == 64


# ---------------------------------------------------------------- 행

def test_rows_added_removed_changed():
    table = table_of({"id": "text", "label": "text", "score": "integer"}, ["id"])
    old = record.fingerprints(table, [["a", "하나", 1], ["b", "둘", 2], ["c", "셋", 3]])
    new_rows = [["a", "하나", 1], ["b", "둘!", 2], ["d", "넷", 4], ["e", "다섯", 5]]
    d = plan.diff_rows(record.fingerprints(table, new_rows), old, names=record.labels(table, new_rows))
    assert (d["added"], d["removed"], d["changed"], d["unchanged"]) == (2, 1, 1, 1)
    # 삭제 행은 CSV에 없어 원본 키를 알 수 없다. 개수만 센다
    assert d["keys"] == {"added": ["d", "e"], "removed": [], "changed": ["b"]}
    # 이름표가 없으면 예시 키도 없다(해시를 보여 주지 않는다)
    assert plan.diff_rows(record.fingerprints(table, new_rows), old)["keys"] == {"added": [], "removed": [], "changed": []}
    assert d["comparable"] is True and d["reason"] is None


def test_rows_composite_primary_key():
    table = table_of({"region": "text", "day": "date", "amount": "integer"}, ["region", "day"])
    old = record.fingerprints(table, [["east", date(2026, 1, 1), 10], ["east", date(2026, 1, 2), 20], ["west", date(2026, 1, 1), 30]])
    new_rows = [["east", date(2026, 1, 1), 10], ["east", date(2026, 1, 2), 25], ["west", date(2026, 1, 2), 40]]
    d = plan.diff_rows(record.fingerprints(table, new_rows), old, names=record.labels(table, new_rows))
    assert (d["added"], d["removed"], d["changed"], d["unchanged"]) == (1, 1, 1, 1)
    assert d["keys"]["added"] == ["west, 2026-01-02"]
    assert d["keys"]["changed"] == ["east, 2026-01-02"] and d["keys"]["removed"] == []


def test_rows_with_duplicate_keys_are_not_silently_overwritten():
    """같은 키가 두 번 나오면 덮어쓰지 않고 비교 불가(전부 추가·전부 삭제)로 돌려준다."""
    d = plan.diff_rows([('["x"]', "h1")], [('["x"]', "h1"), ('["x"]', "h2")])
    assert d["comparable"] is False and (d["added"], d["removed"], d["changed"], d["unchanged"]) == (1, 2, 0, 0)
    d = plan.diff_rows([('["x"]', "h1"), ('["x"]', "h1")], [('["x"]', "h1")])
    assert d["comparable"] is False and (d["added"], d["removed"]) == (2, 1)


def test_rows_key_samples_are_capped():
    d = plan.diff_rows([(f"k{i}", "h") for i in range(100)], [], names={f"k{i}": str(i) for i in range(100)}, max_keys=5)
    assert d["added"] == 100 and len(d["keys"]["added"]) == 5


# ---------------------------------------------------------------- 열, 테이블

def test_columns_added_removed_retyped_reordered():
    old = [("id", "text"), ("score", "bigint"), ("memo", "text")]
    d = plan.diff_columns([("id", "text"), ("score", "numeric"), ("age_days", "integer")], old)
    assert d == {"added": ["age_days"], "removed": ["memo"],
                 "retyped": [{"column": "score", "from": "bigint", "to": "numeric"}], "reordered": False}
    assert plan.diff_columns([("score", "bigint"), ("id", "text"), ("memo", "text")], old)["reordered"] is True
    assert plan.diff_columns(old, old) == {"added": [], "removed": [], "retyped": [], "reordered": False}


def test_table_diff_new_table_counts_everything_as_added():
    table = table_of({"id": "text", "label": "text"}, ["id"])
    d = plan.table_diff("owners", table, [["o1", "가"], ["o2", "나"]], None)
    assert (d["status"], d["added"], d["removed"], d["changed"], d["rows"]) == ("new", 2, 0, 0, 2)


def test_table_diff_against_the_load_record():
    table = table_of({"id": "text", "label": "text"}, ["id"])
    recorded, prints = loaded(table, [["o1", "가"], ["o2", "나"]])
    assert plan.table_diff("owners", table, [["o1", "가"], ["o2", "나"]], recorded, prints)["status"] == "unchanged"
    d = plan.table_diff("owners", table, [["o1", "가"], ["o2", "다"], ["o3", "라"]], recorded, prints)
    assert (d["status"], d["added"], d["removed"], d["changed"], d["comparable"]) == ("changed", 1, 0, 1, True)


def test_table_diff_column_order_alone_keeps_rows_comparable():
    old = table_of({"id": "text", "label": "text"}, ["id"])
    new = table_of({"label": "text", "id": "text"}, ["id"])
    recorded, prints = loaded(old, [["o1", "가"], ["o2", "나"]])
    d = plan.table_diff("owners", new, [["가", "o1"], ["다", "o2"]], recorded, prints)
    assert d["comparable"] is True and (d["added"], d["removed"], d["changed"]) == (0, 0, 1)
    assert d["columns"]["reordered"] is True and d["status"] == "changed"


def test_all_columns_renamed_still_reports_the_old_row_count():
    """리뷰 P2: 공통 열이 하나도 없어도 기존 행 수가 0으로 줄지 않는다(id 열 2행 → code 열 1행이면 +1 −2)."""
    old = table_of({"id": "text"}, ["id"])
    new = table_of({"code": "text"}, ["code"])
    recorded, prints = loaded(old, [["a"], ["b"]])
    d = plan.table_diff("t", new, [["a"]], recorded, prints)
    assert d["comparable"] is False and "열 구성" in d["reason"]
    assert (d["status"], d["added"], d["removed"], d["changed"], d["unchanged"]) == ("changed", 1, 2, 0, 0)
    assert d["columns"]["added"] == ["code"] and d["columns"]["removed"] == ["id"]


def test_changed_primary_key_does_not_hide_deletions():
    """리뷰 P2: 기본 키가 id인 (a,x),(b,x)에서 기본 키를 label로 바꾸고 CSV에 (b,x)만 두면 삭제가 드러난다.
    새 키(label)로 맞추면 지난 두 행이 같은 키 x라 하나가 가려진다. 기본 키 정의가 다르면 맞추지 않는다."""
    old = table_of({"id": "text", "label": "text"}, ["id"])
    new = table_of({"id": "text", "label": "text"}, ["label"])
    recorded, prints = loaded(old, [["a", "x"], ["b", "x"]])
    d = plan.table_diff("t", new, [["b", "x"]], recorded, prints)
    assert d["comparable"] is False and "기본 키" in d["reason"]
    assert (d["status"], d["added"], d["removed"], d["changed"], d["unchanged"]) == ("changed", 1, 2, 0, 0)
    assert "전부 추가·전부 삭제" in plan._table_text(d)


def test_retyped_column_makes_rows_incomparable():
    old = table_of({"id": "text", "n": "integer"}, ["id"])
    new = table_of({"id": "text", "n": "text"}, ["id"])
    recorded, prints = loaded(old, [["a", 1]])
    d = plan.table_diff("t", new, [["a", "1"]], recorded, prints)
    assert d["comparable"] is False and (d["added"], d["removed"]) == (1, 1)
    assert d["columns"]["retyped"] == [{"column": "n", "from": "bigint", "to": "text"}]


def test_enum_and_text_are_the_same_structure():
    old = table_of({"id": "text", "phase": "text"}, ["id"])
    new = {"primary_key": ["id"], "columns": {"id": {"type": "text"}, "phase": {"type": "enum", "values": ["a"]}}}
    recorded, prints = loaded(old, [["k", "a"]])
    assert plan.table_diff("t", new, [["k", "a"]], recorded, prints)["status"] == "unchanged"


def test_record_of_another_project_means_full_replacement():
    table = table_of({"id": "text"}, ["id"])
    recorded, prints = loaded(table, [["a"], ["b"], ["c"]])
    d = plan.table_diff("t", table, [["a"]], recorded, prints, replaced_project="before")
    assert d["comparable"] is False and "before" in d["reason"] and (d["added"], d["removed"]) == (1, 3)
    gone = plan.removed_table("old_t", recorded)
    assert (gone["status"], gone["removed"], gone["columns"]["removed"]) == ("removed", 3, ["id"])


def test_unrecorded_table_carries_only_the_csv_row_count():
    d = plan.unrecorded_table("t", 12)
    assert (d["status"], d["rows"], d["added"], d["removed"], d["changed"], d["comparable"]) == ("unrecorded", 12, None, None, None, False)
    json.dumps(d)


def test_table_columns_follow_schema_types(tmp_path):
    table = project_copy(tmp_path).schema["tables"]["things"]
    assert plan.table_columns(table) == THINGS_NOW


# ---------------------------------------------------------------- 뷰 열

VIEWS = {"things": THINGS_VIEW, "owners": OWNERS_NOW}


def test_views_unchanged_when_nothing_moves(tmp_path):
    project = project_copy(tmp_path)
    predicted = plan.predict_view_columns(project, snapshot_of(project), VIEWS)
    assert predicted["owners"] == {"columns": OWNERS_NOW, "reason": None}
    assert predicted["things"] == {"columns": THINGS_VIEW, "reason": None}      # 정의와 읽는 것이 그대로면 지금 열 그대로
    assert {v["dataset"]: v["status"] for v in plan.diff_views(predicted, VIEWS)} == {
        "things": "unchanged", "owners": "unchanged"}


def test_table_dataset_column_added_is_known_before_apply(tmp_path):
    before = project_copy(tmp_path / "a")

    def add_column(schema):
        schema["tables"]["owners"]["columns"]["age_days"] = {"type": "integer"}

    project = project_copy(tmp_path / "b", mutate_schema=add_column)
    predicted = plan.predict_view_columns(project, snapshot_of(before), VIEWS)
    by_key = {v["dataset"]: v for v in plan.diff_views(predicted, VIEWS)}
    assert by_key["owners"]["status"] == "changed" and by_key["owners"]["columns"]["added"] == ["age_days"]
    # things의 쿼리는 raw.owners를 읽는다. 그 구조가 바뀌므로 적용해 봐야 안다
    assert by_key["things"]["status"] == "unknown" and "raw.owners" in by_key["things"]["reason"]


def test_view_sql_changed_query_is_unknown(tmp_path):
    before = project_copy(tmp_path / "a")

    def widen(dash):
        dash["datasets"]["things"]["sql"] = "SELECT t.*, 1 AS extra FROM raw.things t"

    project = project_copy(tmp_path / "b", mutate_dash=widen)
    predicted = plan.predict_view_columns(project, snapshot_of(before), VIEWS)
    assert predicted["things"] == {"columns": None, "reason": "쿼리가 바뀜"}
    # 다듬으면 같은 글(앞뒤 공백, 끝의 ;)은 바뀐 것이 아니다
    def cosmetic(dash):
        dash["datasets"]["things"]["sql"] = "\n  " + dash["datasets"]["things"]["sql"].strip() + " ;\n"
    project = project_copy(tmp_path / "c", mutate_dash=cosmetic)
    assert plan.predict_view_columns(project, snapshot_of(before), VIEWS)["things"]["columns"] == THINGS_VIEW


def test_view_sql_is_unknown_when_an_earlier_view_it_names_changes(tmp_path):
    def chain(dash):
        dash["datasets"]["later"] = {"sql": "SELECT id FROM dash.things"}
    before = project_copy(tmp_path / "a", mutate_dash=chain)
    views = {**VIEWS, "later": [("id", "text")]}
    assert plan.predict_view_columns(before, snapshot_of(before), views)["later"]["columns"] == [("id", "text")]

    def chain_and_change(dash):
        chain(dash)
        dash["datasets"]["things"]["sql"] = "SELECT t.id FROM raw.things t"
    project = project_copy(tmp_path / "b", mutate_dash=chain_and_change)
    predicted = plan.predict_view_columns(project, snapshot_of(before), views)
    assert predicted["things"]["columns"] is None
    assert predicted["later"]["columns"] is None and "dash.things" in predicted["later"]["reason"]


def test_view_unknown_without_a_usable_record(tmp_path):
    project = project_copy(tmp_path)
    p = plan.predict_view_columns(project, None, VIEWS)
    assert p["things"] == {"columns": None, "reason": "지난 적재 기록 없음"} and p["owners"]["columns"] == OWNERS_NOW
    views = plan.diff_views(plan.predict_view_columns(project, None, {}), {"gone": [("id", "text")]})
    assert {v["dataset"]: v["status"] for v in views} == {"things": "unknown", "owners": "new", "gone": "removed"}
    # 기록은 있는데 그 데이터셋이 새것이면(기록에 없거나 뷰가 없음) 알 수 없다
    snap = snapshot_of(project)
    del snap["datasets"]["things"]
    assert plan.predict_view_columns(project, snap, VIEWS)["things"]["reason"] == "새 데이터셋"


# ---------------------------------------------------------------- 번들

COLUMNS = {"things": THINGS_VIEW, "owners": OWNERS_NOW}


def bundle(project, columns=COLUMNS, ts=None) -> dict[str, str]:
    return build_bundle(project, columns, env={}, timestamp=ts or datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_bundle_same_config_is_no_change_and_timestamp_is_ignored(tmp_path):
    project = project_copy(tmp_path)
    old = bundle(project, ts=datetime(2025, 1, 1, tzinfo=timezone.utc))
    new = bundle(project, ts=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert old["metadata.yaml"] != new["metadata.yaml"]
    d = plan.diff_bundles(new, old)
    assert d["first"] is False and d["database"] == "unchanged" and d["other"] == []
    for kind in ("datasets", "charts", "dashboards"):
        assert (d[kind]["added"], d[kind]["changed"], d[kind]["removed"], d[kind]["unknown"]) == ([], [], [], [])
    assert (d["datasets"]["unchanged"], d["charts"]["unchanged"], d["dashboards"]["unchanged"]) == (2, 4, 1)


def test_bundle_first_apply_lists_everything_as_added(tmp_path):
    d = plan.diff_bundles(bundle(project_copy(tmp_path)), None)
    assert d["first"] is True
    assert d["charts"]["added"] == ["c_line", "c_raw", "c_table", "c_total"]
    assert d["dashboards"]["added"] == ["main"] and d["datasets"]["added"] == ["owners", "things"]


def test_bundle_chart_added_changed_removed_and_layout_summary(tmp_path):
    old = bundle(project_copy(tmp_path / "a"))

    def edit(dash):
        d = dash["dashboards"]["main"]
        d["charts"] = [c for c in d["charts"] if c["key"] != "c_raw"]                       # 삭제
        d["charts"].append({"key": "c_new", "title": "새 차트", "viz": "big_number_total",   # 추가
                            "dataset": "things", "metric": "total_score"})
        next(c for c in d["charts"] if c["key"] == "c_table")["row_limit"] = 9               # 옵션 변경
        next(c for c in d["charts"] if c["key"] == "c_total")["title"] = "총합"              # 제목 변경
        d["layout"] = [["c_total", {"chart": "c_line", "width": 8}], ["c_table", "c_new"]]

    d = plan.diff_bundles(bundle(project_copy(tmp_path / "b", mutate_dash=edit)), old)
    assert d["charts"]["added"] == ["c_new"] and d["charts"]["removed"] == ["c_raw"]
    assert d["charts"]["changed"] == [{"key": "c_table", "detail": "옵션 변경"},
                                      {"key": "c_total", "detail": "제목 변경"}]
    assert d["dashboards"]["changed"] == [{"key": "main", "detail": "배치 변경"}]
    assert d["datasets"]["changed"] == []


def test_bundle_dashboard_filter_and_title_summary(tmp_path):
    old = bundle(project_copy(tmp_path / "a"))

    def edit(dash):
        d = dash["dashboards"]["main"]
        d["title"] = "새 제목"
        d["filters"].append({"key": "f_owner", "label": "담당", "type": "select", "dataset": "things",
                             "column": "owner_label"})
        d["cross_filters"] = True

    d = plan.diff_bundles(bundle(project_copy(tmp_path / "b", mutate_dash=edit)), old)
    assert d["dashboards"]["changed"] == [{"key": "main", "detail": "제목 변경, 필터 변경, 교차 필터 변경"}]
    assert d["charts"]["changed"] == []


def test_bundle_dataset_column_and_metric_summary(tmp_path):
    project = project_copy(tmp_path / "a")
    old = bundle(project)
    wider = {**COLUMNS, "things": THINGS_VIEW + [("age_days", "integer")]}
    d = plan.diff_bundles(bundle(project, wider), old)
    assert d["datasets"]["changed"] == [{"key": "things", "detail": "열 추가: age_days"}]

    def edit(dash):
        dash["datasets"]["things"]["metrics"]["avg_score"] = "AVG(score)"

    d = plan.diff_bundles(bundle(project_copy(tmp_path / "b", mutate_dash=edit)), old)
    assert d["datasets"]["changed"] == [{"key": "things", "detail": "지표 변경"}]


def test_bundle_unknown_files_are_not_reported_as_changes(tmp_path):
    project = project_copy(tmp_path)
    old = bundle(project)
    new = bundle(project, {**COLUMNS, "things": []})
    d = plan.diff_bundles(new, old, {"datasets/dashkit/things.yaml": "이유", "charts/c_line.yaml": "이유"})
    assert d["datasets"]["unknown"] == [{"key": "things", "reason": "이유"}]
    assert d["datasets"]["changed"] == []
    assert [u["key"] for u in d["charts"]["unknown"]] == ["c_line"]


def test_read_bundle_dir_round_trip(tmp_path):
    from dashkit.sync.bundle import write_build, zip_bundle

    files = bundle(project_copy(tmp_path / "p"))
    assert plan.read_bundle_dir(tmp_path / "none") is None
    write_build(files, zip_bundle(files), tmp_path / "build")
    assert plan.read_bundle_dir(tmp_path / "build" / "bundle") == files


def test_other_project_in_state_file_is_reported(tmp_path):
    assert plan.read_other_project(tmp_path, "basic") is None                       # 상태 파일 없음
    (tmp_path / "managed.json").write_text(json.dumps(
        {"version": 1, "project": "basic", "objects": {"chart": ["a"], "dashboard": ["b"]}}), encoding="utf-8")
    assert plan.read_other_project(tmp_path, "basic") is None                       # 같은 프로젝트
    assert plan.read_other_project(tmp_path, "another") == {"project": "basic", "objects": 2}
    (tmp_path / "managed.json").write_text("{깨진", encoding="utf-8")
    assert plan.read_other_project(tmp_path, "another") is None


# ---------------------------------------------------------------- 출력

def _plan(tables, views, superset, changed=True) -> dict:
    return {"project": "basic", "changed": changed, "load_record": {"exists": True, "project": "basic"},
            "data": tables, "views": views, "superset": superset}


def test_format_and_json(tmp_path):
    project = project_copy(tmp_path / "a")
    old = bundle(project)

    def edit(dash):
        d = dash["dashboards"]["main"]
        d["charts"].append({"key": "c_new", "title": "새 차트", "viz": "big_number_total",
                            "dataset": "things", "metric": "total_score"})
        d["layout"].append(["c_new"])

    edited = project_copy(tmp_path / "b", mutate_dash=edit)
    wider = {**COLUMNS, "things": THINGS_VIEW + [("age_days", "integer")]}
    owners, things = edited.schema["tables"]["owners"], edited.schema["tables"]["things"]
    rec_owners, prints_owners = loaded(owners, [["o1", "가"], ["o2", "나"]])
    rec_things, prints_things = loaded(things, [])
    tables = [
        plan.table_diff("owners", owners, [["o1", "가"], ["o2", "다"], ["o3", "라"]], rec_owners, prints_owners),
        plan.table_diff("things", things, [], rec_things, prints_things),
    ]
    predicted = {"things": {"columns": wider["things"], "reason": None},
                 "owners": {"columns": OWNERS_NOW, "reason": None}}
    views = plan.diff_views(predicted, COLUMNS)
    superset = {**plan.diff_bundles(bundle(edited, wider), old), "replaces": {"project": "before", "objects": 7}}
    result = _plan(tables, views, superset)
    json.dumps(result)   # 구조화된 결과는 JSON으로 바꿀 수 있어야 한다
    text = plan.format_plan(result)
    lines = text.splitlines()
    assert lines[0] == "데이터   owners  +1행 −0행 ~1행"
    assert lines[1] == "         변경 없음: things"
    assert lines[2] == "뷰       dash.things  열 추가: age_days"
    assert "Superset 데이터셋 ~1 (things: 열 추가: age_days)" in lines
    assert "         차트 +1 (c_new)" in lines
    assert "         대시보드 ~1 (main: 배치 변경)" in lines
    assert "         지금 적용된 프로젝트 'before'의 객체 7개 삭제" in lines
    assert lines[-1].startswith("→ 바뀌는 것이 있음")


def test_format_no_change_and_no_record(tmp_path):
    project = project_copy(tmp_path)
    files = bundle(project)
    same = _plan([], plan.diff_views({k: {"columns": v, "reason": None} for k, v in COLUMNS.items()}, COLUMNS),
                 plan.diff_bundles(files, files), changed=False)
    text = plan.format_plan(same)
    assert "뷰       변경 없음 (뷰 2개)" in text and "Superset 변경 없음" in text
    assert text.splitlines()[-1] == "→ 바뀌는 것이 없음"

    predicted = plan.predict_view_columns(project, None, {})
    first = _plan([plan.unrecorded_table("owners", 1), plan.unrecorded_table("things", 4)],
                  plan.diff_views(predicted, {}), plan.diff_bundles(files, None))
    lines = plan.format_plan(first).splitlines()
    assert lines[0] == "데이터   지난 적재 기록 없음: 비교할 수 없음(한 번 apply하면 생김). CSV 행 수만 표시"
    assert lines[1] == "         owners  1행" and lines[2] == "         things  4행"
    assert "뷰       dash.things  적용해야 알 수 있음 (지난 적재 기록 없음)" in lines
    assert "Superset 처음 적용 (지난 번들이 없음)" in lines


def test_format_incomparable_and_new_table():
    table = table_of({"code": "text"}, ["code"])
    recorded, prints = loaded(table_of({"id": "text"}, ["id"]), [["a"], ["b"]])
    tables = [plan.table_diff("t", table, [["a"]], recorded, prints), plan.table_diff("fresh", table, [["a"]], None)]
    text = plan.format_plan(_plan(tables, [], plan.diff_bundles({}, {})))
    assert "t  +1행 −2행 ~0행  열 추가: code  열 삭제: id  (열 구성이 지난 적재와 달라 행을 맞출 수 없음: 전부 추가·전부 삭제로 셈)" in text
    assert "fresh  +1행 −0행 ~0행  (새 테이블)" in text


def test_number_fingerprint_keeps_full_precision():
    """Decimal 컨텍스트 정밀도(28자리)를 넘는 값도 반올림 없이 구분한다(리뷰 지적 회귀)."""
    a = Decimal("1.12345678901234567890123456781")
    b = Decimal("1.12345678901234567890123456782")
    assert record.canonical(a) == "1.12345678901234567890123456781"
    assert record.canonical(a) != record.canonical(b)
    # 기본 키로 써도 서로 다른 키가 된다
    assert record.row_key(["n"], ["n"], [a]) != record.row_key(["n"], ["n"], [b])
    assert record.row_hash(["n"], [a]) != record.row_hash(["n"], [b])
    # 표기만 다른 같은 값은 여전히 같다
    for text, expected in (("1.50", "1.5"), ("1.0", "1"), ("100", "100"), ("1E+3", "1000"), ("-0.0", "0"),
                           ("0.000", "0"), ("-2.500", "-2.5"), ("12345678901234567890123456789012.10", "12345678901234567890123456789012.1")):
        assert record.canonical(Decimal(text)) == expected, text
