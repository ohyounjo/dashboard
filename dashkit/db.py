"""데이터 DB 접속. 접속 정보는 환경변수에서만 읽는다.

- owner_conn(): 로더 전용(raw, dash 스키마 소유자). backend와 Superset은 쓰지 않는다.
- reader_conn(): dash 스키마 SELECT만 있는 dash_reader. backend, sync(열 정보 조회), 테스트.
- auth_conn(): dashkit_auth 스키마의 표만 읽고 쓰는 dash_auth. backend의 인증·권한·운용 API와 `dashkit auth` CLI.
  raw·dash는 읽지 못하고, dash_reader는 dashkit_auth를 읽지 못한다(contracts/auth.md "DB 계정").
"""
from __future__ import annotations

import os

import psycopg


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"환경변수 {name}가 필요합니다")
    return value


def _connect(user: str, password: str, dbname: str | None, **kwargs) -> psycopg.Connection:
    return psycopg.connect(
        host=_env("DATA_DB_HOST", "data-db"),
        port=int(_env("DATA_DB_PORT", "5432")),
        dbname=dbname or _env("DATA_DB_NAME", "dash"),
        user=user,
        password=password,
        connect_timeout=int(os.environ.get("DATA_DB_CONNECT_TIMEOUT", "5")),
        application_name=kwargs.pop("application_name", "dashkit"),
        **kwargs,
    )


def owner_conn(dbname: str | None = None, **kwargs) -> psycopg.Connection:
    return _connect(_env("DATA_DB_OWNER_USER", "dash_owner"), _env("DATA_DB_OWNER_PASSWORD"), dbname, **kwargs)


def reader_conn(dbname: str | None = None, **kwargs) -> psycopg.Connection:
    return _connect(_env("DATA_DB_READER_USER", "dash_reader"), _env("DATA_DB_READER_PASSWORD"), dbname, **kwargs)


def auth_conn(dbname: str | None = None, **kwargs) -> psycopg.Connection:
    password = _env("DATA_DB_AUTH_PASSWORD")
    if password == "__GENERATE__":   # .env.example의 자리표시자가 그대로 넘어온 경우(make auth-init이 .env에 채운다)
        raise RuntimeError("환경변수 DATA_DB_AUTH_PASSWORD가 아직 생성되지 않았습니다(make auth-init)")
    return _connect(_env("DATA_DB_AUTH_USER", "dash_auth"), password, dbname, **kwargs)
