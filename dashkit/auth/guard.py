"""backend 요청 보호(contracts/auth.md "요청 보호"). Studio의 guard.py와 같은 방식의 ASGI 미들웨어다.

상태를 바꾸는 요청(POST, PUT, PATCH, DELETE)에만 적용한다. 세션 쿠키(SameSite=Lax)와 함께 CSRF를 막는다.

- Content-Type: application/json만 받는다(HTML 폼이 사전 요청 없이 보낼 수 있는 형식을 막는다). 아니면 415 unsupported_media_type
- Origin: 있으면 WEB_ORIGINS(와 WEB_PUBLIC_URL의 출처) 중 하나여야 한다. 아니면 403 forbidden_origin
- Sec-Fetch-Site: cross-site면 403 forbidden_origin

backend 앞에는 nginx가 있고(Host·X-Real-IP를 넣는다) backend 포트는 밖에 열려 있지 않으므로 Host는 보지 않는다.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Mapping

from . import settings as auth_settings

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]


def check(method: str, headers: Mapping[str, str], origins: frozenset[str]) -> tuple[int, str, str] | None:
    """거절할 요청이면 (상태, 코드, 설명)."""
    if method not in UNSAFE_METHODS:
        return None
    origin = headers.get("origin")
    if origin is not None and origin.strip().lower().rstrip("/") not in origins:
        return 403, "forbidden_origin", "허용하지 않는 출처에서 온 요청입니다"
    if headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        return 403, "forbidden_origin", "다른 사이트에서 온 요청입니다"
    ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if ctype != "application/json":
        return 415, "unsupported_media_type", "Content-Type은 application/json이어야 합니다"
    return None


class Guard:
    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]],
                 origins: frozenset[str] | None = None):
        self.app = app
        self._origins = origins

    @property
    def origins(self) -> frozenset[str]:
        if self._origins is None:
            self._origins = auth_settings.get().web_origins
        return self._origins

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET").upper() not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers: dict[str, str] = {}
        duplicated = False
        for k, v in scope.get("headers") or []:
            name = k.decode("latin-1").lower()
            if name in ("origin", "content-type") and name in headers:
                duplicated = True
            headers[name] = v.decode("latin-1")
        rejected = (400, "bad_request", "Origin 또는 Content-Type 헤더가 여러 개입니다") if duplicated else \
            check(scope["method"].upper(), headers, self.origins)
        if rejected is None:
            await self.app(scope, receive, send)
            return
        status, detail, message = rejected
        body = json.dumps({"detail": detail, "message": message}, ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
        ]})
        await send({"type": "http.response.body", "body": body})
