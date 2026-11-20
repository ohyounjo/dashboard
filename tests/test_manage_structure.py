"""관리 서비스의 구조 변경 경로(contracts/manage.md "구조 변경"): 설정 연산(PATCH /config), 새 테이블(추론 → 만들기),
계보와 쓰이는 곳, 설정 변경 이력과 되돌리기, 설정 내려받기, 합치기, 권한.

make test의 5단계(PROJECT=zz-ingest)가 tests/test_manage_data.py 다음에 부른다. 다른 단계에서는 건너뛴다.
"""
from __future__ import annotations

import io
import os
import secrets
import zipfile

import pytest

from conftest import WEB_URL, delete_user, login, make_user, random_email
from dashkit import db
from test_manage_data import call, multipart, view_count, wait_job

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.environ.get("PROJECT") != "zz-ingest", reason="5단계(PROJECT=zz-ingest)에서만")]


@pytest.fixture(scope="module")
def who():
    made, out = [], {}
    for role in ("operator", "admin"):
        email, password = random_email(f"struct-{role}"), secrets.token_urlsafe(18)
        made.append(make_user(email, password, role=role).id)
        out[role] = login(email, password)
    yield out
    for uid in made:
        delete_user(uid)


def revision(cookie: str) -> str:
    code, body, _h = call("GET", "/tables/entries", cookie)
    assert code == 200, body
    return body["revision"]


def view_columns(view: str) -> list[str]:
    with db.reader_conn() as conn:
        return [r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'dash' AND table_name = %s "
            "ORDER BY ordinal_position", (view,)).fetchall()]


def patch(cookie: str, ops, rev=None):
    return call("PATCH", "/config", cookie, {"ops": ops, "revision": rev if rev is not None else revision(cookie)})


def test_structure_changes_are_admin_only(who):
    op = who["operator"]
    rev = revision(op)
    assert patch(op, [{"op": "column.update", "table": "entries", "column": "memo", "set": {"description": "x"}}],
                 rev)[0] == 403
    ctype, raw = multipart({}, ("items.csv", b"code,score\nK1,3\n"))
    assert call("POST", "/tables/infer", op, raw=raw, ctype=ctype)[0] == 403
    assert call("POST", "/tables", op, {"upload_id": "x", "table": {}, "revision": rev})[0] == 403
    assert call("POST", "/tables/entries/compact", op, {})[0] == 403
    assert call("GET", "/history", op)[0] == 403
    assert call("GET", "/export", op)[0] == 403
    # 계보와 쓰이는 곳은 운용자가 본다
    assert call("GET", "/lineage", op)[0] == 200
    code, used, _h = call("GET", "/tables/entries/used_by", op)
    assert code == 200 and "summary" in used["datasets"] and used["tabs"] == ["main"]


def test_lineage_in_use_and_bad_requests(who):
    admin = who["admin"]
    code, graph, _h = call("GET", "/lineage", admin)
    assert code == 200 and graph["datasets"]["summary"]["sources"] == {"entries": ["amount", "group_id"]}
    code, b, _h = patch(admin, [{"op": "table.remove", "table": "entries"}])
    assert code == 409 and b["detail"] == "in_use" and "데이터셋 summary" in b["errors"], b
    code, b, _h = patch(admin, [{"op": "dataset.remove", "key": "summary"}])
    assert code == 409 and b["detail"] == "in_use" and b["errors"] == ["차트 c_total"], b
    code, b, _h = patch(admin, [{"op": "tab.remove", "key": "main"}])
    assert code == 422 and b["detail"] == "invalid_op", "운용 화면은 구조 연산만"
    code, b, _h = patch(admin, [{"op": "column.update", "table": "entries", "column": "qty", "set": {"type": "boolean"}}])
    assert code == 400 and b["detail"] == "invalid_op", b
    code, b, _h = call("PATCH", "/config", admin, {"ops": [{"op": "table.remove", "table": "groups"}]})
    assert code == 400 and b["detail"] == "invalid_request"
    code, b, _h = patch(admin, [{"op": "column.update", "table": "entries", "column": "memo", "set": {"description": "x"}}],
                        "stale")
    assert code == 202
    assert wait_job(b["job"], admin)["error"]["detail"] == "stale_config"


def test_column_add_reaches_the_form_and_the_dataset(who):
    admin = who["admin"]
    code, b, _h = patch(admin, [{"op": "column.add", "table": "entries", "column": "tier",
                                 "value": {"type": "integer", "default": 1}}])
    assert code == 202, b
    job = wait_job(b["job"], admin)
    assert job["status"] == "succeeded", job
    result = job["result"]
    assert result["changed"] == ["schema.yaml"] and result["sync"]["ok"], result
    assert {"table": "entries", "column": "tier", "datasets": ["summary", "states"]} in result["datasets_without_column"]
    code, t, _h = call("GET", "/tables/entries", who["operator"])
    assert [c["name"] for c in t["columns"]][-1] == "tier"
    assert view_columns("entries")[-1] == "tier"               # table: 데이터셋에 새 열이 나온다
    assert "tier" not in view_columns("summary")                # 열을 적은 sql: 데이터셋은 그대로
    code, b, _h = call("POST", "/tables/entries/batches", who["operator"], {"rows": [{"id": "E901", "tier": 3}]})
    assert code == 200 and b["ok"], b


def test_new_table_from_a_file_then_history_restore(who):
    admin = who["admin"]
    ctype, raw = multipart({}, ("Score Sheet.csv", "Item Code,Score\nK1,3\nK2,5\n".encode()))
    code, inferred, _h = call("POST", "/tables/infer", admin, raw=raw, ctype=ctype)
    assert code == 200, inferred
    table = inferred["table"]
    assert table["name"] == "score_sheet" and [c["source"] for c in table["columns"]] == ["Item Code", "Score"]
    code, b, _h = call("POST", "/tables", admin, {"upload_id": inferred["upload_id"], "table": table, "mode": "append",
                                                  "dataset": True, "suggest": True, "revision": revision(admin)})
    assert code == 202, b
    job = wait_job(b["job"], admin)
    assert job["status"] == "succeeded", job
    assert job["result"]["dataset"] == "score_sheet" and job["result"]["tab"], job["result"]
    code, listing, _h = call("GET", "/tables", who["operator"])
    assert "score_sheet" in [t["name"] for t in listing["items"]]
    assert view_count("score_sheet") == 2
    code, ds, _h = call("GET", "/datasets", who["operator"])
    assert "score_sheet" in [d["key"] for d in ds["items"]]

    code, hist, _h = call("GET", "/history", admin)
    assert code == 200 and hist["items"][0]["action"] == "table-create"
    hid = hist["items"][0]["id"]
    code, b, _h = call("POST", f"/history/{hid}/restore", admin, {"revision": hist["revision"]})
    assert code == 202
    job = wait_job(b["job"], admin)
    assert job["status"] == "succeeded", job
    code, listing, _h = call("GET", "/tables", who["operator"])
    assert "score_sheet" not in [t["name"] for t in listing["items"]]
    # 그 판이 이미 되돌려졌으니(지금 내용이 그 작업 바로 뒤의 판이 아니다) 다시 되돌리지 않는다
    code, b, _h = call("POST", f"/history/{hid}/restore", admin, {"revision": revision(admin)})
    assert wait_job(b["job"], admin)["error"]["detail"] == "stale_config"
    code, b, _h = call("POST", "/history/nope/restore", admin, {"revision": revision(admin)})
    assert code == 404 and b["detail"] == "unknown_history"


def test_compact_keeps_rows_and_leaves_one_batch(who):
    admin = who["admin"]
    for i in range(3):
        code, b, _h = call("POST", "/tables/entries/batches", who["operator"], {"rows": [{"id": f"E95{i}"}]})
        assert code == 200 and b["ok"], b
    before = view_count()
    code, b, _h = call("POST", "/tables/entries/compact", admin, {})
    assert code == 202, b
    job = wait_job(b["job"], admin)
    assert job["status"] == "succeeded" and job["result"]["merged"] >= 4, job
    code, listing, _h = call("GET", "/tables/entries/batches", admin)
    assert [x["file"] for x in listing["items"] if not x["removed"]] == [job["result"]["file"]]
    assert view_count() == before
    merged = [x for x in listing["items"] if not x["removed"]][0]
    assert merged["merged"] is True
    code, b, _h = call("DELETE", f"/tables/entries/batches/{merged['file']}", admin)
    assert code == 409 and b["detail"] == "merged_batch", b
    assert view_count() == before
    code, b, _h = call("POST", "/tables/groups/compact", admin, {})
    assert code == 409 and b["detail"] == "not_append"


def test_export_has_only_the_config_files(who):
    import urllib.request

    req = urllib.request.Request(f"{WEB_URL}/api/manage/export", headers={"Cookie": f"dashkit_session={who['admin']}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        assert r.status == 200 and r.headers.get("Content-Type") == "application/zip"
        names = zipfile.ZipFile(io.BytesIO(r.read())).namelist()
    assert sorted(names) == ["zz-ingest/REVISION", "zz-ingest/dashboard.yaml", "zz-ingest/schema.yaml"]
