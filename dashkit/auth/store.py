"""dashkit_auth 스키마 읽기·쓰기. 접속은 dash_auth 계정(db.auth_conn)뿐이다(contracts/auth.md "DB 계정").

- 사용자 입력이 들어가는 SQL은 전부 매개변수 바인딩이다. 식별자를 문자열로 이어 붙이지 않는다.
- 연결은 작은 풀에 모아 다시 쓴다(backend는 프로세스 하나, 요청은 스레드 풀). 오래 쉰 연결은 꺼내기 전에 확인한다.
- DB에 닿지 못하거나 스키마가 없으면 Unavailable을 던진다(API는 503 auth_unavailable). 로그에는 오류 종류만 남긴다.
"""
from __future__ import annotations

import ipaddress
import logging
import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from .. import db

log = logging.getLogger("dashkit.auth")

_POOL_SIZE = 8
_CHECK_AFTER = 30.0          # 이보다 오래 쉰 연결은 꺼낼 때 SELECT 1로 확인한다(초)
_STATEMENT_TIMEOUT_MS = 10000


class Unavailable(Exception):
    """인증 DB를 쓸 수 없다(접속 실패, 스키마 없음 등). 메시지는 사람이 읽는 짧은 설명이다(비밀값 없음)."""


class Conflict(Exception):
    """유일 제약 위반(이미 있는 이메일·그룹 이름 등)."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def clean_ip(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return str(ipaddress.ip_address(raw.strip()))
    except ValueError:
        return None


# ---------------------------------------------------------------- 연결 풀

class _Pool:
    def __init__(self, size: int = _POOL_SIZE):
        self._idle: queue.LifoQueue[tuple[psycopg.Connection, float]] = queue.LifoQueue()
        self._size = size
        self._lock = threading.Lock()
        self._generation = 0

    def _connect(self) -> psycopg.Connection:
        return db.auth_conn(
            autocommit=True, row_factory=dict_row, application_name="dashkit-auth",
            options=f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}",
        )

    def _take(self) -> psycopg.Connection | None:
        while True:
            try:
                conn, last = self._idle.get_nowait()
            except queue.Empty:
                return None
            if conn.closed or conn.broken:
                continue
            if time.monotonic() - last > _CHECK_AFTER:
                try:
                    conn.execute("SELECT 1")
                except psycopg.Error:
                    conn.close()
                    continue
            return conn

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        conn = self._take()
        if conn is None:
            try:
                conn = self._connect()
            except (psycopg.Error, RuntimeError) as exc:
                log.warning("인증 DB에 접속하지 못함(%s)", type(exc).__name__)
                raise Unavailable("인증 DB에 접속할 수 없습니다") from None
        try:
            yield conn
        except psycopg.errors.UniqueViolation:
            raise Conflict() from None
        except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName):
            log.error("dashkit_auth 스키마가 없음. make auth-init을 실행하세요")
            raise Unavailable("인증 스키마가 없습니다(make auth-init)") from None
        except psycopg.Error as exc:
            log.warning("인증 DB 오류(%s)", type(exc).__name__)
            raise Unavailable("인증 DB 오류") from None
        finally:
            reusable = (not conn.closed and not conn.broken
                        and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE)
            if reusable and self._idle.qsize() < self._size:
                self._idle.put((conn, time.monotonic()))
            else:
                conn.close()

    def clear(self) -> None:
        while True:
            try:
                conn, _ = self._idle.get_nowait()
            except queue.Empty:
                return
            conn.close()


_pool = _Pool()


def connection():
    return _pool.connection()


def reset_pool() -> None:
    _pool.clear()


# ---------------------------------------------------------------- 행

@dataclass(frozen=True)
class User:
    id: str
    email: str
    password_hash: str | None
    role: str
    status: str
    must_change_password: bool
    source: str
    created_at: datetime
    created_by: str | None
    email_verified_at: datetime | None
    password_changed_at: datetime | None
    last_login_at: datetime | None
    login_count: int
    failed_count: int
    locked_until: datetime | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "User":
        return cls(**{k: (str(row[k]) if k in ("id", "created_by") and row[k] is not None else row[k])
                      for k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class SessionInfo:
    session_id: str
    user_id: str
    email: str
    role: str
    status: str
    must_change_password: bool
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime


_USER_COLS = ("id, email, password_hash, role, status, must_change_password, source, created_at, created_by, "
              "email_verified_at, password_changed_at, last_login_at, login_count, failed_count, locked_until")


# ---------------------------------------------------------------- 사용자

def user_by_email(email: str) -> User | None:
    with connection() as conn:
        row = conn.execute(f"SELECT {_USER_COLS} FROM dashkit_auth.users WHERE email = %s", (email,)).fetchone()
    return User.from_row(row) if row else None


def user_by_id(user_id: str) -> User | None:
    with connection() as conn:
        row = conn.execute(f"SELECT {_USER_COLS} FROM dashkit_auth.users WHERE id = %s", (user_id,)).fetchone()
    return User.from_row(row) if row else None


def create_user(email: str, *, role: str, status: str, source: str, password_hash: str | None = None,
                created_by: str | None = None, email_verified: bool = False) -> User:
    """Conflict: 같은 이메일이 이미 있다."""
    with connection() as conn:
        row = conn.execute(
            f"""INSERT INTO dashkit_auth.users
                   (email, password_hash, role, status, source, created_by, email_verified_at, password_changed_at)
                VALUES (%s, %s, %s, %s, %s, %s,
                        CASE WHEN %s::boolean THEN now() END,
                        CASE WHEN %s::text IS NOT NULL THEN now() END)
                RETURNING {_USER_COLS}""",
            (email, password_hash, role, status, source, created_by, email_verified, password_hash),
        ).fetchone()
    return User.from_row(row)


def delete_user(user_id: str) -> bool:
    with connection() as conn:
        cur = conn.execute("DELETE FROM dashkit_auth.users WHERE id = %s", (user_id,))
        return cur.rowcount > 0


def record_login_success(user_id: str, new_hash: str | None = None) -> None:
    """로그인 성공: 접속 횟수·최근 접속일, 실패 횟수와 잠금 초기화. new_hash가 있으면 다시 해시한 값으로 바꾼다."""
    with connection() as conn:
        conn.execute(
            """UPDATE dashkit_auth.users
                  SET login_count = login_count + 1, last_login_at = now(), failed_count = 0, locked_until = NULL,
                      password_hash = coalesce(%s, password_hash)
                WHERE id = %s""",
            (new_hash, user_id),
        )


# ---------------------------------------------------------------- 세션

def create_session(user_id: str, token_hash: bytes, expires_at: datetime,
                   ip: str | None, user_agent: str | None) -> str:
    with connection() as conn:
        row = conn.execute(
            """INSERT INTO dashkit_auth.sessions (user_id, token_hash, expires_at, ip, user_agent)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (user_id, token_hash, expires_at, clean_ip(ip), (user_agent or "")[:400] or None),
        ).fetchone()
    return str(row["id"])


def session_by_hash(token_hash: bytes) -> SessionInfo | None:
    with connection() as conn:
        row = conn.execute(
            """SELECT s.id AS session_id, u.id AS user_id, u.email, u.role, u.status, u.must_change_password,
                      s.created_at, s.last_seen_at, s.expires_at
                 FROM dashkit_auth.sessions s JOIN dashkit_auth.users u ON u.id = s.user_id
                WHERE s.token_hash = %s""",
            (token_hash,),
        ).fetchone()
    if not row:
        return None
    row["session_id"] = str(row["session_id"])
    row["user_id"] = str(row["user_id"])
    return SessionInfo(**row)


def touch_session(session_id: str) -> None:
    with connection() as conn:
        conn.execute("UPDATE dashkit_auth.sessions SET last_seen_at = now() WHERE id = %s", (session_id,))


def delete_session(session_id: str) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM dashkit_auth.sessions WHERE id = %s", (session_id,))


def delete_session_by_hash(token_hash: bytes) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM dashkit_auth.sessions WHERE token_hash = %s", (token_hash,))


def delete_user_sessions(user_id: str, except_session: str | None = None) -> int:
    with connection() as conn:
        cur = conn.execute(
            "DELETE FROM dashkit_auth.sessions WHERE user_id = %s AND (%s::uuid IS NULL OR id <> %s::uuid)",
            (user_id, except_session, except_session),
        )
        return cur.rowcount


# ---------------------------------------------------------------- 스키마

def schema_version() -> int:
    with connection() as conn:
        row = conn.execute("SELECT coalesce(max(version), 0) AS v FROM dashkit_auth.schema_version").fetchone()
    return int(row["v"])
