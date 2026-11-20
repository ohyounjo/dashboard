"""행 조회와 데이터셋 목록(설계서 8-1, 8-2, 9-1, contracts/manage.md "행 조회").

읽기 경로라 **dash_inspect만** 쓴다(읽기 전용 트랜잭션, MANAGE_STATEMENT_TIMEOUT_MS 시간 제한). owner 접속을 열지 않는다.
페이지 나눔·커서·필터는 dashkit/paging.py 한 벌이다(backend의 /api/data와 함께 쓴다).
"""
from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Request
from psycopg import sql
from starlette.concurrency import run_in_threadpool

from ... import config, db, paging
from ...api.data import _TYPE_BY_OID, _value
from ...auth.routes import Principal, require_operator
from ...ingest import IngestError, ledger, lineage
from .. import context
from .tables import _json, _lineage, _project, _table

router = APIRouter()

_SCHEMA_TYPES = {"enum": "text"}
_VIEW_TYPES = {"bigint": "integer", "integer": "integer", "smallint": "integer", "numeric": "number",
               "double precision": "number", "real": "number", "boolean": "boolean", "date": "date",
               "timestamp with time zone": "timestamp", "timestamp without time zone": "timestamp"}


def _connect() -> psycopg.Connection:
    timeout = context.limits().statement_timeout_ms
    try:
        return db.inspect_conn(application_name="dashkit-manage-read",
                               options=f"-c default_transaction_read_only=on -c statement_timeout={timeout}")
    except (psycopg.Error, RuntimeError):
        raise IngestError(503, "db_unavailable", "조회용 DB 계정으로 접속하지 못했습니다(make data-init)") from None


def _to_json(v: Any, kind: str) -> Any:
    return _value(v, kind)


def _versions(cur: psycopg.Cursor) -> tuple[Any, dict[str, dict[str, ledger.Recorded]]]:
    now = ledger.loaded(cur)
    return now.loaded_at, ledger.read(cur)


def _run(work):
    """조회 하나: 접속 → 읽기 → 닫기. 시간 제한을 넘으면 504 query_timeout, 조회 오류는 422."""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            return work(cur)
    except paging.QueryError as exc:
        raise IngestError(422, exc.detail, exc.message) from None
    except psycopg.errors.QueryCanceled:
        raise IngestError(504, "query_timeout", "조회가 시간 제한을 넘었습니다. 조건을 좁혀 주세요") from None
    except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName):
        raise IngestError(409, "rebuild_required", "DB에 이 테이블·뷰가 없습니다. 먼저 적용(apply)하세요") from None
    except psycopg.Error as exc:
        raise IngestError(422, "invalid_query", f"조회할 수 없습니다: {exc.diag.message_primary or type(exc).__name__}") \
            from None
    finally:
        conn.close()


@router.post("/tables/{table}/rows/query")
async def post_table_rows(table: str, request: Request, principal: Principal = Depends(require_operator)):
    body = await _json(request)
    project = _project()
    name = _table(project, table)
    spec = project.schema["tables"][name]
    columns = [paging.Column(c, _SCHEMA_TYPES.get(s["type"], s["type"])) for c, s in spec["columns"].items()]
    q = _parse(body, columns)
    pk = list(spec["primary_key"])

    def work(cur):
        relation = sql.SQL("{}.{}").format(sql.Identifier("raw"), sql.Identifier(name))
        if q.key is not None:
            return _one(cur, relation, columns, pk, q.key)
        loaded_at, recorded = _versions(cur)
        rec = recorded.get(name, {})
        version = paging.data_version(loaded_at, {f: r.sha256 for f, r in rec.items()})
        page = paging.fetch_page(cur, relation, columns, q, key_columns=pk, version=version,
                                 total_rows=sum(r.inserted for r in rec.values()) if rec else None, to_json=_to_json)
        return page.public()

    return await run_in_threadpool(_run, work)


def _one(cur, relation, columns, pk: list[str], key: dict[str, Any]) -> dict[str, Any]:
    if sorted(key) != sorted(pk):
        raise paging.QueryError("invalid_query", f"key는 기본 키 열 전부({', '.join(pk)})입니다")
    found = paging.fetch_row(cur, relation, columns, key, to_json=_to_json)
    if found is None:
        raise IngestError(404, "unknown_row", "그 키의 행이 없습니다")
    return {"columns": [{"name": c.name, "type": c.type} for c in columns], **found}


def _parse(body: dict[str, Any], columns: list[paging.Column]) -> paging.Query:
    try:
        return paging.parse_query(body, columns)
    except paging.QueryError as exc:
        raise IngestError(422, exc.detail, exc.message) from None


def _view_columns(cur, key: str) -> list[paging.Column]:
    cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = 'dash' "
                "AND table_name = %s ORDER BY ordinal_position", (key,))
    return [paging.Column(c, _VIEW_TYPES.get(t, "text")) for c, t in cur.fetchall()]


def _dataset(project: config.Project, key: str) -> str:
    for known in project.dashboard["datasets"]:
        if known == key:
            return known
    raise IngestError(404, "unknown_dataset", "dashboard.yaml에 없는 데이터셋입니다")


@router.post("/datasets/{dataset}/rows/query")
async def post_dataset_rows(dataset: str, request: Request, principal: Principal = Depends(require_operator)):
    """그래프가 실제로 보는 뷰(dash.<데이터셋>)의 행. table: 데이터셋이 기본 키 열을 모두 내보내면 키 방식, 아니면 건너뛰기."""
    body = await _json(request)
    project = _project()
    key = _dataset(project, dataset)
    ds = project.dashboard["datasets"][key]

    def work(cur):
        columns = _view_columns(cur, key)
        if not columns:
            raise IngestError(409, "rebuild_required", "DB에 이 데이터셋의 뷰가 없습니다. 먼저 적용(apply)하세요")
        q = paging.parse_query(body, columns)
        names = {c.name for c in columns}
        table = ds.get("table")
        pk = list(project.schema["tables"][table]["primary_key"]) if table else []
        key_columns = pk if table and all(k in names for k in pk) else None
        relation = sql.SQL("{}.{}").format(sql.Identifier("dash"), sql.Identifier(key))
        if q.key is not None:
            if not key_columns:
                raise paging.QueryError("invalid_query", "이 데이터셋에는 행을 가리키는 키가 없습니다")
            return _one(cur, relation, columns, key_columns, q.key)
        now = ledger.loaded(cur)
        loaded_at, recorded = now.loaded_at, ledger.read(cur)
        # 데이터 판: table: 데이터셋은 그 테이블, sql: 데이터셋은 계보(적재 스냅샷의 sources)가 말하는 읽는 테이블.
        # 계보가 없으면(옛 스냅샷) 모든 테이블로 넓게 본다(놓치는 것보다 한 번 더 알린다)
        sources = lineage.build(project, now.snapshot)["datasets"][key]["sources"]
        tables = [table] if table else sorted(sources) if sources is not None else list(project.schema["tables"])
        version = paging.data_version(loaded_at, {f"{t}/{f}": r.sha256 for t in tables
                                                  for f, r in recorded.get(t, {}).items()})
        total = sum(r.inserted for r in recorded.get(table, {}).values()) if table and recorded.get(table) else None
        page = paging.fetch_page(cur, relation, columns, q, key_columns=key_columns, version=version,
                                 total_rows=total, to_json=_to_json)
        return page.public()

    return await run_in_threadpool(_run, work)


@router.get("/datasets")
def get_datasets(principal: Principal = Depends(require_operator)) -> dict[str, Any]:
    """데이터셋 목록: 종류(table|sql), 읽는 테이블(계보), 쓰는 그래프 수(설정에서 센다)."""
    project = _project()
    graph = _lineage(project)
    used: dict[str, int] = {}
    for _d, chart in config.charts(project):
        used[chart["dataset"]] = used.get(chart["dataset"], 0) + 1
    for key in config.widget_datasets(project):
        used[key] = used.get(key, 0) + 1
    items = []
    for key, ds in project.dashboard["datasets"].items():
        items.append({"key": key, "kind": "table" if "table" in ds else "sql", "table": ds.get("table"),
                      "sources": graph["datasets"][key]["sources"],
                      "description": ds.get("description"), "used_by": used.get(key, 0),
                      "labels": ds.get("labels") or {}})
    return {"items": items, "total": len(items)}
