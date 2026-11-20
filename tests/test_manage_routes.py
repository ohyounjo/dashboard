"""관리 서비스 경로를 프로세스 안에서(ASGI) 직접 부른다: 업로드 오류의 응답 형식, 업로드 임시 파일 정리(리뷰 지적 회귀).

make test 1단계(PROJECT=devtracker, 누적 테이블 없음)에서 돈다. 인증 DB에 테스트 계정·세션을 만들고 지운다.
"""
from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timedelta, timezone

import pytest

from studio_client import Client

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def client():
    from dashkit.auth import passwords, sessions, store
    from dashkit.manage.app import app

    h = passwords.hash_password(secrets.token_urlsafe(12))
    user = store.create_user(f"routes-{secrets.token_hex(4)}@example.com", role="admin", status="active",
                             source="invite", password_hash=h, email_verified=True)
    token = sessions.new_token()
    assert store.start_session(user.id, h, sessions.token_hash(token),
                               datetime.now(timezone.utc) + timedelta(hours=1), None, None)
    yield Client(app, host="localhost"), {"cookie": f"dashkit_session={token}"}
    store.delete_user(user.id)


def _leftovers() -> list:
    from dashkit.manage.uploads import uploads

    return [p for p in uploads._root.iterdir()]


def test_bad_multipart_gets_its_own_status_not_500(client):
    c, headers = client
    raw = b"--XyZ\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.csv\"\r\n\r\nid\n"   # 끝 경계 없음
    r = c.post("/api/manage/tables/whatever/batches", body=raw,
               headers={**headers, "content-type": "multipart/form-data; boundary=XyZ"})
    assert r.status in (400, 404), r.body       # 테이블이 없으면 404가 먼저일 수 있다
    body = json.loads(r.body)
    assert body["detail"] in ("bad_request", "unknown_table")


def test_direct_upload_file_is_removed_after_the_job(client):
    from dashkit import config
    from dashkit.manage import context

    c, headers = client
    table = next(iter(config.load_project(context.current_project()).schema["tables"]))
    before = _leftovers()
    boundary = "Q" + secrets.token_hex(4)
    raw = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.csv\"\r\n\r\n"
           f"id\r\nx\r\n--{boundary}--\r\n").encode()
    r = c.post(f"/api/manage/tables/{table}/batches", body=raw,
               headers={**headers, "content-type": f"multipart/form-data; boundary={boundary}"})
    assert r.status == 202, r.body
    job = json.loads(r.body)["job"]
    for _ in range(100):
        state = json.loads(c.get(f"/api/manage/jobs/{job['id']}", headers=headers).body)
        if state["status"] != "running":
            break
        time.sleep(0.1)
    assert state["status"] == "failed" and state["error"]["detail"] == "not_append"   # 예시는 모두 replace 테이블
    assert _leftovers() == before, "미리보기 없이 올린 파일은 작업이 끝나면 지운다"
    # 잘못된 본문으로 실패한 요청의 폴더도 남지 않는다
    r = c.post(f"/api/manage/tables/{table}/batches", body=b"--zz\r\n",
               headers={**headers, "content-type": "multipart/form-data; boundary=zz"})
    assert r.status == 400 and json.loads(r.body)["detail"] == "bad_request"
    assert _leftovers() == before
