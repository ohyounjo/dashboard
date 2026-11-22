"""관리 서비스의 데이터 운용 경로(contracts/manage.md): 테이블, 데이터 더하기(직접 입력·파일·미리보기 확정), 배치 이력,
되돌리기·되살리기, 작업, 권한.

make test의 5단계가 누적 테이블 프로젝트(tests/fixtures/ledger 사본, PROJECT=zz-ingest)를 적용하고 관리 서비스를 띄운 뒤
nginx(web)를 거쳐 부른다. 다른 단계(PROJECT가 다를 때)에서는 건너뛴다.
"""
from __future__ import annotations

import json
import os
import secrets
import time

import pytest

from conftest import WEB_URL, delete_user, http_full, login, make_user, random_email
from dashkit import db

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.environ.get("PROJECT") != "zz-ingest", reason="5단계(PROJECT=zz-ingest)에서만")]

ORIGIN = None      # 같은 출처 요청처럼 Origin 없이 보낸다(요청 보호는 Origin이 있으면 허용 출처만 받는다)


@pytest.fixture(scope="module")
def who():
    made, out = [], {}
    for role in ("operator", "admin"):
        for n in (1, 2):
            email, password = random_email(f"data-{role}{n}"), secrets.token_urlsafe(18)
            made.append(make_user(email, password, role=role).id)
            out[f"{role}{n}"] = login(email, password)
    yield out
    for uid in made:
        delete_user(uid)


def call(method: str, path: str, cookie: str, body=None, *, raw: bytes | None = None, ctype: str | None = None,
         origin: str | None = ORIGIN):
    headers = {"Cookie": f"dashkit_session={cookie}"}
    if origin:
        headers["Origin"] = origin
    if raw is not None:
        headers["Content-Type"] = ctype or "application/octet-stream"
        return http_full(method, f"{WEB_URL}/api/manage{path}", headers=headers, raw=raw)
    return http_full(method, f"{WEB_URL}/api/manage{path}", body, headers)


def multipart(fields: dict[str, str], file: tuple[str, bytes]) -> tuple[str, bytes]:
    boundary = "----dk" + secrets.token_hex(8)
    out = b""
    for k, v in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    name, content = file
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            "Content-Type: text/csv\r\n\r\n").encode() + content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", out


def wait_job(job: dict, cookie: str, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        code, body, _h = call("GET", f"/jobs/{job['id']}", cookie)
        assert code == 200, body
        if body["status"] != "running":
            return body
        assert time.monotonic() < deadline, body
        time.sleep(0.5)


def view_count(table: str = "entries") -> int:
    """대시보드가 읽는 뷰의 행 수(dash_reader). 적재는 커밋하면 sync 없이 바로 보인다."""
    with db.reader_conn() as conn:
        return conn.execute(f"SELECT count(*) FROM dash.{table}").fetchone()[0]


def test_tables_and_definition(who):
    code, body, _h = call("GET", "/tables", who["operator1"])
    assert code == 200 and body["project"] == "zz-ingest"
    by = {t["name"]: t for t in body["items"]}
    assert by["entries"]["mode"] == "append" and by["entries"]["on_conflict"] == "update"
    assert by["entries"]["rows"] == 5 and by["entries"]["batches"] == 2 and by["groups"]["mode"] == "replace"
    code, body, _h = call("GET", "/tables/entries", who["operator1"])
    cols = {c["name"]: c for c in body["columns"]}
    assert code == 200 and body["primary_key"] == ["id"] and cols["state"]["default"] == "open"
    assert cols["amount"]["label"] == "금액" and cols["id"]["primary_key"] and "entries" in body["datasets"]
    assert call("GET", "/tables/nope", who["operator1"])[0] == 404


def test_form_add_preview_cancel_and_permissions(who):
    before = view_count()
    row = {"id": "E300", "group_id": "g1", "state": "done", "amount": "1,000", "on_day": "2026/11/20",
           "flag": "Y", "qty": "1", "memo": None}
    code, body, _h = call("POST", "/tables/entries/batches", who["operator1"], {"rows": [row], "dry_run": True})
    assert code == 200 and body["dry_run"] and body["batches"][0]["rows"]["inserted"] == 1, body
    assert view_count() == before
    code, body, _h = call("POST", "/tables/entries/batches", who["operator1"], {"rows": [row], "method": "form"})
    assert code == 200, body
    name = body["batches"][0]["file"]
    assert view_count() == before + 1                          # sync 없이 바로 보인다
    code, body, _h = call("POST", "/tables/entries/batches", who["operator1"], {"rows": [row]})
    assert code == 409 and body["detail"] == "duplicate_file"
    code, items, _h = call("GET", "/tables/entries/batches", who["operator1"])
    mine = next(i for i in items["items"] if i["file"] == name)
    assert mine["mine"] and mine["method"] == "form" and mine["inserted"] == 1
    # 다른 operator는 되돌리지 못하고, 더한 사람은 방금 추가 취소(빠른 길)
    code, body, _h = call("DELETE", f"/tables/entries/batches/{name}", who["operator2"])
    assert code == 403 and body["detail"] == "forbidden"
    code, body, _h = call("DELETE", f"/tables/entries/batches/{name}", who["operator1"])
    assert code == 200 and body["fast"] and body["rows"] == 1
    assert view_count() == before


def test_errors_are_reported_and_nothing_changes(who):
    before = view_count()
    code, body, _h = call("POST", "/tables/entries/batches", who["operator1"], {"rows": [{"id": "E301", "on_day": "11/20/2026"}]})
    assert code == 422 and body["detail"] == "invalid_rows" and body["errors"][0].startswith("rows[0].on_day")
    code, body, _h = call("POST", "/tables/entries/batches", who["operator1"], {"rows": [{"id": "E301", "zz": 1}]})
    assert code == 422 and body["detail"] == "unknown_columns" and body["columns"][0]["header"] == "zz"
    code, body, _h = call("POST", "/tables/entries/batches", who["operator1"],
                          {"rows": [{"id": "E301", "zz": 1}], "columns": {"zz": None}, "dry_run": True})
    assert code == 200 and body["batches"][0]["columns"]["dropped"] == ["zz"]
    code, body, _h = call("POST", "/tables/groups/batches", who["operator1"], {"rows": [{"id": "g9", "label": "x"}]})
    assert code == 409 and body["detail"] == "not_append"
    code, body, _h = call("POST", "/tables/entries/batches", who["operator1"], {"rows": [{"id": f"E{i}"} for i in range(400, 901)]})
    assert code == 413 and body["detail"] == "too_many_rows"
    assert view_count() == before


def test_file_preview_then_confirm_without_uploading_again(who):
    before = view_count()
    csv = "ID\t금액\tState Name\r\nE310\t1,234\tdone\r\nE311\t5\topen\r\n".encode("cp949")
    ctype, raw = multipart({"dry_run": "1"}, ("엑셀.csv", csv))
    code, body, _h = call("POST", "/tables/entries/batches", who["operator1"], raw=raw, ctype=ctype)
    assert code == 202, body
    job = wait_job(body["job"], who["operator1"])
    assert job["status"] == "succeeded", job
    result = job["result"]
    assert result["dry_run"] and result["batches"][0]["normalized"]["encoding"] == "cp949" and result["upload_id"]
    assert view_count() == before
    # 다른 사람은 그 upload_id를 쓸 수 없다
    code, body, _h = call("POST", "/tables/entries/batches", who["operator2"], {"upload_id": result["upload_id"]})
    assert code == 410 and body["detail"] == "upload_expired"
    code, body, _h = call("POST", "/tables/entries/batches", who["operator1"], {"upload_id": result["upload_id"]})
    assert code == 202, body
    job = wait_job(body["job"], who["operator1"])
    assert job["status"] == "succeeded" and job["result"]["batches"][0]["rows"]["inserted"] == 2, job
    assert view_count() == before + 2
    code, items, _h = call("GET", "/tables/entries/batches", who["admin1"])
    row = next(i for i in items["items"] if i["file"] == job["result"]["batches"][0]["file"])
    assert row["method"] == "upload" and row["source_name"] == "엑셀.csv" and not row["mine"]
    code, body, headers = call("GET", f"/tables/entries/batches/{row['file']}", who["operator2"])
    assert code == 200 and headers.get("Content-Type", "").startswith("application/gzip")
    code, body, _h = call("POST", "/tables/entries/batches", who["operator1"], {"upload_id": result["upload_id"]})
    assert code == 410, "확정한 업로드는 다시 쓸 수 없다"


def test_older_batch_is_reverted_by_rebuild_and_restored(who):
    code, body, _h = call("DELETE", "/tables/entries/batches/000001-20261120T0910Z.csv", who["operator1"])
    assert code == 403, "기록이 없는 배치(손으로 넣은 것)는 admin만"
    code, body, _h = call("DELETE", "/tables/entries/batches/000001-20261120T0910Z.csv", who["admin1"])
    assert code == 202, body
    job = wait_job(body["job"], who["admin1"])
    assert job["status"] == "succeeded" and not job["result"]["fast"], job
    with db.reader_conn() as conn:
        assert conn.execute("SELECT count(*) FROM dash.entries WHERE id = 'E4'").fetchone()[0] == 0
    code, items, _h = call("GET", "/tables/entries/batches?removed=1", who["admin1"])
    assert any(i["file"] == "000001-20261120T0910Z.csv" and i["removed"] for i in items["items"])
    code, body, _h = call("POST", "/tables/entries/batches/000001-20261120T0910Z.csv/restore", who["admin1"], {})
    assert code == 202, body
    job = wait_job(body["job"], who["admin1"])
    assert job["status"] == "succeeded", job
    with db.reader_conn() as conn:
        assert conn.execute("SELECT count(*) FROM dash.entries WHERE id = 'E4'").fetchone()[0] == 1


def test_uploads_are_guarded_and_audited(who):
    ctype, raw = multipart({"dry_run": "1"}, ("a.csv", b"id\nE320\n"))
    code, body, _h = call("POST", "/tables/entries/batches", who["operator1"], raw=raw, ctype=ctype,
                          origin="https://evil.example")
    assert code == 403 and body["detail"] == "forbidden_origin"
    code, body, _h = call("POST", "/tables/entries/batches", who["operator1"], raw=b"id=1",
                          ctype="application/x-www-form-urlencoded")
    assert code == 415
    code, body, _h = call("GET", "/jobs", who["operator1"])
    assert code == 200 and body["items"] and "log" not in body["items"][0]
    code, body, _h = http_full("GET", f"{WEB_URL}/api/admin/audit?action=data.", headers={
        "Cookie": f"dashkit_session={who['admin1']}"})
    assert code == 200, body
    actions = {i["action"] for i in body["items"]}
    assert {"data.batch.create", "data.batch.remove", "data.batch.restore", "data.batch.failed"} <= actions
    assert not any("1,234" in json.dumps(i["detail"], ensure_ascii=False) for i in body["items"]), "데이터 값을 싣지 않는다"


# ---------------------------------------------------------------- 2b: 여러 파일, 오류 행 빼고 넣기, 빈 양식, 머리글 맞춤 저장

def test_many_files_are_one_transaction(who):
    before = view_count()
    files = [("자식.csv", b"id,entry_id\n700,E700\n"), ("부모.csv", b"id\nE700\n")]
    boundary = "----dk" + secrets.token_hex(8)

    def body(fields):
        out = b""
        for k, v in fields.items():
            out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        for n, c in files:
            out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{n}\"\r\n\r\n"
                    ).encode() + c + b"\r\n"
        return out + f"--{boundary}--\r\n".encode()
    ctype = f"multipart/form-data; boundary={boundary}"
    code, b, _h = call("POST", "/batches", who["operator1"], raw=body({"dry_run": "1"}), ctype=ctype)
    assert code == 422 and b["detail"] == "unknown_table" and len(b["errors"]) == 2      # 이름으로는 짝지을 수 없다
    mapping = json.dumps({"자식.csv": "events", "부모.csv": "entries"}, ensure_ascii=False)
    code, b, _h = call("POST", "/batches", who["operator1"], raw=body({"dry_run": "1", "map": mapping}), ctype=ctype)
    assert code == 202, b
    job = wait_job(b["job"], who["operator1"])
    assert job["status"] == "succeeded" and [x["table"] for x in job["result"]["batches"]] == ["events", "entries"], job
    code, b, _h = call("POST", "/batches", who["operator1"], {"upload_id": job["result"]["upload_id"]})
    job = wait_job(b["job"], who["operator1"])
    assert job["status"] == "succeeded", job
    assert [b["rows"]["inserted"] for b in job["result"]["batches"]] == [1, 1]
    assert view_count() == before + 1


def test_skip_invalid_with_rejects_download(who):
    ctype, raw = multipart({"skip_invalid": "1"}, ("섞임.csv", b"id,entry_id\n710,E1\n711,E-none\n712,E2\n"))
    code, b, _h = call("POST", "/tables/events/batches", who["operator1"], raw=raw, ctype=ctype)
    job = wait_job(b["job"], who["operator1"])
    assert job["status"] == "succeeded" and job["result"]["rejects"], job
    rows = job["result"]["batches"][0]["rows"]
    assert rows["inserted"] == 2 and rows["invalid"] == 1
    assert job["progress"].get("step") in ("read", "lock")
    code, body, headers = call("GET", f"/jobs/{job['id']}/rejects", who["operator1"])
    assert code == 200 and headers.get("Content-Type", "").startswith("text/csv")
    text = body if isinstance(body, str) else json.dumps(body)
    assert "711,E-none" in text and "오류" in text


def test_template_and_alias_saving(who):
    code, body, headers = call("GET", "/tables/entries/template", who["operator1"])
    assert code == 200 and isinstance(body, str) and body.lstrip("﻿").startswith("id,group_id,state")
    code, t, _h = call("GET", "/tables/entries", who["operator1"])
    revision = t["revision"]
    code, b, _h = call("POST", "/tables/entries/aliases", who["operator1"], {"aliases": {"분류": "state"}, "revision": revision})
    assert code == 403, "aliases 저장은 admin만"
    code, b, _h = call("POST", "/tables/entries/aliases", who["admin1"], {"aliases": {"분류": "state"}, "revision": "stale"})
    assert code == 409 and b["detail"] == "stale_config"
    code, b, _h = call("POST", "/tables/entries/aliases", who["admin1"], {"aliases": {"분류": "state"}, "revision": revision})
    assert code == 200 and b["changed"] == ["schema.yaml"], b
    # 저장한 머리글이 다음 파일에서 저절로 맞고, aliases는 DB와 상관없어 적용하지 않아도 더할 수 있다
    code, b, _h = call("POST", "/tables/entries/batches", who["operator1"], {"rows": [{"id": "E720", "분류": "done"}], "dry_run": True})
    assert code == 200 and b["batches"][0]["columns"]["matched"] == {"분류": "state"}, b


def test_lock_wait_separation_and_step_lock(who):
    """선행 쓰기 잠금 중 즉시 응답은 5초 뒤 409, 202 작업은 step: lock으로 기다렸다가 성공한다."""
    from dashkit.project import locks

    before = view_count()
    lock_conn = db.owner_conn()
    created_file = None
    try:
        locks.acquire(lock_conn, 0)
        # 1) 즉시 입력(rows)은 MANAGE_LOCK_WAIT_MS(5초) 초과 시 409 apply_running
        code, body, _h = call("POST", "/tables/entries/batches", who["operator1"],
                              {"rows": [{"id": "E790", "state": "open"}]})
        assert code == 409 and body["detail"] == "apply_running", body

        # 2) 파일 적재(202)는 step: lock으로 대기하다가 잠금 해제 후 성공
        ctype, raw = multipart({}, ("wait.csv", b"id,state\nE791,open\n"))
        code, body, _h = call("POST", "/tables/entries/batches", who["operator1"], raw=raw, ctype=ctype)
        assert code == 202, body
        job = body["job"]
        seen_lock = False
        for _ in range(40):
            state = call("GET", f"/jobs/{job['id']}", who["operator1"])[1]
            if state["progress"].get("step") == "lock":
                seen_lock = True
                break
            time.sleep(0.05)
        assert seen_lock, state
        locks.release(lock_conn)
        done = wait_job(job, who["operator1"])
        assert done["status"] == "succeeded", done
        created_file = done["result"]["batches"][0]["file"]

        # 3) 빠른 취소 후보(방금 넣은 배치)는 잠금 중 5초 뒤 409 apply_running
        locks.acquire(lock_conn, 0)
        code, body, _h = call("DELETE", f"/tables/entries/batches/{created_file}", who["operator1"])
        assert code == 409 and body["detail"] == "apply_running", body

        # 4) 이전 배치(비후보)는 5초 대기 없이 즉시 202 remove 작업으로 진입하고 step: lock 후 성공
        t0 = time.monotonic()
        code, body, _h = call("DELETE", "/tables/entries/batches/000001-20261120T0910Z.csv", who["admin1"])
        assert code == 202 and (time.monotonic() - t0) < 3.0, (code, body)
        rm_job = body["job"]
        seen_lock = False
        for _ in range(40):
            state = call("GET", f"/jobs/{rm_job['id']}", who["admin1"])[1]
            if state["progress"].get("step") == "lock":
                seen_lock = True
                break
            time.sleep(0.05)
        assert seen_lock, state
        locks.release(lock_conn)
        assert wait_job(rm_job, who["admin1"])["status"] == "succeeded"
    finally:
        try:
            locks.release(lock_conn)
        except Exception:
            pass
        lock_conn.close()

    # 되돌린 000001 배치 복원 및 테스트용 created_file 정리
    code, body, _h = call("POST", "/tables/entries/batches/000001-20261120T0910Z.csv/restore", who["admin1"], {})
    assert code == 202 and wait_job(body["job"], who["admin1"])["status"] == "succeeded"
    if created_file:
        code, body, _h = call("DELETE", f"/tables/entries/batches/{created_file}", who["operator1"])
        assert code == 200 and body["fast"]
    assert view_count() == before


def test_xlsx_and_resumable_upload_end_to_end(who):
    """통합 검증: .xlsx 업로드 및 이어 올리기(POST /uploads → PATCH → GET → 미리보기 → 확정) 결과 일치."""
    import hashlib
    from test_additional_intake_unit import make_xlsx_bytes

    before = view_count()
    # 1) .xlsx multipart 적재
    xlsx_raw = make_xlsx_bytes([
        {
            "name": "Entries",
            "rows": [
                ["id", "group_id", "state", "amount", "on_day", "flag", "qty"],
                ["E801", "g1", "open", "1,500", "2026-11-20", {"t": "b", "v": "1"}, "3"],
            ],
        }
    ])
    ctype, raw = multipart({"dry_run": "1"}, ("entries.xlsx", xlsx_raw))
    code, body, _h = call("POST", "/tables/entries/batches", who["operator1"], raw=raw, ctype=ctype)
    assert code == 202, body
    job = wait_job(body["job"], who["operator1"])
    assert job["status"] == "succeeded" and job["result"]["dry_run"]
    assert job["result"]["batches"][0]["rows"]["inserted"] == 1
    code, body, _h = call("POST", "/tables/entries/batches", who["operator1"], {"upload_id": job["result"]["upload_id"]})
    assert code == 202
    job_done = wait_job(body["job"], who["operator1"])
    assert job_done["status"] == "succeeded"
    xlsx_batch = job_done["result"]["batches"][0]["file"]
    assert view_count() == before + 1

    # 2) 이어 올리기: 중단 → GET 오프셋 조회 → 중복 조각 멱등 → 재개 → dry_run: true(실패 후에도 유지) → dry_run: false 확정
    csv_bytes = b"ID,group_id,state,amount\nE802,g1,done,\"2,000\"\nE803,g2,open,500\n"
    sha = hashlib.sha256(csv_bytes).hexdigest()
    code, sess, _h = call("POST", "/uploads", who["operator1"], {
        "table": "entries", "filename": "resume.csv", "total_size": len(csv_bytes), "sha256": sha,
    })
    assert code == 201 and sess["status"] == "receiving", sess
    uid = sess["upload_id"]

    # 1번째 조각 전송
    p1 = csv_bytes[:20]
    headers = {"Cookie": f"dashkit_session={who['operator1']}", "Content-Type": "application/octet-stream", "Upload-Offset": "0"}
    code, r1, _h = http_full("PATCH", f"{WEB_URL}/api/manage/uploads/{uid}", headers=headers, raw=p1)
    assert code == 200 and r1["offset"] == 20, r1

    # 중단 후 GET 오프셋 조회
    code, status_doc, r_hdrs = call("GET", f"/uploads/{uid}", who["operator1"])
    assert code == 200 and status_doc["offset"] == 20 and r_hdrs.get("upload-offset") == "20"

    # 중복 조각 재전송 → 200 멱등
    code, r_dup, _h = http_full("PATCH", f"{WEB_URL}/api/manage/uploads/{uid}", headers=headers, raw=p1)
    assert code == 200 and r_dup["offset"] == 20

    # 건너뛴 오프셋 → 409 offset_mismatch
    bad_hdrs = {"Cookie": f"dashkit_session={who['operator1']}", "Content-Type": "application/octet-stream", "Upload-Offset": "30"}
    code, r_gap, _h = http_full("PATCH", f"{WEB_URL}/api/manage/uploads/{uid}", headers=bad_hdrs, raw=csv_bytes[30:])
    assert code == 409 and r_gap["detail"] == "offset_mismatch" and r_gap["offset"] == 20

    # 나머지 전송 → status: ready
    hdrs2 = {"Cookie": f"dashkit_session={who['operator1']}", "Content-Type": "application/octet-stream", "Upload-Offset": "20"}
    code, r2, _h = http_full("PATCH", f"{WEB_URL}/api/manage/uploads/{uid}", headers=hdrs2, raw=csv_bytes[20:])
    assert code == 200 and r2["status"] == "ready" and r2["offset"] == len(csv_bytes), r2

    # dry_run: true 미리보기
    code, b_prev, _h = call("POST", "/tables/entries/batches", who["operator1"], {"upload_id": uid, "dry_run": True})
    assert code == 202
    j_prev = wait_job(b_prev["job"], who["operator1"])
    assert j_prev["status"] == "succeeded" and j_prev["result"]["dry_run"]
    assert j_prev["result"]["batches"][0]["rows"]["inserted"] == 2
    assert view_count() == before + 1

    # 미리보기 뒤에도 upload_id 유지 → 재업로드 없이 확정(dry_run: false)
    code, b_commit, _h = call("POST", "/tables/entries/batches", who["operator1"], {"upload_id": uid, "dry_run": False})
    assert code == 202
    j_commit = wait_job(b_commit["job"], who["operator1"])
    assert j_commit["status"] == "succeeded" and not j_commit["result"]["dry_run"]
    resumed_batch = j_commit["result"]["batches"][0]["file"]
    assert view_count() == before + 3

    # 확정 후 임시 업로드 세션 삭제됨(410)
    code, _, _ = call("GET", f"/uploads/{uid}", who["operator1"])
    assert code == 410

    # 정리(테스트 전 행 수로 복원)
    code, body, _ = call("DELETE", f"/tables/entries/batches/{resumed_batch}", who["operator1"])
    assert code == 200 and body["fast"]
    code, body, _ = call("DELETE", f"/tables/entries/batches/{xlsx_batch}", who["operator1"])
    assert code == 200 and body["fast"]
    assert view_count() == before
