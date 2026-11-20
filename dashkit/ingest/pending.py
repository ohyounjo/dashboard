"""묶음 기록(설계서 5-4, contracts/ingest.md "묶음 기록과 끊김 복구").

폴더를 바꾸는 작업은 파일을 바꾸기 전에 `data/_pending/<묶음 id>.json`을 먼저 쓰고, 커밋한 뒤 지운다.
도중에 죽으면 기록이 남고, 다음 작업(또는 load)이 잠금을 잡은 뒤 복구 절차(recover.py)로 처리한다.

확정 전의 정규형 파일은 `data/_pending/incoming/`에 둔다. 배치 폴더와 같은 파일 시스템이라 제 이름으로 옮기는 일이
이름 바꾸기 한 번(원자적)이다. `_`로 시작하는 폴더라 로더가 데이터로 읽지 않는다.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .batches import INCOMING_DIR, PENDING_DIR

INCOMING_TTL = 24 * 3600      # 묶음 기록이 가리키지 않는 incoming 파일을 지우는 나이(초)


def pending_dir(data_dir: Path) -> Path:
    return data_dir / PENDING_DIR


def incoming_dir(data_dir: Path) -> Path:
    return data_dir / PENDING_DIR / INCOMING_DIR


def new_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4)


def incoming_path(data_dir: Path) -> Path:
    """확정 전의 정규형 파일을 쓸 새 경로(아직 만들지 않는다)."""
    folder = incoming_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{new_id()}.csv.gz"


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_record(data_dir: Path, record: dict[str, Any]) -> Path:
    """묶음 기록을 원자적으로 쓴다(임시 파일 → fsync → 이름 바꾸기)."""
    folder = pending_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{record['id']}.json"
    fd, tmp = tempfile.mkstemp(prefix=".record-", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(folder)
    return path


def list_records(data_dir: Path) -> list[tuple[Path, dict[str, Any] | None]]:
    """남은 묶음 기록(이름순 = 만든 순서). 읽을 수 없는 기록은 (경로, None)."""
    folder = pending_dir(data_dir)
    if not folder.is_dir():
        return []
    out: list[tuple[Path, dict[str, Any] | None]] = []
    for p in sorted(folder.glob("*.json")):
        if p.name.startswith("."):
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = None
        out.append((p, doc if isinstance(doc, dict) else None))
    return out


def remove_record(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    _fsync_dir(path.parent)


def sweep_incoming(data_dir: Path, keep: set[str], now: float | None = None) -> int:
    """묶음 기록이 가리키지 않고 오래된 incoming 파일을 지운다(끊긴 접수가 남긴 것). 지운 수."""
    folder = incoming_dir(data_dir)
    if not folder.is_dir():
        return 0
    now = time.time() if now is None else now
    removed = 0
    for p in folder.iterdir():
        if p.name in keep or not p.is_file():
            continue
        try:
            if now - p.stat().st_mtime > INCOMING_TTL:
                p.unlink()
                removed += 1
        except OSError:
            pass
    return removed
