"""공통 픽스처. tools 컨테이너(make test)에서 실행한다.

- DB가 필요한 테스트는 dashkit.db의 접속 정보(환경변수)를 쓴다.
- Superset이 필요한 테스트는 dashkit.sync.client.SupersetClient(관리자)를 쓴다.
- 현재 적용된 프로젝트는 env PROJECT(기본 devtracker)다.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"
WEB_URL = os.environ.get("TEST_WEB_URL", "http://web")  # 같은 compose 네트워크의 nginx


def live_project() -> str:
    return os.environ.get("PROJECT", "devtracker")


@pytest.fixture(scope="session")
def superset():
    from dashkit.sync.client import SupersetClient

    client = SupersetClient()
    client.wait_ready(timeout=120)
    client.login()
    return client


def copy_project(src: str, dst_root: Path, new_name: str | None = None) -> Path:
    """projects/<src>를 dst_root/<new_name>로 복사하고 dst_root를 돌려준다."""
    dst = dst_root / (new_name or src)
    shutil.copytree(PROJECTS / src, dst)
    return dst_root


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)


def http(method: str, url: str, body=None, headers=None) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw, code = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read(), e.code
    try:
        return code, json.loads(raw)
    except ValueError:
        return code, raw.decode(errors="replace")


def api(method: str, path: str, body=None) -> tuple[int, dict | str]:
    return http(method, f"{WEB_URL}{path}", body)
