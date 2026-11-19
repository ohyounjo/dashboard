"""Studio 테스트 도우미: ASGI 앱을 직접 호출한다(httpx가 없어 FastAPI TestClient를 쓸 수 없다).

요청 하나를 scope/receive/send로 만들어 `dashkit.studio.app.app`(요청 보호를 씌운 앱)에 넣고 응답을 모은다.
네트워크와 포트를 쓰지 않는다. 스트림 응답(SSE)은 끝까지 읽어 한 덩어리로 돌려준다.
"""
from __future__ import annotations

import asyncio
import json as jsonlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ("devtracker", "sales-sample", "chart-gallery")
ORIGIN = "http://localhost:8090"
BOUNDARY = "----studiotestboundary7MA4YWxk"


@dataclass
class Result:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> Any:
        return jsonlib.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def events(self) -> list[tuple[str, str]]:
        """SSE 본문 → [(event, data)]. 주석 줄(`: …`)은 뺀다."""
        out = []
        for block in self.text.split("\n\n"):
            event, data = "message", []
            for line in block.split("\n"):
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].removeprefix(" "))
            if data:
                out.append((event, "\n".join(data)))
        return out


def multipart(files: Iterable[tuple[str, str, bytes]], boundary: str = BOUNDARY) -> tuple[str, bytes]:
    """[(필드 이름, 파일 이름, 내용)] → (Content-Type, 본문). 브라우저처럼 파일 이름을 UTF-8 그대로 싣는다."""
    out = b""
    for field, filename, content in files:
        shown = filename.replace('"', "%22")
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{shown}\"\r\n"
                "Content-Type: text/csv\r\n\r\n").encode("utf-8") + content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", out


class Client:
    """기본으로 브라우저가 Studio 화면에서 보내는 것과 같은 Host를 붙인다."""

    def __init__(self, app: Any = None, host: str = "localhost:8090"):
        if app is None:
            from dashkit.studio.app import app as studio_app
            app = studio_app
        self.app = app
        self.host = host

    def request(self, method: str, path: str, *, json: Any = None, files: Iterable[tuple[str, str, bytes]] | None = None,
                body: bytes | None = None, headers: dict[str, str] | None = None, raw_path: bool = False) -> Result:
        sent: dict[str, str] = {"host": self.host}
        data = body or b""
        if json is not None:
            data = jsonlib.dumps(json).encode("utf-8")
            sent["content-type"] = "application/json"
        elif files is not None:
            sent["content-type"], data = multipart(files)
        for k, v in (headers or {}).items():
            if v is None:
                sent.pop(k.lower(), None)
            else:
                sent[k.lower()] = v
        if data or method in ("POST", "PUT", "PATCH"):
            sent.setdefault("content-length", str(len(data)))
        parts = urlsplit(path)
        target = parts.path if raw_path else quote(parts.path, safe="/%")
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
            "path": unquote(target), "raw_path": target.encode("ascii"), "query_string": parts.query.encode("ascii"),
            "root_path": "", "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in sent.items()],
            "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8090),
        }
        return asyncio.run(self._call(scope, data))

    async def _call(self, scope: dict, data: bytes) -> Result:
        messages: list[dict] = []
        sent_body = False

        async def receive() -> dict:
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {"type": "http.request", "body": data, "more_body": False}
            await asyncio.sleep(3600)       # 응답이 끝날 때까지 끊지 않는다
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            messages.append(message)

        try:
            await self.app(scope, receive, send)
        except Exception:
            # 처리하지 못한 오류: Starlette는 500 응답을 보낸 뒤 예외를 다시 던진다(서버라면 로그에 남긴다)
            if not any(m["type"] == "http.response.start" for m in messages):
                raise
        start = next(m for m in messages if m["type"] == "http.response.start")
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in start.get("headers") or []}
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        return Result(start["status"], headers, body)

    def get(self, path: str, **kw: Any) -> Result:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Result:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> Result:
        return self.request("PUT", path, **kw)

    def patch(self, path: str, **kw: Any) -> Result:
        return self.request("PATCH", path, **kw)


def copy_examples(dst: Path, names: Iterable[str] = EXAMPLES) -> Path:
    """예시 프로젝트를 임시 폴더로 복사한다(저장소의 projects/는 읽기만 한다)."""
    for name in names:
        shutil.copytree(ROOT / "projects" / name, dst / name)
    return dst


def fresh_client(tmp_path: Path, monkeypatch: Any, *, origin: str = ORIGIN, projects: Iterable[str] = EXAMPLES,
                 host: str | None = None) -> Client:
    """임시 projects/·state 폴더를 가리키는 Studio 클라이언트. DB 환경변수는 지운다(스택 없이 돈다)."""
    from dashkit.studio import app as studio_app
    from dashkit.studio import guard, jobs, store

    root = tmp_path / "projects"
    root.mkdir(exist_ok=True)
    (tmp_path / "state").mkdir(exist_ok=True)
    copy_examples(root, projects)
    monkeypatch.setenv("PROJECTS_DIR", str(root))
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BUILD_DIR", str(tmp_path / "build"))
    monkeypatch.setenv("STUDIO_ORIGIN", origin)
    monkeypatch.setenv("WEB_PUBLIC_URL", "http://localhost:8080")
    for name in ("DATA_DB_READER_PASSWORD", "DATA_DB_OWNER_PASSWORD", "SUPERSET_ADMIN_PASSWORD",
                 "STUDIO_MAX_UPLOAD_MB", "STUDIO_MAX_REQUEST_MB", "STUDIO_WEB_DIR", "STUDIO_LISTEN_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(store, "changes", store.Changes())
    monkeypatch.setattr(store, "uploads", store.Uploads())
    monkeypatch.setattr(store, "_locks", {})
    monkeypatch.setattr(studio_app, "_listing_cache", {})
    monkeypatch.setattr(studio_app, "runner", jobs.ApplyRunner())
    client = Client(guard.Guard(studio_app.api), host or urlsplit(origin).netloc)
    client.projects = root              # type: ignore[attr-defined]
    client.state = tmp_path / "state"   # type: ignore[attr-defined]
    return client
