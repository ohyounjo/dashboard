"""Studio 업로드와 경로 값 검사(contracts/studio.md "요청 보호"의 업로드·경로 값 줄). 스택 없이 돈다."""
from __future__ import annotations

import pytest

from studio_client import Client, fresh_client, multipart

from dashkit.studio import upload

CSV = b"id,name\n1,a\n2,b\n"


@pytest.fixture
def client(tmp_path, monkeypatch) -> Client:
    return fresh_client(tmp_path, monkeypatch, projects=("devtracker",))


def tree(root) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# ---------------------------------------------------------------- 파일 이름

@pytest.mark.parametrize("name", ["orders.csv", "Orders 2024.csv", "주문 내역.csv", "a.b.csv", "DATA.CSV", "a-b_c (1).csv"])
def test_good_filenames(name):
    assert upload.check_filename(name) == name


@pytest.mark.parametrize("name", [
    "../orders.csv", "..\\orders.csv", "a/b.csv", "a\\b.csv", "/etc/passwd.csv", "C:\\x.csv", "a..b.csv", "..csv",
    "orders.csv\0.txt", "orders\0.csv", "orders.txt", "orders", "orders.csv.exe", ".csv", ".hidden.csv", "",
    "line\nbreak.csv", "tab\t.csv", " lead.csv", "trail.csv ", "x" * 300 + ".csv", None,
])
def test_bad_filenames(name):
    with pytest.raises(upload.UploadError) as exc:
        upload.check_filename(name)
    assert (exc.value.status, exc.value.detail) == (400, "invalid_filename")


@pytest.mark.parametrize("name", ["../x.csv", "a/b.csv", "x.txt", "..\\x.csv"])
def test_bad_filename_is_rejected_by_every_upload_endpoint(client, name):
    before = tree(client.projects)
    for method, path, field in (("POST", "/studio/api/projects/infer", "files"),
                                ("POST", "/studio/api/projects/devtracker/data/tasks/check", "file"),
                                ("PUT", "/studio/api/projects/devtracker/data/tasks", "file")):
        r = client.request(method, path, files=[(field, name, CSV)])
        assert (r.status, r.json["detail"]) == (400, "invalid_filename"), path
    assert tree(client.projects) == before


def test_filename_is_never_used_as_a_path(client):
    """올린 파일 이름이 다른 테이블이어도 쓰는 곳은 경로의 {table}이다."""
    good = (client.projects / "devtracker" / "data" / "tasks.csv").read_bytes()
    before = tree(client.projects)
    r = client.put("/studio/api/projects/devtracker/data/tasks", files=[("file", "members.csv", good + b"\n")])
    assert r.status == 200 and r.json["changed"] == ["data/tasks.csv"]
    after = tree(client.projects)
    assert {k for k in after if after[k] != before.get(k)} == {"devtracker/data/tasks.csv"}


def test_duplicate_filenames_in_one_request(client):
    r = client.post("/studio/api/projects/infer", files=[("files", "a.csv", CSV), ("files", "a.csv", CSV)])
    assert (r.status, r.json["detail"]) == (400, "invalid_filename")


# ---------------------------------------------------------------- 크기와 개수

def test_file_size_limit(client, monkeypatch):
    monkeypatch.setenv("STUDIO_MAX_UPLOAD_MB", "1")
    ok = b"id,name\n" + b"1,aaaaaaaa\n" * 50_000                 # 약 0.55MB
    big = b"id,name\n" + b"1,aaaaaaaa\n" * 100_000               # 약 1.1MB
    assert client.post("/studio/api/projects/infer", files=[("files", "ok.csv", ok)]).status in (200, 422)
    r = client.post("/studio/api/projects/infer", files=[("files", "ok.csv", CSV), ("files", "big.csv", big)])
    assert (r.status, r.json["detail"]) == (413, "upload_too_large") and "big.csv" in r.json["message"]
    before = tree(client.projects)
    for method, path in (("POST", "/studio/api/projects/devtracker/data/tasks/check"),
                         ("PUT", "/studio/api/projects/devtracker/data/tasks")):
        r = client.request(method, path, files=[("file", "tasks.csv", big)])
        assert (r.status, r.json["detail"]) == (413, "upload_too_large")
    assert tree(client.projects) == before


def test_request_size_limit_is_checked_before_and_while_reading(client, monkeypatch):
    monkeypatch.setenv("STUDIO_MAX_UPLOAD_MB", "1")
    monkeypatch.setenv("STUDIO_MAX_REQUEST_MB", "2")
    part = b"id,name\n" + b"1,aaaaaaaa\n" * 80_000              # 약 0.9MB씩 3개 = 2.6MB
    files = [("files", f"t{i}.csv", part) for i in range(3)]
    r = client.post("/studio/api/projects/infer", files=files)
    assert (r.status, r.json["detail"]) == (413, "upload_too_large")
    # Content-Length를 속이거나 빼도 읽는 도중에 걸린다
    ctype, body = multipart(files)
    for length in ("10", None):
        r = client.post("/studio/api/projects/infer", body=body, headers={"content-type": ctype, "content-length": length})
        assert (r.status, r.json["detail"]) == (413, "upload_too_large")
    # JSON 본문은 5MB까지
    r = client.patch("/studio/api/projects/devtracker/config", body=b'{"ops": "' + b"x" * (5 * 1024 * 1024) + b'"}',
                     headers={"content-type": "application/json"})
    assert (r.status, r.json["detail"]) == (413, "upload_too_large")


def test_file_count_limit(client):
    files = [("files", f"t{i}.csv", CSV) for i in range(51)]
    r = client.post("/studio/api/projects/infer", files=files)
    assert (r.status, r.json["detail"]) == (400, "too_many_files")
    assert client.post("/studio/api/projects/infer", files=files[:50]).status == 200


def test_single_file_endpoints_take_exactly_one(client):
    r = client.post("/studio/api/projects/devtracker/data/tasks/check",
                    files=[("file", "a.csv", CSV), ("file", "b.csv", CSV)])
    assert (r.status, r.json["detail"]) == (400, "bad_request")
    r = client.post("/studio/api/projects/devtracker/data/tasks/check", files=[("other", "a.csv", CSV)])
    assert (r.status, r.json["detail"]) == (400, "bad_request")


# ---------------------------------------------------------------- multipart 읽기

def test_multipart_content_is_byte_exact():
    content = b"a,b\r\n1,\"x\r\ny\"\r\n--not-a-boundary\r\n\xff\xfe\x00"
    ctype, body = multipart([("file", "한글 이름.csv", content), ("file", "b.csv", b"")])
    parts = upload.parse_multipart(ctype, body)
    assert [(p.name, p.filename, p.content) for p in parts] == [("file", "한글 이름.csv", content), ("file", "b.csv", b"")]


@pytest.mark.parametrize("ctype,body", [
    ("multipart/form-data", b"x"),                                             # 경계 없음
    ("multipart/form-data; boundary=abc", b"no boundary here"),
    ("multipart/form-data; boundary=abc", b"--abc\r\nContent-Disposition: form-data; name=\"f\"\r\n\r\nnever ends"),
    ("multipart/form-data; boundary=abc", b"--abc\r\nX: y\r\n\r\ndata\r\n--abc--\r\n"),      # Content-Disposition 없음
    ("multipart/form-data; boundary=abc", b"--abcXX"),
])
def test_malformed_multipart(ctype, body):
    with pytest.raises(upload.UploadError) as exc:
        upload.parse_multipart(ctype, body)
    assert (exc.value.status, exc.value.detail) == (400, "bad_request")


def test_form_fields_without_filename_are_not_files(client):
    body = (b"--abc\r\nContent-Disposition: form-data; name=\"file\"\r\n\r\nid,name\n1,a\n\r\n--abc--\r\n")
    r = client.post("/studio/api/projects/devtracker/data/tasks/check", body=body,
                    headers={"content-type": "multipart/form-data; boundary=abc"})
    assert (r.status, r.json["detail"]) == (400, "bad_request")


# ---------------------------------------------------------------- 경로 값({p}, {table})

@pytest.mark.parametrize("p", ["nope", "DevTracker", "devtracker%00", "..", ".", "%2e%2e", "..%2Fdevtracker",
                               "devtracker%2Fdata", "-x", "_x", ".git", "devtracker "])
def test_unknown_project_is_404_for_every_route(client, p):
    before = tree(client.projects)
    calls = [
        client.request("GET", f"/studio/api/projects/{p}/config", raw_path=True),
        client.request("GET", f"/studio/api/projects/{p}/plan", raw_path=True),
        client.request("PATCH", f"/studio/api/projects/{p}/config", raw_path=True,
                       json={"ops": [{"op": "tab.remove", "key": "docs"}]}),
        client.request("POST", f"/studio/api/projects/{p}/data/tasks/check", raw_path=True, files=[("file", "t.csv", CSV)]),
        client.request("PUT", f"/studio/api/projects/{p}/data/tasks", raw_path=True, files=[("file", "t.csv", CSV)]),
        client.request("POST", f"/studio/api/projects/{p}/apply", raw_path=True),
    ]
    for r in calls:
        if p == "devtracker%2Fdata":     # 풀리면 다른 경로(/projects/devtracker/data/…) 모양이 된다: 없는 테이블이거나 없는 메서드
            assert (r.status, r.json["detail"]) in ((404, "unknown_table"), (404, "not_found"), (405, "method_not_allowed"))
            continue
        assert r.status == 404 and r.json["detail"] in ("unknown_project", "not_found"), (p, r.status, r.text)
    assert tree(client.projects) == before


def test_project_must_be_a_real_folder_with_a_valid_name(client, tmp_path):
    (client.projects / "Bad Name").mkdir()
    (client.projects / "afile").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "dashboard.yaml").write_text("app: {}\n", encoding="utf-8")
    (client.projects / "linked").symlink_to(outside, target_is_directory=True)
    names = [p["name"] for p in client.get("/studio/api/projects").json["projects"]]
    assert names == ["devtracker"]
    for p in ("afile", "linked", "Bad%20Name"):
        assert client.request("GET", f"/studio/api/projects/{p}/config", raw_path=True).status == 404


@pytest.mark.parametrize("table", ["nope", "Tasks", "..", "%2e%2e", "tasks.csv", "..%2Fschema", "tasks%00", "data"])
def test_unknown_table_is_404_and_nothing_is_written(client, table):
    before = tree(client.projects)
    good = (client.projects / "devtracker" / "data" / "tasks.csv").read_bytes()
    for method, path in (("POST", f"/studio/api/projects/devtracker/data/{table}/check"),
                         ("PUT", f"/studio/api/projects/devtracker/data/{table}")):
        r = client.request(method, path, raw_path=True, files=[("file", "tasks.csv", good)])
        assert r.status == 404 and r.json["detail"] in ("unknown_table", "not_found"), (table, r.text)
    assert tree(client.projects) == before


def test_static_files_cannot_escape_the_web_dir(client, tmp_path, monkeypatch):
    web = tmp_path / "webdir"
    (web / "sub").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (web / "sub" / "a.js").write_text("1", encoding="utf-8")
    (web / ".secret").write_text("x", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("밖", encoding="utf-8")
    (web / "link.txt").symlink_to(tmp_path / "outside.txt")
    monkeypatch.setenv("STUDIO_WEB_DIR", str(web))
    assert client.get("/studio/static/sub/a.js").status == 200
    for path in ("/studio/static/../outside.txt", "/studio/static/%2e%2e/outside.txt", "/studio/static/..%2Foutside.txt",
                 "/studio/static/sub/../../outside.txt", "/studio/static/.secret", "/studio/static/link.txt",
                 "/studio/static/sub", "/studio/static/", "/studio/static/sub%5Ca.js", "/studio/static//etc/passwd"):
        r = client.request("GET", path, raw_path=True)
        assert r.status == 404, path


def test_api_works_without_screen_files(client, tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_WEB_DIR", str(tmp_path / "no-such-dir"))
    r = client.get("/")
    assert r.status == 404 and r.json["detail"] == "not_found"
    assert client.get("/studio/api/health").json == {"status": "ok"}
