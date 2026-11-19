"""요청 보호. 계약: contracts/studio.md "요청 보호".

Studio는 인증이 없는 로컬 서비스다. 브라우저에 열린 다른 사이트가 사용자를 대신해 요청을 보내는 것(CSRF)과,
다른 이름이 127.0.0.1을 가리키게 해 같은 출처인 척하는 것(DNS rebinding)을 막는다.

- Host 검사: 모든 요청. 허용하는 Host는 STUDIO_ORIGIN의 host:port와, 같은 포트(그리고 컨테이너 안의 포트)의
  127.0.0.1·localhost뿐이다.
- Origin 검사: 상태를 바꾸는 요청(POST, PUT, PATCH, DELETE). Origin이 있으면 허용 출처여야 한다.
  브라우저가 보내는 Sec-Fetch-Site가 cross-site·same-site면 거절한다.
- Content-Type: 상태를 바꾸는 요청의 Content-Type은 application/json 또는 multipart/form-data만 받는다
  (HTML 폼이 사전 요청 없이 보낼 수 있는 urlencoded·text/plain을 막는다). 경로마다 어느 쪽인지는 app.py가 본다.
- 응답 헤더: 모든 응답에 Cache-Control: no-store, X-Content-Type-Options: nosniff, CSP.

ASGI 미들웨어라서 경로 처리 전에 돈다(없는 경로, 정적 파일, 스트림 응답에도 같은 규칙이 적용된다).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import urlsplit

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
LOCAL_HOSTS = ("127.0.0.1", "localhost")
DEFAULT_ORIGIN = "http://localhost:8090"
DEFAULT_WEB_URL = "http://localhost:8080"
LISTEN_PORT = 8090
BODY_TYPES = ("application/json", "multipart/form-data")


def _origin_of(url: str, default: str) -> tuple[str, str, int | None]:
    """URL → (scheme, host, port). 형식이 틀리면 default를 쓴다."""
    for candidate in (url, default):
        try:
            parts = urlsplit(candidate.strip())
            if parts.scheme in ("http", "https") and parts.hostname:
                return parts.scheme, parts.hostname.lower(), parts.port
        except ValueError:
            continue
    raise ValueError(f"출처 형식 오류: {url!r}")


def _netloc(host: str, port: int | None) -> str:
    return host if port is None else f"{host}:{port}"


@dataclass(frozen=True)
class Policy:
    """허용하는 Host·Origin과 응답 헤더."""
    hosts: frozenset[str]
    origins: frozenset[str]
    csp: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Policy":
        env = os.environ if env is None else env
        scheme, host, port = _origin_of(env.get("STUDIO_ORIGIN") or DEFAULT_ORIGIN, DEFAULT_ORIGIN)
        try:
            listen = int(env.get("STUDIO_LISTEN_PORT") or LISTEN_PORT)
        except ValueError:
            listen = LISTEN_PORT
        default_port = 443 if scheme == "https" else 80
        names = {host, *LOCAL_HOSTS}
        # 브라우저가 여는 포트. 기본 포트는 Host에서 생략되기도 한다
        public = {_netloc(n, port) for n in names} if port is not None else {n for n in names} | {
            _netloc(n, default_port) for n in names}
        if port == default_port:
            public |= names
        # 컨테이너 안에서 듣는 포트(compose healthcheck가 localhost:8090으로 부른다). 이름은 로컬 이름만 허용한다
        internal = {_netloc(n, listen) for n in LOCAL_HOSTS}
        origins = {f"{scheme}://{h}" for h in public}
        web_scheme, web_host, web_port = _origin_of(env.get("WEB_PUBLIC_URL") or DEFAULT_WEB_URL, DEFAULT_WEB_URL)
        web = f"{web_scheme}://{_netloc(web_host, web_port)}"
        csp = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
               f"connect-src 'self'; frame-src {web}; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        return cls(frozenset(public | internal), frozenset(origins), csp)

    def headers(self) -> list[tuple[bytes, bytes]]:
        return [
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
            (b"content-security-policy", self.csp.encode("latin-1")),
            (b"referrer-policy", b"no-referrer"),
        ]


Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]


class Guard:
    """ASGI 미들웨어. policy를 주지 않으면 요청 때 환경변수에서 읽는다(프로세스당 한 번)."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]], policy: Policy | None = None):
        self.app = app
        self._policy = policy

    @property
    def policy(self) -> Policy:
        if self._policy is None:
            self._policy = Policy.from_env()
        return self._policy

    def check(self, method: str, headers: Mapping[str, str]) -> tuple[int, str, str] | None:
        """거절할 요청이면 (상태, 코드, 설명)."""
        policy = self.policy
        host = headers.get("host", "").strip().lower()
        if host not in policy.hosts:
            return 403, "forbidden_host", "허용하지 않는 Host입니다(Studio는 STUDIO_ORIGIN 주소로만 엽니다)"
        if method not in UNSAFE_METHODS:
            return None
        origin = headers.get("origin")
        if origin is not None and origin.strip().lower().rstrip("/") not in policy.origins:
            return 403, "forbidden_origin", "다른 출처에서 온 요청입니다"
        if headers.get("sec-fetch-site", "").strip().lower() in ("cross-site", "same-site"):
            return 403, "forbidden_origin", "다른 사이트에서 온 요청입니다"
        ctype = headers.get("content-type")
        if ctype is not None and ctype.split(";", 1)[0].strip().lower() not in BODY_TYPES:
            return 415, "unsupported_media_type", "Content-Type은 application/json 또는 multipart/form-data여야 합니다"
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = self.policy.headers()
        names = {k for k, _v in extra}
        headers: dict[str, str] = {}
        duplicated = False
        for k, v in scope.get("headers") or []:
            name = k.decode("latin-1").lower()
            if name in ("host", "origin") and name in headers:
                duplicated = True
            headers[name] = v.decode("latin-1")
        rejected = (400, "bad_request", "Host 또는 Origin 헤더가 여러 개입니다") if duplicated else \
            self.check(scope.get("method", "GET").upper(), headers)
        if rejected is not None:
            status, detail, message = rejected
            body = json.dumps({"detail": detail, "message": message}, ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": status, "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()), *extra]})
            await send({"type": "http.response.body", "body": body})
            return

        async def send_with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                kept = [(k, v) for k, v in message.get("headers") or [] if k.lower() not in names]
                message = {**message, "headers": kept + extra}
            await send(message)

        await self.app(scope, receive, send_with_headers)
