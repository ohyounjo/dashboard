"""Studio 요청 보호(contracts/studio.md "요청 보호"): Host, Origin, Content-Type, 응답 헤더·CSP. 스택 없이 돈다."""
from __future__ import annotations

import pytest

from studio_client import Client, fresh_client

from dashkit.studio import app as studio_app
from dashkit.studio.guard import Guard, Policy

MOVE = {"ops": [{"op": "tab.move", "key": "docs", "index": 0}]}


@pytest.fixture
def client(tmp_path, monkeypatch) -> Client:
    return fresh_client(tmp_path, monkeypatch, projects=("devtracker",))


def before(client: Client) -> bytes:
    return (client.projects / "devtracker" / "dashboard.yaml").read_bytes()


# ---------------------------------------------------------------- Host

@pytest.mark.parametrize("host", ["localhost:8090", "127.0.0.1:8090", "LOCALHOST:8090"])
def test_allowed_hosts(client, host):
    assert client.get("/studio/api/health", headers={"host": host}).status == 200


@pytest.mark.parametrize("host", [
    "evil.example:8090",          # DNS rebinding: 다른 이름이 127.0.0.1을 가리킴
    "localhost:8080",             # 웹앱의 포트
    "localhost",                  # 포트 없음(= 80)
    "localhost.evil.example:8090",
    "127.0.0.1.nip.io:8090",
    "[::1]:8090",
    "",
])
def test_other_hosts_are_rejected_everywhere(client, host):
    for method, path in (("GET", "/studio/api/health"), ("GET", "/"), ("GET", "/studio/static/app.js"),
                         ("GET", "/no/such/path"), ("PATCH", "/studio/api/projects/devtracker/config")):
        r = client.request(method, path, json=MOVE if method == "PATCH" else None, headers={"host": host})
        assert (r.status, r.json["detail"]) == (403, "forbidden_host"), (method, path)
        assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


def test_missing_host_is_rejected(client):
    assert client.get("/studio/api/health", headers={"host": None}).status == 403


def test_host_follows_studio_origin(tmp_path, monkeypatch):
    """STUDIO_ORIGIN이 다른 포트(테스트 스택의 18090)면 그 포트만 받는다. 컨테이너 안의 포트(8090)는 로컬 이름으로만."""
    c = fresh_client(tmp_path, monkeypatch, origin="http://localhost:18090", projects=())
    for host, status in (("localhost:18090", 200), ("127.0.0.1:18090", 200),
                         ("localhost:8090", 200), ("127.0.0.1:8090", 200),      # compose healthcheck
                         ("studio:8090", 403), ("localhost:8080", 403), ("example.com:18090", 403)):
        assert c.get("/studio/api/health", headers={"host": host}).status == status, host


def test_policy_for_named_origin_and_default_port():
    p = Policy.from_env({"STUDIO_ORIGIN": "https://studio.internal", "WEB_PUBLIC_URL": "https://dash.internal:8443/x"})
    assert {"studio.internal", "studio.internal:443", "localhost", "127.0.0.1", "localhost:8090"} <= p.hosts
    assert "studio.internal:8090" not in p.hosts          # 컨테이너 포트는 로컬 이름만
    assert "https://studio.internal" in p.origins and "http://studio.internal" not in p.origins
    assert "frame-src https://dash.internal:8443;" in p.csp
    # 형식이 틀린 값은 기본값으로
    assert "localhost:8090" in Policy.from_env({"STUDIO_ORIGIN": "not a url"}).hosts


def test_duplicate_host_header_is_rejected(client):
    scope_headers = [(b"host", b"localhost:8090"), (b"host", b"evil.example")]

    import asyncio

    async def call():
        out = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            out.append(m)

        await client.app({"type": "http", "method": "GET", "path": "/studio/api/health", "raw_path": b"/studio/api/health",
                          "query_string": b"", "headers": scope_headers, "scheme": "http", "http_version": "1.1",
                          "server": ("127.0.0.1", 8090), "client": ("127.0.0.1", 1), "root_path": ""}, receive, send)
        return out

    assert asyncio.run(call())[0]["status"] == 400


# ---------------------------------------------------------------- Origin

@pytest.mark.parametrize("origin", ["http://localhost:8090", "http://127.0.0.1:8090"])
def test_same_origin_writes_pass(client, origin):
    r = client.patch("/studio/api/projects/devtracker/config", json=MOVE, headers={"origin": origin})
    assert r.status == 200


def test_write_without_origin_passes(client):
    """Origin이 없는 요청(브라우저가 아닌 호출)은 Host 검사만 받는다."""
    assert client.patch("/studio/api/projects/devtracker/config", json=MOVE).status == 200


@pytest.mark.parametrize("origin", [
    "http://localhost:8080",       # 웹앱(같은 사이트, 다른 포트)
    "http://localhost:8088",       # Superset
    "https://localhost:8090",      # scheme이 다름
    "http://evil.example",
    "http://evil.example:8090",
    "null",                        # sandbox iframe, file://
    "",
])
@pytest.mark.parametrize("method,path", [
    ("PATCH", "/studio/api/projects/devtracker/config"),
    ("POST", "/studio/api/projects"),
    ("POST", "/studio/api/projects/devtracker/apply"),
    ("PUT", "/studio/api/projects/devtracker/data/tasks"),
    ("DELETE", "/studio/api/projects/devtracker"),
])
def test_cross_origin_writes_are_rejected(client, origin, method, path):
    old = before(client)
    r = client.request(method, path, json=MOVE, headers={"origin": origin})
    assert (r.status, r.json["detail"]) == (403, "forbidden_origin")
    assert before(client) == old
    assert studio_app.runner.status()["last"] is None and not studio_app.runner.status()["running"]


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_sec_fetch_site_from_other_sites_is_rejected(client, site):
    r = client.patch("/studio/api/projects/devtracker/config", json=MOVE, headers={"sec-fetch-site": site})
    assert (r.status, r.json["detail"]) == (403, "forbidden_origin")
    ok = client.patch("/studio/api/projects/devtracker/config", json=MOVE,
                      headers={"sec-fetch-site": "same-origin", "origin": "http://localhost:8090"})
    assert ok.status == 200


def test_reads_do_not_need_origin_check(client):
    """GET은 상태를 바꾸지 않는다. 다른 출처의 읽기는 브라우저의 동일 출처 정책이 막는다(CORS 헤더를 주지 않는다)."""
    r = client.get("/studio/api/projects", headers={"origin": "http://evil.example"})
    assert r.status == 200
    assert not any(k.startswith("access-control-") for k in r.headers)


def test_preflight_gets_no_cors_permission(client):
    r = client.request("OPTIONS", "/studio/api/projects/devtracker/config",
                       headers={"origin": "http://evil.example", "access-control-request-method": "PATCH"})
    assert r.status == 405 and not any(k.startswith("access-control-") for k in r.headers)


# ---------------------------------------------------------------- Content-Type

@pytest.mark.parametrize("ctype", ["application/x-www-form-urlencoded", "text/plain", "text/plain; charset=utf-8",
                                   "application/jsonx", "multipart/form-data; boundary=x"])
def test_json_endpoints_only_take_json(client, ctype):
    old = before(client)
    import json
    for method, path in (("PATCH", "/studio/api/projects/devtracker/config"), ("POST", "/studio/api/projects")):
        r = client.request(method, path, body=json.dumps(MOVE).encode(), headers={"content-type": ctype})
        assert (r.status, r.json["detail"]) == (415, "unsupported_media_type"), (method, ctype)
    assert before(client) == old


def test_json_endpoint_without_content_type_is_rejected(client):
    import json
    r = client.request("PATCH", "/studio/api/projects/devtracker/config", body=json.dumps(MOVE).encode())
    assert (r.status, r.json["detail"]) == (415, "unsupported_media_type")


def test_json_content_type_with_charset_passes(client):
    import json
    r = client.request("PATCH", "/studio/api/projects/devtracker/config", body=json.dumps(MOVE).encode(),
                       headers={"content-type": "Application/JSON; charset=utf-8"})
    assert r.status == 200


@pytest.mark.parametrize("body", [b"[]", b"not json", b'"text"', b"\xff\xfe"])
def test_json_body_must_be_an_object(client, body):
    r = client.request("PATCH", "/studio/api/projects/devtracker/config", body=body,
                       headers={"content-type": "application/json"})
    assert (r.status, r.json["detail"]) == (400, "invalid_request")


@pytest.mark.parametrize("path", ["/studio/api/projects/infer", "/studio/api/projects/devtracker/data/tasks/check"])
def test_upload_endpoints_only_take_multipart(client, path):
    r = client.post(path, json={"files": "x"})
    assert (r.status, r.json["detail"]) == (415, "unsupported_media_type")
    r = client.post(path, body=b"a,b\n1,2\n", headers={"content-type": "text/csv"})
    assert (r.status, r.json["detail"]) == (415, "unsupported_media_type")


def test_form_post_cannot_start_apply(client, monkeypatch):
    """HTML 폼(사전 요청 없는 단순 요청)으로는 apply를 시작할 수 없다. 본문 없는 fetch(Content-Type 없음)는 된다."""
    started = []
    from dashkit.studio import jobs
    monkeypatch.setattr(studio_app, "runner", jobs.ApplyRunner(lambda name, log: started.append(name)))
    for ctype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x"):
        r = client.post("/studio/api/projects/devtracker/apply", body=b"", headers={"content-type": ctype})
        assert r.status == 415, ctype
    assert started == []
    assert client.post("/studio/api/projects/devtracker/apply").status == 200
    assert client.post("/studio/api/projects/devtracker/apply", json={}).status == 200
    assert started == ["devtracker", "devtracker"]


# ---------------------------------------------------------------- 응답 헤더

def check_headers(r, web="http://localhost:8080"):
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    csp = dict(part.strip().split(" ", 1) for part in r.headers["content-security-policy"].split(";"))
    assert csp["default-src"] == "'self'" and csp["script-src"] == "'self'"
    assert csp["frame-src"] == web                      # 미리보기 iframe은 웹앱 주소만
    assert csp["frame-ancestors"] == "'none'"           # Studio 자신은 어디에도 넣을 수 없다
    assert csp["connect-src"] == "'self'" and csp["base-uri"] == "'self'" and csp["form-action"] == "'self'"
    assert "unsafe-eval" not in r.headers["content-security-policy"]
    assert "unsafe-inline" not in csp["script-src"]


def test_headers_on_every_kind_of_response(client, tmp_path, monkeypatch):
    web = tmp_path / "webdir"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>s</title>", encoding="utf-8")
    (web / "app.js").write_text("export {};", encoding="utf-8")
    monkeypatch.setenv("STUDIO_WEB_DIR", str(web))
    responses = [
        client.get("/studio/api/health"),                                           # 200 JSON
        client.get("/"),                                                            # 화면
        client.get("/studio/static/app.js"),                                        # 정적 파일
        client.get("/studio/static/missing.js"),                                    # 404
        client.get("/studio/api/nope"),                                             # 없는 경로
        client.request("DELETE", "/studio/api/health"),                             # 405
        client.get("/studio/api/health", headers={"host": "evil.example"}),         # 403
        client.patch("/studio/api/projects/devtracker/config", json={"ops": []}),   # 400
        client.post("/studio/api/projects/devtracker/apply"),                       # 스트림
    ]
    assert [r.status for r in responses] == [200, 200, 200, 404, 404, 405, 403, 400, 200]
    for r in responses:
        check_headers(r)
    assert responses[1].headers["content-type"].startswith("text/html")
    assert responses[2].headers["content-type"].startswith("text/javascript")
    assert responses[8].headers["content-type"].startswith("text/event-stream")


def test_frame_src_follows_web_public_url(tmp_path, monkeypatch):
    c = fresh_client(tmp_path, monkeypatch, projects=())
    monkeypatch.setenv("WEB_PUBLIC_URL", "http://localhost:18080/")
    c = Client(Guard(studio_app.api), "localhost:8090")
    check_headers(c.get("/studio/api/health"), web="http://localhost:18080")


def test_error_bodies_use_the_api_format(client):
    for r in (client.get("/studio/api/nope"), client.request("DELETE", "/studio/api/health"),
              client.get("/studio/api/projects/nope/config")):
        assert set(r.json) >= {"detail", "message"} and r.headers["content-type"].startswith("application/json")
    assert client.get("/studio/api/nope").json["detail"] == "not_found"
    assert client.request("DELETE", "/studio/api/health").json["detail"] == "method_not_allowed"


def test_no_api_docs_routes(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status == 404


def test_projects_tell_the_studio_origin_for_the_preview(tmp_path, monkeypatch):
    """미리보기(웹앱 iframe)는 STUDIO_ORIGIN에서 연 Studio에서만 뜬다(웹앱의 frame-ancestors가 그 출처 하나이고,
    로그인 쿠키는 SameSite=Lax라 다른 사이트의 틀 안에서는 실리지 않는다). 화면이 다른 주소(127.0.0.1 등)로 열렸는지
    알 수 있게 목록 응답에 그 출처를 싣는다."""
    c = fresh_client(tmp_path, monkeypatch, origin="http://localhost:18090/", projects=())
    body = c.get("/studio/api/projects", headers={"host": "127.0.0.1:18090"}).json
    assert body["studio_url"] == "http://localhost:18090"
    assert body["web_url"] == "http://localhost:8080"


def test_studio_origin_is_normalized():
    from dashkit.studio.guard import studio_origin
    assert studio_origin({}) == "http://localhost:8090"
    assert studio_origin({"STUDIO_ORIGIN": "http://LOCALHOST:18090/x"}) == "http://localhost:18090"
    assert studio_origin({"STUDIO_ORIGIN": "https://studio.internal:443"}) == "https://studio.internal"
    assert studio_origin({"STUDIO_ORIGIN": "http://127.0.0.1:80"}) == "http://127.0.0.1"
    assert studio_origin({"STUDIO_ORIGIN": "not a url"}) == "http://localhost:8090"
