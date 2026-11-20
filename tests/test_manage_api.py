"""관리 서비스(/api/manage, contracts/manage.md)의 골격: 상태 확인, 로그인 확인과 등급, 요청 보호.

make test가 테스트 스택에 관리 서비스를 띄운 뒤(make manage) nginx(web)를 거쳐 부른다.
"""
from __future__ import annotations

import secrets

import pytest

from conftest import WEB_URL, delete_user, http_full, login, make_user, random_email

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def cookies():
    made, out = [], {}
    for role in ("user", "operator", "admin"):
        email, password = random_email(f"manage-{role}"), secrets.token_urlsafe(18)
        made.append(make_user(email, password, role=role).id)
        out[role] = login(email, password)
    yield out
    for uid in made:
        delete_user(uid)


def get(path: str, cookie: str | None = None):
    headers = {"Cookie": f"dashkit_session={cookie}"} if cookie else {}
    return http_full("GET", f"{WEB_URL}/api/manage{path}", headers=headers)


def test_health_needs_no_login():
    code, body, headers = get("/health")
    assert code == 200 and body == {"status": "ok"}
    assert headers.get("Cache-Control") == "no-store"


def test_config_requires_operator(cookies):
    code, body, _h = get("/config")
    assert code == 401 and body["detail"] == "unauthenticated"
    code, body, _h = get("/config", cookies["user"])
    assert code == 403 and body["detail"] == "forbidden"
    for role in ("operator", "admin"):
        code, body, headers = get("/config", cookies[role])
        assert code == 200, body
        assert body["role"] == role and body["project"]
        assert body["limits"]["inline_max_rows"] > 0 and body["limits"]["page_max"] == 200
        assert headers.get("Cache-Control") == "no-store"


def test_unknown_path_is_contract_404(cookies):
    code, body, _h = get("/nope", cookies["admin"])
    assert code == 404 and body["detail"] == "not_found"


def test_state_changing_requests_are_guarded(cookies):
    cookie = {"Cookie": f"dashkit_session={cookies['admin']}"}
    code, body, _h = http_full("POST", f"{WEB_URL}/api/manage/config", {},
                               {**cookie, "Origin": "https://evil.example"})
    assert code == 403 and body["detail"] == "forbidden_origin"
    code, body, _h = http_full("POST", f"{WEB_URL}/api/manage/config", raw=b"x=1",
                               headers={**cookie, "Content-Type": "application/x-www-form-urlencoded"})
    assert code == 415 and body["detail"] == "unsupported_media_type"
