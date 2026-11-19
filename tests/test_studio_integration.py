"""Studio만으로 CSV 폴더에서 대시보드까지(보강설계서 8절 5a 완료 조건). 테스트 스택에서 실행 중인 Studio에 HTTP로 붙는다.

`make test`의 Studio 단계가 `make studio`로 Studio를 띄운 뒤 STUDIO_TEST_URL=http://studio:8090 을 주고 이 파일만 실행한다
(tools 컨테이너에서 돈다. ruamel.yaml이 필요 없다). 평소 pytest 실행에서는 건너뛴다.

흐름: CSV 업로드 → 추론 → 프로젝트 생성 → apply → 웹앱이 그 프로젝트를 보여 줌 → 탭 이동(PATCH) → 재적용 없이 웹앱에 반영
      → CSV 교체 check/PUT → apply. 끝나면 임시 프로젝트(projects/zz-studio-it)를 지우고 원래 프로젝트를 다시 적용한다.
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
