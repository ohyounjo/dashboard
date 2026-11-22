"""설정 파일을 최신 형식 버전으로 고친다(python -m dashkit migrate, 보강설계서 5-3).

- 버전은 schema.yaml, dashboard.yaml 최상위의 `version`이다(없으면 1). 현재 버전은 config.CONFIG_VERSION.
- 파일은 텍스트 수준에서 고친다. YAML을 읽어 다시 쓰지 않으므로 주석·순서·따옴표가 그대로 남는다.
- 버전 N → N+1 변환은 `MIGRATIONS[N]`에 함수 하나로 등록한다. 함수는 (파일 이름, 텍스트)를 받아 버전 N+1 형식의
  텍스트를 돌려준다(`version:` 줄은 이 모듈이 고치므로 함수는 내용만 바꾼다).
- 형식 변경 기록은 저장소의 CHANGELOG.md에 적는다.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from .config import CONFIG_VERSION, DASHBOARD_FILE, SCHEMA_FILE, ConfigError

CONFIG_FILES = (SCHEMA_FILE, DASHBOARD_FILE)

Migration = Callable[[str, str], str]

# 버전 N → N+1 변환 함수. 지금은 버전이 1뿐이라 비어 있다.
# 예: MIGRATIONS[1] = _v1_to_v2  (def _v1_to_v2(file: str, text: str) -> str)
MIGRATIONS: dict[int, Migration] = {}

_VERSION_LINE = re.compile(r"^version[ \t]*:[ \t]*\S+", re.M)


@dataclass(frozen=True)
class FileResult:
    file: str                 # schema.yaml | dashboard.yaml
    path: Path
    before: int               # 고치기 전 버전(version 키가 없으면 1)
    after: int
    steps: tuple[str, ...]    # 한 일(사람이 읽는 문장). 비어 있으면 이미 최신

    @property
    def changed(self) -> bool:
        return bool(self.steps)


def _top_level(text: str, file: str) -> dict:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"{mark.line + 1}행" if mark else "(최상위)"
        raise ConfigError([f"{file}:{where}: YAML 문법 오류: {getattr(exc, 'problem', exc)}"]) from None
    if not isinstance(doc, dict):
        raise ConfigError([f"{file}:(최상위): 최상위가 키-값 형식이 아님"])
    return doc


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def insert_version(text: str, version: int) -> str:
    """`version: <n>` 줄을 맨 위에 넣는다. BOM, `%` 지시문, YAML 시작 표시(`---`)가 있으면 그 뒤다."""
    nl = _newline(text)
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    lines = text[len(bom):].splitlines(keepends=True)
    # 내용이 시작되기 전(빈 줄·주석·지시문만 지나서)에 `---`가 있으면 그 다음 줄, 없으면 첫 줄에 넣는다
    at = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "---" or stripped.startswith("--- "):
            at = i + 1
            break
        if stripped and not stripped.startswith(("#", "%")):
            break
    head = [f"version: {version}{nl}"]
    if at < len(lines) and lines[at].strip() != "":
        head.append(nl)
    return bom + "".join(lines[:at] + head + lines[at:])


def set_version(text: str, version: int) -> str:
    """이미 있는 최상위 `version:` 줄의 값을 바꾼다(줄 끝의 주석은 그대로 둔다)."""
    out, n = _VERSION_LINE.subn(f"version: {version}", text, count=1)
    if n != 1:
        raise ValueError("최상위 version 줄을 찾지 못함")
    return out


def migrate_text(file: str, text: str, *, check: bool = False) -> tuple[str, int, list[str]]:
    """텍스트를 최신 버전으로 고친다. (새 텍스트, 고치기 전 버전, 한 일 목록). 이미 최신이면 한 일이 비어 있다."""
    doc = _top_level(text, file)
    steps: list[str] = []
    inserted = "version" not in doc
    if inserted:
        before = 1
        text = insert_version(text, before)
        if not check:
            steps.append(f"`version: {before}`을 맨 위에 넣음")
    else:
        before = doc["version"]
        if isinstance(before, bool) or not isinstance(before, int) or before < 1:
            raise ConfigError([f"{file}:version: 1 이상의 정수가 아님 ({before!r})"])
    if before > CONFIG_VERSION:
        raise ConfigError([f"{file}:version: 이 dashkit은 설정 버전 {CONFIG_VERSION}까지 지원함(파일은 버전 {before}). "
                           f"dashkit을 업그레이드하세요"])
    current = before
    while current < CONFIG_VERSION:
        step = MIGRATIONS.get(current)
        if step is None:
            raise ConfigError([f"{file}:version: 버전 {current} → {current + 1} 변환이 등록되어 있지 않음"])
        text = set_version(step(file, text), current + 1)
        steps.append(f"버전 {current} → {current + 1}")
        current += 1
    if steps or inserted:
        # 고친 결과가 여전히 YAML이고 버전이 맞는지 확인한다(변환 함수의 실수를 파일에 쓰기 전에 잡는다)
        after = _top_level(text, file).get("version")
        if after != CONFIG_VERSION:
            raise ConfigError([f"{file}:version: 변환 결과의 버전이 {after!r}임(기대 {CONFIG_VERSION})"])
    return text, before, steps


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def migrate_project(root: Path, *, check: bool = False) -> list[FileResult]:
    """두 설정 파일을 고친다. check=True면 쓰지 않고 필수 버전 변환만 돌려준다.

    두 파일을 모두 변환해 본 뒤에 쓴다(한 파일이 실패하면 아무것도 쓰지 않는다).
    """
    root = Path(root)
    errors: list[str] = []
    planned: list[tuple[FileResult, str]] = []
    for file in CONFIG_FILES:
        path = root / file
        if not path.is_file():
            errors.append(f"{file}:(최상위): 파일이 없음 ({path})")
            continue
        try:
            with open(path, encoding="utf-8", newline="") as f:
                text = f.read()
            new_text, before, steps = migrate_text(file, text, check=check)
        except ConfigError as exc:
            errors += exc.errors
            continue
        except UnicodeDecodeError as exc:
            errors.append(f"{file}:(최상위): UTF-8이 아님 ({exc.reason})")
            continue
        planned.append((FileResult(file, path, before, CONFIG_VERSION, tuple(steps)), new_text))
    if errors:
        raise ConfigError(errors)
    if not check:
        for result, new_text in planned:
            if result.changed:
                _write_atomic(result.path, new_text)
    return [r for r, _t in planned]
