"""데이터 DB의 쓰기 잠금(설계서 reference/data-design.md 10-3, contracts/ingest.md "쓰기 잠금").

쓰기는 한 번에 하나다. 잠금은 **데이터 DB에 하나**뿐인 세션 단위 advisory lock이다(프로젝트별·테이블별 잠금이 없다).
배치 폴더나 raw를 바꾸는 모든 작업(더하기, 되돌리기, 합치기, load·apply, 구조 변경)이 이 잠금을 잡는다.

- 잠금은 그 작업의 DB 접속에 묶인다. 작업이 끝나면 풀고, 프로세스가 죽거나 접속이 끊기면 DB가 푼다.
- 잠금 키는 고정 상수다. advisory lock은 데이터베이스마다 따로이므로 테스트가 만든 임시 DB의 잠금은 서로 겹치지 않는다.
- 기다리는 방법은 pg_try_advisory_lock을 짧게 되풀이하는 것이다(lock_timeout 설정에 기대지 않는다).
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

import psycopg

# "dashkit write"를 뜻하는 고정 키(64비트). 다른 기능이 advisory lock을 쓰면 다른 키를 고른다
WRITE_LOCK_KEY = 0x6461_7368_6B69_7401
POLL_SECONDS = 0.05


class LockBusy(Exception):
    """기다리는 시간 안에 잠금을 잡지 못함(다른 쓰기 작업이 도는 중)."""


def try_acquire(conn: psycopg.Connection) -> bool:
    row = conn.execute("SELECT pg_try_advisory_lock(%s)", (WRITE_LOCK_KEY,)).fetchone()
    if conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS:
        conn.commit()      # 잠금은 세션 단위라 트랜잭션을 끝내도 남는다. 잠금을 잡은 뒤의 작업이 새 트랜잭션으로 시작하게 한다
    return bool(row and row[0])


def acquire(conn: psycopg.Connection, wait_ms: int) -> None:
    """잠금을 잡는다. wait_ms 안에 못 잡으면 LockBusy."""
    deadline = time.monotonic() + max(wait_ms, 0) / 1000
    while True:
        if try_acquire(conn):
            return
        if time.monotonic() >= deadline:
            raise LockBusy()
        time.sleep(POLL_SECONDS)


def release(conn: psycopg.Connection) -> None:
    """잠금을 푼다. 접속이 이미 끊겼으면 DB가 풀었으므로 아무것도 하지 않는다."""
    if conn.closed:
        return
    try:
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s)", (WRITE_LOCK_KEY,))
        if conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS:
            conn.commit()
    except psycopg.Error:
        pass


@contextmanager
def write_lock(conn: psycopg.Connection, wait_ms: int) -> Iterator[None]:
    acquire(conn, wait_ms)
    try:
        yield
    finally:
        release(conn)


def held_by_other(conn: psycopg.Connection) -> bool:
    """다른 세션이 잠금을 잡고 있는지(진단·테스트용). 이 세션의 잠금은 세지 않는다."""
    row = conn.execute(
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted AND pid <> pg_backend_pid() "
        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
        "AND ((classid::bigint << 32) | objid::bigint) = %s", (WRITE_LOCK_KEY,)).fetchone()
    if conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS:
        conn.commit()
    return bool(row and row[0])
