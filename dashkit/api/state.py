"""요청마다 파일 서명(mtime, 크기, inode)을 확인해 바뀌었을 때만 다시 읽는 상태.

- EmbedState: `.state/embed.json`(마지막으로 sync에 성공한 프로젝트와 그 임베드 UUID)
- ProjectState: `projects/<이름>/`의 schema.yaml, dashboard.yaml(검증에 실패하면 마지막으로 유효했던 설정을 유지)
- CurrentState: 둘을 묶어 "현재 프로젝트"를 정한다. embed.json의 project를 따르고, 쓸 수 없으면 env PROJECT를 쓴다
  (contracts/api.md "현재 프로젝트").
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass
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
        return self.read()[0]

    def read(self) -> tuple[config.Project, list[str]]:
        """(지금 쓰는 설정, 그 설정이 파일과 다르면 오류 목록). 한 번의 잠금 안에서 함께 읽는다."""
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
            return self._project, list(self._errors)

    def status(self) -> dict:
        """/api/health의 `config`: 지금 쓰는 설정이 파일과 같은지(valid), 다르면 오류 개수."""
        self.project()
        with self._lock:
            return {"valid": not self._errors, "errors": len(self._errors), "reloads": self._reloads}

    def errors(self) -> list[str]:
        with self._lock:
            return list(self._errors)


class EmbedState:
    """embed.json의 (project, {대시보드 키: embed_uuid}). 파일이 없거나 깨졌으면 (None, {})."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._sig: tuple | None = None  # (mtime_ns, size, inode); 파일 없음은 ("missing",)
        self._value: tuple[str | None, dict[str, str]] = (None, {})

    def read(self) -> tuple[str | None, dict[str, str]]:
        with self._lock:
            sig = file_signature(self.path)   # 잠금 안에서 읽는다(ProjectState.read와 같은 이유)
            if sig != self._sig:
                self._value = self._read(sig)
                self._sig = sig
            return self._value

    def _read(self, sig: tuple) -> tuple[str | None, dict[str, str]]:
        if sig[0] == "missing":
            log.warning("embed.json이 없음(%s). apply 전으로 취급합니다", self.path)
            return None, {}
        if sig[0] == "error":
            log.warning("embed.json을 읽을 수 없음(%s): %s", self.path, sig[1])
            return None, {}
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            log.warning("embed.json을 읽을 수 없음(%s): %s. 비어 있는 것으로 취급합니다", self.path, exc)
            return None, {}
        if (not isinstance(data, dict) or not isinstance(data.get("dashboards"), dict)
                or not isinstance(data.get("project"), str)):
            log.warning("embed.json 형식 오류(%s). 비어 있는 것으로 취급합니다", self.path)
            return None, {}
        out: dict[str, str] = {}
        for key, entry in data["dashboards"].items():
            if isinstance(entry, dict) and isinstance(entry.get("embed_uuid"), str) and entry["embed_uuid"]:
                out[key] = entry["embed_uuid"]
        log.info("embed.json을 읽음: 프로젝트 '%s', 대시보드 %d개", data["project"], len(out))
        return data["project"], out


_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")   # config.load_project와 같은 형식


@dataclass(frozen=True)
class Current:
    """요청 하나가 쓰는 현재 상태. 한 번 받아 응답이 끝날 때까지 쓴다(도중에 프로젝트나 설정이 바뀌어도 섞이지 않는다)."""
    project: config.Project
    embeds: dict[str, str]      # 대시보드 키 → embed_uuid. embed.json이 이 프로젝트의 것이 아니면 비어 있다
    errors: tuple[str, ...]     # 설정 파일이 바뀌었는데 검증에 실패해 이전 설정을 쓰는 중이면 그 오류
    source: str                 # "embed"(embed.json의 project) | "env"(env PROJECT) | "stale"(둘 다 읽을 수 없어 마지막 상태를 유지)

    @property
    def valid(self) -> bool:
        return not self.errors


class CurrentState:
    """현재 프로젝트 = embed.json의 `project`(마지막으로 sync에 성공한 프로젝트).

    embed.json이 없거나 깨졌거나, 그 프로젝트 폴더가 없거나, 그 설정을 한 번도 유효하게 읽지 못했으면
    env PROJECT(fallback)를 쓴다. 이때 embed.json이 fallback의 것이 아니면 임베드는 비어 있다(미적용).

    - 한 번 유효하게 읽은 프로젝트의 설정이 나중에 깨지면 ProjectState가 마지막으로 유효했던 설정을 유지한다.
    - embed.json의 프로젝트를 쓸 수 없다는 판단은 그 설정 파일들의 서명이 바뀔 때까지 다시 시도하지 않는다
      (요청마다 검증을 되풀이하지 않고, 로그도 변경 한 번에 한 번).
    - 어느 쪽도 쓸 수 없으면: 처음(생성자)에는 ConfigError, 그 뒤에는 마지막으로 돌려준 상태를 유지한다.
    """

    def __init__(self, fallback: str, embed_path: Path, projects_dir: Path | None = None,
                 loader: Callable[[str, Path], config.Project] | None = None):
        self.fallback = fallback
        self.projects_dir = Path(projects_dir) if projects_dir else config.default_projects_dir()
        self._loader = loader
        self._embed = EmbedState(embed_path)
        self._lock = threading.Lock()
        self._followed: ProjectState | None = None      # embed.json을 따라 읽은 프로젝트
        self._fallback: ProjectState | None = None      # env PROJECT
        self._rejected: tuple | None = None             # (이름, 서명): 쓸 수 없다고 판단한 embed.json의 프로젝트
        self._fallback_rejected: tuple | None = None
        self._last: Current | None = None
        self.current()                                   # 실패하면 ConfigError

    def _files_signature(self, name: str) -> tuple:
        root = self.projects_dir / name
        return (name, root.is_dir()) + tuple(file_signature(root / f) for f in (config.SCHEMA_FILE, config.DASHBOARD_FILE))

    def _follow(self, name: str) -> ProjectState | None:
        """embed.json이 가리키는 프로젝트의 상태. 쓸 수 없으면 None."""
        if not _NAME_RE.fullmatch(name):
            if self._rejected != (name,):
                self._rejected = (name,)
                log.warning("embed.json의 project 이름 형식 오류. env PROJECT를 씁니다")
            self._followed = None
            return None
        if not (self.projects_dir / name).is_dir():
            # 폴더가 없어진 프로젝트는 마지막 설정도 쓰지 않는다(지운 프로젝트가 계속 보이지 않게)
            sig = self._files_signature(name)
            if self._rejected != sig:
                self._rejected = sig
                log.warning("embed.json의 프로젝트 '%s' 폴더가 없음. env PROJECT를 씁니다", name)
            self._followed = None
            return None
        if self._followed is not None and self._followed.name == name:
            return self._followed
        self._followed = None
        sig = self._files_signature(name)
        if self._rejected == sig:
            return None
        try:
            self._followed = ProjectState(name, self.projects_dir, self._loader)
        except config.ConfigError as exc:
            self._rejected = sig
            log.error("embed.json의 프로젝트 '%s' 설정에 오류 %d개. env PROJECT를 씁니다:", name, len(exc.errors))
            for e in exc.errors:
                log.error("  %s", e)
            return None
        self._rejected = None
        return self._followed

    def _env(self) -> ProjectState:
        """env PROJECT의 상태. 읽을 수 없으면 ConfigError."""
        if self._fallback is None:
            sig = self._files_signature(self.fallback) if _NAME_RE.fullmatch(self.fallback or "") else (self.fallback,)
            if self._fallback_rejected is not None and self._fallback_rejected[0] == sig:
                raise config.ConfigError(self._fallback_rejected[1])
            try:
                self._fallback = ProjectState(self.fallback, self.projects_dir, self._loader)
            except config.ConfigError as exc:
                self._fallback_rejected = (sig, list(exc.errors))
                raise
            self._fallback_rejected = None
        return self._fallback

    def current(self) -> Current:
        with self._lock:
            name, embeds = self._embed.read()
            state = self._follow(name) if name is not None and name != self.fallback else None
            source = "embed"
            if state is None:
                source = "env"
                try:
                    state = self._env()
                except config.ConfigError as exc:
                    if self._last is None:
                        raise
                    if self._last.source != "stale":
                        log.error("env PROJECT '%s'도 읽을 수 없음(오류 %d개). 마지막으로 유효했던 상태를 계속 씁니다",
                                  self.fallback, len(exc.errors))
                        self._last = Current(self._last.project, self._last.embeds, tuple(exc.errors), "stale")
                    return self._last
            project, errors = state.read()
            if self._last is None or self._last.project.name != project.name or self._last.source != source:
                log.info("현재 프로젝트: '%s' (%s)", project.name,
                         "embed.json의 project" if source == "embed" else "env PROJECT")
            self._last = Current(project, embeds if name == project.name else {}, tuple(errors), source)
            return self._last
