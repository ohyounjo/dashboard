"""관리 서비스의 작업(설계서 5-4 "진행률", 11절 "작업"). 계약: contracts/manage.md "작업".

오래 걸리는 일(파일 적재, 되돌리기의 다시 만들기, 구조 변경)은 작업 스레드에서 돌고 202와 작업을 돌려준다.
- 작업 상태·진행률·로그·결과는 이 프로세스의 메모리에 있다(관리 서비스는 프로세스 하나다). 다시 뜨면 없어지고,
  적재가 어디까지 갔는지는 묶음 기록의 복구가 정한다. 끝난 작업의 결과은 감사 로그에 남는다.
- 화면을 떠나도 작업은 끝까지 간다. 최근 MAX_KEEP개만 기억한다.
- 로그와 오류 글의 비밀값은 Studio의 apply와 같은 규칙으로 가린다(jobs.scrub).
"""
from __future__ import annotations

import secrets
import shutil
import threading
import traceback
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .jobs import scrub, secret_values

MAX_KEEP = 100
MAX_LOG = 500


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Run:
    id: str
    kind: str                     # ingest | remove | restore | …
    table: str | None
    actor_id: str | None
    actor_email: str | None
    status: str = "running"       # running | succeeded | failed
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    rejects: Path | None = None   # 빠진 행 파일(있으면)
    token_id: str | None = None   # API 토큰으로 시작한 작업 ID(public()에는 노출하지 않음)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "table": self.table, "status": self.status,
                "started_at": self.started_at, "finished_at": self.finished_at, "actor": self.actor_email,
                "progress": dict(self.progress), "log": self.log[-MAX_LOG:], "result": self.result,
                "error": self.error, "rejects": self.rejects is not None}


class Context:
    """작업 함수가 받는 것: 진행률과 로그."""

    def __init__(self, run: Run, secrets_: list[str]):
        self.run = run
        self._secrets = secrets_

    def progress(self, step: str, **info: Any) -> None:
        self.run.progress = {"step": step, **{k: v for k, v in info.items() if v is not None}}

    def log(self, line: str) -> None:
        self.run.log.append(scrub(str(line), self._secrets))
        if len(self.run.log) > MAX_LOG * 2:
            del self.run.log[:MAX_LOG]


Work = Callable[[Context], dict[str, Any]]


class Runs:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, Run]" = OrderedDict()

    def start(self, kind: str, work: Work, *, table: str | None = None, actor_id: str | None = None,
              actor_email: str | None = None, token_id: str | None = None,
              on_done: Callable[[Run], None] | None = None) -> Run:
        run = Run(secrets.token_hex(8), kind, table, actor_id, actor_email, token_id=token_id)
        with self._lock:
            self._items[run.id] = run
            while len(self._items) > MAX_KEEP:
                old = next(iter(self._items))
                if self._items[old].status == "running":
                    break
                dropped = self._items.pop(old)
                if dropped.rejects is not None:
                    # 빠진 행 파일은 그 작업 전용 임시 폴더에 있다. 작업을 잊을 때 폴더째 지운다
                    shutil.rmtree(dropped.rejects.parent, ignore_errors=True)
        threading.Thread(target=self._run, args=(run, work, on_done), name=f"manage-{kind}-{run.id}",
                         daemon=True).start()
        return run

    def _run(self, run: Run, work: Work, on_done: Callable[[Run], None] | None) -> None:
        secrets_ = secret_values()
        ctx = Context(run, secrets_)
        try:
            run.result = work(ctx)
            run.status = "succeeded"
        except Exception as exc:   # 작업 스레드가 조용히 죽지 않게. 오류 형식은 API 오류와 같다
            status = getattr(exc, "status", 500)
            detail = getattr(exc, "detail", "internal_error")
            message = getattr(exc, "message", None) or "서버 오류"
            errors = getattr(exc, "errors", None)
            extra = getattr(exc, "extra", None) or {}
            run.error = {"status": status, "detail": detail, "message": scrub(str(message), secrets_),
                          **({"errors": [scrub(e, secrets_) for e in errors]} if errors else {}),
                          **{k: v for k, v in extra.items() if k not in ("detail", "message", "errors")}}
            run.status = "failed"
            if status >= 500:
                ctx.log(scrub("".join(traceback.format_exception_only(type(exc), exc)).strip(), secrets_))
        finally:
            run.finished_at = _now()
            if on_done is not None:
                try:
                    on_done(run)
                except Exception:
                    pass

    def get(self, run_id: str) -> Run | None:
        with self._lock:
            return self._items.get(run_id)

    def list(self) -> list[Run]:
        with self._lock:
            return list(reversed(self._items.values()))
