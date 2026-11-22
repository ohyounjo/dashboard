"""외부 원본 DB에서 스트리밍으로 데이터를 읽어 접수에 넘긴다(설계서 3절).

- CursorParser(PARSERS["cursor"]): 서버사이드 커서(itersize=2000)로 행을 스트리밍하며 _db_cell 규칙으로 변환한다.
- ingest_from_db: DSN은 환경 변수 이름(--dsn-env, 기본 SOURCE_DB_DSN)으로만 받고 CLI 인자·코드·로그·오류·감사 로그에
  절대 남기지 않는다. 접속 직후 read_only=True(default_transaction_read_only=on)와 statement_timeout을 강제한다.
"""
from __future__ import annotations

import math
import os
import re
import secrets
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

from .. import config
from .intake import IntakeError, Parser, RowError

DEFAULT_DSN_ENV = "SOURCE_DB_DSN"
DEFAULT_TIMEOUT_MS = 300_000
DEFAULT_ITERSIZE = 2000

_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_IDENT_PART_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
_ALLOWED_QUERY_START = frozenset({"select", "with", "values", "table", "("})
_URI_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@\s]+)@")
_KV_PW_RE = re.compile(r"(password\s*=\s*)(?:'[^']*'|\"[^\"]*\"|\S+)", re.IGNORECASE)
_KV_USER_RE = re.compile(r"(user\s*=\s*)(?:'[^']*'|\"[^\"]*\"|\S+)", re.IGNORECASE)
_KV_HOST_RE = re.compile(r"(host(?:addr)?\s*=\s*)(?:'[^']*'|\"[^\"]*\"|\S+)", re.IGNORECASE)


def _db_cell(val: Any, where: str) -> str | RowError:
    """외부 DB 스칼라 값 → 접수용 문자열. 비스칼라·바이너리는 RowError."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, Decimal):
        return format(val, "f") if val.is_finite() else str(val)
    if isinstance(val, float):
        return repr(val) if math.isfinite(val) else str(val)
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, uuid.UUID):
        return str(val)
    if isinstance(val, str):
        return val
    if isinstance(val, (bytes, bytearray, memoryview)):
        return RowError(where, "바이너리(bytea) 값은 적재할 수 없음")
    if isinstance(val, (list, tuple, dict, set)):
        return RowError(where, "값은 스칼라(문자열·숫자·참거짓·날짜·null)만 받음")
    return RowError(where, f"지원하지 않는 값 유형({type(val).__name__})")


class CursorParser(Parser):
    """DB 커서 스트리밍 파서(PARSERS['cursor'])."""

    kind = "cursor"
    encoding = "db"
    delimiter = None

    def __init__(self, cursor: Any, name: str = "db", *, itersize: int = DEFAULT_ITERSIZE,
                 owns_cursor: bool = False):
        self._cursor = cursor
        self.name = name
        self._itersize = max(int(itersize), 1)
        self._owns_cursor = owns_cursor
        self._header: list[str] | None = None
        self._prefetched: list[Any] | None = None

    def _col_names(self, desc: Any) -> list[str]:
        names: list[str] = []
        for col in desc:
            raw = getattr(col, "name", None)
            if raw is None:
                raw = col[0]
            names.append(str(raw).strip())
        return names

    def header(self) -> list[str]:
        if self._header is not None:
            return list(self._header)
        desc = getattr(self._cursor, "description", None)
        if desc is None and hasattr(self._cursor, "fetchmany"):
            self._prefetched = list(self._cursor.fetchmany(self._itersize))
            desc = getattr(self._cursor, "description", None)
        if not desc:
            raise IntakeError("invalid_rows", "쿼리 결과에 열이 없습니다")
        names = self._col_names(desc)
        if not names or all(n == "" for n in names):
            raise IntakeError("invalid_rows", "쿼리 결과에 열이 없습니다")
        self._header = names
        return list(self._header)

    def _iter_batches(self) -> Iterator[list[Any]]:
        if self._prefetched is not None:
            first = self._prefetched
            self._prefetched = None
            if first:
                yield first
            if not hasattr(self._cursor, "fetchmany"):
                return
        if hasattr(self._cursor, "fetchmany"):
            while True:
                batch = self._cursor.fetchmany(self._itersize)
                if not batch:
                    break
                yield batch
        else:
            batch: list[Any] = []
            for row in self._cursor:
                batch.append(row)
                if len(batch) >= self._itersize:
                    yield batch
                    batch = []
            if batch:
                yield batch

    def rows(self) -> Iterator[tuple[int, list[str] | RowError]]:
        cols = self.header()
        width = len(cols)
        at = 0
        for batch in self._iter_batches():
            for db_row in batch:
                at += 1
                if len(db_row) != width:
                    yield at, RowError(self.where(at, "-"), f"값 개수 {len(db_row)}개가 머리글 {width}개와 다름")
                    continue
                values: list[str] = []
                bad: RowError | None = None
                for col_name, val in zip(cols, db_row):
                    cell = _db_cell(val, self.where(at, col_name))
                    if isinstance(cell, RowError):
                        if bad is None:
                            bad = cell
                        values.append("")
                    else:
                        values.append(cell)
                if bad is not None:
                    bad.raw = values
                    yield at, bad
                else:
                    yield at, values

    def where(self, at: int, column: str) -> str:
        return f"{self.name}:{at}:{column}"

    def close(self) -> None:
        if self._owns_cursor and self._cursor is not None and hasattr(self._cursor, "close"):
            try:
                self._cursor.close()
            except Exception:
                pass
            self._cursor = None


def validate_dsn_env(env_name: str) -> str:
    """DSN을 읽을 환경 변수 이름을 검증하고 값을 읽는다(비밀값은 예외/메시지에 넣지 않는다)."""
    from .operation import IngestError

    if not isinstance(env_name, str) or not _ENV_NAME_RE.fullmatch(env_name):
        raise IngestError(400, "bad_request", f"DSN 환경 변수 이름 형식이 올바르지 않습니다: '{env_name}'")
    value = os.environ.get(env_name, "").strip()
    if not value:
        raise IngestError(400, "bad_request", f"환경변수 {env_name}가 비어 있거나 설정되지 않았습니다")
    return value


def _dsn_secrets(dsn_value: str) -> list[str]:
    secrets_list: list[str] = [dsn_value]
    if "://" in dsn_value:
        try:
            parsed = urlsplit(dsn_value)
        except ValueError:
            parsed = None
        if parsed is not None:
            for item in (parsed.password, parsed.username, parsed.hostname):
                if item and len(item) >= 2:
                    secrets_list.append(item)
    for pat in (
        r"(?:password|user|host|hostaddr)\s*=\s*'([^']*)'",
        r'(?:password|user|host|hostaddr)\s*=\s*"([^"]*)"',
        r"(?:password|user|host|hostaddr)\s*=\s*(\S+)",
    ):
        for m in re.finditer(pat, dsn_value, re.IGNORECASE):
            val = m.group(1)
            if val and len(val) >= 2:
                secrets_list.append(val)
    # 긴 문자열부터 먼저 치환한다
    return sorted({s for s in secrets_list if s}, key=len, reverse=True)


def sanitize_db_error(exc: BaseException, dsn_value: str | None = None) -> str:
    """외부 DB 오류 메시지에서 DSN 원문·URI 자격 증명·호스트·비밀번호를 마스킹한다."""
    diag = getattr(exc, "diag", None)
    primary = diag.message_primary if diag and getattr(diag, "message_primary", None) else None
    text = str(primary or exc).strip()
    if dsn_value:
        for secret in _dsn_secrets(dsn_value):
            text = text.replace(secret, "***")
    text = _URI_CRED_RE.sub(r"\1***@", text)
    text = _KV_PW_RE.sub(r"\1***", text)
    text = _KV_USER_RE.sub(r"\1***", text)
    text = _KV_HOST_RE.sub(r"\1***", text)
    sqlstate = getattr(exc, "sqlstate", None)
    kind = type(exc).__name__
    if not text:
        text = kind
    return f"{kind} [{sqlstate}]: {text}" if sqlstate else f"{kind}: {text}"


def _prepare_import_sql(raw_sql: str) -> str:
    """단일 조회 SELECT/WITH/VALUES/TABLE 문장인지 검사하고 _dashkit_src 서브쿼리로 감싼다."""
    from ..manage.sql_guard import _tokenize
    from .operation import IngestError

    trimmed = raw_sql.strip()
    if not trimmed:
        raise IngestError(422, "invalid_query", "SQL 파일이 비어 있습니다")
    tokens = _tokenize(trimmed)
    if not tokens:
        raise IngestError(422, "invalid_query", "SQL 파일이 비어 있습니다")
    cleaned = trimmed
    if tokens[-1].kind == "punct" and tokens[-1].value == ";":
        semi = tokens.pop()
        cleaned = (trimmed[:semi.start] + trimmed[semi.end:]).strip()
        if not tokens:
            raise IngestError(422, "invalid_query", "SQL 파일이 비어 있습니다")
    for tok in tokens:
        if tok.kind == "punct" and tok.value == ";":
            raise IngestError(422, "invalid_query", "한 번에 하나의 조회 문장만 실행할 수 있습니다")
    depth = 0
    for tok in tokens:
        if tok.kind == "punct":
            if tok.value == "(":
                depth += 1
            elif tok.value == ")":
                depth -= 1
                if depth < 0:
                    raise IngestError(422, "invalid_query", "괄호 짝이 맞지 않습니다")
    if depth != 0:
        raise IngestError(422, "invalid_query", "괄호가 닫히지 않았습니다")
    first_val = tokens[0].value.lower() if tokens[0].kind in ("word", "punct") else ""
    if first_val not in _ALLOWED_QUERY_START:
        raise IngestError(422, "invalid_query", "읽기 전용 SELECT 문장만 실행할 수 있습니다")
    return f"SELECT * FROM (\n{cleaned}\n) AS _dashkit_src"


def build_source_query(
    *,
    source_table: str | None = None,
    query_file: Path | None = None,
    query: str | None = None,
) -> tuple[Any, str]:
    """--source-table 또는 --query-file/--query를 안전한 SELECT 문과 표시 이름으로 만든다."""
    from .operation import IngestError

    given = sum(1 for x in (source_table, query_file, query) if x is not None)
    if given != 1:
        raise IngestError(400, "bad_request", "--source-table과 --query-file 중 하나만 지정해야 합니다")
    if source_table is not None:
        parts = [p.strip() for p in source_table.split(".")]
        if len(parts) not in (1, 2) or any(not _IDENT_PART_RE.fullmatch(p) for p in parts):
            raise IngestError(400, "bad_request", f"원본 테이블 이름 형식이 올바르지 않습니다: '{source_table}'")
        stmt = sql.SQL("SELECT * FROM {}").format(sql.Identifier(*parts))
        return stmt, f"db:{source_table}"
    if query_file is not None:
        path = Path(query_file)
        if not path.is_file():
            raise IngestError(400, "bad_request", f"쿼리 파일이 없습니다: {path}")
        try:
            sql_text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise IngestError(400, "bad_request", f"쿼리 파일을 읽을 수 없습니다: {exc}") from None
        return _prepare_import_sql(sql_text), f"db:{path.name}"
    assert query is not None
    return _prepare_import_sql(query), "db:query"


def _audit_db(project: str, table: str, source_name: str, *, batch: Mapping[str, Any] | None = None,
              detail_code: str | None = None) -> None:
    try:
        from ..auth import store as auth_store

        if batch is not None:
            auth_store.record_audit(
                "data.batch.create",
                target_type="table",
                target_id=table,
                target_label=table,
                detail={
                    "project": project,
                    "table": table,
                    "file": batch["file"],
                    "sha256": batch["sha256"],
                    "rows": batch["rows"],
                    "method": "db",
                    "source_name": source_name,
                },
            )
        else:
            auth_store.record_audit(
                "data.batch.failed",
                target_type="table",
                target_id=table,
                target_label=table,
                detail={
                    "project": project,
                    "table": table,
                    "method": "db",
                    "source_name": source_name,
                    "detail_code": detail_code or "unknown",
                },
            )
    except Exception:
        pass


def ingest_from_db(
    project_name: str,
    table: str,
    *,
    dsn_env: str = DEFAULT_DSN_ENV,
    source_table: str | None = None,
    query_file: Path | None = None,
    query: str | None = None,
    columns: Mapping[str, Any] | None = None,
    source_name: str | None = None,
    dry_run: bool = False,
    force: bool = False,
    skip_invalid: bool = False,
    projects_dir: Path | None = None,
    dbname: str | None = None,
    wait_ms: int | None = None,
    timeout_ms: int | None = None,
    itersize: int = DEFAULT_ITERSIZE,
) -> dict[str, Any]:
    """외부 DB에서 읽기 전용 커서로 스트리밍 추출해 대상 누적 테이블에 적재한다."""
    from .add import Item, check_table, ingest
    from .operation import IngestError

    dsn = validate_dsn_env(dsn_env)
    stmt, default_name = build_source_query(source_table=source_table, query_file=query_file, query=query)
    label = (source_name or "").strip() or default_name

    # 대상 테이블이 유효한 append 테이블인지 먼저 확인한다
    try:
        project = config.load_project(project_name, projects_dir)
    except config.ConfigError as exc:
        raise IngestError(422, "invalid_config", "설정 검사를 통과하지 못했습니다", exc.errors) from None
    check_table(project, table)

    if timeout_ms is None:
        try:
            timeout_ms = max(int(os.environ.get("SOURCE_DB_TIMEOUT_MS") or DEFAULT_TIMEOUT_MS), 1000)
        except ValueError:
            timeout_ms = DEFAULT_TIMEOUT_MS

    ext_conn: psycopg.Connection | None = None
    try:
        ext_conn = psycopg.connect(dsn, connect_timeout=10)
    except psycopg.Error as exc:
        msg = f"원본 DB 접속 실패: {sanitize_db_error(exc, dsn)}"
        if not dry_run:
            _audit_db(project_name, table, label, detail_code="db_unavailable")
        raise IngestError(503, "db_unavailable", msg) from None

    active_cursors: list[Any] = []
    try:
        try:
            ext_conn.read_only = True
            with ext_conn.cursor() as cfg_cur:
                cfg_cur.execute("SELECT set_config('default_transaction_read_only', 'on', false)")
                cfg_cur.execute("SELECT set_config('statement_timeout', %s, false)", (str(timeout_ms),))
            ext_conn.commit()
            batch_size = max(int(itersize), 1)

            def _open_cursor() -> Any:
                # 이전 시도의 서버사이드 커서가 남아 있으면 정리하고 읽기 전용 트랜잭션을 새로 시작한다
                while active_cursors:
                    prev = active_cursors.pop()
                    try:
                        prev.close()
                    except Exception:
                        pass
                try:
                    ext_conn.rollback()
                except Exception:
                    pass
                cur_name = f"dashkit_src_{secrets.token_hex(4)}"
                cur = ext_conn.cursor(name=cur_name)
                cur.itersize = batch_size
                try:
                    cur.execute(stmt)
                except Exception:
                    try:
                        cur.close()
                    except Exception:
                        pass
                    raise
                active_cursors.append(cur)
                return cur

            item = Item(table, {"kind": "cursor", "cursor_factory": _open_cursor, "name": label, "itersize": batch_size},
                        columns=columns)
            result = ingest(
                project_name,
                [item],
                dry_run=dry_run,
                force=force,
                skip_invalid=skip_invalid,
                projects_dir=projects_dir,
                dbname=dbname,
                wait_ms=wait_ms,
            )
        except IngestError as exc:
            if not dry_run:
                _audit_db(project_name, table, label, detail_code=exc.detail)
            raise
        except psycopg.errors.ReadOnlySqlTransaction as exc:
            if not dry_run:
                _audit_db(project_name, table, label, detail_code="invalid_query")
            raise IngestError(422, "invalid_query", f"읽기 전용 세션에서는 쓰기 쿼리를 실행할 수 없습니다: {sanitize_db_error(exc, dsn)}") from None
        except psycopg.Error as exc:
            if not dry_run:
                _audit_db(project_name, table, label, detail_code="invalid_query")
            raise IngestError(422, "invalid_query", f"원본 DB 쿼리 오류: {sanitize_db_error(exc, dsn)}") from None
    finally:
        while active_cursors:
            prev = active_cursors.pop()
            try:
                prev.close()
            except Exception:
                pass
        if ext_conn is not None:
            try:
                ext_conn.close()
            except Exception:
                pass

    if not dry_run and result.get("ok"):
        _audit_db(project_name, table, label, batch=result["batches"][0])
    return result
