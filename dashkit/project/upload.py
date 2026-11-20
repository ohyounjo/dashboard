"""multipart/form-data 업로드 읽기. 계약: contracts/studio.md "요청 보호"의 업로드 줄.

python-multipart를 들이지 않고(새 의존성 금지) 필요한 만큼만 읽는다: 파일 부분(`name`, `filename`, 내용)과 글 부분.
Studio(parse_multipart)는 본문을 한도까지만 메모리에 읽고, 관리 서비스의 큰 파일(stream_multipart)은 조각씩 읽어
파일 부분을 디스크의 임시 파일로 쓴다. 파일 이름은 표시와 추론(표 이름)에만 쓰고 **파일 경로로 쓰지 않는다**.
"""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

MAX_FILES = 50
DEFAULT_MAX_FILE_MB = 20
DEFAULT_MAX_REQUEST_MB = 100
MAX_NAME_LENGTH = 200
_BOUNDARY_RE = re.compile(r'boundary=(?:"([^"]{1,70})"|([^;,\s]{1,70}))', re.I)
_PARAM_RE = re.compile(r';\s*([a-zA-Z*]+)="((?:[^"\\]|\\.)*)"')


class UploadError(Exception):
    def __init__(self, status: int, detail: str, message: str):
        self.status = status
        self.detail = detail
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Part:
    name: str
    filename: str | None
    content: bytes


def _mb(env_name: str, default: int) -> int:
    try:
        value = int(os.environ.get(env_name) or default)
    except ValueError:
        value = default
    return max(value, 1) * 1024 * 1024


def max_file_bytes() -> int:
    return _mb("STUDIO_MAX_UPLOAD_MB", DEFAULT_MAX_FILE_MB)


def max_request_bytes() -> int:
    """요청 하나의 본문 상한. 파일 하나의 한도보다 작을 수는 없다."""
    return max(_mb("STUDIO_MAX_REQUEST_MB", DEFAULT_MAX_REQUEST_MB), max_file_bytes() + 1024 * 1024)


def check_filename(name: str | None) -> str:
    """`<이름>.csv`만 받는다. 경로 문자(/, \\, .., NUL)와 제어 문자는 거절한다."""
    if not name:
        raise UploadError(400, "invalid_filename", "파일 이름이 없습니다")
    bad = (len(name) > MAX_NAME_LENGTH or "/" in name or "\\" in name or ".." in name
           or any(ord(ch) < 32 or ord(ch) == 127 for ch in name) or name.startswith(".")
           or name != name.strip())
    if bad or not name.lower().endswith(".csv") or len(name) <= 4:
        shown = "".join(ch if ch.isprintable() else "?" for ch in name[:60])
        raise UploadError(400, "invalid_filename", f"파일 이름은 <이름>.csv여야 하고 경로 문자를 쓸 수 없습니다: '{shown}'")
    return name


async def read_body(stream: AsyncIterator[bytes], content_length: str | None, limit: int) -> bytes:
    """본문을 limit 바이트까지만 읽는다. 넘으면 413."""
    too_large = UploadError(413, "upload_too_large", f"요청이 너무 큽니다(상한 {limit // (1024 * 1024)}MB)")
    if content_length is not None:
        try:
            if int(content_length) > limit:
                raise too_large
        except ValueError:
            raise UploadError(400, "bad_request", "Content-Length 형식 오류") from None
    chunks: list[bytes] = []
    size = 0
    async for chunk in stream:
        size += len(chunk)
        if size > limit:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


def parse_multipart(content_type: str | None, body: bytes) -> list[Part]:
    """multipart/form-data 본문 → 부분 목록. 형식이 틀리면 UploadError."""
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media != "multipart/form-data":
        raise UploadError(415, "unsupported_media_type", "업로드는 multipart/form-data로 보냅니다")
    m = _BOUNDARY_RE.search(content_type or "")
    if not m:
        raise UploadError(400, "bad_request", "multipart 경계(boundary)가 없습니다")
    delimiter = b"--" + (m.group(1) or m.group(2)).encode("latin-1")
    bad = UploadError(400, "bad_request", "multipart 본문 형식 오류")
    if not body.startswith(delimiter):
        start = body.find(b"\r\n" + delimiter)
        if start < 0:
            raise bad
        body = body[start + 2:]
    parts: list[Part] = []
    pos = len(delimiter)
    while True:
        if body[pos:pos + 2] == b"--":
            return parts
        if body[pos:pos + 2] != b"\r\n":
            raise bad
        pos += 2
        head_end = body.find(b"\r\n\r\n", pos)
        nxt = body.find(b"\r\n" + delimiter, pos)
        if head_end < 0 or nxt < 0 or head_end > nxt:
            # 머리글이 없는 부분(빈 줄로 바로 시작)도 형식 오류로 본다
            raise bad
        disposition = ""
        for line in body[pos:head_end].split(b"\r\n"):
            key, _, value = line.decode("utf-8", errors="replace").partition(":")
            if key.strip().lower() == "content-disposition":
                disposition = value.strip()
        if not disposition.lower().startswith("form-data"):
            raise bad
        params = {k.lower(): re.sub(r"\\(.)", r"\1", v) for k, v in _PARAM_RE.findall(disposition)}
        if "name" not in params:
            raise bad
        parts.append(Part(params["name"], params.get("filename"), body[head_end + 4:nxt]))
        if len(parts) > MAX_FILES + 10:
            raise UploadError(400, "too_many_files", f"한 번에 올릴 수 있는 파일은 {MAX_FILES}개까지입니다")
        pos = nxt + 2 + len(delimiter)


def csv_files(parts: list[Part], field: str, *, single: bool = False) -> dict[str, bytes]:
    """field 이름의 파일 부분들 → {파일 이름: 내용}. 이름·개수·크기를 검사한다."""
    files = [p for p in parts if p.name == field and p.filename is not None]
    if not files:
        raise UploadError(400, "bad_request", f"multipart에 `{field}` 파일이 없습니다")
    if single and len(files) != 1:
        raise UploadError(400, "bad_request", f"`{field}` 파일은 하나만 보냅니다")
    if len(files) > MAX_FILES:
        raise UploadError(400, "too_many_files", f"한 번에 올릴 수 있는 파일은 {MAX_FILES}개까지입니다")
    limit = max_file_bytes()
    out: dict[str, bytes] = {}
    for p in files:
        name = check_filename(p.filename)
        if len(p.content) > limit:
            raise UploadError(413, "upload_too_large", f"파일이 너무 큽니다(상한 {limit // (1024 * 1024)}MB): {name}")
        if name in out:
            raise UploadError(400, "invalid_filename", f"같은 이름의 파일이 두 번 있습니다: {name}")
        out[name] = p.content
    return out


# ---------------------------------------------------------------- 디스크로 받는 multipart(관리 서비스의 큰 파일)

@dataclass
class DiskPart:
    """디스크의 임시 파일로 받은 부분. 글 부분은 value(문자열), 파일 부분은 path와 size."""
    name: str
    filename: str | None
    path: Path | None = None
    size: int = 0
    value: str | None = None


class _Sink:
    def __init__(self, folder: Path, limit: int):
        fd, name = tempfile.mkstemp(prefix="upload-", suffix=".part", dir=folder)
        self.path = Path(name)
        self.file = os.fdopen(fd, "wb")
        self.size = 0
        self.limit = limit

    def write(self, data: bytes) -> None:
        self.size += len(data)
        if self.size > self.limit:
            raise UploadError(413, "upload_too_large", f"파일이 너무 큽니다(상한 {self.limit // (1024 * 1024)}MB)")
        self.file.write(data)


async def stream_multipart(content_type: str | None, stream: AsyncIterator[bytes], content_length: str | None, *,
                           folder: Path, file_limit: int, request_limit: int, text_limit: int = 1024 * 1024,
                           max_files: int = MAX_FILES) -> list[DiskPart]:
    """multipart/form-data를 조각씩 읽어 파일 부분은 folder의 임시 파일로 쓴다(메모리에 전부 올리지 않는다).

    실패하면 만든 임시 파일을 지우고 UploadError. 성공하면 부른 쪽이 파일을 지운다(discard_parts).
    """
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if media != "multipart/form-data":
        raise UploadError(415, "unsupported_media_type", "업로드는 multipart/form-data로 보냅니다")
    m = _BOUNDARY_RE.search(content_type or "")
    if not m:
        raise UploadError(400, "bad_request", "multipart 경계(boundary)가 없습니다")
    if content_length is not None:
        try:
            if int(content_length) > request_limit:
                raise UploadError(413, "upload_too_large", f"요청이 너무 큽니다(상한 {request_limit // (1024 * 1024)}MB)")
        except ValueError:
            raise UploadError(400, "bad_request", "Content-Length 형식 오류") from None
    boundary = b"--" + (m.group(1) or m.group(2)).encode("latin-1")
    delim = b"\r\n" + boundary
    bad = UploadError(400, "bad_request", "multipart 본문 형식 오류")
    folder.mkdir(parents=True, exist_ok=True)
    parts: list[DiskPart] = []
    buf = b""
    total = 0
    state = "preamble"           # preamble → headers → body → (headers | end)
    sink: _Sink | None = None
    text = bytearray()
    current: DiskPart | None = None

    def close_part() -> None:
        nonlocal sink, current, text
        assert current is not None
        if sink is not None:
            sink.file.close()
            current.path, current.size = sink.path, sink.size
            sink = None
        else:
            current.value = bytes(text).decode("utf-8", errors="replace")
        parts.append(current)
        current = None
        text = bytearray()

    try:
        async for chunk in stream:
            total += len(chunk)
            if total > request_limit:
                raise UploadError(413, "upload_too_large", f"요청이 너무 큽니다(상한 {request_limit // (1024 * 1024)}MB)")
            buf += chunk
            while True:
                if state == "preamble":
                    i = buf.find(boundary)
                    if i < 0:
                        buf = buf[-len(boundary):]
                        break
                    buf = buf[i + len(boundary):]
                    state = "after_boundary"
                if state == "after_boundary":
                    if len(buf) < 2:
                        break
                    if buf[:2] == b"--":
                        state = "end"
                        break
                    if buf[:2] != b"\r\n":
                        raise bad
                    buf = buf[2:]
                    state = "headers"
                if state == "headers":
                    j = buf.find(b"\r\n\r\n")
                    if j < 0:
                        if len(buf) > 16 * 1024:
                            raise bad
                        break
                    head, buf = buf[:j], buf[j + 4:]
                    disposition = ""
                    for line in head.split(b"\r\n"):
                        key, _, value = line.decode("utf-8", errors="replace").partition(":")
                        if key.strip().lower() == "content-disposition":
                            disposition = value.strip()
                    if not disposition.lower().startswith("form-data"):
                        raise bad
                    params = {k.lower(): re.sub(r"\\(.)", r"\1", v) for k, v in _PARAM_RE.findall(disposition)}
                    if "name" not in params:
                        raise bad
                    current = DiskPart(params["name"], params.get("filename"))
                    if current.filename is not None:
                        if sum(1 for p in parts if p.filename is not None) >= max_files:
                            raise UploadError(400, "too_many_files", f"한 번에 올릴 수 있는 파일은 {max_files}개까지입니다")
                        sink = _Sink(folder, file_limit)
                    state = "body"
                if state == "body":
                    k = buf.find(delim)
                    if k < 0:
                        keep = len(delim)
                        if len(buf) > keep:
                            data, buf = buf[:-keep], buf[-keep:]
                            if sink is not None:
                                sink.write(data)
                            else:
                                text.extend(data)
                                if len(text) > text_limit:
                                    raise UploadError(413, "upload_too_large", "글 부분이 너무 깁니다")
                        break
                    data, buf = buf[:k], buf[k + len(delim):]
                    if sink is not None:
                        sink.write(data)
                    else:
                        text.extend(data)
                    close_part()
                    state = "after_boundary"
                    continue
                if state == "end":
                    break
            if state == "end":
                break
        if state != "end":
            raise bad
    except BaseException:
        if sink is not None:
            sink.file.close()
            parts.append(DiskPart("", None, sink.path))
        discard_parts(parts)
        raise
    return parts


def discard_parts(parts: list[DiskPart]) -> None:
    for p in parts:
        if p.path is not None:
            try:
                p.path.unlink()
            except FileNotFoundError:
                pass
