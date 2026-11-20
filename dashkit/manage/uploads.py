"""미리보기한 파일을 다시 올리지 않고 확정하기(설계서 5-4, contracts/manage.md "업로드 보관").

파일로 올린 미리보기의 결과에는 upload_id가 들어 있다. 받은 파일을 UPLOAD_TTL(1시간) 동안 디스크에 잡아 두고,
같은 테이블에 {"upload_id"}를 보내면 그 파일로 적재한다. 지났으면 410 upload_expired.
파일은 프로세스의 임시 폴더(관리 서비스 컨테이너 안)에 있고 projects/에 쓰지 않는다. 목록은 메모리에 있다.
"""
from __future__ import annotations

import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ingest.operation import IngestError

UPLOAD_TTL = 3600
MAX_KEEP = 20


@dataclass
class Held:
    id: str
    table: str                       # 여러 파일이면 ""
    files: list[tuple[str, Path]]    # (원래 파일 이름, 디스크 경로)
    actor_id: str | None
    at: float = field(default_factory=time.monotonic)
    map: dict[str, str] = field(default_factory=dict)


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
        held = Held(secrets.token_urlsafe(16), table, files, actor_id, **extra)
        with self._lock:
            self._items[held.id] = held
            self._expire_locked()
        return held.id

    def get(self, upload_id: Any, table: str, actor_id: str | None) -> Held:
        """잡아 둔 파일. 없거나 지났거나 다른 테이블·다른 사람의 것이면 410 upload_expired."""
        with self._lock:
            self._expire_locked()
            held = self._items.get(upload_id) if isinstance(upload_id, str) else None
        if held is None or held.table != table or held.actor_id != actor_id:
            raise IngestError(410, "upload_expired", "미리보기한 파일을 찾을 수 없습니다(1시간이 지났거나 다른 요청). 파일을 다시 올려 주세요")
        return held

    def drop(self, upload_id: str) -> None:
        with self._lock:
            held = self._items.pop(upload_id, None)
        if held is not None:
            _remove(held)

    def _expire_locked(self) -> None:
        now = time.monotonic()
        old = [k for k, h in self._items.items() if now - h.at > UPLOAD_TTL]
        while len(self._items) - len(old) > MAX_KEEP:
            rest = [k for k in sorted(self._items, key=lambda k: self._items[k].at) if k not in old]
            old.append(rest[0])
        for k in old:
            _remove(self._items.pop(k))


def _remove(held: Held) -> None:
    for _name, path in held.files:
        shutil.rmtree(path.parent, ignore_errors=True)


uploads = Uploads()
