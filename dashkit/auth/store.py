"""dashkit_auth 스키마 읽기·쓰기. 접속은 dash_auth 계정(db.auth_conn)뿐이다(contracts/auth.md "DB 계정").

- 사용자 입력이 들어가는 SQL은 전부 매개변수 바인딩이다. 식별자를 문자열로 이어 붙이지 않는다.
- 연결은 작은 풀에 모아 다시 쓴다(backend는 프로세스 하나, 요청은 스레드 풀). 오래 쉰 연결은 꺼내기 전에 확인한다.
- DB에 닿지 못하거나 스키마가 없으면 Unavailable을 던진다(API는 503 auth_unavailable). 로그에는 오류 종류만 남긴다.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import uuid
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


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


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


def delete_session_by_hash(token_hash: bytes) -> dict[str, Any] | None:
    """지운 세션의 {user_id, email}. 없으면 None."""
    with connection() as conn:
        row = conn.execute(
            """DELETE FROM dashkit_auth.sessions s USING dashkit_auth.users u
                WHERE s.token_hash = %s AND u.id = s.user_id RETURNING u.id AS user_id, u.email""",
            (token_hash,),
        ).fetchone()
    return {"user_id": str(row["user_id"]), "email": row["email"]} if row else None


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


# ---------------------------------------------------------------- 그룹과 탭 판정

def user_tab_keys(user_id: str, project: str) -> set[str]:
    """그 사용자가 속한 그룹들이 그 프로젝트에 허용한 탭 키의 합집합."""
    with connection() as conn:
        rows = conn.execute(
            """SELECT DISTINCT gt.tab_key
                 FROM dashkit_auth.group_tabs gt
                 JOIN dashkit_auth.group_members gm ON gm.group_id = gt.group_id
                WHERE gm.user_id = %s AND gt.project = %s""",
            (user_id, project),
        ).fetchall()
    return {r["tab_key"] for r in rows}


_GROUP_COLS = "g.id, g.name, g.description, g.is_default, g.created_at, g.updated_at"


def _group(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["id"] = str(row["id"])
    return row


def list_groups(project: str) -> list[dict[str, Any]]:
    """그룹 목록. member_count와 그 프로젝트의 허용 탭 키(tab_keys)를 함께 돌려준다."""
    with connection() as conn:
        rows = conn.execute(
            f"""SELECT {_GROUP_COLS},
                       (SELECT count(*) FROM dashkit_auth.group_members m WHERE m.group_id = g.id) AS member_count,
                       coalesce((SELECT array_agg(t.tab_key ORDER BY t.tab_key) FROM dashkit_auth.group_tabs t
                                  WHERE t.group_id = g.id AND t.project = %s), '{{}}') AS tab_keys
                  FROM dashkit_auth.groups g ORDER BY g.name""",
            (project,),
        ).fetchall()
    return [_group(r) for r in rows]


def group_by_id(group_id: str) -> dict[str, Any] | None:
    if not is_uuid(group_id):
        return None
    with connection() as conn:
        row = conn.execute(f"SELECT {_GROUP_COLS} FROM dashkit_auth.groups g WHERE g.id = %s", (group_id,)).fetchone()
    return _group(row) if row else None


def create_group(name: str, description: str, is_default: bool) -> dict[str, Any]:
    """Conflict: 같은 이름의 그룹이 있다."""
    with connection() as conn:
        row = conn.execute(
            f"""INSERT INTO dashkit_auth.groups AS g (name, description, is_default) VALUES (%s, %s, %s)
                RETURNING {_GROUP_COLS}""",
            (name, description, is_default),
        ).fetchone()
    return _group(row)


def update_group(group_id: str, *, name: str | None = None, description: str | None = None,
                 is_default: bool | None = None) -> dict[str, Any] | None:
    """바꾸지 않을 값은 None. 없는 그룹이면 None. Conflict: 이름 중복."""
    if not is_uuid(group_id):
        return None
    with connection() as conn:
        row = conn.execute(
            f"""UPDATE dashkit_auth.groups AS g
                   SET name = coalesce(%s, name), description = coalesce(%s, description),
                       is_default = coalesce(%s, is_default), updated_at = now()
                 WHERE id = %s RETURNING {_GROUP_COLS}""",
            (name, description, is_default, group_id),
        ).fetchone()
    return _group(row) if row else None


def touch_group(group_id: str) -> None:
    with connection() as conn:
        conn.execute("UPDATE dashkit_auth.groups SET updated_at = now() WHERE id = %s", (group_id,))


def delete_group(group_id: str) -> dict[str, Any] | None:
    """구성원·허용 탭 연결은 함께 지워진다(FK). 사용자 계정은 남는다."""
    if not is_uuid(group_id):
        return None
    with connection() as conn:
        row = conn.execute(f"DELETE FROM dashkit_auth.groups AS g WHERE id = %s RETURNING {_GROUP_COLS}",
                           (group_id,)).fetchone()
    return _group(row) if row else None


def group_tabs(group_id: str) -> list[tuple[str, str]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT project, tab_key FROM dashkit_auth.group_tabs WHERE group_id = %s ORDER BY project, tab_key",
            (group_id,),
        ).fetchall()
    return [(r["project"], r["tab_key"]) for r in rows]


def set_group_tabs(group_id: str, project: str, tab_keys: list[str],
                   valid_keys: set[str]) -> tuple[list[str], list[str]]:
    """그 프로젝트의 허용 탭 중 **설정에 있는 탭(valid_keys)** 을 tab_keys로 통째로 바꾼다. (더한 키, 뺀 키).

    설정에 없는 탭(없어진 탭, 읽을 수 없는 프로젝트의 탭)의 기존 기록은 그대로 둔다(remove_group_tab으로 하나씩 지운다).
    같은 그룹의 동시 저장은 부모 groups 행을 먼저 잠가 순서를 세운다(기존 탭이 없어도 직렬화된다).
    """
    with connection() as conn, conn.transaction():
        conn.execute("SELECT 1 FROM dashkit_auth.groups WHERE id = %s FOR UPDATE", (group_id,))
        before = {r["tab_key"] for r in conn.execute(
            "SELECT tab_key FROM dashkit_auth.group_tabs WHERE group_id = %s AND project = %s",
            (group_id, project)).fetchall()}
        after = set(tab_keys)
        removed = sorted((before & valid_keys) - after)
        added = sorted(after - before)
        if removed:
            conn.execute("DELETE FROM dashkit_auth.group_tabs WHERE group_id = %s AND project = %s AND tab_key = ANY(%s::text[])",
                         (group_id, project, removed))
        for key in added:
            conn.execute("INSERT INTO dashkit_auth.group_tabs (group_id, project, tab_key) VALUES (%s, %s, %s)",
                         (group_id, project, key))
        if added or removed:
            conn.execute("UPDATE dashkit_auth.groups SET updated_at = now() WHERE id = %s", (group_id,))
    return added, removed


def remove_group_tab(group_id: str, project: str, tab_key: str) -> bool:
    """허용 탭 기록 하나를 지운다(없는 탭 정리). 지웠으면 True."""
    with connection() as conn, conn.transaction():
        conn.execute("SELECT 1 FROM dashkit_auth.groups WHERE id = %s FOR UPDATE", (group_id,))
        n = conn.execute("DELETE FROM dashkit_auth.group_tabs WHERE group_id = %s AND project = %s AND tab_key = %s",
                         (group_id, project, tab_key)).rowcount
        if n:
            conn.execute("UPDATE dashkit_auth.groups SET updated_at = now() WHERE id = %s", (group_id,))
    return n > 0


def group_tab_refs() -> list[dict[str, Any]]:
    """그룹들이 참조하는 (프로젝트, 탭 키)와 그 그룹 수."""
    with connection() as conn:
        rows = conn.execute(
            """SELECT project, tab_key, count(*) AS groups FROM dashkit_auth.group_tabs
                GROUP BY project, tab_key ORDER BY project, tab_key"""
        ).fetchall()
    return [dict(r) for r in rows]


def group_members(group_id: str) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            """SELECT u.id, u.email, u.role, u.status, m.added_at
                 FROM dashkit_auth.group_members m JOIN dashkit_auth.users u ON u.id = m.user_id
                WHERE m.group_id = %s ORDER BY u.email""",
            (group_id,),
        ).fetchall()
    return [{**r, "id": str(r["id"])} for r in rows]


def users_by_refs(emails: list[str], ids: list[str]) -> list[dict[str, Any]]:
    """이메일 또는 id로 찾은 계정(id, email, role). 없는 것은 빠진다."""
    ids = [i for i in ids if is_uuid(i)]
    if not emails and not ids:
        return []
    with connection() as conn:
        rows = conn.execute(
            "SELECT id, email, role FROM dashkit_auth.users WHERE email = ANY(%s::text[]) OR id = ANY(%s::uuid[])",
            (emails, ids),
        ).fetchall()
    return [{**r, "id": str(r["id"])} for r in rows]


def change_group_members(group_id: str, add_ids: list[str], remove_ids: list[str],
                         added_by: str | None) -> tuple[list[str], list[str]]:
    """(실제로 더한 id, 실제로 뺀 id)."""
    added: list[str] = []
    removed: list[str] = []
    with connection() as conn, conn.transaction():
        for uid in remove_ids:
            if conn.execute("DELETE FROM dashkit_auth.group_members WHERE group_id = %s AND user_id = %s",
                            (group_id, uid)).rowcount:
                removed.append(uid)
        for uid in add_ids:
            if conn.execute(
                    """INSERT INTO dashkit_auth.group_members (group_id, user_id, added_by) VALUES (%s, %s, %s)
                       ON CONFLICT DO NOTHING""", (group_id, uid, added_by)).rowcount:
                added.append(uid)
        if added or removed:
            conn.execute("UPDATE dashkit_auth.groups SET updated_at = now() WHERE id = %s", (group_id,))
    return added, removed


# ---------------------------------------------------------------- 감사 로그

def record_audit(action: str, *, actor_id: str | None = None, actor_email: str | None = None,
                 target_type: str | None = None, target_id: str | None = None, target_label: str | None = None,
                 detail: dict[str, Any] | None = None, ip: str | None = None) -> None:
    """감사 로그 한 줄. detail에 비밀번호·토큰·해시를 넣지 않는다(부르는 쪽 책임, 키 이름으로도 한 번 거른다)."""
    clean = {k: v for k, v in (detail or {}).items()
             if not any(bad in k.lower() for bad in ("password", "token", "hash", "secret"))}
    with connection() as conn:
        conn.execute(
            """INSERT INTO dashkit_auth.audit_log
                   (actor_id, actor_email, action, target_type, target_id, target_label, detail, ip)
               VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)""",
            (actor_id, actor_email, action, target_type, target_id, target_label,
             json.dumps(clean, ensure_ascii=False, default=str), clean_ip(ip)),
        )
