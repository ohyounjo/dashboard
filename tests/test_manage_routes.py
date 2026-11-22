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


def test_read_paths_never_open_the_owner_account(client, monkeypatch):
    """관리 서비스의 읽기 경로(목록·행 조회·데이터셋)는 dash_inspect만 쓴다. owner 접속을 열면 실패하게 해 둔다."""
    from dashkit import config, db
    from dashkit.manage import context

    def no_owner(*_a, **_k):
        raise AssertionError("읽기 경로가 owner 접속을 열었다")

    monkeypatch.setattr(db, "owner_conn", no_owner)
    c, headers = client
    project = config.load_project(context.current_project())
    table = next(iter(project.schema["tables"]))
    pk = project.schema["tables"][table]["primary_key"]
    for method, path, body in (("GET", "/api/manage/tables", None), ("GET", f"/api/manage/tables/{table}", None),
                               ("GET", f"/api/manage/tables/{table}/batches", None), ("GET", "/api/manage/datasets", None),
                               ("POST", f"/api/manage/tables/{table}/rows/query", {"limit": 5})):
        r = c.request(method, path, json=body, headers=headers)
        assert r.status == 200, (path, r.body)
    page = json.loads(c.post(f"/api/manage/tables/{table}/rows/query", json={"limit": 2}, headers=headers).body)
    assert len(page["rows"]) == 2 and page["total"] is not None
    first = page["rows"][0]
    one = json.loads(c.post(f"/api/manage/tables/{table}/rows/query",
                            json={"key": {k: first[k] for k in pk}}, headers=headers).body)
    assert one["row"] == first
    key = next(iter(project.dashboard["datasets"]))
    r = c.post(f"/api/manage/datasets/{key}/rows/query", json={"limit": 3, "count": True}, headers=headers)
    assert r.status == 200, r.body


def test_filter_injection_is_rejected(client):
    from dashkit import config
    from dashkit.manage import context

    c, headers = client
    table = next(iter(config.load_project(context.current_project()).schema["tables"]))
    for body in ({"filters": [{"column": "x; DROP TABLE raw.y", "op": "eq", "value": 1}]},
                 {"sort": [{"column": "1; --"}]}, {"limit": 1000}):
        r = c.post(f"/api/manage/tables/{table}/rows/query", json=body, headers=headers)
        assert r.status == 422 and json.loads(r.body)["detail"] in ("invalid_filter", "invalid_query"), body


def test_202_job_waits_with_lock_step_and_fails_409_on_timeout(client, monkeypatch):
    """선행 쓰기 잠금 중 202 작업은 step: lock으로 대기하고, 한도 초과 시 409 apply_running으로 실패한다."""
    from dashkit import config, db
    from dashkit.manage import context
    from dashkit.project import locks

    c, headers = client
    project = config.load_project(context.current_project())
    table = next(iter(project.schema["tables"]))
    col = next(iter(project.schema["tables"][table]["columns"]))
    desc = project.schema["tables"][table]["columns"][col].get("description")
    tinfo = json.loads(c.get(f"/api/manage/tables/{table}", headers=headers).body)
    ops = [{"op": "column.update", "table": table, "column": col, "set": {"description": desc}}]

    lock_conn = db.owner_conn()
    try:
        locks.acquire(lock_conn, 0)
        # 1) MANAGE_JOB_LOCK_WAIT_MS 초과 시 status: failed, error: 409 apply_running
        monkeypatch.setenv("MANAGE_JOB_LOCK_WAIT_MS", "200")
        r = c.request("PATCH", "/api/manage/config", json={"ops": ops, "revision": tinfo["revision"]}, headers=headers)
        assert r.status == 202, r.body
        job_id = json.loads(r.body)["job"]["id"]
        for _ in range(50):
            state = json.loads(c.get(f"/api/manage/jobs/{job_id}", headers=headers).body)
            if state["status"] != "running":
                break
            time.sleep(0.05)
        assert state["status"] == "failed", state
        assert state["error"]["status"] == 409 and state["error"]["detail"] == "apply_running", state

        # 2) 긴 대기 한도 안에서는 step: lock으로 기다렸다가 잠금 해제 후 성공한다
        monkeypatch.setenv("MANAGE_JOB_LOCK_WAIT_MS", "5000")
        r = c.request("PATCH", "/api/manage/config", json={"ops": ops, "revision": tinfo["revision"]}, headers=headers)
        assert r.status == 202, r.body
        job_id = json.loads(r.body)["job"]["id"]
        seen_lock = False
        for _ in range(40):
            state = json.loads(c.get(f"/api/manage/jobs/{job_id}", headers=headers).body)
            if state["progress"].get("step") == "lock":
                seen_lock = True
                break
            time.sleep(0.05)
        assert seen_lock, state
    finally:
        locks.release(lock_conn)
        lock_conn.close()

    for _ in range(100):
        state = json.loads(c.get(f"/api/manage/jobs/{job_id}", headers=headers).body)
        if state["status"] != "running":
            break
        time.sleep(0.05)
    assert state["status"] == "succeeded", state


def test_resumable_upload_guard_owner_isolation_and_concurrency(client, monkeypatch):
    """이어 올리기 경로의 ManageGuard, 타인 410, 동시 PATCH 409, DELETE 정리, GET 읽기 경로 owner 미사용."""
    import hashlib
    from dashkit import config, db
    from dashkit.auth import passwords, sessions, store
    from dashkit.manage import context
    from dashkit.manage.app import app
    from dashkit.manage.uploads import uploads

    c, headers = client
    table = next(iter(config.load_project(context.current_project()).schema["tables"]))

    # replace 테이블은 세션 시작 단계에서 즉시 409 not_append
    r_rep = c.post("/api/manage/uploads", json={"table": table, "filename": "a.csv", "total_size": 10}, headers=headers)
    assert r_rep.status == 409 and json.loads(r_rep.body)["detail"] == "not_append"

    # "*" 는 여러 파일/추론 세션으로 생성 가능
    payload = b"id,name\n1,alpha\n2,beta\n"
    sha = hashlib.sha256(payload).hexdigest()
    r_create = c.post(
        "/api/manage/uploads",
        json={"table": "*", "filename": "items.csv", "total_size": len(payload), "sha256": sha},
        headers=headers,
    )
    assert r_create.status == 201, r_create.body
    uid = json.loads(r_create.body)["upload_id"]
    try:
        # GET /uploads/{id} 는 dash_owner를 열지 않는다
        monkeypatch.setattr(db, "owner_conn", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("owner_conn 금지")))
        r_get = c.get(f"/api/manage/uploads/{uid}", headers=headers)
        assert r_get.status == 200
        assert r_get.headers.get("upload-offset") == "0" and r_get.headers.get("upload-length") == str(len(payload))

        # ManageGuard: PATCH /uploads/{id}는 application/octet-stream만 허용, cross-site 출처 거절
        r_bad_ctype = c.request("PATCH", f"/api/manage/uploads/{uid}", body=payload[:5],
                                headers={**headers, "content-type": "text/plain", "upload-offset": "0"})
        assert r_bad_ctype.status == 415
        r_csrf = c.request("PATCH", f"/api/manage/uploads/{uid}", body=payload[:5],
                           headers={**headers, "content-type": "application/octet-stream",
                                    "origin": "https://evil.example", "upload-offset": "0"})
        assert r_csrf.status == 403 and json.loads(r_csrf.body)["detail"] == "forbidden_origin"

        # 1번째 조각 전송
        r_p1 = c.request("PATCH", f"/api/manage/uploads/{uid}", body=payload[:10],
                         headers={**headers, "content-type": "application/octet-stream", "upload-offset": "0"})
        assert r_p1.status == 200 and json.loads(r_p1.body)["offset"] == 10

        # 다른 사용자 접근 → 410 upload_expired
        h2 = passwords.hash_password(secrets.token_urlsafe(12))
        u2 = store.create_user(f"other-{secrets.token_hex(4)}@example.com", role="operator", status="active",
                               source="invite", password_hash=h2, email_verified=True)
        tok2 = sessions.new_token()
        store.start_session(u2.id, h2, sessions.token_hash(tok2),
                            datetime.now(timezone.utc) + timedelta(hours=1), None, None)
        try:
            c2 = Client(app, host="localhost")
            r_other = c2.get(f"/api/manage/uploads/{uid}", headers={"cookie": f"dashkit_session={tok2}"})
            assert r_other.status == 410 and json.loads(r_other.body)["detail"] == "upload_expired"
        finally:
            store.delete_user(u2.id)

        # 미완료 업로드 확정 시도 → 409 upload_incomplete
        r_inc = c.post("/api/manage/batches", json={"upload_id": uid}, headers=headers)
        assert r_inc.status == 409 and json.loads(r_inc.body)["detail"] == "upload_incomplete"

        # 동시 PATCH 잠금 시 409 upload_in_progress
        held = uploads._items[uid]
        assert held.chunk_lock.acquire(blocking=False)
        try:
            r_busy = c.request("PATCH", f"/api/manage/uploads/{uid}", body=payload[10:],
                               headers={**headers, "content-type": "application/octet-stream", "upload-offset": "10"})
            assert r_busy.status == 409 and json.loads(r_busy.body)["detail"] == "upload_in_progress"
        finally:
            held.chunk_lock.release()

        # DELETE 취소 시 디스크 즉시 삭제
        folder = held.files[0][1].parent
        r_del = c.request("DELETE", f"/api/manage/uploads/{uid}", json={}, headers=headers)
        assert r_del.status == 200 and json.loads(r_del.body)["ok"] is True
        assert not folder.exists()
        assert c.get(f"/api/manage/uploads/{uid}", headers=headers).status == 410
    finally:
        uploads.drop(uid)
