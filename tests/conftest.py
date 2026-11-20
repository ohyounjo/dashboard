"""공통 픽스처. tools 컨테이너(make test)에서 실행한다.

- DB가 필요한 테스트는 dashkit.db의 접속 정보(환경변수)를 쓴다.
- Superset이 필요한 테스트는 dashkit.sync.client.SupersetClient(관리자)를 쓴다.
- 현재 적용된 프로젝트는 env PROJECT(기본 devtracker)다.
- 웹앱 API(/api/app 등)는 로그인이 필요하다. api()는 테스트용 admin 계정(처음 부를 때 만들고 세션이 끝나면 지운다)으로
  로그인한 세션 쿠키를 붙여 부른다. 로그인 없이 부르려면 api(..., auth=False).
- 엔드포인트 함수를 직접 부르는 단위 테스트는 ADMIN(로그인한 admin)을 user 인자로 넘긴다.
"""
from __future__ import annotations

import csv
import json
import os
import secrets
import shutil
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from dashkit.auth.routes import Principal

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ROOT / "projects"
WEB_URL = os.environ.get("TEST_WEB_URL", "http://web")  # 같은 compose 네트워크의 nginx
# 인증 POST(/api/auth/*)는 IP당 1분에 20번이다. 테스트는 backend에 바로 보내고 요청마다 다른 X-Real-IP를 붙인다
# (nginx를 거치면 모든 테스트가 tools 컨테이너 IP 하나로 세어진다). 운영에서 backend는 내부망에만 열려 있다.
BACKEND_URL = os.environ.get("TEST_BACKEND_URL", "http://backend:8000")


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


# ---------------------------------------------------------------- 로그인 도우미(contracts/auth.md)

# 단위 테스트에서 엔드포인트 함수를 직접 부를 때 넘기는 로그인 사용자
ADMIN = Principal(id="00000000-0000-4000-8000-000000000001", email="admin@example.com", role="admin")

SESSION_COOKIE = "dashkit_session"


def random_email(prefix: str) -> str:
    """허용 도메인(AUTH_EMAIL_DOMAINS의 첫 항목)의 테스트용 임의 주소."""
    domain = (os.environ.get("AUTH_EMAIL_DOMAINS") or "example.com").split(",")[0].strip().lstrip("@").lower()
    return f"{prefix}-{secrets.token_hex(4)}@{domain}"


def make_user(email: str, password: str | None, *, role: str = "user", status: str = "active", **kw):
    """인증 DB에 계정을 바로 만든다(dash_auth 계정). 테스트가 끝나면 delete_user로 지운다."""
    from dashkit.auth import passwords, store

    return store.create_user(email, role=role, status=status, source=kw.pop("source", "invite"),
                             password_hash=passwords.hash_password(password) if password else None,
                             email_verified=status == "active", **kw)


def delete_user(user_id: str) -> None:
    from dashkit.auth import store

    store.delete_user(user_id)


def http_full(method: str, url: str, body=None, headers=None, raw: bytes | None = None):
    """(상태, 본문(JSON이면 dict), 응답 헤더). raw를 주면 그 바이트를 본문으로 보낸다(Content-Type은 headers로)."""
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    hdrs = {} if raw is not None else {"Content-Type": "application/json"}
    req = urllib.request.Request(url, data=data, method=method, headers={**hdrs, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            content, code, rh = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        content, code, rh = e.read(), e.code, e.headers
    try:
        parsed = json.loads(content) if content else None
    except ValueError:
        parsed = content.decode(errors="replace")
    return code, parsed, rh


def session_cookie(headers) -> str | None:
    """응답 헤더의 Set-Cookie에서 세션 쿠키 값."""
    for raw in headers.get_all("Set-Cookie") or []:
        name, _, rest = raw.partition("=")
        if name.strip() == SESSION_COOKIE:
            value = rest.split(";", 1)[0].strip()
            return value or None
    return None


def fake_ip() -> str:
    """요청 제한에 걸리지 않게 요청마다 다른 사설 IP(테스트 전용)."""
    return "10.{}.{}.{}".format(*secrets.token_bytes(3))


def auth_post(path: str, body=None, headers=None, ip: str | None = None):
    """인증 경로 POST를 backend에 바로 보낸다. (상태, 본문, 응답 헤더)."""
    return http_full("POST", f"{BACKEND_URL}{path}", body if body is not None else {},
                     {"X-Real-IP": ip or fake_ip(), **(headers or {})})


def login(email: str, password: str) -> str:
    code, body, headers = auth_post("/api/auth/login", {"email": email, "password": password})
    assert code == 200, (code, body)
    cookie = session_cookie(headers)
    assert cookie
    return cookie


class _AdminSession:
    user_id: str | None = None
    cookie: str | None = None


_admin = _AdminSession()


def admin_cookie() -> str:
    """테스트용 admin으로 로그인한 세션 쿠키(세션 하나에 한 번 만든다)."""
    if _admin.cookie is None:
        email, password = random_email("test-admin"), secrets.token_urlsafe(18)
        _admin.user_id = make_user(email, password, role="admin").id
        _admin.cookie = login(email, password)
    return _admin.cookie


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_admin():
    yield
    if _admin.user_id:
        delete_user(_admin.user_id)
        _admin.user_id = _admin.cookie = None


def api(method: str, path: str, body=None, *, auth: bool = True, cookie: str | None = None) -> tuple[int, dict | str]:
    headers = {}
    if cookie or auth:
        headers["Cookie"] = f"{SESSION_COOKIE}={cookie or admin_cookie()}"
    return http(method, f"{WEB_URL}{path}", body, headers)
