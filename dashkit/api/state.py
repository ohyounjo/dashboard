"""`.state/embed.json` 읽기. 요청마다 mtime을 확인해 바뀌었을 때만 다시 읽는다."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("dashkit.api")


def default_state_dir() -> Path:
    return Path(os.environ.get("STATE_DIR") or "/app/.state")


class EmbedState:
    """embed.json의 {대시보드 키: embed_uuid}. 파일이 없거나 깨졌거나 project가 다르면 빈 dict."""

    def __init__(self, path: Path, project: str):
        self.path = path
        self.project = project
        self._lock = threading.Lock()
        self._sig: tuple | None = None  # (mtime_ns, size, inode); 파일 없음은 ("missing",)
        self._embeds: dict[str, str] = {}

    def embeds(self) -> dict[str, str]:
        try:
            st = self.path.stat()
            sig: tuple = (st.st_mtime_ns, st.st_size, st.st_ino)
        except FileNotFoundError:
            sig = ("missing",)
        except OSError as exc:
            sig = ("error", str(exc))
        with self._lock:
            if sig != self._sig:
                self._embeds = self._read(sig)
                self._sig = sig
            return self._embeds

    def _read(self, sig: tuple) -> dict[str, str]:
        if sig[0] == "missing":
            log.warning("embed.json이 없음(%s). apply 전으로 취급합니다", self.path)
            return {}
        if sig[0] == "error":
            log.warning("embed.json을 읽을 수 없음(%s): %s", self.path, sig[1])
            return {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            log.warning("embed.json을 읽을 수 없음(%s): %s. 비어 있는 것으로 취급합니다", self.path, exc)
            return {}
        if not isinstance(data, dict) or not isinstance(data.get("dashboards"), dict):
            log.warning("embed.json 형식 오류(%s). 비어 있는 것으로 취급합니다", self.path)
            return {}
        if data.get("project") != self.project:
            log.warning("embed.json의 project '%s'가 현재 프로젝트 '%s'와 다름. 비어 있는 것으로 취급합니다",
                        data.get("project"), self.project)
            return {}
        out: dict[str, str] = {}
        for key, entry in data["dashboards"].items():
            if isinstance(entry, dict) and isinstance(entry.get("embed_uuid"), str) and entry["embed_uuid"]:
                out[key] = entry["embed_uuid"]
        log.info("embed.json을 읽음: 대시보드 %d개", len(out))
        return out
