"""Studio API(contracts/studio.md): 프로젝트 마법사, 설정 변경 연산, 저장 방식, 데이터 교체, apply 스트림, 레지스트리.

스택 없이 돈다(DB·Superset이 필요한 흐름은 tests/test_studio_integration.py). 저장소의 projects/는 읽기만 하고,
모든 쓰기는 임시 폴더의 사본에 한다.
"""
from __future__ import annotations

import json
import threading
import time

import pytest
import yaml

from studio_client import ROOT, Client, fresh_client

from dashkit import config
from dashkit.loader.validate import validate_csv
from dashkit.studio import app as studio_app
from dashkit.studio import jobs, ops, store

SALES = sorted((ROOT / "projects" / "sales-sample" / "data").glob("*.csv"))
CONFIG = "/studio/api/projects/devtracker/config"


@pytest.fixture
def client(tmp_path, monkeypatch) -> Client:
    return fresh_client(tmp_path, monkeypatch)


def tree(root) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def patch(client: Client, *op_list: dict, project: str = "devtracker", **extra):
    return client.patch(f"/studio/api/projects/{project}/config", json={"ops": list(op_list), **extra})


def dashboard_text(client: Client, project: str = "devtracker") -> str:
    return (client.projects / project / "dashboard.yaml").read_text(encoding="utf-8")


def check_project(client: Client, name: str) -> config.Project:
    """`make check`와 같은 검사(config.load_project) + CSV 검증."""
    project = config.load_project(name, client.projects)
    _data, errors = validate_csv(project.schema, project.data_dir, config.table_order(project))
    assert errors == []
    return project


# ---------------------------------------------------------------- 목록, 설정 읽기

def test_projects_list_and_current(client):
    body = client.get("/studio/api/projects").json
    assert [p["name"] for p in body["projects"]] == ["chart-gallery", "devtracker", "sales-sample"]
    assert all(p["valid"] and p["errors"] == 0 and p["title"] for p in body["projects"])
    assert body["current"] is None and body["web_url"] == "http://localhost:8080"

    (client.state / "embed.json").write_text(json.dumps({"version": 1, "project": "sales-sample", "dashboards": {}}))
    assert client.get("/studio/api/projects").json["current"] == "sales-sample"

    # 깨진 프로젝트도 목록에 나온다(고칠 수 있게). 파일을 고치면 다음 요청에 반영된다
    path = client.projects / "devtracker" / "dashboard.yaml"
    good = path.read_text(encoding="utf-8")
    path.write_text(good.replace("icon: grid", "icon: no-such-icon"), encoding="utf-8")
    row = next(p for p in client.get("/studio/api/projects").json["projects"] if p["name"] == "devtracker")
    assert row["valid"] is False and row["errors"] == 1 and row["title"]
    path.write_text(good + "\n", encoding="utf-8")
    row = next(p for p in client.get("/studio/api/projects").json["projects"] if p["name"] == "devtracker")
    assert row["valid"] is True


def test_config_is_the_file_as_written(client):
    body = client.get(CONFIG).json
    raw = yaml.safe_load(dashboard_text(client))
    assert body["name"] == "devtracker" and body["valid"] is True and body["errors"] == []
    # 기본값을 채우지 않은, 파일에 쓴 그대로(날짜만 문자열)
    assert body["dashboard"]["tabs"] == raw["tabs"]
    assert "height" not in body["dashboard"]["tabs"][0]["sections"][0]
    assert list(body["schema"]["tables"]) == list(yaml.safe_load(
        (client.projects / "devtracker" / "schema.yaml").read_text(encoding="utf-8"))["tables"])
    # DB가 없으면 table: 데이터셋의 열만 스키마에서 채운다
    assert body["columns_source"] == "schema"
    table_datasets = [k for k, d in raw["datasets"].items() if "table" in d]
    assert sorted(body["columns"]) == sorted(table_datasets) and table_datasets
    assert all(set(c) == {"name", "type"} for cols in body["columns"].values() for c in cols)
    tasks = body["tables"]["tasks"]
    assert tasks["rows"] == 16 and {"name": "id", "type": "text"} in tasks["columns"]
    assert isinstance(body["revision"], str) and len(body["revision"]) == 16
    # 데이터셋마다 지표 이름(차트 폼의 선택지). 지표가 없는 데이터셋은 빈 목록
    assert body["metrics"] == {k: list(d.get("metrics") or {}) for k, d in raw["datasets"].items()}
    assert any(body["metrics"].values()) and not all(body["metrics"].values())


def test_config_of_broken_project_reports_errors(client):
    path = client.projects / "devtracker" / "dashboard.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("dashboard: issues", "dashboard: nowhere"), encoding="utf-8")
    body = client.get(CONFIG).json
    assert body["valid"] is False and len(body["errors"]) == 1 and "nowhere" in body["errors"][0]
    assert body["dashboard"] is not None
    path.write_text("tabs: [", encoding="utf-8")
    body = client.get(CONFIG).json
    assert body["valid"] is False and body["dashboard"] is None and body["schema"] is not None
    assert body["metrics"] == {}


def test_registry(client):
    body = client.get("/studio/api/registry").json
    from dashkit import charts, widgets
    assert [c["viz"] for c in body["charts"]] == list(charts.registry())
    line = next(c for c in body["charts"] if c["viz"] == "line")
    assert line["label"] and line["viz_type"] and {"name", "kind", "required"} <= set(line["fields"][0])
    assert any(f["required"] for f in line["fields"])
    enums = [f for c in body["charts"] for f in c["fields"] if f["kind"] == "enum"]
    assert enums and all(isinstance(f["values"], list) and f["values"] for f in enums)
    assert [w["type"] for w in body["widgets"]] == list(widgets.registry())
    assert all(w["options"]["properties"] for w in body["widgets"])
    assert "grid" in body["icons"] and body["tones"] == ["primary", "good", "warn", "bad", "neutral"]
    # 위젯 옵션의 $ref는 모두 defs에서 풀 수 있다
    refs: set[str] = set()
    studio_app._refs([w["options"] for w in body["widgets"]] + list(body["defs"].values()), refs)
    assert refs and refs <= set(body["defs"]) and all(body["defs"][r] for r in refs)


# ---------------------------------------------------------------- 변경 연산

def test_all_5a_ops_in_one_request(client):
    r = patch(
        client,
        {"op": "app.update", "values": {"title": "새 제목", "logo_text": None}},
        {"op": "tab.add", "tab": {"key": "extra", "label": "추가", "icon": "flag"}, "index": 1},
        {"op": "section.add", "tab": "extra", "section": {"type": "superset", "dashboard": "docs", "height": 500}},
        {"op": "section.add", "tab": "extra", "index": 0, "section": {"type": "superset", "dashboard": "issues"}},
        {"op": "section.update", "tab": "extra", "index": 1, "values": {"height": None, "title": "문서들"}},
        {"op": "section.move", "tab": "extra", "index": 1, "to_index": 0},
        {"op": "section.move", "tab": "schedule", "index": 1, "to_tab": "extra", "to_index": 2},
        {"op": "tab.update", "key": "schedule", "values": {"label": "계획", "icon": "target"}},
        {"op": "tab.move", "key": "docs", "index": 0},
        {"op": "tab.remove", "key": "issues"},
    )
    assert r.status == 200, r.text
    body = r.json
    assert body["changed"] == ["dashboard.yaml"] and body["written"] == {"dashboard.yaml": "exact"}
    tabs = body["dashboard"]["tabs"]
    assert [t["key"] for t in tabs] == ["docs", "overview", "extra", "schedule"]
    assert body["dashboard"]["app"]["title"] == "새 제목" and "logo_text" not in body["dashboard"]["app"]
    extra = tabs[2]
    assert extra["label"] == "추가" and extra["icon"] == "flag"
    assert [s["type"] for s in extra["sections"]] == ["superset", "superset", "kanban"]
    assert extra["sections"][0] == {"type": "superset", "dashboard": "docs", "title": "문서들"}
    assert extra["sections"][1] == {"type": "superset", "dashboard": "issues"}
    assert tabs[3]["label"] == "계획" and tabs[3]["icon"] == "target" and [s["type"] for s in tabs[3]["sections"]] == ["gantt"]
    # 응답의 dashboard는 파일을 읽은 그대로이고, 파일은 make check를 통과한다
    assert body["dashboard"] == client.get(CONFIG).json["dashboard"]
    assert body["schema"] == client.get(CONFIG).json["schema"]
    project = check_project(client, "devtracker")
    assert [t["key"] for t in project.dashboard["tabs"]] == ["docs", "overview", "extra", "schedule"]


def test_saved_file_keeps_hand_written_comments(client):
    """Studio가 쓴 파일: 손으로 쓴 주석이 모두 남고, 바뀐 줄 외에는 그대로다(API를 거친 저장 전체에서)."""
    before = dashboard_text(client)
    r = patch(client, {"op": "tab.move", "key": "schedule", "index": 3},
              {"op": "section.update", "tab": "issues", "index": 0, "values": {"height": 900}})
    assert r.status == 200
    after = dashboard_text(client)
    old_lines, new_lines = before.split("\n"), after.split("\n")
    comments = [line for line in old_lines if "#" in line and '"#' not in line]
    assert comments and all(line in new_lines for line in comments)
    assert sorted(set(old_lines) - set(new_lines)) == ["    sections: [{ type: superset, dashboard: issues }]"]
    assert sorted(set(new_lines) - set(old_lines)) == ["    sections: [{ type: superset, dashboard: issues, height: 900 }]"]
    check_project(client, "devtracker")


def test_move_index_is_the_final_position(client):
    def keys():
        return [t["key"] for t in client.get(CONFIG).json["dashboard"]["tabs"]]

    assert keys() == ["overview", "schedule", "issues", "docs"]
    assert patch(client, {"op": "tab.move", "key": "overview", "index": 2}).status == 200
    assert keys() == ["schedule", "issues", "overview", "docs"]
    assert patch(client, {"op": "tab.move", "key": "docs", "index": 3}).json["changed"] == []      # 제자리
    assert patch(client, {"op": "section.move", "tab": "schedule", "index": 0, "to_index": 1}).status == 200
    sections = client.get(CONFIG).json["dashboard"]["tabs"][0]["sections"]
    assert [s["type"] for s in sections] == ["kanban", "gantt"]


def test_section_update_is_a_shallow_merge(client):
    r = patch(client, {"op": "section.update", "tab": "schedule", "index": 1,
                       "values": {"card": {"title": "title"}, "title": None}})
    assert r.status == 200
    kanban = r.json["dashboard"]["tabs"][1]["sections"][1]
    assert kanban["card"] == {"title": "title"} and "title" not in kanban          # card는 통째로 바뀐다
    assert kanban["group_by"] == "stage" and len(kanban["columns"]) == 4           # 다른 키는 그대로


@pytest.mark.parametrize("bad,reason", [
    ({"op": "tab.nope", "key": "docs"}, "모르는 연산"),
    ({"op": "chart.rename", "key": "doc_table", "to": "x"}, "모르는 연산"),
    ({"op": "tab.move", "key": "nope", "index": 0}, "탭 'nope'가 없음"),
    ({"op": "tab.move", "key": "docs", "index": 4}, "범위"),
    ({"op": "tab.move", "key": "docs", "index": -1}, "범위"),
    ({"op": "tab.move", "key": "docs", "index": "0"}, "정수"),
    ({"op": "tab.move", "key": "docs", "index": True}, "정수"),
    ({"op": "tab.move", "key": "docs"}, "`index`이 필요함"),
    ({"op": "tab.move", "key": "docs", "index": 0, "extra": 1}, "모르는 필드"),
    ({"op": "tab.add", "tab": {"key": "docs", "label": "x", "icon": "grid"}}, "이미 있음"),
    ({"op": "tab.add", "tab": {"key": "x", "label": "x"}}, "icon"),
    ({"op": "tab.add", "tab": {"key": "x", "label": "x", "icon": "grid", "sections": []}}, "모르는 키"),
    ({"op": "tab.update", "key": "docs", "values": {"key": "renamed"}}, "바꿀 수 없는 키"),
    ({"op": "tab.update", "key": "docs", "values": {"label": None}}, "지울 수 없음"),
    ({"op": "app.update", "values": {"datasets": {}}}, "바꿀 수 없는 키"),
    ({"op": "app.update", "values": []}, "객체"),
    ({"op": "section.add", "tab": "docs", "section": {"dashboard": "docs"}}, "section.type"),
    ({"op": "section.update", "tab": "docs", "index": 0, "values": {"type": "gantt"}}, "type은 바꿀 수 없음"),
    ({"op": "section.update", "tab": "docs", "index": 1, "values": {}}, "범위"),
    ({"op": "section.move", "tab": "docs", "index": 0, "to_index": 0, "to_tab": "nope"}, "탭 'nope'가 없음"),
    ({"op": "section.move", "tab": "docs", "index": 0, "to_index": 3, "to_tab": "schedule"}, "범위"),
    ({"op": "section.remove", "tab": "docs", "index": 5}, "범위"),
    ("tab.move", "객체"),
    ({"key": "docs"}, "객체"),
])
def test_invalid_op_saves_nothing(client, bad, reason):
    before = tree(client.projects)
    r = patch(client, {"op": "app.update", "values": {"title": "먼저 적용될 뻔한 변경"}}, bad)
    assert (r.status, r.json["detail"]) == (400, "invalid_op"), r.text
    assert r.json["op_index"] == 1 and r.json["message"].startswith("ops[1]") and reason in r.json["message"]
    assert r.json["errors"] == [r.json["message"]]
    assert tree(client.projects) == before
    assert client.get("/studio/api/changes").json == {"files": []}


@pytest.mark.parametrize("body", [{}, {"ops": []}, {"ops": "tab.move"}, {"ops": None}])
def test_ops_must_be_a_non_empty_list(client, body):
    r = client.patch(CONFIG, json=body)
    assert (r.status, r.json["detail"]) == (400, "invalid_op")


@pytest.mark.parametrize("op_list,expect", [
    ([{"op": "tab.add", "tab": {"key": "empty", "label": "빈 탭", "icon": "grid"}}], "tabs[4].sections"),      # 섹션 없는 탭
    ([{"op": "tab.add", "tab": {"key": "Bad Key", "label": "x", "icon": "grid"}},
      {"op": "section.add", "tab": "Bad Key", "section": {"type": "superset", "dashboard": "docs"}}], "tabs[4].key"),
    ([{"op": "tab.update", "key": "docs", "values": {"icon": "no-such-icon"}}], "tabs[3].icon"),
    ([{"op": "section.add", "tab": "docs", "section": {"type": "superset", "dashboard": "nowhere"}}], "nowhere"),
    ([{"op": "section.add", "tab": "docs", "section": {"type": "no_such_widget"}}], "tabs[3].sections[1]"),
    ([{"op": "section.add", "tab": "docs", "section": {"type": "kanban", "dataset": "tasks"}}], "tabs[3].sections[1]"),
    ([{"op": "section.update", "tab": "schedule", "index": 0, "values": {"fields": {"label": "no_such_column"}}}], "sections[0]"),
    ([{"op": "section.update", "tab": "docs", "index": 0, "values": {"height": 10}}], "height"),
    ([{"op": "section.remove", "tab": "docs", "index": 0}], "tabs[3].sections"),
    ([{"op": "app.update", "values": {"title": None}}], "app"),
    ([{"op": "app.update", "values": {"as_of": "not-a-date"}}], "as_of"),
    ([{"op": "tab.remove", "key": k} for k in ("overview", "schedule", "issues", "docs")], "tabs"),
])
def test_invalid_result_is_422_and_file_is_untouched(client, op_list, expect):
    before = tree(client.projects)
    r = patch(client, *op_list)
    assert (r.status, r.json["detail"]) == (422, "invalid_config"), r.text
    assert r.json["errors"] and all(e.startswith("dashboard.yaml:") for e in r.json["errors"])
    assert any(expect in e for e in r.json["errors"]), r.json["errors"]
    assert tree(client.projects) == before
    assert client.get("/studio/api/changes").json == {"files": []}


def test_save_is_atomic_and_leaves_no_temp_files(client, monkeypatch):
    """교체가 실패하면 원래 파일이 그대로이고 임시 파일이 남지 않는다."""
    import os
    before = tree(client.projects)
    real = os.replace

    def failing(src, dst):
        raise OSError("디스크 오류 흉내")

    monkeypatch.setattr(store.os, "replace", failing)
    r = patch(client, {"op": "tab.move", "key": "docs", "index": 0})
    assert r.status == 500 and r.json["detail"] == "internal_error" and "디스크" not in r.text
    monkeypatch.setattr(store.os, "replace", real)
    assert tree(client.projects) == before

    assert patch(client, {"op": "tab.move", "key": "docs", "index": 0}).status == 200
    names = [p.name for p in (client.projects / "devtracker").iterdir()]
    assert sorted(names) == ["dashboard.yaml", "data", "schema.yaml"]
    # 사본은 시스템 임시 폴더에 만들고 projects/ 아래에 두지 않는다
    assert sorted(p.name for p in client.projects.iterdir()) == ["chart-gallery", "devtracker", "sales-sample"]


def test_file_mode_is_kept(client):
    path = client.projects / "devtracker" / "dashboard.yaml"
    path.chmod(0o640)
    assert patch(client, {"op": "tab.move", "key": "docs", "index": 0}).status == 200
    assert path.stat().st_mode & 0o777 == 0o640


def test_revision_guards_against_edits_made_meanwhile(client):
    rev = client.get(CONFIG).json["revision"]
    r = patch(client, {"op": "tab.move", "key": "docs", "index": 0}, revision=rev)
    assert r.status == 200 and r.json["revision"] != rev
    before = tree(client.projects)
    stale = patch(client, {"op": "tab.move", "key": "docs", "index": 1}, revision=rev)      # 옛 revision
    assert (stale.status, stale.json["detail"]) == (409, "stale_config")
    assert tree(client.projects) == before
    assert patch(client, {"op": "tab.move", "key": "docs", "index": 1}, revision=r.json["revision"]).status == 200
    assert patch(client, {"op": "tab.move", "key": "docs", "index": 2}).status == 200      # 생략하면 보지 않는다


def test_changes_lists_files_this_process_wrote(client):
    assert client.get("/studio/api/changes").json == {"files": []}
    patch(client, {"op": "tab.move", "key": "docs", "index": 0})
    patch(client, {"op": "app.update", "values": {"title": "판매"}}, project="sales-sample")
    patch(client, {"op": "tab.move", "key": "docs", "index": 1})
    patch(client, {"op": "tab.move", "key": "docs", "index": 1})          # 바뀐 것 없음
    files = client.get("/studio/api/changes").json["files"]
    assert [f["path"] for f in files] == ["projects/sales-sample/dashboard.yaml", "projects/devtracker/dashboard.yaml"]
    assert all(f["at"].endswith("Z") for f in files)


def test_hand_edit_between_requests_is_respected(client):
    """Studio는 캐시 없이 매번 파일을 읽는다. 손으로 고친 내용 위에 연산을 적용한다."""
    path = client.projects / "devtracker" / "dashboard.yaml"
    path.write_text(dashboard_text(client).replace("label: 문서", "label: 손으로 고친 라벨   # 손 편집"), encoding="utf-8")
    r = patch(client, {"op": "tab.move", "key": "docs", "index": 0})
    assert r.status == 200 and r.json["dashboard"]["tabs"][0]["label"] == "손으로 고친 라벨"
    assert "    label: 손으로 고친 라벨   # 손 편집" in dashboard_text(client).split("\n")


def test_ops_registry_takes_new_ops(monkeypatch):
    """연산은 등록 표에 함수를 더하면 는다."""
    assert sorted(ops.OPS) == [
        "app.update", "chart.add", "chart.remove", "chart.update", "column.add", "column.update",
        "dashboard.add", "dashboard.update", "dataset.add", "dataset.remove", "dataset.update",
        "filter.add", "filter.move", "filter.remove", "filter.update", "layout.set",
        "section.add", "section.move", "section.remove", "section.update",
        "tab.add", "tab.move", "tab.remove", "tab.update", "table.add", "table.remove", "table.update"]
    monkeypatch.setattr(ops, "OPS", dict(ops.OPS))
    monkeypatch.setattr(ops, "OP_FILES", dict(ops.OP_FILES))

    @ops.op("filter.drop_first")
    def filter_drop_first(doc, item):
        doc["dashboards"][item["dashboard"]]["filters"].pop(0)

    text = (ROOT / "projects" / "devtracker" / "dashboard.yaml").read_text(encoding="utf-8")
    name = next(k for k, d in yaml.safe_load(text)["dashboards"].items() if len(d.get("filters", [])) > 1)
    first = yaml.safe_load(text)["dashboards"][name]["filters"][0]["key"]
    op_list = ops.check_ops([{"op": "filter.drop_first", "dashboard": name}])
    new, how = store.edit_yaml(text, lambda doc: ops.apply_ops(doc, op_list))
    assert how == "exact"
    gone = [line for line in text.split("\n") if line not in new.split("\n")]
    assert len(gone) == 1 and f"key: {first}," in gone[0]
    with pytest.raises(ValueError):
        ops.op("filter.drop_first")(filter_drop_first)


# ---------------------------------------------------------------- 5b: 대시보드 배치, 필터

GALLERY = "chart-gallery"


def raw_dashboard(client: Client, project: str = "devtracker") -> dict:
    return yaml.safe_load(dashboard_text(client, project))


def dashboard_with(doc: dict, need) -> str:
    """조건에 맞는 첫 대시보드의 키(예시 파일의 내용이 바뀌어도 맞게 내용으로 찾는다)."""
    return next(k for k, d in doc["dashboards"].items() if need(d))


def row_charts(row: list) -> list[str]:
    return [i if isinstance(i, str) else i.get("chart") for i in row]


def test_all_5b_ops_in_one_request(client):
    doc = raw_dashboard(client)
    name = dashboard_with(doc, lambda d: len(d.get("filters", [])) >= 2 and len(d["charts"]) == 1)
    d = doc["dashboards"][name]
    base = d["charts"][0]
    filters = [f["key"] for f in d["filters"]]
    chart = {**base, "key": "added_one", "title": "더한 차트", "description": None}
    r = patch(
        client,
        {"op": "dashboard.update", "dashboard": name, "values": {"title": "고친 제목", "cross_filters": True}},
        {"op": "chart.add", "dashboard": name, "chart": chart, "row": 0, "index": 0, "width": 5},
        {"op": "chart.add", "dashboard": name, "chart": {**base, "key": "added_two", "title": "둘째"}},
        {"op": "chart.update", "key": "added_two", "values": {"title": "둘째(고침)", "row_limit": 3, "search": None}},
        {"op": "layout.set", "dashboard": name,
         "layout": [[{"header": "머리"}], ["added_two"], [{"chart": "added_one", "width": 4}, {"chart": base["key"], "width": 8}]]},
        {"op": "filter.add", "dashboard": name, "index": 0,
         "filter": {"key": "f_when", "label": "기간", "type": "time_range", "default": "Last year", "charts": ["added_one", "added_two"]}},
        {"op": "filter.update", "dashboard": name, "key": filters[0], "values": {"label": "고친 라벨", "multi": None}},
        {"op": "filter.move", "dashboard": name, "key": filters[-1], "index": 0},
        {"op": "filter.remove", "dashboard": name, "key": filters[1]},
        {"op": "chart.remove", "key": "added_one"},
    )
    assert r.status == 200, r.text
    assert r.json["changed"] == ["dashboard.yaml"] and r.json["written"] == {"dashboard.yaml": "exact"}
    got = r.json["dashboard"]["dashboards"][name]
    assert got == raw_dashboard(client)["dashboards"][name]
    assert (got["title"], got["cross_filters"]) == ("고친 제목", True)
    assert [c["key"] for c in got["charts"]] == [base["key"], "added_two"]
    two = got["charts"][1]
    assert list(two)[:2] == ["key", "title"] and two["title"] == "둘째(고침)" and two["row_limit"] == 3 and "search" not in two
    # added_one이 빠졌다: 행에서, 필터의 charts에서
    assert got["layout"] == [[{"header": "머리"}], ["added_two"], [{"chart": base["key"], "width": 8}]]
    kept = [k for k in filters if k != filters[1]]
    assert [f["key"] for f in got["filters"]] == [filters[-1], "f_when", *[k for k in kept if k != filters[-1]]]
    when = next(f for f in got["filters"] if f["key"] == "f_when")
    assert when == {"key": "f_when", "label": "기간", "type": "time_range", "default": "Last year", "charts": ["added_two"]}
    first = next(f for f in got["filters"] if f["key"] == filters[0])
    assert first["label"] == "고친 라벨" and "multi" not in first
    # 다른 대시보드는 그대로
    assert {k: v for k, v in r.json["dashboard"]["dashboards"].items() if k != name} == \
        {k: v for k, v in doc["dashboards"].items() if k != name}
    check_project(client, "devtracker")


def test_chart_add_places_the_chart(client):
    doc = raw_dashboard(client)
    name = dashboard_with(doc, lambda d: len(d["layout"]) >= 2 and all(isinstance(r, list) for r in d["layout"]))
    d = doc["dashboards"][name]
    base = next(c for c in d["charts"] if c["key"] == row_charts(d["layout"][0])[0])
    last = len(d["layout"]) - 1
    r = patch(client,
              {"op": "chart.add", "dashboard": name, "chart": {**base, "key": "n_end", "title": "맨 아래"}},
              {"op": "chart.add", "dashboard": name, "chart": {**base, "key": "n_wide", "title": "너비"}, "width": 6},
              {"op": "chart.add", "dashboard": name, "chart": {**base, "key": "n_row", "title": "행 끝"}, "row": last},
              {"op": "chart.add", "dashboard": name, "chart": {**base, "key": "n_at", "title": "행 안"}, "row": last, "index": 1})
    assert r.status == 200 and r.json["written"] == {"dashboard.yaml": "exact"}, r.text
    got = r.json["dashboard"]["dashboards"][name]
    assert [c["key"] for c in got["charts"]] == [*[c["key"] for c in d["charts"]], "n_end", "n_wide", "n_row", "n_at"]
    assert got["layout"][:last] == d["layout"][:last]
    old = d["layout"][last]
    assert got["layout"][last] == [old[0], "n_at", *old[1:], "n_row"]
    assert got["layout"][last + 1:] == [["n_end"], [{"chart": "n_wide", "width": 6}]]
    check_project(client, "devtracker")


def test_chart_ops_find_the_chart_in_any_dashboard(client):
    doc = raw_dashboard(client)
    names = list(doc["dashboards"])
    key = doc["dashboards"][names[-1]]["charts"][0]["key"]
    r = patch(client, {"op": "chart.update", "key": key, "values": {"title": "다른 대시보드의 차트"}})
    assert r.status == 200
    assert r.json["dashboard"]["dashboards"][names[-1]]["charts"][0]["title"] == "다른 대시보드의 차트"
    # 차트 키는 프로젝트 전체에서 유일하다: 다른 대시보드의 키로는 더할 수 없다
    r = patch(client, {"op": "chart.add", "dashboard": names[0], "chart": {**doc["dashboards"][names[-1]]["charts"][0]}})
    assert (r.status, r.json["detail"]) == (400, "invalid_op") and "이미 있음" in r.json["message"]


def test_chart_remove_cleans_layout_tabs_and_filter_scopes(client):
    doc = raw_dashboard(client, GALLERY)
    name = dashboard_with(doc, lambda d: any(isinstance(e, dict) for e in d["layout"]) and
                          any(f.get("charts") for f in d.get("filters", [])))
    d = doc["dashboards"][name]
    group = next(e for e in d["layout"] if isinstance(e, dict))
    lone_tab = next(t for t in group["tabs"] if len(t["rows"]) == 1 and len(t["rows"][0]) == 1)
    in_tab = row_charts(lone_tab["rows"][0])[0]
    scoped = next(f for f in d["filters"] if f.get("charts") and len(f["charts"]) == 1)
    r = patch(client, {"op": "chart.remove", "key": in_tab}, {"op": "chart.remove", "key": scoped["charts"][0]}, project=GALLERY)
    assert r.status == 200 and r.json["written"] == {"dashboard.yaml": "exact"}, r.text
    got = r.json["dashboard"]["dashboards"][name]
    gone = {in_tab, scoped["charts"][0]}
    assert [c["key"] for c in got["charts"]] == [c["key"] for c in d["charts"] if c["key"] not in gone]
    new_group = next(e for e in got["layout"] if isinstance(e, dict))
    # 행이 없어진 탭은 탭째 없어진다. 남은 탭과 다른 행은 그대로
    assert new_group["tabs"] == [t for t in group["tabs"] if t is not lone_tab]
    flat = [c for e in got["layout"] for row in ([e] if isinstance(e, list) else [r for t in e["tabs"] for r in t["rows"]])
            for c in row_charts(row) if c]
    assert sorted(flat) == sorted(c["key"] for c in got["charts"])
    # 그 차트만 가리키던 필터는 charts 키가 없어진다(모든 차트에 적용)
    after = next(f for f in got["filters"] if f["key"] == scoped["key"])
    assert "charts" not in after and {k: v for k, v in scoped.items() if k != "charts"} == after
    check_project(client, GALLERY)

    # 제목(header) 행이 남은 탭은 남는다(차트 행만 없어진다)
    rest = [c for t in new_group["tabs"] for row in t["rows"] for c in row_charts(row) if c]
    r = patch(client, *[{"op": "chart.remove", "key": k} for k in rest], project=GALLERY)
    assert r.status == 200, r.text
    left = next(e for e in r.json["dashboard"]["dashboards"][name]["layout"] if isinstance(e, dict))
    assert [t["label"] for t in left["tabs"]] == [t["label"] for t in new_group["tabs"]]
    assert all("header" in i for t in left["tabs"] for row in t["rows"] for i in row)
    check_project(client, GALLERY)


def test_chart_remove_drops_a_tab_group_left_without_tabs(client):
    doc = raw_dashboard(client)
    name = dashboard_with(doc, lambda d: len(d["charts"]) >= 3)
    d = doc["dashboards"][name]
    keys = [c["key"] for c in d["charts"]]
    layout = [keys[2:], {"tabs": [{"label": "하나", "rows": [[keys[0]]]}, {"label": "둘", "rows": [[keys[1]]]}]}]
    r = patch(client, {"op": "layout.set", "dashboard": name, "layout": layout})
    assert r.status == 200 and r.json["dashboard"]["dashboards"][name]["layout"] == layout, r.text
    r = patch(client, {"op": "chart.remove", "key": keys[0]})
    assert r.status == 200 and r.json["dashboard"]["dashboards"][name]["layout"] == [keys[2:], {"tabs": [layout[1]["tabs"][1]]}]
    r = patch(client, {"op": "chart.remove", "key": keys[1]})
    assert r.status == 200 and r.json["dashboard"]["dashboards"][name]["layout"] == [keys[2:]], r.text
    check_project(client, "devtracker")


def test_layout_set_replaces_the_whole_layout(client):
    doc = raw_dashboard(client, GALLERY)
    name = dashboard_with(doc, lambda d: any(isinstance(e, dict) for e in d["layout"]))
    old = doc["dashboards"][name]["layout"]
    group = next(e for e in old if isinstance(e, dict))
    rows = [e for e in old if isinstance(e, list)]
    # 탭 순서를 뒤집고, 첫 탭 이름을 바꾸고, 최상위 행 순서를 뒤집는다
    new = [*reversed(rows), {"tabs": [{**t, "label": t["label"] + "!"} if i == 0 else t for i, t in enumerate(reversed(group["tabs"]))]}]
    r = patch(client, {"op": "layout.set", "dashboard": name, "layout": new}, project=GALLERY)
    assert r.status == 200 and r.json["written"] == {"dashboard.yaml": "exact"}, r.text
    assert r.json["dashboard"]["dashboards"][name]["layout"] == new
    check_project(client, GALLERY)
    # 같은 layout을 다시 보내면 바뀌는 것이 없다
    r = patch(client, {"op": "layout.set", "dashboard": name, "layout": new}, project=GALLERY)
    assert r.status == 200 and r.json["changed"] == []
    # 원래대로 되돌리면 값이 같다
    r = patch(client, {"op": "layout.set", "dashboard": name, "layout": old}, project=GALLERY)
    assert r.status == 200 and raw_dashboard(client, GALLERY) == doc


def test_filter_remove_refuses_while_another_filter_points_at_it(client):
    doc = raw_dashboard(client, GALLERY)
    name = dashboard_with(doc, lambda d: any("parent" in f for f in d.get("filters", [])))
    child = next(f for f in doc["dashboards"][name]["filters"] if "parent" in f)
    before = tree(client.projects)
    r = patch(client, {"op": "filter.remove", "dashboard": name, "key": child["parent"]}, project=GALLERY)
    assert (r.status, r.json["detail"], r.json["op_index"]) == (400, "invalid_op", 0)
    assert child["key"] in r.json["message"] and "parent" in r.json["message"]
    assert tree(client.projects) == before
    r = patch(client, {"op": "filter.update", "dashboard": name, "key": child["key"], "values": {"parent": None}},
              {"op": "filter.remove", "dashboard": name, "key": child["parent"]}, project=GALLERY)
    assert r.status == 200 and r.json["written"] == {"dashboard.yaml": "exact"}, r.text
    got = r.json["dashboard"]["dashboards"][name]["filters"]
    assert child["parent"] not in [f["key"] for f in got]
    assert "parent" not in next(f for f in got if f["key"] == child["key"])
    check_project(client, GALLERY)


def test_first_filter_creates_the_key_and_last_removal_drops_it(client):
    doc = raw_dashboard(client)
    name = dashboard_with(doc, lambda d: "filters" not in d)
    before = dashboard_text(client)
    r = patch(client, {"op": "filter.add", "dashboard": name, "filter": {"label": "기간", "key": "f_new", "type": "time_range",
                                                                        "default": None}})
    assert r.status == 200, r.text
    assert r.json["dashboard"]["dashboards"][name]["filters"] == [{"key": "f_new", "label": "기간", "type": "time_range"}]
    r = patch(client, {"op": "filter.remove", "dashboard": name, "key": "f_new"})
    assert r.status == 200 and "filters" not in r.json["dashboard"]["dashboards"][name]
    assert dashboard_text(client) == before


def test_5b_ops_mix_with_5a_ops(client):
    doc = raw_dashboard(client)
    name = next(iter(doc["dashboards"]))
    base = doc["dashboards"][name]["charts"][0]
    r = patch(
        client,
        {"op": "app.update", "values": {"title": "섞은 요청"}},
        {"op": "chart.add", "dashboard": name, "chart": {**base, "key": "mixed_chart", "title": "섞음"}},
        {"op": "tab.add", "tab": {"key": "extra", "label": "더한 탭", "icon": "grid"}, "index": 0},
        {"op": "section.add", "tab": "extra", "section": {"type": "superset", "dashboard": name}},
        {"op": "dashboard.update", "dashboard": name, "values": {"title": "섞은 대시보드"}},
        {"op": "tab.move", "key": doc["tabs"][-1]["key"], "index": 1},
    )
    assert r.status == 200 and r.json["written"] == {"dashboard.yaml": "exact"}, r.text
    got = r.json["dashboard"]
    assert got["app"]["title"] == "섞은 요청" and got["dashboards"][name]["title"] == "섞은 대시보드"
    assert [t["key"] for t in got["tabs"]][:2] == ["extra", doc["tabs"][-1]["key"]]
    assert got["dashboards"][name]["layout"][-1] == ["mixed_chart"]
    project = check_project(client, "devtracker")
    assert "mixed_chart" in [c["key"] for _d, c in config.charts(project)]


def _first(doc: dict, need=lambda d: True) -> str:
    return dashboard_with(doc, need)


@pytest.mark.parametrize("make,reason", [
    (lambda d, f: {"op": "dashboard.update", "dashboard": "nope", "values": {}}, "대시보드 'nope'가 없음"),
    (lambda d, f: {"op": "dashboard.update", "dashboard": d, "values": {"charts": []}}, "바꿀 수 없는 키"),
    (lambda d, f: {"op": "dashboard.update", "dashboard": d}, "`values`이 필요함"),
    (lambda d, f: {"op": "chart.add", "dashboard": d, "chart": {"title": "x"}}, "chart.key"),
    (lambda d, f: {"op": "chart.add", "dashboard": d, "chart": []}, "객체"),
    (lambda d, f: {"op": "chart.add", "dashboard": "nope", "chart": {"key": "x"}}, "대시보드 'nope'가 없음"),
    (lambda d, f: {"op": "chart.add", "dashboard": d, "chart": {"key": "x"}, "row": 99}, "범위"),
    (lambda d, f: {"op": "chart.add", "dashboard": d, "chart": {"key": "x"}, "index": 0}, "`row`와 함께"),
    (lambda d, f: {"op": "chart.add", "dashboard": d, "chart": {"key": "x"}, "row": 0, "index": 99}, "범위"),
    (lambda d, f: {"op": "chart.add", "dashboard": d, "chart": {"key": "x"}, "width": "6"}, "정수"),
    (lambda d, f: {"op": "chart.add", "dashboard": d, "chart": {"key": "x"}, "height": 60}, "모르는 필드"),
    (lambda d, f: {"op": "chart.update", "key": "nope", "values": {}}, "차트 'nope'가 없음"),
    (lambda d, f: {"op": "chart.update", "key": f["chart"], "values": {"key": "renamed"}}, "key는 바꿀 수 없음"),
    (lambda d, f: {"op": "chart.update", "key": f["chart"], "dashboard": d, "values": {}}, "모르는 필드"),
    (lambda d, f: {"op": "chart.remove", "key": "nope"}, "차트 'nope'가 없음"),
    (lambda d, f: {"op": "chart.remove", "key": 3}, "문자열"),
    (lambda d, f: {"op": "layout.set", "dashboard": d}, "`layout`이 필요함"),
    (lambda d, f: {"op": "layout.set", "dashboard": d, "layout": {"rows": []}}, "배열"),
    (lambda d, f: {"op": "layout.set", "dashboard": d, "layout": ["chart_key"]}, "`layout`[0]"),
    (lambda d, f: {"op": "layout.set", "dashboard": d, "layout": [[["nested"]]]}, "`layout`[0][0]"),
    (lambda d, f: {"op": "layout.set", "dashboard": d, "layout": [[3]]}, "`layout`[0][0]"),
    (lambda d, f: {"op": "layout.set", "dashboard": d, "layout": [{"tabs": [{"label": "x"}]}]}, "tabs[0].rows"),
    (lambda d, f: {"op": "layout.set", "dashboard": d, "layout": [{"tabs": ["x"]}]}, "tabs[0]"),
    (lambda d, f: {"op": "layout.set", "dashboard": d,
                   "layout": [{"tabs": [{"label": "x", "rows": [{"tabs": []}]}]}]}, "탭 안에 탭"),
    (lambda d, f: {"op": "filter.add", "dashboard": d, "filter": {"label": "x"}}, "filter.key"),
    (lambda d, f: {"op": "filter.add", "dashboard": d, "filter": {"key": f["filter"], "label": "x"}}, "이미 있음"),
    (lambda d, f: {"op": "filter.add", "dashboard": d, "filter": {"key": "x", "label": "x"}, "index": 99}, "범위"),
    (lambda d, f: {"op": "filter.update", "dashboard": d, "key": "nope", "values": {}}, "필터 'nope'가 없음"),
    (lambda d, f: {"op": "filter.update", "dashboard": d, "key": f["filter"], "values": {"key": "x"}}, "key는 바꿀 수 없음"),
    (lambda d, f: {"op": "filter.update", "key": f["filter"], "values": {}}, "`dashboard`이 필요함"),
    (lambda d, f: {"op": "filter.move", "dashboard": d, "key": f["filter"], "index": 99}, "범위"),
    (lambda d, f: {"op": "filter.move", "dashboard": d, "key": f["filter"]}, "`index`이 필요함"),
    (lambda d, f: {"op": "filter.remove", "dashboard": d, "key": "nope"}, "필터 'nope'가 없음"),
])
def test_invalid_5b_op_saves_nothing(client, make, reason):
    doc = raw_dashboard(client)
    name = _first(doc, lambda d: d.get("filters"))
    found = {"chart": doc["dashboards"][name]["charts"][0]["key"], "filter": doc["dashboards"][name]["filters"][0]["key"]}
    before = tree(client.projects)
    r = patch(client, {"op": "dashboard.update", "dashboard": name, "values": {"title": "먼저 적용될 뻔한 변경"}}, make(name, found))
    assert (r.status, r.json["detail"]) == (400, "invalid_op"), r.text
    assert r.json["op_index"] == 1 and r.json["message"].startswith("ops[1]") and reason in r.json["message"], r.json
    assert tree(client.projects) == before


@pytest.mark.parametrize("make,expect", [
    # 차트: 필수 필드, 없는 데이터셋·지표·열, 유형에 없는 필드
    (lambda n, d: [{"op": "chart.add", "dashboard": n, "chart": {"key": "x", "title": "x"}}], "charts["),
    (lambda n, d: [{"op": "chart.add", "dashboard": n, "chart": {**d["charts"][0], "key": "Bad Key"}}], "key"),
    (lambda n, d: [{"op": "chart.add", "dashboard": n, "chart": {**d["charts"][0], "key": "x", "dataset": "nowhere"}}], "nowhere"),
    (lambda n, d: [{"op": "chart.add", "dashboard": n, "chart": {
        "key": "x", "title": "x", "viz": "big_number_total", "dataset": d["charts"][0]["dataset"], "metric": "no_such_metric"}}],
     "no_such_metric"),
    (lambda n, d: [{"op": "chart.update", "key": d["charts"][0]["key"], "values": {"viz": "no_such_viz"}}], "charts[0]"),
    (lambda n, d: [{"op": "chart.update", "key": d["charts"][0]["key"], "values": {"title": None}}], "charts[0]"),
    (lambda n, d: [{"op": "chart.update", "key": d["charts"][0]["key"], "values": {"no_such_field": 1}}], "charts[0]"),
    (lambda n, d: [{"op": "chart.remove", "key": c["key"]} for c in d["charts"]], "charts"),
    # 배치: 빠진 차트, 두 번 놓인 차트, 없는 차트, 12칸 초과, 혼자가 아닌 제목
    (lambda n, d: [{"op": "layout.set", "dashboard": n, "layout": [[d["charts"][0]["key"]]]},
                   {"op": "chart.add", "dashboard": n, "chart": {**d["charts"][0], "key": "x"}},
                   {"op": "layout.set", "dashboard": n, "layout": [[d["charts"][0]["key"]]]}], "'x'가 layout에 배치되지 않음"),
    (lambda n, d: [{"op": "layout.set", "dashboard": n, "layout": [*d["layout"], [d["charts"][0]["key"]]]}], "2번 배치됨"),
    (lambda n, d: [{"op": "layout.set", "dashboard": n, "layout": [*d["layout"], ["ghost"]]}], "'ghost'"),
    (lambda n, d: [{"op": "chart.add", "dashboard": n, "chart": {**d["charts"][0], "key": "x"}, "width": 13}], "layout"),
    (lambda n, d: [{"op": "chart.add", "dashboard": n, "chart": {**d["charts"][0], "key": "x"}, "width": 7},
                   {"op": "chart.add", "dashboard": n, "chart": {**d["charts"][0], "key": "y"},
                    "row": len(d["layout"]), "width": 6}], "행 너비가 12칸을 넘음"),
    (lambda n, d: [{"op": "layout.set", "dashboard": n, "layout": [[{"header": "머리"}, *d["layout"][0]], *d["layout"][1:]]}],
     "한 행에 혼자"),
    (lambda n, d: [{"op": "layout.set", "dashboard": n, "layout": []}], "layout"),
    (lambda n, d: [{"op": "layout.set", "dashboard": n, "layout": [*d["layout"], {"tabs": []}]}], "layout"),
    # 필터: 유형별 필수·금지 필드, 없는 parent, 기본값 형식
    (lambda n, d: [{"op": "filter.add", "dashboard": n, "filter": {"key": "f_x", "label": "x", "type": "select"}}], "filters["),
    (lambda n, d: [{"op": "filter.add", "dashboard": n, "filter": {"key": "f_x", "label": "x", "type": "time_range",
                                                                   "dataset": d["charts"][0]["dataset"]}}], "time_range 필터에는"),
    (lambda n, d: [{"op": "filter.update", "dashboard": n, "key": d["filters"][0]["key"], "values": {"parent": "ghost"}}], "'ghost'"),
    (lambda n, d: [{"op": "filter.update", "dashboard": n, "key": d["filters"][0]["key"], "values": {"label": None}}], "filters[0]"),
    (lambda n, d: [{"op": "filter.update", "dashboard": n, "key": d["filters"][0]["key"], "values": {"charts": ["ghost"]}}], "'ghost'"),
    (lambda n, d: [{"op": "filter.add", "dashboard": n, "filter": {"key": "f_x", "label": "x", "type": "time_grain",
                                                                   "dataset": d["charts"][0]["dataset"], "default": "P7Y"}}], "default"),
    (lambda n, d: [{"op": "dashboard.update", "dashboard": n, "values": {"title": None}}], "title"),
    (lambda n, d: [{"op": "dashboard.update", "dashboard": n, "values": {"cross_filters": "yes"}}], "cross_filters"),
])
def test_invalid_5b_result_is_422_and_file_is_untouched(client, make, expect):
    doc = raw_dashboard(client)
    name = _first(doc, lambda d: d.get("filters"))
    before = tree(client.projects)
    r = patch(client, *make(name, doc["dashboards"][name]))
    assert (r.status, r.json["detail"]) == (422, "invalid_config"), r.text
    assert r.json["errors"] and all(e.startswith("dashboard.yaml:") for e in r.json["errors"])
    assert any(expect in e for e in r.json["errors"]), r.json["errors"]
    assert tree(client.projects) == before
    assert client.get("/studio/api/changes").json == {"files": []}


# ---------------------------------------------------------------- 프로젝트 마법사

def infer(client: Client, paths=SALES):
    return client.post("/studio/api/projects/infer", files=[("files", p.name, p.read_bytes()) for p in paths])


def test_wizard_creates_a_project_that_passes_check(client):
    before = tree(client.projects)
    r = infer(client)
    assert r.status == 200
    inference, upload_id = r.json["inference"], r.json["upload_id"]
    assert [t["name"] for t in inference["tables"]] == [p.stem for p in SALES] and inference["problems"] == []
    assert tree(client.projects) == before, "추론은 projects/에 아무것도 쓰지 않는다"

    r = client.post("/studio/api/projects", json={"name": "zz-wizard", "upload_id": upload_id, "inference": inference})
    assert (r.status, r.json) == (201, {"name": "zz-wizard"})
    project = check_project(client, "zz-wizard")
    assert sorted(project.schema["tables"]) == sorted(p.stem for p in SALES)
    assert "# 추론: 확인 필요" in dashboard_text(client, "zz-wizard")
    listed = client.get("/studio/api/projects").json["projects"]
    assert {"name": "zz-wizard", "title": "zz-wizard", "valid": True, "errors": 0} in listed
    changed = [f["path"] for f in client.get("/studio/api/changes").json["files"]]
    assert "projects/zz-wizard/dashboard.yaml" in changed and "projects/zz-wizard/data/deals.csv" in changed

    # 만든 프로젝트를 바로 Studio로 고칠 수 있고, 추론 주석이 남는다
    tabs = client.get("/studio/api/projects/zz-wizard/config").json["dashboard"]["tabs"]
    r = patch(client, {"op": "tab.move", "key": tabs[-1]["key"], "index": 0}, project="zz-wizard")
    assert r.status == 200 and r.json["written"] == {"dashboard.yaml": "exact"}
    assert dashboard_text(client, "zz-wizard").count("# 추론: 확인 필요") == \
        scaffold_marks(inference, "zz-wizard", {p.name: p.read_bytes() for p in SALES})
    check_project(client, "zz-wizard")

    # 같은 업로드로 다시 만들 수 없다(쓰고 나면 잊는다)
    again = client.post("/studio/api/projects", json={"name": "zz-wizard2", "upload_id": upload_id, "inference": inference})
    assert (again.status, again.json["detail"]) == (410, "upload_expired")


def scaffold_marks(inference, name, sources) -> int:
    from dashkit import scaffold
    return scaffold.render(name, inference, sources)["dashboard.yaml"].decode("utf-8").count("# 추론: 확인 필요")


def test_wizard_with_edited_tables_resuggests_the_dashboard(client):
    r = infer(client)
    inference, upload_id = r.json["inference"], r.json["upload_id"]
    deals = next(t for t in inference["tables"] if t["name"] == "deals")
    deals["name"] = "sales_deals"          # 표 이름을 고침 → 옛 이름을 가리키는 초안 차트는 쓸 수 없다
    for t in inference["tables"]:
        for c in t["columns"]:
            if c.get("ref", "").startswith("deals."):
                c["ref"] = c["ref"].replace("deals.", "sales_deals.")
    body = {"name": "zz-edited", "upload_id": upload_id, "inference": inference}
    r = client.post("/studio/api/projects", json=body)
    assert (r.status, r.json["detail"]) == (422, "invalid_inference") and r.json["errors"]
    assert not (client.projects / "zz-edited").exists()
    r = client.post("/studio/api/projects", json={**body, "resuggest": True})
    assert r.status == 201
    project = check_project(client, "zz-edited")
    assert "sales_deals" in project.schema["tables"] and "sales_deals" in project.dashboard["datasets"]
    assert (client.projects / "zz-edited" / "data" / "sales_deals.csv").is_file()


def test_wizard_errors(client):
    r = infer(client)
    inference, upload_id = r.json["inference"], r.json["upload_id"]
    before = tree(client.projects)

    def create(**kw):
        return client.post("/studio/api/projects", json={"name": "zz-x", "upload_id": upload_id, "inference": inference, **kw})

    assert (create(name="devtracker").status, create(name="devtracker").json["detail"]) == (409, "project_exists")
    for bad in ("Bad Name", "../x", "", "x/y", ".x", None, 3):
        r = create(name=bad)
        assert (r.status, r.json["detail"]) == (422, "invalid_name"), bad
    assert create(upload_id="nope").json["detail"] == "upload_expired"
    assert create(upload_id=None).json["detail"] == "upload_expired"
    assert create(inference="x").json["detail"] == "invalid_request"
    assert create(inference={"format": 99, "tables": inference["tables"], "dashboard": inference["dashboard"]}).json["detail"] \
        == "invalid_inference"
    broken = json.loads(json.dumps(inference))
    broken["tables"][0]["primary_key"] = []
    r = create(inference=broken)
    assert (r.status, r.json["detail"]) == (422, "invalid_inference") and "기본 키" in r.json["errors"][0]
    assert tree(client.projects) == before, "실패한 생성은 아무것도 남기지 않는다"
    assert create().status == 201          # 실패해도 업로드는 남아 있어 고쳐서 다시 시도할 수 있다


def test_infer_reports_unreadable_csv(client):
    r = client.post("/studio/api/projects/infer", files=[("files", "ok.csv", b"id,name\n1,a\n"),
                                                         ("files", "bad.csv", b"id,name\n1,a,extra\n"),
                                                         ("files", "empty.csv", b"")])
    assert (r.status, r.json["detail"]) == (422, "invalid_csv")
    assert any("bad.csv" in e for e in r.json["errors"]) and any("empty.csv" in e for e in r.json["errors"])


def test_uploads_are_kept_in_memory_only_for_a_few(client):
    ids = [infer(client, SALES[:1]).json["upload_id"] for _ in range(4)]
    assert len(set(ids)) == 4
    with pytest.raises(store.StoreError):
        store.uploads.get(ids[0])           # 최근 3개만 기억한다
    assert store.uploads.get(ids[3])


# ---------------------------------------------------------------- 데이터 교체

def tasks_csv(client: Client) -> bytes:
    return (client.projects / "devtracker" / "data" / "tasks.csv").read_bytes()


def test_data_check_validates_without_writing(client):
    before = tree(client.projects)
    good = tasks_csv(client)
    r = client.post("/studio/api/projects/devtracker/data/tasks/check", files=[("file", "tasks.csv", good)])
    assert r.status == 200
    body = r.json
    # DB가 없으면 검증 결과만 주고 차이는 이유와 함께 비운다(스택이 있을 때의 차이는 통합 테스트가 본다)
    assert body["valid"] is True and body["errors"] == [] and body["rows"] == 16
    assert body["diff"] is None and "DB 접속 실패" in body["diff_error"]

    header, first = good.split(b"\n")[0], good.split(b"\n")[1]
    bad = header + b"\n" + first + b"\n" + first + b"\n" + first.replace(b"T1,", b"T99,").replace(b",ms", b",nope") + b"\n"
    r = client.post("/studio/api/projects/devtracker/data/tasks/check", files=[("file", "tasks.csv", bad)])
    assert r.status == 200
    body = r.json
    assert body["valid"] is False and body["diff"] is None
    assert any(e.startswith("data/tasks.csv:3:id:") and "기본 키 중복" in e for e in body["errors"])
    assert any("참조 대상" in e for e in body["errors"])
    assert tree(client.projects) == before


def test_data_check_sees_broken_references_from_other_tables(client):
    """참조되는 표를 바꾸면 그 표를 가리키는 다른 CSV의 오류도 함께 보인다."""
    members = (client.projects / "devtracker" / "data" / "members.csv").read_bytes().split(b"\n")
    r = client.post("/studio/api/projects/devtracker/data/members/check",
                    files=[("file", "members.csv", b"\n".join(members[:2]) + b"\n")])
    body = r.json
    assert body["valid"] is False and any(not e.startswith("data/members.csv") and "참조 대상 members" in e
                                          for e in body["errors"])


def test_data_put_replaces_only_that_csv_after_validation(client):
    before = tree(client.projects)
    good = tasks_csv(client)
    rows = good.rstrip(b"\n").split(b"\n")
    fewer = b"\n".join(rows[:-1]) + b"\n"

    bad = good + b"T1,x\n"
    r = client.put("/studio/api/projects/devtracker/data/tasks", files=[("file", "tasks.csv", bad)])
    assert (r.status, r.json["detail"]) == (422, "invalid_csv") and r.json["errors"]
    assert tree(client.projects) == before

    r = client.put("/studio/api/projects/devtracker/data/tasks", files=[("file", "새 작업 목록.csv", fewer)])
    assert (r.status, r.json) == (200, {"changed": ["data/tasks.csv"], "rows": 15})
    after = tree(client.projects)
    assert after["devtracker/data/tasks.csv"] == fewer
    assert {k for k in after if after[k] != before.get(k)} == {"devtracker/data/tasks.csv"} and set(after) == set(before)
    assert client.get(CONFIG).json["tables"]["tasks"]["rows"] == 15
    assert [f["path"] for f in client.get("/studio/api/changes").json["files"]] == ["projects/devtracker/data/tasks.csv"]
    check_project(client, "devtracker")

    r = client.put("/studio/api/projects/devtracker/data/tasks", files=[("file", "tasks.csv", fewer)])
    assert r.json == {"changed": [], "rows": 15}          # 같은 내용


def test_data_endpoints_need_a_valid_config(client):
    path = client.projects / "devtracker" / "dashboard.yaml"
    path.write_text("tabs: [", encoding="utf-8")
    for method, url in (("POST", "/studio/api/projects/devtracker/data/tasks/check"),
                        ("PUT", "/studio/api/projects/devtracker/data/tasks")):
        r = client.request(method, url, files=[("file", "tasks.csv", tasks_csv(client))])
        assert (r.status, r.json["detail"]) == (422, "invalid_config") and r.json["errors"]
    r = client.get("/studio/api/projects/devtracker/plan")
    assert (r.status, r.json["detail"]) == (422, "invalid_config")


def test_plan_without_db_reports_unavailable(client):
    r = client.get("/studio/api/projects/devtracker/plan")
    assert (r.status, r.json["detail"]) == (503, "plan_unavailable") and r.json["errors"]


def test_read_paths_use_the_reader_account_only(client, monkeypatch):
    """config의 열 목록, check의 차이, plan은 dash_reader만 쓴다(AGENTS.md). owner 연결을 열면 실패하게 해 둔다."""
    from dashkit import db

    opened = []

    def no_owner(*a, **kw):
        raise AssertionError("읽기 경로에서 owner 계정을 씀")

    def fake_reader(*a, **kw):
        opened.append(kw.get("options"))
        raise RuntimeError("테스트: DB 없음")

    monkeypatch.setattr(db, "owner_conn", no_owner)
    monkeypatch.setattr(db, "reader_conn", fake_reader)
    assert client.get(CONFIG).status == 200
    assert client.post("/studio/api/projects/devtracker/data/tasks/check",
                       files=[("file", "tasks.csv", tasks_csv(client))]).status == 200
    assert client.get("/studio/api/projects/devtracker/plan").status == 503
    assert len(opened) == 3 and all("default_transaction_read_only=on" in o for o in opened)


# ---------------------------------------------------------------- apply

def use_runner(monkeypatch, fn) -> jobs.ApplyRunner:
    runner = jobs.ApplyRunner(fn)
    monkeypatch.setattr(studio_app, "runner", runner)
    return runner


def test_apply_streams_log_lines_then_done(client, monkeypatch):
    def fake(name, log):
        log(f"▶ check: {name}")
        log("여러 줄\n로그")
        log("✔ sync: 끝")

    use_runner(monkeypatch, fake)
    r = client.post("/studio/api/projects/devtracker/apply", json={})
    assert r.status == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = r.events()
    assert events[:-1] == [("log", "▶ check: devtracker"), ("log", "여러 줄"), ("log", "로그"), ("log", "✔ sync: 끝")]
    assert events[-1][0] == "done"
    assert json.loads(events[-1][1]) == {"ok": True, "step": "sync", "errors": [], "project": "devtracker"}
    status = client.get("/studio/api/apply").json
    assert status["running"] is False and status["last"]["ok"] is True and status["last"]["log"][-1] == "✔ sync: 끝"


@pytest.mark.parametrize("step", ["check", "load", "sync"])
def test_apply_failure_names_the_step_and_reasons(client, monkeypatch, step):
    def fake(name, log):
        log("시작")
        raise jobs.StepFailed(step, ["data/tasks.csv:3:id: 기본 키 중복", "둘째 이유"])

    use_runner(monkeypatch, fake)
    events = client.post("/studio/api/projects/devtracker/apply").events()
    done = json.loads(events[-1][1])
    assert done == {"ok": False, "step": step, "errors": ["data/tasks.csv:3:id: 기본 키 중복", "둘째 이유"],
                    "project": "devtracker"}
    assert ("log", f"✖ {step} 실패 (2건)") in events and ("log", "  둘째 이유") in events


def test_apply_runs_the_real_steps_in_order_and_stops_at_the_first_failure(client, monkeypatch):
    """기존 함수를 그대로 부른다: load_project → loader.load → sync.sync(log 콜백)."""
    import dashkit.loader
    import dashkit.sync

    calls: list[str] = []

    def fake_load(project):
        calls.append(f"load:{project.name}")
        return {"members": 6, "tasks": 16}

    def fake_sync(project, *, log):
        calls.append(f"sync:{project.name}")
        log("· import: 흉내")
        return {"dataset": ["a"], "chart": ["b", "c"]}

    monkeypatch.setattr(dashkit.loader, "load", fake_load)
    monkeypatch.setattr(dashkit.sync, "sync", fake_sync)
    events = client.post("/studio/api/projects/devtracker/apply").events()
    assert calls == ["load:devtracker", "sync:devtracker"]
    lines = [d for e, d in events if e == "log"]
    assert lines == ["▶ check: devtracker 설정 검사", "✔ check: devtracker 설정이 유효함", "▶ load: CSV 검증 후 적재",
                     "✔ load: devtracker 22행 적재 (members 6, tasks 16)", "▶ sync: Superset에 반영", "· import: 흉내",
                     "✔ sync: devtracker Superset 반영 완료 (dataset 1, chart 2)"]
    assert json.loads(events[-1][1])["ok"] is True

    # check에서 걸리면 load·sync를 부르지 않는다
    calls.clear()
    (client.projects / "devtracker" / "dashboard.yaml").write_text("tabs: [", encoding="utf-8")
    done = json.loads(client.post("/studio/api/projects/devtracker/apply").events()[-1][1])
    assert done["ok"] is False and done["step"] == "check" and "YAML 문법 오류" in done["errors"][0] and calls == []

    # load에서 걸리면 sync를 부르지 않는다
    (client.projects / "devtracker" / "dashboard.yaml").write_bytes(
        (ROOT / "projects" / "devtracker" / "dashboard.yaml").read_bytes())

    def failing_load(project):
        raise dashkit.loader.LoadError(["data/tasks.csv:2:id: 값이 필요함"])

    monkeypatch.setattr(dashkit.loader, "load", failing_load)
    done = json.loads(client.post("/studio/api/projects/devtracker/apply").events()[-1][1])
    assert (done["ok"], done["step"], done["errors"], calls) == (False, "load", ["data/tasks.csv:2:id: 값이 필요함"], [])

    monkeypatch.setattr(dashkit.loader, "load", fake_load)

    def failing_sync(project, *, log):
        raise dashkit.sync.SyncError("import 실패(HTTP 500)\n  Superset 로그를 확인하세요(make logs)")

    monkeypatch.setattr(dashkit.sync, "sync", failing_sync)
    done = json.loads(client.post("/studio/api/projects/devtracker/apply").events()[-1][1])
    assert (done["ok"], done["step"]) == (False, "sync") and done["errors"][0] == "import 실패(HTTP 500)"


def test_only_one_apply_at_a_time_and_saves_wait_for_it(client, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow(name, log):
        log("시작")
        started.set()
        assert release.wait(20)
        log("끝")

    runner = use_runner(monkeypatch, slow)
    results: dict[str, object] = {}
    t = threading.Thread(target=lambda: results.update(first=client.post("/studio/api/projects/devtracker/apply")))
    t.start()
    try:
        assert started.wait(10)
        assert client.get("/studio/api/apply").json["running"] is True
        assert runner.running() == "devtracker"
        # 실행 중에는 다른 apply를 받지 않는다(다른 프로젝트도)
        for p in ("devtracker", "sales-sample"):
            r = client.post(f"/studio/api/projects/{p}/apply")
            assert (r.status, r.json["detail"]) == (409, "apply_running")
        # 다른 프로젝트의 저장은 된다
        assert patch(client, {"op": "app.update", "values": {"title": "판매"}}, project="sales-sample").status == 200
    finally:
        release.set()
        t.join(20)
    first = results["first"]
    assert json.loads(first.events()[-1][1])["ok"] is True
    assert [d for e, d in first.events() if e == "log"] == ["시작", "끝"]
    # 끝나면 다시 받을 수 있다
    release.set()
    assert json.loads(client.post("/studio/api/projects/sales-sample/apply").events()[-1][1])["project"] == "sales-sample"


def test_save_during_apply_of_the_same_project_is_refused(client, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def slow(name, log):
        started.set()
        assert release.wait(20)

    use_runner(monkeypatch, slow)
    monkeypatch.setattr(studio_app._Busy, "__enter__", lambda self: _fast_enter(self))
    t = threading.Thread(target=lambda: client.post("/studio/api/projects/devtracker/apply"))
    t.start()
    try:
        assert started.wait(10)
        before = tree(client.projects)
        r = patch(client, {"op": "tab.move", "key": "docs", "index": 0})
        assert (r.status, r.json["detail"]) == (409, "apply_running")
        r = client.put("/studio/api/projects/devtracker/data/tasks", files=[("file", "tasks.csv", tasks_csv(client) + b"\n")])
        assert (r.status, r.json["detail"]) == (409, "apply_running")
        assert tree(client.projects) == before
    finally:
        release.set()
        t.join(20)
    assert patch(client, {"op": "tab.move", "key": "docs", "index": 0}).status == 200


def _fast_enter(busy) -> None:
    """테스트에서는 잠금을 오래 기다리지 않는다."""
    if not busy.lock.acquire(timeout=0.2):
        raise store.StoreError(409, "apply_running", "이 프로젝트에 적용(apply)이 실행 중입니다. 끝난 뒤 다시 시도하세요")


def test_apply_log_and_errors_never_carry_secrets(client, monkeypatch):
    monkeypatch.setenv("SUPERSET_ADMIN_PASSWORD", "sup3r-admin-pw")
    monkeypatch.setenv("DATA_DB_OWNER_PASSWORD", "0wner-db-pw")
    monkeypatch.setenv("DATA_DB_READER_PASSWORD", "r3ader-db-pw")
    monkeypatch.setenv("GUEST_TOKEN_JWT_SECRET", "jwt-secret-value-1234567890")
    monkeypatch.setenv("SUPERSET_SECRET_KEY", "flask-secret-key-abcdef")
    monkeypatch.setenv("GUEST_TOKEN_JWT_AUDIENCE", "superset")          # 비밀값이 아니다(가리면 로그가 망가진다)
    monkeypatch.setenv("DATA_DB_OWNER_USER", "dash_owner")

    def leaky(name, log):
        log("접속 문자열 postgresql://dash_owner:0wner-db-pw@data-db/dash")
        log("login {'username': 'admin', 'password': 'sup3r-admin-pw'} → superset")
        raise jobs.StepFailed("sync", ["import 실패: passwords={'databases/x.yaml': 'r3ader-db-pw'}",
                                       "토큰 jwt-secret-value-1234567890 / flask-secret-key-abcdef"])

    use_runner(monkeypatch, leaky)
    r = client.post("/studio/api/projects/devtracker/apply")
    status = client.get("/studio/api/apply")
    for text in (r.text, status.text):
        for secret in ("sup3r-admin-pw", "0wner-db-pw", "r3ader-db-pw", "jwt-secret-value-1234567890", "flask-secret-key-abcdef"):
            assert secret not in text
        assert "***" in text and "dash_owner" in text and "superset" in text


def test_unexpected_error_in_a_step_is_reported_with_its_step(client, monkeypatch):
    import dashkit.loader

    def boom(project):
        raise ZeroDivisionError("예상 못 한 오류")

    monkeypatch.setattr(dashkit.loader, "load", boom)
    done = json.loads(client.post("/studio/api/projects/devtracker/apply").events()[-1][1])
    assert done == {"ok": False, "step": "load", "errors": ["ZeroDivisionError: 예상 못 한 오류"], "project": "devtracker"}
    assert client.get("/studio/api/apply").json["running"] is False


def test_apply_stream_sends_keep_alive_while_quiet(monkeypatch):
    job = jobs.Job("x")

    def finish():
        time.sleep(0.25)
        job.events.put(("done", {"ok": True}))

    threading.Thread(target=finish).start()
    chunks = list(jobs.stream(job, heartbeat=0.05))
    assert chunks[0] == b": keep-alive\n\n" and chunks[-1].startswith(b"event: done\ndata: {")


def test_studio_subcommand_exists():
    from dashkit.__main__ import main
    with pytest.raises(SystemExit) as exc:
        main(["studio", "--help"])
    assert exc.value.code == 0


def test_column_update_saves_aliases_the_same_way_as_the_manager(client, tmp_path, monkeypatch):
    """schema.yaml 열 연산(column.update): Studio의 PATCH와 관리 서비스가 쓰는 store.save_ops가 같은 바이트를 쓴다
    (공용 코어 한 벌, contracts/studio.md "변경 연산")."""
    op = {"op": "column.update", "table": "members", "column": "name", "set": {"aliases": ["이름", "Name"]}}
    r = patch(client, op)
    assert r.status == 200, r.body
    via_studio = (client.projects / "devtracker" / "schema.yaml").read_bytes()
    assert "aliases: [이름, Name]".encode() in via_studio or b"- Name" in via_studio
    (tmp_path / "other").mkdir()
    other = fresh_client(tmp_path / "other", monkeypatch)
    out = store.save_ops("devtracker", [op], store.revision(other.projects / "devtracker"))
    assert out["changed"] == ["schema.yaml"]
    assert (other.projects / "devtracker" / "schema.yaml").read_bytes() == via_studio
    r = patch(client, {"op": "column.update", "table": "members", "column": "name", "set": {"type": "integer"}})
    assert r.status == 400 and "바꿀 수 없는 키" in json.loads(r.body)["message"]
