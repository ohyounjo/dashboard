"""Superset 6.1.0 REST 클라이언트(관리자 계정). sync와 통합 테스트가 함께 쓴다.

인증: POST /api/v1/security/login(provider db) → Bearer, GET /api/v1/security/csrf_token/ → X-CSRFToken.
세션 쿠키는 requests.Session이 유지하고, Referer는 base_url로 둔다.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Iterable

import requests

from .errors import SyncError

KIND_PATHS = {"database": "database", "dataset": "dataset", "chart": "chart", "dashboard": "dashboard"}
LOG_HINT = "Superset 로그를 확인하세요(make logs)"


def _snippet(resp: requests.Response, limit: int = 500) -> str:
    text = (resp.text or "").strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _rison_str(value: str) -> str:
    return "'" + value.replace("!", "!!").replace("'", "!'") + "'"


class SupersetClient:
    def __init__(self, base_url: str | None = None, username: str | None = None,
                 password: str | None = None, *, timeout: float = 60.0):
        self.base_url = (base_url or os.environ.get("SUPERSET_URL") or "http://superset:8088").rstrip("/")
        self.username = username or os.environ.get("SUPERSET_ADMIN_USER") or "admin"
        self.password = password if password is not None else os.environ.get("SUPERSET_ADMIN_PASSWORD", "")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Referer"] = self.base_url + "/"

    # ------------------------------------------------------------ 기본 요청

    def url(self, path: str) -> str:
        return path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        try:
            return self.session.request(method, self.url(path), **kwargs)
        except requests.RequestException as exc:
            raise SyncError(f"Superset 요청 실패: {method} {path}: {exc}") from exc

    def get(self, path: str, **kwargs) -> requests.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> requests.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> requests.Response:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs) -> requests.Response:
        return self.request("DELETE", path, **kwargs)

    @staticmethod
    def expect(resp: requests.Response, what: str, ok: Iterable[int] = (200, 201)) -> Any:
        """상태 코드가 ok가 아니면 SyncError. JSON 본문을 돌려준다(아니면 None)."""
        if resp.status_code not in tuple(ok):
            raise SyncError(f"{what} 실패: HTTP {resp.status_code}: {_snippet(resp)} — {LOG_HINT}")
        try:
            return resp.json()
        except ValueError:
            return None

    # ------------------------------------------------------------ 준비·로그인

    def wait_ready(self, timeout: float = 120.0, interval: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        last = ""
        while True:
            try:
                resp = self.session.get(self.url("/health"), timeout=5)
                if resp.status_code == 200:
                    return
                last = f"HTTP {resp.status_code}"
            except requests.RequestException as exc:
                last = str(exc)
            if time.monotonic() >= deadline:
                raise SyncError(f"Superset이 {int(timeout)}초 안에 준비되지 않음({self.base_url}/health: {last})")
            time.sleep(interval)

    def login(self) -> None:
        resp = self.post("/api/v1/security/login", json={
            "username": self.username, "password": self.password, "provider": "db", "refresh": False,
        })
        if resp.status_code != 200:
            raise SyncError(f"Superset 관리자 로그인 실패({self.username}): HTTP {resp.status_code}: {_snippet(resp, 200)}")
        token = resp.json().get("access_token")
        if not token:
            raise SyncError("Superset 로그인 응답에 access_token이 없음")
        self.session.headers["Authorization"] = f"Bearer {token}"
        body = self.expect(self.get("/api/v1/security/csrf_token/"), "CSRF 토큰 조회")
        csrf = (body or {}).get("result")
        if not csrf:
            raise SyncError("CSRF 토큰 응답에 result가 없음")
        self.session.headers["X-CSRFToken"] = csrf

    # ------------------------------------------------------------ 객체

    def find_id(self, kind: str, uuid: str) -> int | None:
        """uuid로 객체 id를 찾는다. 없으면 None."""
        q = f"(filters:!((col:uuid,opr:eq,value:{_rison_str(uuid)})),columns:!(id,uuid),page:0,page_size:100)"
        body = self.expect(self.get(f"/api/v1/{KIND_PATHS[kind]}/", params={"q": q}), f"{kind} {uuid} 조회", ok=(200,))
        rows = (body or {}).get("result") or []
        # 필터가 무시되는 경우에 엉뚱한 객체를 지우지 않도록 uuid를 다시 확인한다
        matches = [r for r in rows if str(r.get("uuid")) == uuid]
        if rows and not matches and any("uuid" not in r for r in rows):
            raise SyncError(f"{kind} 목록 응답에 uuid 열이 없어 객체를 확인할 수 없음")
        return int(matches[0]["id"]) if matches else None

    def list_objects(self, kind: str, columns: Iterable[str], filters: str = "", page_size: int = 100) -> list[dict]:
        """목록 API를 끝까지 넘겨 모든 행을 모은다. filters는 rison 필터 항목(예: "(col:id,opr:eq,value:1)").

        요청한 열이 응답에 없을 수 있다(종류마다 목록에 나오는 열이 다르다). 호출하는 쪽이 확인한다.
        """
        cols = ",".join(columns)
        rows: dict[Any, dict] = {}
        page = 0
        while True:
            # 정렬은 종류별 기본 순서를 쓴다(id 정렬은 허용되지 않는다). 페이지 사이에 순서가 흔들려도 id로 한 번만 센다
            q = f"(filters:!({filters}),columns:!({cols}),page:{page},page_size:{page_size})"
            body = self.expect(self.get(f"/api/v1/{KIND_PATHS[kind]}/", params={"q": q}), f"{kind} 목록 조회", ok=(200,))
            batch = (body or {}).get("result") or []
            for row in batch:
                rows.setdefault(row.get("id", len(rows)), row)
            page += 1
            if len(batch) < page_size or page * page_size >= int((body or {}).get("count") or 0):
                return list(rows.values())

    def get_object(self, kind: str, obj_id: int) -> dict | None:
        """객체 하나. 없으면 None."""
        resp = self.get(f"/api/v1/{KIND_PATHS[kind]}/{obj_id}")
        if resp.status_code == 404:
            return None
        return (self.expect(resp, f"{kind} {obj_id} 조회", ok=(200,)) or {}).get("result") or {}

    def delete_object(self, kind: str, obj_id: int) -> bool:
        """객체를 지운다. 이미 없으면(404) False."""
        resp = self.delete(f"/api/v1/{KIND_PATHS[kind]}/{obj_id}")
        if resp.status_code == 404:
            return False
        self.expect(resp, f"{kind} {obj_id} 삭제", ok=(200, 204))
        return True

    def import_bundle(self, zip_bytes: bytes, passwords: dict[str, str]) -> None:
        resp = self.post(
            "/api/v1/assets/import/",
            files={"bundle": ("bundle.zip", zip_bytes, "application/zip")},
            data={"passwords": json.dumps(passwords)},
            timeout=max(self.timeout, 300),
        )
        if resp.status_code != 200:
            raise SyncError(f"asset 번들 가져오기 실패: HTTP {resp.status_code}: {_snippet(resp)} — {LOG_HINT}")

    def get_embedded(self, dashboard_id: int) -> dict | None:
        resp = self.get(f"/api/v1/dashboard/{dashboard_id}/embedded")
        if resp.status_code == 404:
            return None
        return (self.expect(resp, f"대시보드 {dashboard_id} 임베드 조회", ok=(200,)) or {}).get("result")

    def set_embedded(self, dashboard_id: int, allowed_domains: list[str]) -> str:
        """임베드를 켠다(upsert, 기존 임베드 UUID 유지). 임베드 UUID를 돌려준다."""
        body = self.expect(
            self.post(f"/api/v1/dashboard/{dashboard_id}/embedded", json={"allowed_domains": list(allowed_domains)}),
            f"대시보드 {dashboard_id} 임베드 설정",
        )
        embed_uuid = ((body or {}).get("result") or {}).get("uuid")
        if not embed_uuid:
            raise SyncError(f"대시보드 {dashboard_id} 임베드 응답에 uuid가 없음")
        return str(embed_uuid)
