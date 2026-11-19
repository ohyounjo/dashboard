"""요청마다 파일 서명(mtime, 크기, inode)을 확인해 바뀌었을 때만 다시 읽는 상태 두 가지.

- EmbedState: `.state/embed.json`
- ProjectState: `projects/<PROJECT>/`의 schema.yaml, dashboard.yaml(검증에 실패하면 마지막으로 유효했던 설정을 유지)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Callable

from .. import config

log = logging.getLogger("dashkit.api")


def default_state_dir() -> Path:
    return Path(os.environ.get("STATE_DIR") or "/app/.state")


def file_signature(path: Path) -> tuple:
    """(mtime_ns, 크기, inode). 파일이 없으면 ("missing",), 읽을 수 없으면 ("error", 이유)."""
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except FileNotFoundError:
        return ("missing",)
    except OSError as exc:
        return ("error", str(exc))


class ProjectState:
    """현재 프로젝트 설정. `project()`를 부를 때마다 두 설정 파일의 서명을 보고 바뀌었으면 다시 읽는다.

    - 처음 읽기(생성자)가 실패하면 ConfigError를 그대로 던진다(backend는 시작하지 않는다).
    - 다시 읽기가 실패하면 마지막으로 유효했던 설정을 계속 돌려주고, 오류는 그 서명에서 한 번만 로그에 남긴다.
      파일이 다시 바뀌면(서명이 달라지면) 다시 시도한다.
    - 잠금 하나로 서명 읽기·비교와 설정 읽기를 묶는다. 변경 한 번에 한 번만 다시 읽는다. 돌려준 Project는 바뀌지 않는 값이라 요청 하나는 처음 받은 것을 끝까지 쓴다.
    """

    def __init__(self, name: str, projects_dir: Path | None = None,
                 loader: Callable[[str, Path], config.Project] | None = None):
        self.name = name
        self.projects_dir = Path(projects_dir) if projects_dir else config.default_projects_dir()
        self._loader = loader or config.load_project
        self._paths = tuple(self.projects_dir / name / f for f in (config.SCHEMA_FILE, config.DASHBOARD_FILE))
        self._lock = threading.Lock()
        self._errors: list[str] = []
        self._reloads = 0
        self._sig = self._signature()
        self._project = self._loader(name, self.projects_dir)   # 실패하면 ConfigError

    def _signature(self) -> tuple:
        return tuple(file_signature(p) for p in self._paths)

    def project(self) -> config.Project:
        # 서명은 잠금 안에서 읽는다. 밖에서 읽으면 변경 전에 읽어 둔 낡은 서명이 뒤늦게 들어와 다시 읽기를 되풀이한다
        with self._lock:
            sig = self._signature()
            if sig != self._sig:
                # 서명을 먼저 기록한다. 읽는 도중 파일이 또 바뀌면 다음 요청의 서명이 달라져 다시 읽는다
                self._sig = sig
                try:
                    self._project = self._loader(self.name, self.projects_dir)
                except config.ConfigError as exc:
                    self._errors = list(exc.errors)
                    log.error("설정을 다시 읽었지만 오류 %d개. 마지막으로 유효했던 설정을 계속 씁니다 (PROJECT=%s):",
                              len(exc.errors), self.name)
                    for e in exc.errors:
                        log.error("  %s", e)
                except Exception as exc:  # 읽는 중 파일이 사라지는 등. 요청을 실패시키지 않는다
                    self._errors = [f"{type(exc).__name__}: {exc}"]
                    log.error("설정을 다시 읽지 못함(%s). 마지막으로 유효했던 설정을 계속 씁니다 (PROJECT=%s)",
                              self._errors[0], self.name)
                else:
                    self._errors = []
                    self._reloads += 1
                    log.info("설정 변경을 감지해 프로젝트 '%s'를 다시 읽음", self.name)
            return self._project

    def status(self) -> dict:
        """/api/health의 `config`: 지금 쓰는 설정이 파일과 같은지(valid), 다르면 오류 개수."""
        self.project()
        with self._lock:
            return {"valid": not self._errors, "errors": len(self._errors), "reloads": self._reloads}

    def errors(self) -> list[str]:
        with self._lock:
            return list(self._errors)


class EmbedState:
    """embed.json의 {대시보드 키: embed_uuid}. 파일이 없거나 깨졌거나 project가 다르면 빈 dict."""

    def __init__(self, path: Path, project: str):
        self.path = path
        self.project = project
        self._lock = threading.Lock()
        self._sig: tuple | None = None  # (mtime_ns, size, inode); 파일 없음은 ("missing",)
        self._embeds: dict[str, str] = {}

    def embeds(self) -> dict[str, str]:
        with self._lock:
            sig = file_signature(self.path)   # 잠금 안에서 읽는다(ProjectState.project와 같은 이유)
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
