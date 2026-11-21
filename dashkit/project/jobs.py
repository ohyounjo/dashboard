"""apply(check → load → sync)를 작업 스레드에서 실행하고 진행 로그를 흘린다. 계약: contracts/studio.md의 apply.

- 한 번에 하나만 실행한다(프로세스 전체에 하나). 실행 중이면 start()가 None을 돌려준다(→ 409 apply_running).
- 기존 함수를 그대로 부른다: config.load_project, loader.load(owner 계정), sync.sync(log 콜백).
- 요청 스레드는 큐에서 이벤트를 꺼내 SSE로 내보내기만 한다. 브라우저가 끊어도 작업은 끝까지 간다
  (중간에 멈춘 적재·sync를 남기지 않는다). 마지막 결과는 status()로 볼 수 있다.
- 로그와 오류 글에 비밀값(환경변수의 비밀번호·키)이 섞여 있으면 가린다.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Mapping

from .. import config

STEPS = ("check", "load", "sync")
HEARTBEAT = 15.0           # 이 시간(초) 동안 로그가 없으면 SSE 주석 줄을 보내 연결을 유지한다
_SECRET_WORDS = ("PASSWORD", "SECRET")       # 이름에 들어 있으면 비밀값
_SECRET_ENDINGS = ("_TOKEN", "_KEY")         # 이름이 이렇게 끝나면 비밀값
_MIN_SECRET = 6
MASK = "***"


def _secret_name(name: str) -> bool:
    return any(w in name for w in _SECRET_WORDS) or name.endswith(_SECRET_ENDINGS)


def secret_values(env: Mapping[str, str] | None = None) -> list[str]:
    """환경변수 가운데 이름이 비밀값 꼴인 것의 값(긴 것부터)."""
    env = os.environ if env is None else env
    values = {v for k, v in env.items() if _secret_name(k.upper()) and len(v) >= _MIN_SECRET}
    return sorted(values, key=len, reverse=True)


def scrub(text: str, secrets: list[str]) -> str:
    for value in secrets:
        if value in text:
            text = text.replace(value, MASK)
    return text


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Job:
    project: str
    started_at: str = field(default_factory=_now)
    events: "queue.Queue[tuple[str, Any]]" = field(default_factory=queue.Queue)
    lines: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    finished_at: str | None = None


Runner = Callable[[str, Callable[[str], None]], None]


class StepFailed(Exception):
    def __init__(self, step: str, errors: list[str]):
        self.step = step
        self.errors = list(errors)
        super().__init__(step)


def run_apply(name: str, log: Callable[[str], None]) -> None:
    """check → load → sync. 실패하면 StepFailed(단계, 이유들)."""
    from ..loader import LoadError, load
    from ..sync import SyncError, sync

    step = "check"
    try:
        log(f"▶ check: {name} 설정 검사")
        from ..ingest.structure import recover_before_read

        for outcome in recover_before_read(name):       # 끝나지 않은 구조 변경을 설정을 읽기 전에 정리한다
            log(f"• 끊긴 구조 변경 {outcome.id}: {outcome.result}")
        project = config.load_project(name)
        log(f"✔ check: {project.name} 설정이 유효함")

        step = "load"
        log("▶ load: CSV 검증 후 적재")
        counts = load(project)
        detail = ", ".join(f"{t} {n}" for t, n in counts.items())
        log(f"✔ load: {project.name} {sum(counts.values())}행 적재 ({detail})")

        step = "sync"
        log("▶ sync: Superset에 반영")
        result = sync(project, log=log)
        log(f"✔ sync: {project.name} Superset 반영 완료 ({', '.join(f'{k} {len(v)}' for k, v in result.items())})")
    except config.ConfigError as exc:
        raise StepFailed(step, exc.errors) from None
    except LoadError as exc:
        raise StepFailed(step, exc.errors) from None
    except SyncError as exc:
        raise StepFailed(step, str(exc).splitlines() or ["sync 실패"]) from None
    except Exception as exc:      # 예상하지 못한 오류도 어느 단계인지와 함께 알린다
        raise StepFailed(step, [f"{type(exc).__name__}: {exc}"]) from None


class ApplyRunner:
    def __init__(self, runner: Runner = run_apply):
        self._runner = runner
        self._lock = threading.Lock()
        self._job: Job | None = None       # 실행 중인 작업
        self._last: Job | None = None      # 마지막으로 끝난 작업

    def running(self) -> str | None:
        with self._lock:
            return self._job.project if self._job is not None else None

    def start(self, name: str, guard: threading.Lock | None = None) -> Job | None:
        """작업을 시작한다. 이미 실행 중이면 None. guard(프로젝트 잠금)는 작업이 끝날 때까지 잡는다."""
        with self._lock:
            if self._job is not None:
                return None
            if guard is not None and not guard.acquire(timeout=5):      # 저장 중이면 잠깐 기다린다
                return None
            job = Job(name)
            self._job = job
        threading.Thread(target=self._work, args=(job, guard), name=f"studio-apply-{name}", daemon=True).start()
        return job

    def _work(self, job: Job, guard: threading.Lock | None) -> None:
        secrets = secret_values()

        def log(message: str) -> None:
            for line in scrub(str(message), secrets).splitlines() or [""]:
                job.lines.append(line)
                job.events.put(("log", line))

        try:
            self._runner(job.project, log)
            result: dict[str, Any] = {"ok": True, "step": STEPS[-1], "errors": []}
        except StepFailed as exc:
            errors = [scrub(e, secrets) for e in exc.errors]
            log(f"✖ {exc.step} 실패 ({len(errors)}건)")
            for e in errors:
                log(f"  {e}")
            result = {"ok": False, "step": exc.step, "errors": errors}
        except BaseException as exc:   # 작업 스레드가 조용히 죽지 않게
            result = {"ok": False, "step": None, "errors": [scrub(f"{type(exc).__name__}: {exc}", secrets)]}
        finally:
            if guard is not None:
                guard.release()
        job.result = {**result, "project": job.project}
        job.finished_at = _now()
        with self._lock:
            self._last = job
            self._job = None
        job.events.put(("done", job.result))

    def status(self) -> dict[str, Any]:
        with self._lock:
            job, last = self._job, self._last
        out: dict[str, Any] = {"running": job is not None, "project": job.project if job else None,
                               "started_at": job.started_at if job else None, "last": None}
        if last is not None:
            out["last"] = {**(last.result or {}), "started_at": last.started_at, "finished_at": last.finished_at,
                           "log": last.lines[-200:]}
        return out


def sse(event: str, data: str) -> bytes:
    """SSE 이벤트 하나. data가 여러 줄이면 줄마다 `data:`를 붙인다."""
    body = "".join(f"data: {line}\n" for line in data.split("\n"))
    return f"event: {event}\n{body}\n".encode("utf-8")


def stream(job: Job, heartbeat: float = HEARTBEAT) -> Iterator[bytes]:
    """작업의 이벤트를 SSE로. `event: log`의 data는 로그 한 줄(글자 그대로), 마지막 `event: done`의 data는 JSON."""
    deadline = time.monotonic() + 3600
    while True:
        try:
            event, data = job.events.get(timeout=heartbeat)
        except queue.Empty:
            if time.monotonic() > deadline:
                return
            yield b": keep-alive\n\n"
            continue
        if event == "done":
            yield sse("done", json.dumps(data, ensure_ascii=False))
            return
        yield sse("log", data)
