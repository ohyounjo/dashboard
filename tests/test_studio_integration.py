"""Studio만으로 CSV 폴더에서 대시보드까지(보강설계서 8절 5a 완료 조건). 테스트 스택에서 실행 중인 Studio에 HTTP로 붙는다.

`make test`의 Studio 단계가 `make studio`로 Studio를 띄운 뒤 STUDIO_TEST_URL=http://studio:8090 을 주고 이 파일만 실행한다
(tools 컨테이너에서 돈다. ruamel.yaml이 필요 없다). 평소 pytest 실행에서는 건너뛴다.

흐름: CSV 업로드 → 추론 → 프로젝트 생성 → apply → 웹앱이 그 프로젝트를 보여 줌 → 탭 이동(PATCH) → 재적용 없이 웹앱에 반영
      → CSV 교체 check/PUT → apply
      → (5b 완료 조건) 차트 추가 → 이동·너비 조절 → 필터 추가 → apply → Superset의 배치에 반영 → 차트 삭제 → apply → 사라짐.
끝나면 임시 프로젝트(projects/zz-studio-it)를 지우고 원래 프로젝트를 다시 적용한다.
"""
from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request

import pytest

from dashkit import config

from conftest import PROJECTS, api, live_project
from studio_client import Result, multipart

STUDIO_URL = os.environ.get("STUDIO_TEST_URL")
# Studio는 Host가 자기 주소일 때만 받는다. 컨테이너 안의 포트(8090)는 로컬 이름으로만 허용된다
STUDIO_HOST = os.environ.get("STUDIO_TEST_HOST", "localhost:8090")
NAME = "zz-studio-it"
SOURCE = PROJECTS / "sales-sample" / "data"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not STUDIO_URL, reason="make test의 Studio 단계에서만 실행(STUDIO_TEST_URL)"),
]


def studio(method: str, path: str, *, body=None, files=None, headers=None, timeout: float = 60) -> Result:
    sent = {"Host": STUDIO_HOST, **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        sent["Content-Type"] = "application/json"
    elif files is not None:
        sent["Content-Type"], data = multipart(files)
    req = urllib.request.Request(f"{STUDIO_URL}/studio/api{path}", data=data, method=method, headers=sent)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Result(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        return Result(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())


def apply(name: str) -> tuple[dict, list[str]]:
    r = studio("POST", f"/projects/{name}/apply", body={}, timeout=600)
    assert r.status == 200 and r.headers["content-type"].startswith("text/event-stream"), r.text
    events = r.events()
    assert events and events[-1][0] == "done", events[-5:]
    return json.loads(events[-1][1]), [d for e, d in events if e == "log"]


def csv_lines(path) -> list[bytes]:
    return path.read_bytes().rstrip(b"\n").split(b"\n")


@pytest.fixture(scope="module")
def original():
    """시작할 때 웹앱이 보여 주던 프로젝트. 끝나면 임시 프로젝트를 지우고 이 프로젝트를 다시 적용한다."""
    code, app = api("GET", "/api/app")
    assert code == 200 and app["applied"] is True, "테스트 스택에 프로젝트가 적용돼 있어야 함"
    name = app["project"]
    assert name == live_project() and name != NAME
    shutil.rmtree(PROJECTS / NAME, ignore_errors=True)      # 지난 실행이 남긴 것
    yield name
    try:
        done, _log = apply(name)
        assert done["ok"] is True, done
        code, app = api("GET", "/api/app")
        assert (code, app["project"], app["applied"]) == (200, name, True)
    finally:
        shutil.rmtree(PROJECTS / NAME, ignore_errors=True)
    assert not (PROJECTS / NAME).exists()


@pytest.fixture(scope="module")
def created(original):
    """마법사: CSV 업로드 → 추론 → 프로젝트 생성."""
    files = [("files", p.name, p.read_bytes()) for p in sorted(SOURCE.glob("*.csv"))]
    r = studio("POST", "/projects/infer", files=files)
    assert r.status == 200, r.text
    inference = r.json["inference"]
    assert inference["problems"] == [] and len(inference["tables"]) == len(files)
    assert not (PROJECTS / NAME).exists()
    r = studio("POST", "/projects", body={"name": NAME, "upload_id": r.json["upload_id"], "inference": inference})
    assert (r.status, r.json) == (201, {"name": NAME}), r.text
    return inference


@pytest.fixture(scope="module")
def applied(created):
    done, log = apply(NAME)
    assert done == {"ok": True, "step": "sync", "errors": [], "project": NAME}, (done, log[-10:])
    return log


def test_requests_with_another_host_are_refused():
    r = studio("GET", "/health", headers={"Host": "studio:8090"})       # compose 서비스 이름으로는 받지 않는다
    assert (r.status, r.json["detail"]) == (403, "forbidden_host")
    r = studio("POST", f"/projects/{NAME}/apply", body={}, headers={"Origin": "http://web"})
    assert (r.status, r.json["detail"]) == (403, "forbidden_origin")
    assert studio("GET", "/health").json == {"status": "ok"}


def test_created_project_passes_check_and_is_listed(created, original):
    project = config.load_project(NAME, PROJECTS)                       # make check와 같은 검사
    assert sorted(project.schema["tables"]) == sorted(t["name"] for t in created["tables"])
    body = studio("GET", "/projects").json
    assert {"name": NAME, "title": NAME, "valid": True, "errors": 0} in body["projects"]
    assert body["current"] == original, "만들기만 해서는 웹앱이 바뀌지 않는다"
    code, app = api("GET", "/api/app")
    assert app["project"] == original


def test_plan_before_first_apply_says_everything_is_new(created, original):
    r = studio("GET", f"/projects/{NAME}/plan")
    assert r.status == 200, r.text
    plan = r.json
    assert plan["project"] == NAME and plan["changed"] is True and plan["superset"]["first"] is True
    assert plan["superset"]["replaces"]["project"] == original
    assert {d["table"] for d in plan["data"] if d["status"] != "removed"} == {t["name"] for t in created["tables"]}


def test_apply_log_has_the_three_steps_and_no_secrets(applied):
    text = "\n".join(applied)
    for mark in ("▶ check:", "✔ check:", "▶ load:", "✔ load:", "▶ sync:", "· import:", "· embed:", "· prune:", "✔ sync:"):
        assert mark in text, mark
    for name in ("SUPERSET_ADMIN_PASSWORD", "DATA_DB_OWNER_PASSWORD", "DATA_DB_READER_PASSWORD"):
        secret = os.environ.get(name)
        assert secret and secret not in text, name
    status = studio("GET", "/apply").json
    assert status["running"] is False and status["last"]["ok"] is True and status["last"]["project"] == NAME


def test_web_app_shows_the_project_studio_applied(applied, created):
    """backend는 embed.json의 project를 따른다: env PROJECT는 그대로인데 웹앱이 새 프로젝트를 보여 준다."""
    project = config.load_project(NAME, PROJECTS)
    code, app = api("GET", "/api/app")
    assert code == 200 and app["project"] == NAME and app["applied"] is True
    assert [t["key"] for t in app["tabs"]] == [t["key"] for t in project.dashboard["tabs"]]
    assert all(s["embed_id"] for t in app["tabs"] for s in t["sections"] if s["type"] == "superset")
    code, health = api("GET", "/api/health")
    assert (code, health["project"], health["applied"], health["config"]) == (200, NAME, True, {"valid": True, "errors": 0})
    assert studio("GET", "/projects").json["current"] == NAME
    dkey = next(iter(project.dashboard["dashboards"]))
    code, token = api("POST", "/api/guest-token", {"dashboard": dkey})
    assert code == 200 and token["token"]
    for ds in config.widget_datasets(project):
        code, data = api("GET", f"/api/data/{ds}")
        assert code == 200 and data["rows"], ds


def test_config_columns_come_from_the_loaded_views(applied):
    body = studio("GET", f"/projects/{NAME}/config").json
    project = config.load_project(NAME, PROJECTS)
    assert body["valid"] is True and body["columns_source"] == "db"
    assert sorted(body["columns"]) == sorted(project.dashboard["datasets"])
    deals = {c["name"]: c["type"] for c in body["columns"]["deals"]}
    assert deals["amount_krw"] == "integer" and deals["opened_on"] == "date" and deals["pipeline_step"] == "text"
    plan = studio("GET", f"/projects/{NAME}/plan").json
    assert plan["changed"] is False, "적용 직후에는 바뀔 것이 없다"


def test_tab_move_shows_in_web_app_without_reapply(applied):
    before = (PROJECTS / NAME / "dashboard.yaml").read_text(encoding="utf-8")
    keys = [t["key"] for t in api("GET", "/api/app")[1]["tabs"]]
    assert len(keys) >= 2
    r = studio("PATCH", f"/projects/{NAME}/config", body={"ops": [{"op": "tab.move", "key": keys[-1], "index": 0}]})
    assert r.status == 200 and r.json["changed"] == ["dashboard.yaml"] and r.json["written"] == {"dashboard.yaml": "exact"}, r.text
    moved = [keys[-1], *keys[:-1]]
    code, app = api("GET", "/api/app")                                  # backend가 설정 파일을 다시 읽는다
    assert [t["key"] for t in app["tabs"]] == moved and app["project"] == NAME and app["applied"] is True
    # Studio가 쓴 파일: make check 통과, 추론 주석을 포함해 모든 줄이 그대로(자리만 바뀜)
    after = (PROJECTS / NAME / "dashboard.yaml").read_text(encoding="utf-8")
    assert sorted(after.split("\n")) == sorted(before.split("\n")) and after != before
    assert after.count("# 추론: 확인 필요") == before.count("# 추론: 확인 필요") > 0
    assert [t["key"] for t in config.load_project(NAME, PROJECTS).dashboard["tabs"]] == moved
    assert "projects/zz-studio-it/dashboard.yaml" in [f["path"] for f in studio("GET", "/changes").json["files"]]
    # 탭 순서는 Superset 객체가 아니므로 적용할 것이 없다
    assert studio("GET", f"/projects/{NAME}/plan").json["changed"] is False


def test_invalid_change_is_refused_and_web_app_keeps_working(applied):
    before = (PROJECTS / NAME / "dashboard.yaml").read_bytes()
    r = studio("PATCH", f"/projects/{NAME}/config",
               body={"ops": [{"op": "tab.update", "key": api("GET", "/api/app")[1]["tabs"][0]["key"],
                              "values": {"icon": "no-such-icon"}}]})
    assert (r.status, r.json["detail"]) == (422, "invalid_config") and r.json["errors"]
    assert (PROJECTS / NAME / "dashboard.yaml").read_bytes() == before
    assert api("GET", "/api/health")[1]["config"] == {"valid": True, "errors": 0}


def test_csv_replace_check_put_apply(applied):
    path = PROJECTS / NAME / "data" / "deals.csv"
    original_bytes = path.read_bytes()
    lines = csv_lines(path)
    header = lines[0].decode("utf-8").split(",")
    amount = header.index("amount_krw")
    changed_row = lines[1].split(b",")
    changed_row[amount] = str(int(changed_row[amount]) + 1).encode()
    added_row = lines[2].split(b",")
    added_row[0] = b"DL-9999"
    new = b"\n".join([lines[0], b",".join(changed_row), *lines[2:-1], b",".join(added_row)]) + b"\n"   # ~1, −1, +1

    # 검증만: 파일을 쓰지 않고 차이를 알려 준다(적재 기록의 행 지문과 비교, dash_reader)
    r = studio("POST", f"/projects/{NAME}/data/deals/check", files=[("file", "deals-new.csv", new)])
    assert r.status == 200, r.text
    body = r.json
    assert body["valid"] is True and body["errors"] == [] and "diff_error" not in body
    diff = body["diff"]
    assert (diff["added"], diff["removed"], diff["changed"], diff["comparable"]) == (1, 1, 1, True), diff
    assert diff["rows"] == len(lines) - 1 and diff["unchanged"] == len(lines) - 3 and diff["status"] == "changed"
    assert diff["keys"]["added"] == ["DL-9999"]
    assert path.read_bytes() == original_bytes

    # 잘못된 CSV: 오류를 행·열로 알려 주고 아무것도 바꾸지 않는다
    bad = new + b",".join(added_row) + b"\n"
    r = studio("POST", f"/projects/{NAME}/data/deals/check", files=[("file", "deals.csv", bad)])
    assert r.status == 200 and r.json["valid"] is False and r.json["diff"] is None
    assert any("deal_code" in e and "기본 키 중복" in e for e in r.json["errors"]), r.json["errors"]
    r = studio("PUT", f"/projects/{NAME}/data/deals", files=[("file", "deals.csv", bad)])
    assert (r.status, r.json["detail"]) == (422, "invalid_csv")
    assert path.read_bytes() == original_bytes

    # 교체: 파일만 바뀌고 적재는 아직이다
    r = studio("PUT", f"/projects/{NAME}/data/deals", files=[("file", "deals.csv", new)])
    assert (r.status, r.json) == (200, {"changed": ["data/deals.csv"], "rows": len(lines) - 1})
    assert path.read_bytes() == new
    plan = studio("GET", f"/projects/{NAME}/plan").json
    deals = next(d for d in plan["data"] if d["table"] == "deals")
    assert plan["changed"] is True and (deals["added"], deals["removed"], deals["changed"]) == (1, 1, 1)

    # 적용하면 차이가 없어진다
    done, log = apply(NAME)
    assert done["ok"] is True, (done, log[-10:])
    r = studio("POST", f"/projects/{NAME}/data/deals/check", files=[("file", "deals.csv", new)])
    diff = r.json["diff"]
    assert (diff["added"], diff["removed"], diff["changed"], diff["status"]) == (0, 0, 0, "unchanged")
    assert studio("GET", f"/projects/{NAME}/plan").json["changed"] is False
    code, app = api("GET", "/api/app")
    assert app["project"] == NAME and app["applied"] is True


# ---------------------------------------------------------------- 5b: 차트 추가·이동·너비 조절이 layout과 Superset에 반영된다

ADDED = "it_added_chart"
ADDED_FILTER = "f_it_added"


def patch_config(*op_list: dict) -> Result:
    return studio("PATCH", f"/projects/{NAME}/config", body={"ops": list(op_list)})


def superset_dashboard(superset, dkey: str) -> tuple[dict, list[dict]]:
    """Superset REST로 읽은 대시보드의 (position, native filter 목록)."""
    dash_id = superset.find_id("dashboard", config.object_uuid(NAME, "dashboard", dkey))
    assert dash_id is not None
    result = superset.get(f"/api/v1/dashboard/{dash_id}").json()["result"]
    position = json.loads(result["position_json"])
    filters = json.loads(result["json_metadata"]).get("native_filter_configuration") or []
    return position, filters


def chart_nodes(position: dict) -> dict[str, dict]:
    return {k: n for k, n in position.items() if isinstance(n, dict) and n.get("type") == "CHART"}


def test_chart_add_move_resize_filter_reach_superset_and_removal_too(applied, superset):
    body = studio("GET", f"/projects/{NAME}/config").json
    assert "deals" in body["metrics"] and set(body["metrics"]) == set(body["dashboard"]["datasets"])
    dashboards = body["dashboard"]["dashboards"]
    embedded = [s["dashboard"] for t in body["dashboard"]["tabs"] for s in t["sections"] if s["type"] == "superset"]
    dkey = next(k for k in dashboards if k in embedded and any(
        isinstance(e, list) and all(isinstance(i, str) or "chart" in i for i in e) for e in dashboards[k]["layout"]))
    before_text = (PROJECTS / NAME / "dashboard.yaml").read_text(encoding="utf-8")
    chart_uuid = config.object_uuid(NAME, "chart", ADDED)
    assert superset.find_id("chart", chart_uuid) is None

    # 1. 차트 추가: charts에 들어가고 layout의 맨 아래 새 행에 놓인다
    r = patch_config({"op": "chart.add", "dashboard": dkey, "chart": {
        "key": ADDED, "title": "Studio가 더한 차트", "viz": "pie", "dataset": "deals",
        "groupby": ["pipeline_step"], "metric": {"sql": "COUNT(*)", "label": "건수"}}})
    assert r.status == 200 and r.json["written"] == {"dashboard.yaml": "exact"}, r.text
    layout = r.json["dashboard"]["dashboards"][dkey]["layout"]
    assert layout[-1] == [ADDED] and layout[:-1] == dashboards[dkey]["layout"]

    # 2. 이동과 너비 조절: 첫 차트 행의 첫 차트 옆으로 옮기고 5칸 + 7칸으로
    at = next(i for i, e in enumerate(layout) if isinstance(e, list) and all(isinstance(i, str) or "chart" in i for i in e))
    row = layout[at]
    neighbour = row[0] if isinstance(row[0], str) else row[0]["chart"]
    rest = [i if isinstance(i, str) else i["chart"] for i in row[1:]]
    new_layout = [*layout[:at], [{"chart": ADDED, "width": 5}, {"chart": neighbour, "width": 7}], *([rest] if rest else []),
                  *layout[at + 1:-1]]
    r = patch_config({"op": "layout.set", "dashboard": dkey, "layout": new_layout})
    assert r.status == 200 and r.json["written"] == {"dashboard.yaml": "exact"}, r.text
    assert r.json["dashboard"]["dashboards"][dkey]["layout"] == new_layout

    # 3. 필터 추가(더한 차트에만 적용)
    r = patch_config({"op": "filter.add", "dashboard": dkey, "filter": {
        "key": ADDED_FILTER, "label": "단계", "type": "select", "dataset": "deals", "column": "pipeline_step",
        "charts": [ADDED]}})
    assert r.status == 200 and r.json["written"] == {"dashboard.yaml": "exact"}, r.text

    # 파일: make check 통과, 다른 대시보드와 추론 주석은 그대로
    project = config.load_project(NAME, PROJECTS)
    assert project.dashboard["dashboards"][dkey]["layout"] == new_layout
    after_text = (PROJECTS / NAME / "dashboard.yaml").read_text(encoding="utf-8")
    assert after_text.count("# 추론: 확인 필요") == before_text.count("# 추론: 확인 필요")
    assert {k: v for k, v in project.dashboard["dashboards"].items() if k != dkey} == \
        {k: v for k, v in config.load_project(NAME, PROJECTS).dashboard["dashboards"].items() if k != dkey}
    plan = studio("GET", f"/projects/{NAME}/plan").json
    assert plan["changed"] is True, "차트·배치·필터는 적용해야 Superset에 반영된다"

    # 4. 적용 → Superset에 차트가 생기고, 대시보드 position에 그 자리·그 너비로 놓인다
    done, log = apply(NAME)
    assert done["ok"] is True, (done, log[-10:])
    chart_id = superset.find_id("chart", chart_uuid)
    assert chart_id is not None
    chart = superset.get(f"/api/v1/chart/{chart_id}").json()["result"]
    assert chart["slice_name"] == "Studio가 더한 차트" and chart["viz_type"] == "pie"
    position, filters = superset_dashboard(superset, dkey)
    nodes = chart_nodes(position)
    node = nodes[f"CHART-{ADDED}"]
    assert node["meta"]["width"] == 5 and node["meta"]["chartId"] == chart_id
    assert nodes[f"CHART-{neighbour}"]["meta"]["width"] == 7
    parent = position[node["parents"][-1]]
    assert parent["type"] == "ROW" and parent["children"] == [f"CHART-{ADDED}", f"CHART-{neighbour}"]
    grid_rows = [c for c in position["GRID_ID"]["children"]]
    assert grid_rows.index(parent["id"]) == at
    added_filter = next(f for f in filters if f["id"] == f"NATIVE_FILTER-{ADDED_FILTER}")
    assert added_filter["name"] == "단계" and added_filter["targets"][0]["column"]["name"] == "pipeline_step"
    # charts: [더한 차트] → 그 밖의 차트는 필터 범위에서 빠진다
    assert set(added_filter["scope"]["excluded"]) == {n["meta"]["chartId"] for k, n in nodes.items() if k != f"CHART-{ADDED}"}
    assert chart_id not in added_filter["scope"]["excluded"] and len(nodes) > 1
    assert studio("GET", f"/projects/{NAME}/plan").json["changed"] is False
    code, app = api("GET", "/api/app")
    assert code == 200 and app["project"] == NAME and app["applied"] is True

    # 5. 차트 삭제: charts·layout·필터의 charts에서 빠진다 → 적용하면 Superset에서 사라진다
    r = patch_config({"op": "chart.remove", "key": ADDED})
    assert r.status == 200 and r.json["written"] == {"dashboard.yaml": "exact"}, r.text
    got = r.json["dashboard"]["dashboards"][dkey]
    assert ADDED not in [c["key"] for c in got["charts"]]
    assert got["layout"][at] == [{"chart": neighbour, "width": 7}]
    assert "charts" not in next(f for f in got["filters"] if f["key"] == ADDED_FILTER)
    done, log = apply(NAME)
    assert done["ok"] is True, (done, log[-10:])
    assert superset.find_id("chart", chart_uuid) is None, "지운 차트는 prune으로 Superset에서 사라진다"
    position, filters = superset_dashboard(superset, dkey)
    assert f"CHART-{ADDED}" not in position
    assert set(chart_nodes(position)) == {f"CHART-{c['key']}" for c in got["charts"]}

    # 뒷정리: 필터를 지우고 layout을 처음대로 되돌린다(값이 처음과 같다)
    r = patch_config({"op": "filter.remove", "dashboard": dkey, "key": ADDED_FILTER},
                     {"op": "layout.set", "dashboard": dkey, "layout": dashboards[dkey]["layout"]})
    assert r.status == 200, r.text
    assert r.json["dashboard"]["dashboards"] == dashboards
    done, _log = apply(NAME)
    assert done["ok"] is True
    position, filters = superset_dashboard(superset, dkey)
    assert all(f["id"] != f"NATIVE_FILTER-{ADDED_FILTER}" for f in filters)


def test_invalid_layout_is_refused_and_superset_stays(applied, superset):
    body = studio("GET", f"/projects/{NAME}/config").json
    dkey = next(iter(body["dashboard"]["dashboards"]))
    before = (PROJECTS / NAME / "dashboard.yaml").read_bytes()
    r = patch_config({"op": "layout.set", "dashboard": dkey, "layout": [["no_such_chart"]]})
    assert (r.status, r.json["detail"]) == (422, "invalid_config") and any("no_such_chart" in e for e in r.json["errors"])
    r = patch_config({"op": "layout.set", "dashboard": dkey, "layout": "rows"})
    assert (r.status, r.json["detail"], r.json["op_index"]) == (400, "invalid_op", 0)
    assert (PROJECTS / NAME / "dashboard.yaml").read_bytes() == before
    assert studio("GET", f"/projects/{NAME}/plan").json["changed"] is False


def test_failed_apply_reports_the_step_and_web_app_stays(applied):
    """load에서 실패하면(검사는 통과하지만 CSV가 틀림) DB와 웹앱은 이전 상태 그대로다."""
    path = PROJECTS / NAME / "data" / "deals.csv"
    good = path.read_bytes()
    try:
        path.write_bytes(good + csv_lines(path)[1] + b"\n")            # 손으로 넣은 중복 키(Studio의 PUT은 거절한다)
        done, log = apply(NAME)
        assert done["ok"] is False and done["step"] == "load", done
        assert any("기본 키 중복" in e for e in done["errors"])
        assert any(line.startswith("✖ load 실패") for line in log)
    finally:
        path.write_bytes(good)
    code, app = api("GET", "/api/app")
    assert app["project"] == NAME and app["applied"] is True


def test_temporary_project_objects_are_gone_after_restoring(original, superset, request):
    """정리 뒤의 상태는 모듈 끝의 fixture가 확인한다. 여기서는 전환 전에 임시 프로젝트의 객체가 Superset에 있음을 본다."""
    project = config.load_project(NAME, PROJECTS)
    dkey = next(iter(project.dashboard["dashboards"]))
    assert superset.find_id("dashboard", config.object_uuid(NAME, "dashboard", dkey)) is not None
    before = config.load_project(original, PROJECTS)
    okey = next(iter(before.dashboard["dashboards"]))
    assert superset.find_id("dashboard", config.object_uuid(original, "dashboard", okey)) is None, \
        "다른 프로젝트를 적용하면 이전 프로젝트의 객체는 지워진다"
