"""레지스트리에서 계약 파일의 생성 구간을 만든다(python -m dashkit gen, make gen).

파일 안의 `<!-- 생성 시작: <이름> -->` ~ `<!-- 생성 끝: <이름> -->` 사이만 바꾼다. 손으로 고치지 않는다.
tests/test_charts_registry.py가 최신인지 확인한다.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from . import charts


def _repo_root() -> Path:
    return Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parent.parent)


# (저장소 기준 경로, 구간 이름, 내용 생성 함수)
SECTIONS: list[tuple[str, str, Callable[[], str]]] = [
    ("contracts/chart-shorthand.md", "chart-types", charts.shorthand_table),
]


def _replace(text: str, name: str, body: str, path: str) -> str:
    begin, end = f"<!-- 생성 시작: {name} -->", f"<!-- 생성 끝: {name} -->"
    try:
        a = text.index(begin) + len(begin)
        b = text.index(end, a)
    except ValueError as exc:
        raise RuntimeError(f"{path}: 생성 구간 표시 '{name}'이 없음") from exc
    return text[:a] + "\n" + body + text[b:]


def render() -> dict[str, tuple[str, str]]:
    """{경로: (현재 내용, 생성 결과)}"""
    out: dict[str, tuple[str, str]] = {}
    root = _repo_root()
    for rel, name, fn in SECTIONS:
        current = out[rel][1] if rel in out else (root / rel).read_text(encoding="utf-8")
        original = out[rel][0] if rel in out else current
        out[rel] = (original, _replace(current, name, fn(), rel))
    return out


def check() -> list[str]:
    return [rel for rel, (old, new) in render().items() if old != new]


def write() -> list[str]:
    changed = []
    for rel, (old, new) in render().items():
        if old != new:
            (_repo_root() / rel).write_text(new, encoding="utf-8")
            changed.append(rel)
    return changed
