"""미리보기한 파일 보관 및 중단된 업로드 이어 올리기(설계서 5-4, contracts/manage.md).

2단계 수명주기:
- status == "receiving": 조각(PATCH /uploads/{id})을 디스크 임시 파일에 이어 적는 중(INCOMPLETE_UPLOAD_TTL 30분).
- status == "ready": 모든 조각 수신·검증 완료(또는 multipart 미리보기 완료). UPLOAD_TTL(1시간) 동안 보관하며
  POST /tables/{t}/batches, POST /batches, POST /tables 에서 upload_id로 재사용한다.

파일은 프로세스의 임시 폴더에 두고 projects/에 쓰지 않는다. 파일 이름은 절대 디스크 경로로 쓰지 않는다.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..ingest.operation import IngestError
from ..project import upload

UPLOAD_TTL = 3600
INCOMPLETE_UPLOAD_TTL = 1800
MAX_KEEP = 20
DEFAULT_CHUNK_MAX_MB = 16
DEFAULT_MAX_UPLOAD_MB = 200
DEFAULT_MAX_REQUEST_MB = 500
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _mb(env_name: str, default: int) -> int:
    try:
        value = int(os.environ.get(env_name) or default)
    except ValueError:
        value = default
    return max(value, 1) * 1024 * 1024


def chunk_max_bytes() -> int:
    return _mb("INGEST_CHUNK_MAX_MB", DEFAULT_CHUNK_MAX_MB)


def max_upload_bytes() -> int:
    return _mb("INGEST_MAX_UPLOAD_MB", DEFAULT_MAX_UPLOAD_MB)


def max_total_upload_bytes() -> int:
    return _mb("INGEST_MAX_REQUEST_MB", DEFAULT_MAX_REQUEST_MB) * 2


@dataclass
class Held:
    id: str
    table: str                       # 여러 파일이면 "*", 새 테이블 추론이면 "+new"
    files: list[tuple[str, Path]]    # (원래 파일 이름, 디스크 경로)
    actor_id: str | None
    at: float = field(default_factory=time.monotonic)
    wall_at: float = field(default_factory=time.time)
    map: dict[str, str] = field(default_factory=dict)          # 여러 파일: {파일 이름: 테이블}
    columns: dict[str, Any] = field(default_factory=dict)      # {파일 이름: 머리글 맞춤}(미리보기 때 보낸 것)
    status: str = "ready"                                      # "receiving" | "ready"
    total_size: int = 0
    offset: int = 0
    sha256: str | None = None
    resumable: bool = False
    chunk_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def ttl(self) -> int:
        return INCOMPLETE_UPLOAD_TTL if self.status == "receiving" else UPLOAD_TTL

    @property
    def expires_at(self) -> str:
        return datetime.fromtimestamp(self.wall_at + self.ttl, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def disk_bytes(self) -> int:
        total = 0
        for _name, p in self.files:
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    def public(self, max_chunk: int | None = None) -> dict[str, Any]:
        filename = self.files[0][0] if self.files else ""
        return {
            "upload_id": self.id,
            "table": self.table,
            "filename": filename,
            "offset": self.offset,
            "total_size": self.total_size,
            "chunk_max_bytes": chunk_max_bytes() if max_chunk is None else max_chunk,
            "status": self.status,
            "expires_at": self.expires_at,
        }


class Uploads:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, Held] = {}
        self._root = Path(tempfile.mkdtemp(prefix="dashkit-manage-uploads-"))

    def folder(self) -> Path:
        """새 업로드를 받을 폴더(요청마다 하나)."""
        path = self._root / secrets.token_hex(8)
        path.mkdir(parents=True)
        return path

    def hold(self, table: str, files: list[tuple[str, Path]], actor_id: str | None, **extra: Any) -> str:
        total = 0
        for _name, p in files:
            try:
                total += p.stat().st_size
            except OSError:
                pass
        held = Held(
            secrets.token_urlsafe(16),
            table,
            files,
            actor_id,
            status="ready",
            total_size=total,
            offset=total,
            **extra,
        )
        with self._lock:
            self._items[held.id] = held
            self._expire_locked()
        return held.id

    def start_resumable(self, table: str, filename: str, total_size: Any, actor_id: str | None, *,
                        sha256: Any = None) -> dict[str, Any]:
        """이어 올리기 세션을 시작한다(status='receiving')."""
        try:
            checked_name = upload.check_ingest_filename(filename)
        except upload.UploadError as exc:
            raise IngestError(exc.status, exc.detail, exc.message) from None
        if isinstance(total_size, bool) or not isinstance(total_size, int) or total_size <= 0:
            raise IngestError(400, "invalid_request", "total_size는 1 이상의 정수여야 합니다")
        limit = max_upload_bytes()
        if total_size > limit:
            raise IngestError(413, "upload_too_large", f"파일이 너무 큽니다(상한 {limit // (1024 * 1024)}MB)")
        norm_sha: str | None = None
        if sha256 is not None:
            if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256.strip().lower()):
                raise IngestError(400, "invalid_request", "sha256은 64자리 16진수 문자열이어야 합니다")
            norm_sha = sha256.strip().lower()

        folder = self.folder()
        file_path = folder / "upload.bin"
        try:
            with open(file_path, "wb"):
                pass
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise

        held = Held(
            f"u_{secrets.token_urlsafe(16)}",
            table,
            [(checked_name, file_path)],
            actor_id,
            status="receiving",
            total_size=total_size,
            offset=0,
            sha256=norm_sha,
            resumable=True,
        )
        with self._lock:
            self._items[held.id] = held
            self._expire_locked()
        return held.public()

    def status(self, upload_id: Any, actor_id: str | None) -> dict[str, Any]:
        """현재 오프셋·상태 조회(읽기 경로: owner 계정을 열지 않는다)."""
        with self._lock:
            self._expire_locked()
            held = self._items.get(upload_id) if isinstance(upload_id, str) else None
            if held is None or held.actor_id != actor_id:
                raise IngestError(410, "upload_expired", "업로드 세션을 찾을 수 없거나 만료되었습니다")
            held.at = time.monotonic()
            held.wall_at = time.time()
            return held.public()

    def begin_chunk(self, upload_id: Any, actor_id: str | None, req_offset: int, *,
                    content_length: int | None = None) -> Held:
        """조각 쓰기를 시작하기 전에 세션 소유권·잠금·오프셋·크기를 검사한다. 성공 시 held.chunk_lock을 쥔 채 반환."""
        with self._lock:
            self._expire_locked()
            held = self._items.get(upload_id) if isinstance(upload_id, str) else None
            if held is None or held.actor_id != actor_id:
                raise IngestError(410, "upload_expired", "업로드 세션을 찾을 수 없거나 만료되었습니다")
            if not held.chunk_lock.acquire(blocking=False):
                raise IngestError(409, "upload_in_progress", "이 업로드에 다른 조각이 전송 중입니다")
        try:
            if not held.resumable:
                raise IngestError(400, "invalid_request", "이어 올리기 세션이 아닙니다")
            if req_offset < 0:
                raise IngestError(400, "bad_request", "Upload-Offset은 0 이상의 정수여야 합니다")
            if req_offset > held.offset:
                raise IngestError(409, "offset_mismatch", "업로드 오프셋이 일치하지 않습니다", offset=held.offset)
            cmax = chunk_max_bytes()
            if content_length is not None:
                if content_length <= 0:
                    raise IngestError(400, "invalid_request", "조각 크기는 1바이트 이상이어야 합니다")
                if content_length > cmax:
                    raise IngestError(413, "upload_too_large", f"조각이 너무 큽니다(상한 {cmax // (1024 * 1024)}MB)")
                if req_offset + content_length > held.total_size:
                    raise IngestError(413, "upload_too_large", "전체 파일 크기를 초과했습니다")
            held.at = time.monotonic()
            held.wall_at = time.time()
            return held
        except BaseException:
            held.chunk_lock.release()
            raise

    def end_chunk(self, held: Held) -> None:
        held.chunk_lock.release()

    def commit_chunk(self, held: Held, req_offset: int, data: bytes, *,
                     content_sha256: str | None = None) -> dict[str, Any]:
        """읽은 조각을 검증하고 디스크에 반영한다(held.chunk_lock 보유 중 호출)."""
        if not data:
            raise IngestError(400, "invalid_request", "조각 크기는 1바이트 이상이어야 합니다")
        cmax = chunk_max_bytes()
        if len(data) > cmax:
            raise IngestError(413, "upload_too_large", f"조각이 너무 큽니다(상한 {cmax // (1024 * 1024)}MB)")
        if content_sha256 is not None:
            actual = hashlib.sha256(data).hexdigest()
            if actual != content_sha256.lower():
                raise IngestError(422, "checksum_mismatch", "조각 SHA-256 무결성 검사에 실패했습니다")
        if req_offset > held.offset:
            raise IngestError(409, "offset_mismatch", "업로드 오프셋이 일치하지 않습니다", offset=held.offset)
        if req_offset + len(data) > held.total_size:
            raise IngestError(413, "upload_too_large", "전체 파일 크기를 초과했습니다")

        filename, path = held.files[0]
        if req_offset < held.offset:
            overlap = min(len(data), held.offset - req_offset)
            with open(path, "rb") as f:
                f.seek(req_offset)
                existing = f.read(overlap)
            if existing != data[:overlap]:
                raise IngestError(409, "offset_mismatch", "이미 받은 조각과 내용이 다릅니다", offset=held.offset)
            tail = data[overlap:]
            if not tail:
                held.at = time.monotonic()
                held.wall_at = time.time()
                return held.public(cmax)
        else:
            tail = data

        start_offset = held.offset
        with open(path, "r+b") as f:
            try:
                f.seek(start_offset)
                f.write(tail)
                f.flush()
                os.fsync(f.fileno())
            except BaseException:
                try:
                    os.ftruncate(f.fileno(), start_offset)
                except OSError:
                    pass
                raise
        held.offset = start_offset + len(tail)
        held.at = time.monotonic()
        held.wall_at = time.time()

        if held.offset == held.total_size and held.status != "ready":
            if held.sha256 is not None:
                digest = hashlib.sha256()
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != held.sha256:
                    self._drop_locked(held.id)
                    raise IngestError(422, "checksum_mismatch", "전체 파일 SHA-256 무결성 검사에 실패했습니다")
            if filename.lower().endswith(".xlsx"):
                with open(path, "rb") as f:
                    magic = f.read(4)
                if magic != b"PK\x03\x04":
                    self._drop_locked(held.id)
                    raise IngestError(422, "invalid_xlsx", "암호화되지 않은 .xlsx 파일만 지원합니다")
            held.status = "ready"

        with self._lock:
            self._expire_locked()
        return held.public(cmax)

    def cancel(self, upload_id: Any, actor_id: str | None) -> None:
        """진행 중이거나 보관 중인 업로드 세션을 즉시 폐기하고 디스크에서 삭제한다."""
        with self._lock:
            self._expire_locked()
            held = self._items.get(upload_id) if isinstance(upload_id, str) else None
            if held is None or held.actor_id != actor_id:
                raise IngestError(410, "upload_expired", "업로드 세션을 찾을 수 없거나 만료되었습니다")
            if held.chunk_lock.locked():
                raise IngestError(409, "upload_in_progress", "조각 전송이 진행 중입니다. 잠시 후 다시 시도하세요")
            self._items.pop(upload_id, None)
        _remove(held)

    def get(self, upload_id: Any, table: str, actor_id: str | None) -> Held:
        """잡아 둔 파일. 없거나 지났거나 다른 테이블·다른 사람의 것이면 410 upload_expired.
        아직 조각 전송이 끝나지 않았으면 409 upload_incomplete."""
        with self._lock:
            self._expire_locked()
            held = self._items.get(upload_id) if isinstance(upload_id, str) else None
        if held is None or held.table != table or held.actor_id != actor_id:
            raise IngestError(410, "upload_expired", "미리보기한 파일을 찾을 수 없습니다(1시간이 지났거나 다른 요청). 파일을 다시 올려 주세요")
        if held.status != "ready":
            raise IngestError(409, "upload_incomplete", "파일 업로드가 아직 끝나지 않았습니다",
                              offset=held.offset, total_size=held.total_size)
        return held

    def drop(self, upload_id: str) -> None:
        with self._lock:
            held = self._items.pop(upload_id, None)
        if held is not None:
            _remove(held)

    def _drop_locked(self, upload_id: str) -> None:
        with self._lock:
            held = self._items.pop(upload_id, None)
        if held is not None:
            _remove(held)

    def _expire_locked(self) -> None:
        now = time.monotonic()
        old: list[str] = []
        for k, h in self._items.items():
            if h.chunk_lock.locked():
                continue
            if now - h.at > h.ttl:
                old.append(k)
        for k in old:
            _remove(self._items.pop(k))

        max_total = max_total_upload_bytes()
        while len(self._items) > MAX_KEEP or sum(h.disk_bytes() for h in self._items.values()) > max_total:
            candidates = [k for k in sorted(self._items, key=lambda x: self._items[x].at)
                          if not self._items[k].chunk_lock.locked()]
            if not candidates:
                break
            _remove(self._items.pop(candidates[0]))


def _remove(held: Held) -> None:
    for _name, path in held.files:
        shutil.rmtree(path.parent, ignore_errors=True)


uploads = Uploads()
