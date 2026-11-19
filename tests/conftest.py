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


BROKEN_CSV_PROJECT = "devtracker"
BROKEN_CSV_ERRORS = [
    ("data/issues.csv:2:milestone_id:", "ms99"),
    ("data/issues.csv:3:severity:", "blocker"),
    ("data/milestones.csv:4:progress:", "120"),
    ("data/docs.csv:2:version:", "2.1"),
    ("data/tasks.csv:18:id:", "T1"),
]


def broken_csv_copy(tmp_path: Path) -> Path:
    """devtracker 사본에 오류 5종을 심는다. 기대 오류는 BROKEN_CSV_ERRORS."""
    root = copy_project(BROKEN_CSV_PROJECT, tmp_path)
    data = root / BROKEN_CSV_PROJECT / "data"

    h, rows = read_csv(data / "issues.csv")
    rows[0]["milestone_id"] = "ms99"          # 없는 참조
    rows[1]["severity"] = "blocker"           # 잘못된 enum
    write_csv(data / "issues.csv", h, rows)

    h, rows = read_csv(data / "milestones.csv")
    rows[2]["progress"] = "120"               # 범위 밖 숫자
    write_csv(data / "milestones.csv", h, rows)

    h, rows = read_csv(data / "docs.csv")
    rows[0]["version"] = "2.1"                # 패턴 불일치
    write_csv(data / "docs.csv", h, rows)

    h, rows = read_csv(data / "tasks.csv")
    rows.append(dict(rows[0]))                # 중복 키
    write_csv(data / "tasks.csv", h, rows)
    return root


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
