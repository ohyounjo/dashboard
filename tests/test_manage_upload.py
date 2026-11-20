"""관리 서비스의 큰 파일 업로드(dashkit/project/upload.py의 stream_multipart): 조각씩 읽어 디스크로 쓴다."""
from __future__ import annotations

import asyncio

import pytest

from dashkit.project import upload


def body(parts, boundary="XyZ"):
    out = b""
    for name, filename, content in parts:
        disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        out += f"--{boundary}\r\nContent-Disposition: {disp}\r\n\r\n".encode() + content + b"\r\n"
    return f"multipart/form-data; boundary={boundary}", out + f"--{boundary}--\r\n".encode()


def run(ctype, raw, tmp_path, chunk=7, **kw):
    async def stream():
        for i in range(0, len(raw), chunk):
            yield raw[i:i + chunk]
    kw.setdefault("file_limit", 1024 * 1024)
    kw.setdefault("request_limit", 2 * 1024 * 1024)
    return asyncio.run(upload.stream_multipart(ctype, stream(), str(len(raw)), folder=tmp_path, **kw))


@pytest.mark.parametrize("chunk", [1, 3, 7, 64, 100000])
def test_parts_are_split_across_any_chunk_size(tmp_path, chunk):
    content = b"id,memo\r\nE1,\"a\r\n--X not the boundary\"\r\n" * 50
    ctype, raw = body([("dry_run", None, b"1"), ("file", "a.csv", content), ("columns", None, '{"x": null}'.encode())])
    parts = run(ctype, raw, tmp_path, chunk=chunk)
    by = {p.name: p for p in parts}
    assert by["dry_run"].value == "1" and by["columns"].value == '{"x": null}'
    assert by["file"].filename == "a.csv" and by["file"].path.read_bytes() == content and by["file"].size == len(content)
    upload.discard_parts(parts)
    assert not list(tmp_path.iterdir())


def test_limits_and_bad_bodies_leave_no_files(tmp_path):
    ctype, raw = body([("file", "a.csv", b"x" * 2000)])
    with pytest.raises(upload.UploadError) as exc:
        run(ctype, raw, tmp_path, file_limit=1000)
    assert exc.value.status == 413
    with pytest.raises(upload.UploadError) as exc:
        run(ctype, raw, tmp_path, request_limit=100)
    assert exc.value.status == 413
    with pytest.raises(upload.UploadError) as exc:
        run(ctype, raw[:-10], tmp_path)                 # 끝 경계가 없다
    assert exc.value.detail == "bad_request"
    with pytest.raises(upload.UploadError) as exc:
        run("text/plain", raw, tmp_path)
    assert exc.value.status == 415
    assert not list(tmp_path.iterdir())


def test_too_many_files(tmp_path):
    ctype, raw = body([("file", f"{i}.csv", b"id\n") for i in range(3)])
    with pytest.raises(upload.UploadError) as exc:
        run(ctype, raw, tmp_path, max_files=2)
    assert exc.value.detail == "too_many_files" and not list(tmp_path.iterdir())


def test_merged_rejects_leave_only_the_zip(tmp_path):
    """여러 파일의 빠진 행은 zip 하나로 묶고 낱개 파일을 지운다(리뷰 지적 회귀. 작업을 잊을 때 폴더째 지운다)."""
    from dashkit.manage.routes.tables import _merge_rejects

    (tmp_path / "000.csv").write_text("a\n", encoding="utf-8")
    (tmp_path / "001.csv").write_text("b\n", encoding="utf-8")
    out = _merge_rejects(tmp_path, [{"rejected": 1}, {"rejected": 1}], [{"name": "x.csv"}, {"name": "y.csv"}])
    assert out.name == "rejects.zip" and [p.name for p in tmp_path.iterdir()] == ["rejects.zip"]
