"""백엔드 API(nginx 경유)와 guest token 권한 경계."""
from __future__ import annotations

import base64
import json
import os

import pytest
import requests

from dashkit import config
from dashkit.config import load_project

from conftest import PROJECTS, api, live_project

# 허용 출처(WEB_ORIGINS)의 첫 항목. 테스트 스택은 개발 스택과 포트가 다르다
REFERER = {"Referer": (os.environ.get("WEB_ORIGINS") or "http://localhost:8080").split(",")[0].strip() + "/"}
pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def project():
    return load_project(live_project(), PROJECTS)


def claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def superset_dashboards(project) -> list[str]:
    return [sec["dashboard"] for tab in project.dashboard["tabs"] for sec in tab["sections"] if sec["type"] == "superset"]


@pytest.fixture(scope="module")
def two_dashboards(project):
    keys = list(dict.fromkeys(superset_dashboards(project)))
    assert len(keys) >= 2, "권한 경계 확인에는 superset 섹션 대시보드가 2개 이상 필요"
    return keys[0], keys[1]


def guest_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"X-GuestToken": token, **REFERER})
    return s


# ---------------------------------------------------------------- 기본 엔드포인트

def test_health_and_app(project):
    code, body = api("GET", "/api/health")
    assert code == 200 and body["status"] == "ok" and body["project"] == project.name and body["applied"] is True
    code, app = api("GET", "/api/app")
    assert code == 200
    assert app["project"] == project.name and app["applied"] is True
    assert [t["key"] for t in app["tabs"]] == [t["key"] for t in project.dashboard["tabs"]]
    assert app["app"]["as_of"] == config.resolve_as_of(project).isoformat()
    for tab in app["tabs"]:
        for sec in tab["sections"]:
            if sec["type"] == "superset":
                assert sec["embed_id"]


def test_data_only_for_widget_datasets(project):
    allowed = config.widget_datasets(project)
    assert allowed
    for ds in allowed:
        code, body = api("GET", f"/api/data/{ds}")
        assert code == 200 and body["rows"] and {c["name"] for c in body["columns"]}
    others = set(project.dashboard["datasets"]) - allowed
    assert others, "위젯이 참조하지 않는 데이터셋이 있어야 404를 확인할 수 있음"
    for ds in others:
        code, body = api("GET", f"/api/data/{ds}")
        assert code == 404 and body["detail"] == "unknown_dataset"
    code, _ = api("GET", "/api/data/pg_catalog.pg_roles")
    assert code == 404


def test_guest_token_contract(two_dashboards):
    code, body = api("POST", "/api/guest-token", {"dashboard": "no_such_dashboard"})
    assert code == 404 and body["detail"] == "unknown_dashboard"
    code, body = api("POST", "/api/guest-token", {})
    assert code == 422 and body["detail"] == "invalid_request"
    code, body = api("POST", "/api/guest-token", {"dashboard": two_dashboards[0]})
    assert code == 200 and body["expires_at"].endswith("Z")
    c = claims(body["token"])
    assert c["type"] == "guest" and c["rls_rules"] == [] and len(c["resources"]) == 1
    assert c["exp"] - c["iat"] == 300


# ---------------------------------------------------------------- guest token으로 Superset

@pytest.fixture(scope="module")
def guest(project, two_dashboards, superset):
    mine, other = two_dashboards
    _, body = api("POST", "/api/guest-token", {"dashboard": mine})
    token = body["token"]
    embed_id = claims(token)["resources"][0]["id"]
    ids = {k: superset.find_id("dashboard", config.object_uuid(project.name, "dashboard", k)) for k in (mine, other)}
    return guest_session(token), embed_id, ids[mine], ids[other], mine, other


def _first_chart(project, dkey):
    return project.dashboard["dashboards"][dkey]["charts"][0]


def _chart_query(superset, project, dkey, dashboard_id):
    chart = _first_chart(project, dkey)
    chart_id = superset.find_id("chart", config.object_uuid(project.name, "chart", chart["key"]))
    detail = superset.get(f"/api/v1/chart/{chart_id}").json()["result"]
    params = json.loads(detail["params"])
    ds_id = superset.find_id("dataset", config.object_uuid(project.name, "dataset", chart["dataset"]))
    q = {"columns": [], "metrics": [], "filters": [], "row_limit": 10, "extras": {}}
    if "metric" in params:
        q["metrics"] = [params["metric"]]
    elif params.get("metrics"):
        q["metrics"] = params["metrics"][:1]
    elif params.get("all_columns"):
        q["columns"] = params["all_columns"][:1]
    body = {
        "datasource": {"id": ds_id, "type": "table"},
        "queries": [q],
        "form_data": {**params, "slice_id": chart_id, "dashboardId": dashboard_id},
        "result_format": "json", "result_type": "full",
    }
    return chart_id, body


def test_guest_can_view_embedded_dashboard_and_chart_data(guest, project, superset):
    s, embed_id, mine_id, _, mine, _ = guest
    r = s.get(f"{superset.base_url}/embedded/{embed_id}")
    assert r.status_code == 200
    r = s.get(f"{superset.base_url}/api/v1/dashboard/{mine_id}")
    assert r.status_code == 200
    r = s.get(f"{superset.base_url}/api/v1/dashboard/{mine_id}/charts")
    assert r.status_code == 200 and len(r.json()["result"]) == len(project.dashboard["dashboards"][mine]["charts"])
    _, body = _chart_query(superset, project, mine, mine_id)
    r = s.post(f"{superset.base_url}/api/v1/chart/data", json=body)
    assert r.status_code == 200, r.text[:300]
    assert r.json()["result"][0]["data"]


def test_guest_time_range_and_form_data_permissions(guest, project, superset):
    s, _, _, _, _, other = guest
    base = superset.base_url
    response = s.get(f"{base}/api/v1/time_range/", params={"q": "'Last week'"})
    assert response.status_code == 200, response.text[:300]
    parsed = response.json()["result"][0]
    assert parsed["since"] and parsed["until"] and parsed["timeRange"] == "Last week"
    invalid = s.get(f"{base}/api/v1/time_range/", params={"q": "'2026-01-01 : invalid'"})
    assert invalid.status_code == 400
    other_chart = _first_chart(project, other)
    chart_id = superset.find_id("chart", config.object_uuid(project.name, "chart", other_chart["key"]))
    assert s.get(f"{base}/api/v1/form_data/", params={"slice_id": chart_id}).status_code == 403


def test_guest_cannot_call_explore_json(guest, project, superset):
    """이슈 #50 확정 결정: guest에게 can_explore_json on Superset을 주지 않으므로
    생성 SQL이 노출되는 deprecated explore_json 경로가 기본/query=true/GET 모두 403으로 차단돼야 한다.
    """
    s, _, mine_id, _, mine, _ = guest
    chart_id, body = _chart_query(superset, project, mine, mine_id)
    ds = body["datasource"]
    form_data = {**body["form_data"], "datasource": f"{ds['id']}__{ds['type']}"}
    url = f"{superset.base_url}/superset/explore_json/"
    base_params = {"form_data": json.dumps({"slice_id": chart_id}), "dashboard_id": str(mine_id)}
    payload = {"form_data": json.dumps(form_data)}

    r = s.post(url, params=base_params, data=payload, allow_redirects=False)
    assert r.status_code == 403, r.text[:300]
    assert "SELECT " not in r.text.upper()

    r_query = s.post(url, params={**base_params, "query": "true"}, data=payload, allow_redirects=False)
    assert r_query.status_code == 403, r_query.text[:300]
    assert "SELECT " not in r_query.text.upper()

    r_get = s.get(url, params={"form_data": json.dumps(form_data), "dashboard_id": str(mine_id)}, allow_redirects=False)
    assert r_get.status_code == 403, r_get.text[:300]
    assert "SELECT " not in r_get.text.upper()


def test_guest_cannot_view_other_dashboard(guest, project, superset):
    s, _, _, other_id, _, other = guest
    r = s.get(f"{superset.base_url}/api/v1/dashboard/{other_id}")
    assert r.status_code in (403, 404)
    r = s.get(f"{superset.base_url}/api/v1/dashboard/{other_id}/charts")
    assert r.status_code in (403, 404)
    _, body = _chart_query(superset, project, other, other_id)
    r = s.post(f"{superset.base_url}/api/v1/chart/data", json=body)
    assert r.status_code in (401, 403), r.text[:200]


def test_guest_query_scope(guest, project, superset):
    """guest가 차트 데이터 API로 닿을 수 있는 범위(Superset 6.1.0 실측, contracts/superset-notes.md 4절 정정).

    - 자기 대시보드 차트가 쓰는 데이터셋 밖은 거부된다(다른 대시보드의 데이터셋, 대시보드에 없는 차트).
    - 서브쿼리는 거부된다(다른 뷰·raw 우회 불가).
    - 한계: 자기 대시보드 데이터셋 안에서는 지표·열을 바꾼 요청도 허용된다(guest는 차트 목록 권한이 없어
      Superset의 payload 변조 검사가 건너뛰어진다). 그래서 dash 뷰에는 보여도 되는 열만 둔다.
    """
    s, _, mine_id, other_id, mine, other = guest
    url = f"{superset.base_url}/api/v1/chart/data"
    _, body = _chart_query(superset, project, mine, mine_id)

    sub = json.loads(json.dumps(body))
    sub["queries"][0]["metrics"] = [{"expressionType": "SQL", "sqlExpression": "(SELECT count(*) FROM raw.issues)", "label": "x"}]
    assert s.post(url, json=sub).status_code == 400

    mine_datasets = {c["dataset"] for c in project.dashboard["dashboards"][mine]["charts"]}
    outside = next((key for key in project.dashboard["datasets"] if key not in mine_datasets), None)
    assert outside is not None, "다른 데이터셋의 접근 경계 확인에 대시보드 밖 데이터셋이 필요"
    other_ds = superset.find_id("dataset", config.object_uuid(project.name, "dataset", outside))
    cross = json.loads(json.dumps(body))
    cross["datasource"]["id"] = other_ds
    assert s.post(url, json=cross).status_code == 403

    csv = json.loads(json.dumps(body))
    csv["result_format"] = "csv"
    assert s.post(url, json=csv).status_code == 403

    other_chart = _first_chart(project, other)
    other_chart_id = superset.find_id("chart", config.object_uuid(project.name, "chart", other_chart["key"]))
    foreign = json.loads(json.dumps(body))
    foreign["form_data"]["slice_id"] = other_chart_id  # 내 대시보드 id + 남의 차트
    assert s.post(url, json=foreign).status_code == 403


def test_guest_cannot_write(guest, project, superset):
    s, _, mine_id, _, mine, _ = guest
    chart_id, _ = _chart_query(superset, project, mine, mine_id)
    before_chart = superset.get(f"/api/v1/chart/{chart_id}").json()["result"]
    before_dash = superset.get(f"/api/v1/dashboard/{mine_id}").json()["result"]
    base = superset.base_url
    attempts = [
        ("PUT", f"{base}/api/v1/chart/{chart_id}", {"slice_name": "changed", "description": "changed"}),
        ("DELETE", f"{base}/api/v1/chart/{chart_id}", None),
        ("PUT", f"{base}/api/v1/dashboard/{mine_id}", {"dashboard_title": "changed"}),
        ("POST", f"{base}/api/v1/dashboard/", {"dashboard_title": "new"}),
        ("POST", f"{base}/api/v1/sqllab/execute/", {"database_id": 1, "sql": "SELECT 1"}),
    ]
    for method, url, body in attempts:
        r = s.request(method, url, json=body, allow_redirects=False)
        assert not (200 <= r.status_code < 300), (method, url, r.status_code)
    assert s.get(f"{base}/api/v1/security/csrf_token/").status_code in (401, 403)
    after_chart = superset.get(f"/api/v1/chart/{chart_id}").json()["result"]
    after_dash = superset.get(f"/api/v1/dashboard/{mine_id}").json()["result"]
    assert after_chart["slice_name"] == before_chart["slice_name"]
    assert after_chart.get("description") == before_chart.get("description")
    assert after_dash["dashboard_title"] == before_dash["dashboard_title"]


def test_embedded_page_rejects_other_referrer(guest, superset):
    _, embed_id, *_ = guest
    r = requests.get(f"{superset.base_url}/embedded/{embed_id}", headers={"Referer": "http://evil.test/"})
    assert r.status_code == 403
