"""배치·필터 확장(보강설계서 4-3)과 표식(3-5)의 번들 생성·설정 검사. DB와 Superset 없이 돈다.

형태의 근거는 contracts/superset-notes.md 11절(표식), 12절(배치 노드와 필터)이다.
"""
from __future__ import annotations

import pytest
import yaml

from dashkit import config
from dashkit.config import load_project
from dashkit.sync import bundle

from test_config import errors_of, make_project

VIEW_COLUMNS = {
    "things": [("id", "text"), ("owner_id", "text"), ("phase", "text"), ("score", "integer"),
               ("begins", "date"), ("ends", "date"), ("owner_label", "text")],
    "owners": [("id", "text"), ("label", "text")],
}


def rich(d: dict) -> None:
    """픽스처 대시보드에 4-3의 요소를 모두 넣는다."""
    main = d["dashboards"]["main"]
    main["cross_filters"] = True
    main["layout"] = [
        [{"header": "머리"}],
        ["c_total", {"markdown": "**글**", "width": 3}],
        {"tabs": [
            {"label": "하나", "rows": [[{"chart": "c_line", "width": 8, "height": 30}, {"markdown": "옆 글"}]]},
            {"label": "둘", "rows": [[{"header": "탭 안 머리"}], ["c_table", "c_raw"], [{"markdown": "혼자", "height": 12}]]},
        ]},
    ]
    main["filters"] = [
        {"key": "f_when", "label": "기간", "type": "time_range", "default": "Last quarter"},
        {"key": "f_score", "label": "점수", "type": "number_range", "dataset": "things", "column": "score", "default": [2, 8]},
        {"key": "f_phase", "label": "단계", "dataset": "things", "column": "phase", "default": ["a", "b"]},
        {"key": "f_owner", "label": "담당", "type": "select", "dataset": "things", "column": "owner_label",
         "multi": False, "parent": "f_phase", "charts": ["c_table", "c_line"]},
        {"key": "f_grain", "label": "단위", "type": "time_grain", "dataset": "things", "default": "P1M", "charts": ["c_line"]},
    ]


def build(tmp_path, mutate=None, env=None) -> dict[str, dict]:
    project = load_project("basic", make_project(tmp_path, mutate_dash=mutate))
    files = bundle.build_bundle(project, VIEW_COLUMNS, env=env or {})
    return {path: yaml.safe_load(text) for path, text in files.items()}


def errs(tmp_path, mutate) -> list[str]:
    def m(d):
        rich(d)
        mutate(d["dashboards"]["main"])
    return errors_of(tmp_path, mutate_dash=m)


# ---------------------------------------------------------------- 기존 설정은 그대로

def test_plain_dashboard_keeps_its_shape(tmp_path):
    """새 기능을 쓰지 않으면 자리표시 chartId는 0, scope는 비어 있고, 시간 조건도 붙지 않는다."""
    files = build(tmp_path)
    dash = files["dashboards/main.yaml"]
    pos = dash["position"]
    assert pos["GRID_ID"]["children"] == ["ROW-1", "ROW-2"]
    assert {n["meta"]["chartId"] for n in pos.values() if isinstance(n, dict) and n["type"] == "CHART"} == {0}
    assert {n["type"] for n in pos.values() if isinstance(n, dict)} == {"ROOT", "GRID", "ROW", "CHART"}
    flt = dash["metadata"]["native_filter_configuration"][0]
    assert flt["filterType"] == "filter_select"
    assert flt["defaultDataMask"] == {"extraFormData": {}, "filterState": {}, "ownState": {}}
    assert flt["cascadeParentIds"] == [] and flt["scope"] == {"rootPath": ["ROOT_ID"], "excluded": []}
    assert dash["metadata"]["cross_filters_enabled"] is False
    assert "adhoc_filters" not in files["charts/c_line.yaml"]["params"]


def test_filter_type_defaults_to_select(tmp_path):
    def m(d):
        del d["dashboards"]["main"]["filters"][0]["type"]
    p = load_project("basic", make_project(tmp_path, mutate_dash=m))
    flt = p.dashboard["dashboards"]["main"]["filters"][0]
    assert flt["type"] == "select" and flt["multi"] is True
    assert p.dashboard["dashboards"]["main"]["cross_filters"] is False


# ---------------------------------------------------------------- 배치

def test_layout_nodes(tmp_path):
    pos = build(tmp_path, rich)["dashboards/main.yaml"]["position"]
    grid = ["ROOT_ID", "GRID_ID"]
    assert pos["GRID_ID"]["children"] == ["HEADER-1", "ROW-2", "TABS-3"]
    assert pos["HEADER-1"] == {
        "type": "HEADER", "id": "HEADER-1", "children": [], "parents": grid,
        "meta": {"text": "머리", "headerSize": "MEDIUM_HEADER", "background": "BACKGROUND_TRANSPARENT"},
    }
    assert pos["ROW-2"]["children"] == ["CHART-c_total", "MARKDOWN-2-2"]
    assert pos["MARKDOWN-2-2"] == {
        "type": "MARKDOWN", "id": "MARKDOWN-2-2", "children": [], "parents": grid + ["ROW-2"],
        "meta": {"code": "**글**", "width": 3, "height": 50},      # 높이 기본: 같은 행 차트의 높이
    }
    assert pos["CHART-c_total"]["meta"]["width"] == 9             # 남은 칸
    assert pos["TABS-3"] == {"type": "TABS", "id": "TABS-3", "children": ["TAB-3-1", "TAB-3-2"], "parents": grid, "meta": {}}
    assert pos["TAB-3-1"] == {"type": "TAB", "id": "TAB-3-1", "children": ["ROW-3-1-1"],
                              "parents": grid + ["TABS-3"], "meta": {"text": "하나"}}
    assert pos["TAB-3-2"]["children"] == ["HEADER-3-2-1", "ROW-3-2-2", "ROW-3-2-3"]
    tab2 = grid + ["TABS-3", "TAB-3-2"]
    assert pos["HEADER-3-2-1"]["parents"] == tab2
    assert pos["ROW-3-2-2"]["parents"] == tab2
    assert pos["CHART-c_table"]["parents"] == tab2 + ["ROW-3-2-2"]
    assert pos["CHART-c_line"]["parents"] == grid + ["TABS-3", "TAB-3-1", "ROW-3-1-1"]
    assert pos["MARKDOWN-3-1-1-2"]["meta"] == {"code": "옆 글", "width": 4, "height": 30}
    assert pos["MARKDOWN-3-2-3-1"]["meta"] == {"code": "혼자", "width": 12, "height": 12}
    # 모든 노드의 children이 실제 노드이고, parents의 마지막이 그 노드를 children으로 가진다
    for node_id, node in pos.items():
        if not isinstance(node, dict):
            continue
        assert all(c in pos for c in node["children"]), node_id
        if node.get("parents"):
            assert node_id in pos[node["parents"][-1]]["children"], node_id


def test_markdown_alone_gets_text_height(tmp_path):
    def m(d):
        d["dashboards"]["main"]["layout"].append([{"markdown": "끝"}])
    pos = build(tmp_path, m)["dashboards/main.yaml"]["position"]
    assert pos["MARKDOWN-3-1"]["meta"] == {"code": "끝", "width": 12, "height": 20}


def test_layout_rows_skips_headers_and_tabs(tmp_path):
    p = load_project("basic", make_project(tmp_path, mutate_dash=rich))
    d = p.dashboard["dashboards"]["main"]
    assert [[sorted(i) for i in row] for row in config.layout_rows(d)] == [[["chart", "height", "width"], ["height", "markdown", "width"]]]
    assert [b["kind"] for b in config.layout_blocks(d)] == ["header", "row", "tabs"]


@pytest.mark.parametrize("mutate, where, reason", [
    (lambda m: m["layout"].__setitem__(0, [{"header": "머리"}, "c_total"]), "layout[0][0]", "한 행에 혼자"),
    (lambda m: m["layout"][2]["tabs"][0]["rows"].append(["c_total"]), "dashboards.main.layout:", "2번 배치됨"),
    (lambda m: m["layout"][2]["tabs"][1]["rows"].__setitem__(1, ["c_table"]), "dashboards.main.layout:", "'c_raw'가 layout에 배치되지 않음"),
    (lambda m: m["layout"][2]["tabs"][1]["rows"][1].append("ghost"), "layout[2].tabs[1].rows[1][2]", "차트 'ghost'가"),
    (lambda m: m["layout"][2]["tabs"][0]["rows"].__setitem__(0, [{"chart": "c_line", "width": 8}, {"markdown": "글", "width": 5}]),
     "layout[2].tabs[0].rows[0]:", "12칸을 넘음"),
])
def test_layout_errors(tmp_path, mutate, where, reason):
    found = errs(tmp_path, mutate)
    assert any(e.startswith("dashboard.yaml:") and where in e and reason in e for e in found), found


@pytest.mark.parametrize("item", [
    {"header": ""}, {"header": "머리", "width": 4}, {"markdown": "글", "chart": "c_total"}, {"markdown": "글", "width": 13},
])
def test_layout_item_schema_errors(tmp_path, item):
    found = errs(tmp_path, lambda m: m["layout"].__setitem__(0, [item]))
    assert any("dashboards.main.layout[0]" in e for e in found), found


def test_tabs_need_label_and_rows(tmp_path):
    found = errs(tmp_path, lambda m: m["layout"].__setitem__(2, {"tabs": [{"label": "하나"}]}))
    assert any("dashboards.main.layout[2]" in e for e in found), found


# ---------------------------------------------------------------- 필터

def filters_of(tmp_path) -> dict[str, dict]:
    dash = build(tmp_path, rich)["dashboards/main.yaml"]
    assert dash["metadata"]["cross_filters_enabled"] is True
    return {f["id"].removeprefix("NATIVE_FILTER-"): f for f in dash["metadata"]["native_filter_configuration"]}


def test_filter_types_and_defaults(tmp_path):
    f = filters_of(tmp_path)
    ds = config.object_uuid("basic", "dataset", "things")
    assert [x["filterType"] for x in f.values()] == ["filter_time", "filter_range", "filter_select", "filter_select", "filter_timegrain"]

    assert f["f_when"]["targets"] == [{}]
    assert f["f_when"]["controlValues"] == {"enableEmptyFilter": False}
    assert f["f_when"]["defaultDataMask"] == {
        "extraFormData": {"time_range": "Last quarter"}, "filterState": {"value": "Last quarter"}, "ownState": {}}

    assert f["f_score"]["targets"] == [{"column": {"name": "score"}, "datasetUuid": ds}]
    assert f["f_score"]["defaultDataMask"] == {
        "extraFormData": {"filters": [{"col": "score", "op": ">=", "val": 2}, {"col": "score", "op": "<=", "val": 8}]},
        "filterState": {"label": "2 ≤ x ≤ 8", "value": [2, 8]}, "ownState": {}}

    assert f["f_phase"]["controlValues"]["multiSelect"] is True
    assert f["f_phase"]["defaultDataMask"] == {
        "extraFormData": {"filters": [{"col": "phase", "op": "IN", "val": ["a", "b"]}]},
        "filterState": {"label": "a, b", "value": ["a", "b"]}, "ownState": {}}

    assert f["f_owner"]["controlValues"]["multiSelect"] is False
    assert f["f_owner"]["defaultDataMask"] == {"extraFormData": {}, "filterState": {}, "ownState": {}}

    assert f["f_grain"]["targets"] == [{"datasetUuid": ds}]
    assert f["f_grain"]["defaultDataMask"] == {
        "extraFormData": {"time_grain_sqla": "P1M"}, "filterState": {"label": "Month", "value": ["P1M"]}, "ownState": {}}


def test_default_forms(tmp_path):
    def m(d):
        rich(d)
        flt = d["dashboards"]["main"]["filters"]
        flt[1]["default"] = [None, 8]      # 한쪽만
        flt[2]["default"] = "a"            # 값 하나
    f = {x["id"]: x for x in build(tmp_path, m)["dashboards/main.yaml"]["metadata"]["native_filter_configuration"]}
    assert f["NATIVE_FILTER-f_score"]["defaultDataMask"]["extraFormData"] == {"filters": [{"col": "score", "op": "<=", "val": 8}]}
    assert f["NATIVE_FILTER-f_score"]["defaultDataMask"]["filterState"] == {"label": "x ≤ 8", "value": [None, 8]}
    assert f["NATIVE_FILTER-f_phase"]["defaultDataMask"]["filterState"] == {"label": "a", "value": ["a"]}


def test_no_yaml_anchors_in_bundle(tmp_path):
    project = load_project("basic", make_project(tmp_path, mutate_dash=rich))
    text = bundle.build_bundle(project, VIEW_COLUMNS, env={})["dashboards/main.yaml"]
    assert "&id" not in text and "*id" not in text


def test_parent_and_scope(tmp_path):
    files = build(tmp_path, rich)
    dash = files["dashboards/main.yaml"]
    f = {x["id"]: x for x in dash["metadata"]["native_filter_configuration"]}
    assert f["NATIVE_FILTER-f_owner"]["cascadeParentIds"] == ["NATIVE_FILTER-f_phase"]
    assert f["NATIVE_FILTER-f_phase"]["cascadeParentIds"] == []
    # charts를 쓴 필터가 있으면 차트마다 다른 자리표시 번호(charts 순서, 1부터)를 주고 나열하지 않은 차트를 제외한다
    numbers = {n["id"]: n["meta"]["chartId"] for n in dash["position"].values() if isinstance(n, dict) and n["type"] == "CHART"}
    assert numbers == {"CHART-c_total": 1, "CHART-c_line": 2, "CHART-c_table": 3, "CHART-c_raw": 4}
    assert f["NATIVE_FILTER-f_owner"]["scope"] == {"rootPath": ["ROOT_ID"], "excluded": [1, 4]}
    assert f["NATIVE_FILTER-f_grain"]["scope"]["excluded"] == [1, 3, 4]
    assert f["NATIVE_FILTER-f_when"]["scope"]["excluded"] == []
    assert all("chartsInScope" not in x for x in f.values())


def test_time_range_filter_adds_temporal_condition_to_charts(tmp_path):
    """대시보드의 시간 범위 필터는 차트에 시간 조건(No filter)이 있어야 적용된다(superset-notes.md 12절)."""
    files = build(tmp_path, rich)
    cond = {"expressionType": "SIMPLE", "clause": "WHERE", "operator": "TEMPORAL_RANGE", "comparator": "No filter"}
    # 차트가 참조한 시간 열(x: begins). 없으면 뷰의 첫 시간 열(begins)
    assert files["charts/c_line.yaml"]["params"]["adhoc_filters"] == [{**cond, "subject": "begins"}]
    assert files["charts/c_total.yaml"]["params"]["adhoc_filters"] == [{**cond, "subject": "begins"}]
    # 원본(viz_type) 차트의 params는 건드리지 않는다. 시간 열이 없는 데이터셋의 차트에도 붙지 않는다
    assert files["charts/c_raw.yaml"]["params"] == {"viz_type": "word_cloud", "series": "label"}


def test_temporal_condition_follows_scope_and_where(tmp_path):
    def m(d):
        rich(d)
        main = d["dashboards"]["main"]
        main["filters"][0]["charts"] = ["c_table"]
        main["charts"][2]["where"] = "score > 1"
    files = build(tmp_path, m)
    assert "adhoc_filters" not in files["charts/c_total.yaml"]["params"]
    kinds = [f["expressionType"] for f in files["charts/c_table.yaml"]["params"]["adhoc_filters"]]
    assert kinds == ["SQL", "SIMPLE"]      # where 조건 뒤에 시간 조건


def test_referenced_columns_cover_new_filter_types(tmp_path):
    p = load_project("basic", make_project(tmp_path, mutate_dash=rich))
    refs = config.referenced_columns(p)["things"]
    assert "dashboards.main.filters[1].column" in refs["score"]
    assert "dashboards.main.filters[3].column" in refs["owner_label"]
    # time_range, time_grain 필터는 열을 참조하지 않는다
    assert not any("filters[0]" in p_ or "filters[4]" in p_ for paths in refs.values() for p_ in paths)


def test_missing_filter_column_is_reported(tmp_path):
    found = errs(tmp_path, lambda m: m["filters"][1].update(dataset="owners", column="no_such_col"))
    assert any("filters[1].column" in e and "no_such_col" in e for e in found), found


@pytest.mark.parametrize("mutate, where, reason", [
    (lambda f: f[1].pop("column"), "filters[1]", "'column' is a required property"),
    (lambda f: f[2].pop("dataset"), "filters[2]", "'dataset' is a required property"),
    (lambda f: f[4].pop("dataset"), "filters[4]", "'dataset' is a required property"),
    (lambda f: f[0].update(dataset="things"), "filters[0].dataset", "time_range 필터에는 dataset을 쓸 수 없음"),
    (lambda f: f[0].update(column="begins"), "filters[0].column", "time_range 필터에는 column을 쓸 수 없음"),
    (lambda f: f[1].update(multi=False), "filters[1].multi", "number_range 필터에는 multi을 쓸 수 없음"),
    (lambda f: f[4].update(column="begins"), "filters[4].column", "time_grain 필터에는 column을 쓸 수 없음"),
    (lambda f: f[1].update(parent="f_phase"), "filters[1].parent", "number_range 필터에는 parent을 쓸 수 없음"),
    (lambda f: f[1].update(dataset="nope"), "filters[1].dataset", "데이터셋 'nope'이 없음"),
    (lambda f: f[3].update(parent="f_owner"), "filters[3].parent", "자기 자신"),
    (lambda f: f[3].update(parent="f_nope"), "filters[3].parent", "필터 'f_nope'가 이 대시보드의 filters에 없음"),
    (lambda f: f[2].update(parent="f_owner"), "filters[2].parent", "순환"),
    (lambda f: f[3].update(charts=["c_table", "ghost"]), "filters[3].charts[1]", "차트 'ghost'가 이 대시보드의 charts에 없음"),
    (lambda f: f[3].update(charts=[]), "filters[3].charts", "should be non-empty"),
    (lambda f: f[0].update(default=["a"]), "filters[0].default", "시간 범위 문자열"),
    (lambda f: f[1].update(default=[8, 2]), "filters[1].default", "최소가 최대보다"),
    (lambda f: f[1].update(default=5), "filters[1].default", "[최소, 최대]"),
    (lambda f: f[2].update(default=[]), "filters[2].default", "값 또는 값 목록"),
    (lambda f: f[3].update(default=["a", "b"]), "filters[3].default", "값 하나여야"),
    (lambda f: f[4].update(default="PT1H"), "filters[4].default", "P1D, P1W, P1M, P3M, P1Y 중 하나"),
    (lambda f: f[4].update(type="grain"), "filters[4].type", "is not one of"),
    (lambda f: f.append(dict(f[0])), "filters[5].key", "필터 키 'f_when'가 중복됨"),
])
def test_filter_errors(tmp_path, mutate, where, reason):
    found = errs(tmp_path, lambda m: mutate(m["filters"]))
    assert any(e.startswith("dashboard.yaml:") and where in e and reason in e for e in found), found


# ---------------------------------------------------------------- 표식(3-5)

KIND_OF = {"databases": "database", "datasets": "dataset", "charts": "chart", "dashboards": "dashboard"}


def test_every_object_carries_the_mark(tmp_path):
    files = build(tmp_path)
    seen = set()
    for path, doc in files.items():
        kind = KIND_OF.get(path.split("/")[0])
        if kind is None:
            assert "is_managed_externally" not in doc
            continue
        key = "main" if kind == "database" else path.rsplit("/", 1)[1].removesuffix(".yaml")
        assert doc["is_managed_externally"] is True, path
        assert doc["external_url"] == f"dashkit://basic/{kind}/{key}", path
        assert list(doc)[-2:] == ["is_managed_externally", "external_url"], path
        seen.add(kind)
    assert seen == set(KIND_OF.values())


@pytest.mark.parametrize("value, locked", [("0", False), ("false", False), ("1", True), ("", True)])
def test_sync_lock_ui_only_changes_the_lock(tmp_path, value, locked):
    """SYNC_LOCK_UI=0은 화면 편집 차단(is_managed_externally)만 끈다. 키는 항상 쓰고 external_url은 그대로다."""
    files = build(tmp_path, env={"SYNC_LOCK_UI": value})
    docs = [doc for path, doc in files.items() if path.split("/")[0] in KIND_OF]
    assert {doc["is_managed_externally"] for doc in docs} == {locked}
    assert all(doc["external_url"].startswith("dashkit://basic/") for doc in docs)
