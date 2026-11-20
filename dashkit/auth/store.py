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


class LastAdmin(Exception):
    """이 변경으로 활성 admin이 없어진다(마지막 활성 admin)."""


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


def _guard_last_admin(conn: psycopg.Connection, user_id: str) -> None:
    """(트랜잭션 안) 활성 admin 행을 모두 잠그고, user_id가 그중 마지막 하나면 LastAdmin.
    두 admin이 동시에 서로를 정지·강등·삭제해도 한쪽은 이 잠금을 기다린 뒤 줄어든 수를 보고 거절된다."""
    rows = conn.execute(
        "SELECT id FROM dashkit_auth.users WHERE role = 'admin' AND status = 'active' ORDER BY id FOR UPDATE"
    ).fetchall()
    if [str(r["id"]) for r in rows] == [user_id]:
        raise LastAdmin()


def delete_user(user_id: str, *, guard_admin: bool = False) -> bool:
    """guard_admin이면 같은 트랜잭션에서 마지막 활성 admin인지 다시 본다(LastAdmin)."""
    with connection() as conn, conn.transaction():
        if guard_admin:
            _guard_last_admin(conn, user_id)
        cur = conn.execute("DELETE FROM dashkit_auth.users WHERE id = %s", (user_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------- 세션

def start_session(user_id: str, verified_hash: str, token_hash: bytes, expires_at: datetime,
                  ip: str | None, user_agent: str | None, new_hash: str | None = None) -> bool:
    """로그인 성공 처리를 한 트랜잭션으로: 사용자 행을 잠그고, 검증에 쓴 해시가 **지금도** 그 계정의 해시이고 계정이
    active이며 잠겨 있지 않을 때만 세션을 만들고 접속 횟수·최근 접속일을 남긴다(new_hash면 다시 해시한 값으로).

    비밀번호 재설정·변경·임시 비밀번호(use_reset_token, set_password)도 같은 행 잠금을 잡으므로, 옛 비밀번호로 시작한
    로그인이 그 뒤에 세션을 만들 수 없다. 조건이 맞지 않으면 아무것도 바꾸지 않고 False.
    """
    done = False
    with connection() as conn, conn.transaction() as tx:
        row = conn.execute(
            "SELECT password_hash, status, locked_until FROM dashkit_auth.users WHERE id = %s FOR UPDATE", (user_id,)
        ).fetchone()
        if (not row or row["password_hash"] != verified_hash or row["status"] != "active"
                or (row["locked_until"] is not None and row["locked_until"] > now())):
            raise psycopg.Rollback(tx)
        conn.execute(
            """INSERT INTO dashkit_auth.sessions (user_id, token_hash, expires_at, ip, user_agent)
               VALUES (%s, %s, %s, %s, %s)""",
            (user_id, token_hash, expires_at, clean_ip(ip), (user_agent or "")[:400] or None),
        )
        conn.execute(
            """UPDATE dashkit_auth.users
                  SET login_count = login_count + 1, last_login_at = now(), failed_count = 0, locked_until = NULL,
                      password_hash = coalesce(%s, password_hash)
                WHERE id = %s""",
            (new_hash, user_id),
        )
        done = True
    return done


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


# ---------------------------------------------------------------- 계정 상태·비밀번호·잠금

def create_pending_user(email: str, password_hash: str, token_hash: bytes, expires_at: datetime) -> User:
    """스스로 가입: pending 계정과 첫 인증 토큰을 한 트랜잭션으로 만든다(그 사이에 다시 가입이 끼어들 틈이 없다).
    Conflict: 같은 이메일이 있다."""
    with connection() as conn, conn.transaction():
        row = conn.execute(
            f"""INSERT INTO dashkit_auth.users (email, password_hash, role, status, source, password_changed_at)
                VALUES (%s, %s, 'user', 'pending', 'signup', now()) RETURNING {_USER_COLS}""",
            (email, password_hash),
        ).fetchone()
        conn.execute("""INSERT INTO dashkit_auth.tokens (user_id, purpose, token_hash, expires_at)
                        VALUES (%s, 'verify_email', %s, %s)""", (row["id"], token_hash, expires_at))
    return User.from_row(row)


def resign_pending(user_id: str, password_hash: str, token_hash: bytes, expires_at: datetime) -> bool:
    """인증 전(pending) 계정에 다시 가입: 한 트랜잭션에서 사용자 행을 잠그고, 아직 pending일 때만 비밀번호를 바꾸고
    이전 인증 토큰을 지우고 새 토큰을 넣는다. 그 사이 인증돼 pending이 아니면 아무것도 바꾸지 않고 False.
    메일 인증(use_verify_token)도 같은 행 잠금을 먼저 잡으므로 둘은 겹치지 않는다."""
    done = False
    with connection() as conn, conn.transaction() as tx:
        row = conn.execute("SELECT status FROM dashkit_auth.users WHERE id = %s FOR UPDATE", (user_id,)).fetchone()
        if not row or row["status"] != "pending":
            raise psycopg.Rollback(tx)
        conn.execute("UPDATE dashkit_auth.users SET password_hash = %s WHERE id = %s", (password_hash, user_id))
        conn.execute("DELETE FROM dashkit_auth.tokens WHERE user_id = %s AND purpose = 'verify_email'", (user_id,))
        conn.execute("""INSERT INTO dashkit_auth.tokens (user_id, purpose, token_hash, expires_at)
                        VALUES (%s, 'verify_email', %s, %s)""", (user_id, token_hash, expires_at))
        done = True
    return done


def _activate(conn: psycopg.Connection, user_id: str, from_status: str) -> bool:
    """(트랜잭션 안) from_status → active, 메일 인증 시각, 기본 그룹(is_default)."""
    cur = conn.execute(
        """UPDATE dashkit_auth.users SET status = 'active', email_verified_at = coalesce(email_verified_at, now())
            WHERE id = %s AND status = %s""",
        (user_id, from_status),
    )
    if not cur.rowcount:
        return False
    conn.execute(
        """INSERT INTO dashkit_auth.group_members (group_id, user_id)
           SELECT id, %s FROM dashkit_auth.groups WHERE is_default ON CONFLICT DO NOTHING""",
        (user_id,),
    )
    return True


_SET_PASSWORD = """UPDATE dashkit_auth.users
                     SET password_hash = %s, password_changed_at = now(), must_change_password = %s,
                         failed_count = 0, locked_until = NULL
                   WHERE id = %s"""


def set_password(user_id: str, password_hash: str, *, must_change: bool = False,
                 end_sessions: bool = False, keep_session: str | None = None) -> None:
    """새 비밀번호. 비밀번호 변경일을 남기고 실패 횟수·잠금을 지운다.
    end_sessions면 같은 트랜잭션에서 그 사용자의 세션을 지운다(keep_session 하나는 남길 수 있다)."""
    with connection() as conn, conn.transaction():
        conn.execute("SELECT 1 FROM dashkit_auth.users WHERE id = %s FOR UPDATE", (user_id,))
        conn.execute(_SET_PASSWORD, (password_hash, must_change, user_id))
        if end_sessions:
            conn.execute("DELETE FROM dashkit_auth.sessions WHERE user_id = %s AND (%s::uuid IS NULL OR id <> %s::uuid)",
                         (user_id, keep_session, keep_session))


def use_reset_token(token_id: str, user_id: str, purpose: str, password_hash: str) -> bool:
    """재설정·초대 수락을 한 트랜잭션으로: 토큰을 지우고(한 번만), 새 비밀번호, 초대면 active·기본 그룹,
    그 사용자의 모든 세션 삭제. 토큰이 이미 쓰였거나 상태가 맞지 않으면 아무것도 바꾸지 않고 False."""
    done = False
    with connection() as conn, conn.transaction() as tx:
        conn.execute("SELECT 1 FROM dashkit_auth.users WHERE id = %s FOR UPDATE", (user_id,))
        if not conn.execute("DELETE FROM dashkit_auth.tokens WHERE id = %s AND user_id = %s AND purpose = %s",
                            (token_id, user_id, purpose)).rowcount:
            raise psycopg.Rollback(tx)
        conn.execute(_SET_PASSWORD, (password_hash, False, user_id))
        if purpose == "invite" and not _activate(conn, user_id, "invited"):
            raise psycopg.Rollback(tx)
        conn.execute("DELETE FROM dashkit_auth.sessions WHERE user_id = %s", (user_id,))
        done = True
    return done


def use_verify_token(token_id: str, user_id: str) -> bool:
    """메일 인증을 한 트랜잭션으로: 토큰을 지우고 pending → active·기본 그룹. 아니면 아무것도 바꾸지 않고 False."""
    done = False
    with connection() as conn, conn.transaction() as tx:
        # 다시 가입(resign_pending)과 겹치지 않게 사용자 행을 먼저 잠근다
        conn.execute("SELECT 1 FROM dashkit_auth.users WHERE id = %s FOR UPDATE", (user_id,))
        if not conn.execute("DELETE FROM dashkit_auth.tokens WHERE id = %s AND user_id = %s AND purpose = 'verify_email'",
                            (token_id, user_id)).rowcount:
            raise psycopg.Rollback(tx)
        if not _activate(conn, user_id, "pending"):
            raise psycopg.Rollback(tx)
        done = True
    return done


def record_login_failure(user_id: str, threshold: int, lock_minutes: int) -> datetime | None:
    """실패 횟수를 1 올린다. threshold에 닿으면 lock_minutes 동안 잠그고(횟수는 0부터 다시) 잠금 끝 시각을 돌려준다."""
    with connection() as conn:
        row = conn.execute(
            """UPDATE dashkit_auth.users
                  SET failed_count = CASE WHEN failed_count + 1 >= %(n)s THEN 0 ELSE failed_count + 1 END,
                      locked_until = CASE WHEN failed_count + 1 >= %(n)s THEN now() + make_interval(mins => %(m)s)
                                          ELSE locked_until END
                WHERE id = %(id)s
            RETURNING failed_count, locked_until, (locked_until > now() AND failed_count = 0) AS just_locked""",
            {"n": threshold, "m": lock_minutes, "id": user_id},
        ).fetchone()
    return row["locked_until"] if row and row["just_locked"] else None


# ---------------------------------------------------------------- 일회용 토큰

def create_token(user_id: str, purpose: str, token_hash: bytes, expires_at: datetime) -> None:
    """같은 사용자·같은 용도의 이전 토큰을 지우고 새로 넣는다(이전 링크는 무효).
    사용자 행을 먼저 잠가 같은 사용자의 동시 발급을 직렬화한다(유효한 토큰은 용도마다 하나만 남는다)."""
    with connection() as conn, conn.transaction():
        conn.execute("SELECT 1 FROM dashkit_auth.users WHERE id = %s FOR UPDATE", (user_id,))
        conn.execute("DELETE FROM dashkit_auth.tokens WHERE user_id = %s AND purpose = %s", (user_id, purpose))
        conn.execute(
            "INSERT INTO dashkit_auth.tokens (user_id, purpose, token_hash, expires_at) VALUES (%s, %s, %s, %s)",
            (user_id, purpose, token_hash, expires_at),
        )


def token_owner(token_hash: bytes, purposes: tuple[str, ...]) -> dict[str, Any] | None:
    """만료되지 않은 토큰의 {token_id, purpose, user_id, email, role, status}. 지우지 않는다."""
    with connection() as conn:
        row = conn.execute(
            """SELECT t.id AS token_id, t.purpose, u.id AS user_id, u.email, u.role, u.status
                 FROM dashkit_auth.tokens t JOIN dashkit_auth.users u ON u.id = t.user_id
                WHERE t.token_hash = %s AND t.purpose = ANY(%s::text[]) AND t.expires_at > now()""",
            (token_hash, list(purposes)),
        ).fetchone()
    if not row:
        return None
    return {**row, "token_id": str(row["token_id"]), "user_id": str(row["user_id"])}


def delete_user_tokens(user_id: str) -> int:
    with connection() as conn:
        return conn.execute("DELETE FROM dashkit_auth.tokens WHERE user_id = %s", (user_id,)).rowcount


# ---------------------------------------------------------------- 테스트 메일함(outbox)

def outbox_add(to_email: str, kind: str, link: str | None) -> bool:
    """받는 계정이 있을 때만 쌓는다(user_id로 읽는 사람의 등급 제한을 판정한다)."""
    with connection() as conn:
        cur = conn.execute(
            """INSERT INTO dashkit_auth.outbox (user_id, to_email, kind, link)
               SELECT id, email, %s, %s FROM dashkit_auth.users WHERE email = %s""",
            (kind, link, to_email),
        )
        return cur.rowcount > 0


def outbox_list(only_role: str | None, limit: int = 1000) -> list[dict[str, Any]]:
    """최근 순. only_role을 주면 받는 계정의 **지금** 등급이 그것인 행만."""
    with connection() as conn:
        rows = conn.execute(
            """SELECT o.id, o.to_email, o.kind, o.link, o.created_at, u.role
                 FROM dashkit_auth.outbox o JOIN dashkit_auth.users u ON u.id = o.user_id
                WHERE (%s::text IS NULL OR u.role = %s::text)
                ORDER BY o.created_at DESC, o.id DESC LIMIT %s""",
            (only_role, only_role, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 보존 기간 정리

def cleanup(pending_days: int, outbox_days: int) -> dict[str, int]:
    """인증 전 계정·오래된 테스트 메일·만료된 토큰과 세션을 지운다. 지운 행 수."""
    out: dict[str, int] = {}
    with connection() as conn:
        out["pending_users"] = conn.execute(
            "DELETE FROM dashkit_auth.users WHERE status = 'pending' AND created_at < now() - make_interval(days => %s)",
            (pending_days,)).rowcount
        out["outbox"] = conn.execute(
            "DELETE FROM dashkit_auth.outbox WHERE created_at < now() - make_interval(days => %s)", (outbox_days,)).rowcount
        out["tokens"] = conn.execute("DELETE FROM dashkit_auth.tokens WHERE expires_at <= now()").rowcount
        out["sessions"] = conn.execute("DELETE FROM dashkit_auth.sessions WHERE expires_at <= now()").rowcount
    return out


# ---------------------------------------------------------------- 사용자 관리(운용)

_LIST_COLS = """u.id, u.email, u.role, u.status, u.source, u.created_at, u.last_login_at, u.login_count,
                u.failed_count, u.locked_until, u.must_change_password, u.email_verified_at, u.password_changed_at,
                (u.locked_until IS NOT NULL AND u.locked_until > now()) AS locked,
                coalesce((SELECT json_agg(json_build_object('id', g.id, 'name', g.name) ORDER BY g.name)
                            FROM dashkit_auth.group_members m JOIN dashkit_auth.groups g ON g.id = m.group_id
                           WHERE m.user_id = u.id), '[]'::json) AS groups"""


def _user_row(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row["id"] = str(row["id"])
    row["groups"] = [{"id": str(g["id"]), "name": g["name"]} for g in (row.get("groups") or [])]
    if "created_by" in row and row["created_by"] is not None:
        row["created_by"] = str(row["created_by"])
    return row


def list_users() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(f"SELECT {_LIST_COLS} FROM dashkit_auth.users u ORDER BY u.email").fetchall()
    return [_user_row(r) for r in rows]


def user_detail(user_id: str) -> dict[str, Any] | None:
    if not is_uuid(user_id):
        return None
    with connection() as conn:
        row = conn.execute(
            f"""SELECT {_LIST_COLS}, u.created_by, c.email AS created_by_email
                  FROM dashkit_auth.users u LEFT JOIN dashkit_auth.users c ON c.id = u.created_by
                 WHERE u.id = %s""",
            (user_id,),
        ).fetchone()
    return _user_row(row) if row else None


def users_by_ids(ids: list[str]) -> list[dict[str, Any]]:
    ids = [i for i in ids if is_uuid(i)]
    if not ids:
        return []
    with connection() as conn:
        rows = conn.execute("SELECT id, email, role, status FROM dashkit_auth.users WHERE id = ANY(%s::uuid[])",
                            (ids,)).fetchall()
    return [{**r, "id": str(r["id"])} for r in rows]


def active_admins() -> list[str]:
    with connection() as conn:
        rows = conn.execute("SELECT id FROM dashkit_auth.users WHERE role = 'admin' AND status = 'active'").fetchall()
    return [str(r["id"]) for r in rows]


def groups_by_ids(ids: list[str]) -> list[dict[str, Any]]:
    ids = [i for i in ids if is_uuid(i)]
    if not ids:
        return []
    with connection() as conn:
        rows = conn.execute("SELECT id, name FROM dashkit_auth.groups WHERE id = ANY(%s::uuid[])", (ids,)).fetchall()
    return [{"id": str(r["id"]), "name": r["name"]} for r in rows]


def set_role(user_id: str, role: str, *, guard_admin: bool = False) -> None:
    """등급 변경. 그 사용자의 세션과 쓰지 않은 일회용 토큰을 함께 지운다(user일 때 발급돼 operator가 볼 수 있었던 링크가
    승급 뒤에 쓰이지 않게). guard_admin이면 같은 트랜잭션에서 마지막 활성 admin인지 다시 본다."""
    with connection() as conn, conn.transaction():
        if guard_admin:
            _guard_last_admin(conn, user_id)
        conn.execute("UPDATE dashkit_auth.users SET role = %s WHERE id = %s", (role, user_id))
        conn.execute("DELETE FROM dashkit_auth.sessions WHERE user_id = %s", (user_id,))
        conn.execute("DELETE FROM dashkit_auth.tokens WHERE user_id = %s", (user_id,))


def set_status(user_id: str, status: str, *, guard_admin: bool = False) -> None:
    """active ↔ suspended. 정지하면 세션을 지운다(그룹 구성·접속 기록·비밀번호는 그대로).
    guard_admin이면 같은 트랜잭션에서 마지막 활성 admin인지 다시 본다."""
    with connection() as conn, conn.transaction():
        if guard_admin:
            _guard_last_admin(conn, user_id)
        conn.execute("UPDATE dashkit_auth.users SET status = %s WHERE id = %s", (status, user_id))
        if status == "suspended":
            conn.execute("DELETE FROM dashkit_auth.sessions WHERE user_id = %s", (user_id,))


def unlock_user(user_id: str) -> None:
    with connection() as conn:
        conn.execute("UPDATE dashkit_auth.users SET failed_count = 0, locked_until = NULL WHERE id = %s", (user_id,))


def set_user_groups(user_id: str, group_ids: list[str], added_by: str | None) -> tuple[list[str], list[str]]:
    """그 사용자의 그룹을 통째로 바꾼다. (더한 그룹 id, 뺀 그룹 id)."""
    with connection() as conn, conn.transaction():
        before = {str(r["group_id"]) for r in conn.execute(
            "SELECT group_id FROM dashkit_auth.group_members WHERE user_id = %s FOR UPDATE", (user_id,)).fetchall()}
        after = set(group_ids)
        for gid in before - after:
            conn.execute("DELETE FROM dashkit_auth.group_members WHERE user_id = %s AND group_id = %s", (user_id, gid))
        for gid in after - before:
            conn.execute("INSERT INTO dashkit_auth.group_members (group_id, user_id, added_by) VALUES (%s, %s, %s)",
                         (gid, user_id, added_by))
    return sorted(after - before), sorted(before - after)


def create_invited_user(email: str, role: str, created_by: str, group_ids: list[str],
                        token_hash: bytes, expires_at: datetime) -> User:
    """임의 가입·초대를 한 트랜잭션으로: invited 계정, 그룹, 초대 토큰. Conflict: 같은 이메일이 있다."""
    with connection() as conn, conn.transaction():
        row = conn.execute(
            f"""INSERT INTO dashkit_auth.users (email, role, status, source, created_by)
                VALUES (%s, %s, 'invited', 'invite', %s) RETURNING {_USER_COLS}""",
            (email, role, created_by),
        ).fetchone()
        for gid in group_ids:
            conn.execute("""INSERT INTO dashkit_auth.group_members (group_id, user_id, added_by) VALUES (%s, %s, %s)
                            ON CONFLICT DO NOTHING""", (gid, row["id"], created_by))
        conn.execute("""INSERT INTO dashkit_auth.tokens (user_id, purpose, token_hash, expires_at)
                        VALUES (%s, 'invite', %s, %s)""", (row["id"], token_hash, expires_at))
    return User.from_row(row)
