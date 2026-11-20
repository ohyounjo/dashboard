"""적재 기록: 로더가 적재할 때 남기고 `dashkit plan`이 읽는 비교용 자료(contracts/plan.md "적재 기록").

plan은 조회 전용이라 dash_reader 계정만 쓴다. dash_reader는 raw를 읽을 수 없으므로, 로더(owner)가 적재하는
같은 트랜잭션에서 "무엇을 적재했는지"를 별도 스키마 `dashkit_meta`에 남기고 dash_reader에게 SELECT만 준다.

- `dashkit_meta.row_fingerprint(table_name, row_key, row_hash)`: 행마다 기본 키의 정규 표현과 행 해시.
  둘 다 SHA-256이다. **raw의 원본 값은 하나도 저장하지 않는다**(기본 키 값도 해시로만).
- `dashkit_meta.load_snapshot(id, loaded_at, snapshot)`: 한 건. 적재한 프로젝트, 테이블 구조, 데이터셋 정의의 해시,
  데이터 설정 지문(`data_revision`).
- `dashkit_meta.batch(table_name, file, sha256, added_at, rows, inserted, updated, skipped, unchanged)`: 프로젝트의
  **모든 원본 데이터 파일**이 한 줄씩(replace 테이블의 CSV, append 테이블의 배치마다). 원본 값은 없다(이름, 해시, 행 수).
  추가 적재(dashkit/ingest)도 같은 표에 더한다(contracts/ingest.md).
- 누적(append) 테이블은 행 지문을 남기지 않는다. plan은 배치 단위로 비교한다.

지문은 DB 값이 아니라 **CSV를 파싱한 파이썬 값**에서 계산한다. plan도 CSV에서 같은 함수로 계산해 비교하므로
DB의 타입 표현 차이가 끼어들지 않는다. 이 파일의 계산 함수는 순수 함수다(DB에 닿는 것은 write 하나).
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

META = "dashkit_meta"
FINGERPRINT_TABLE = "row_fingerprint"
SNAPSHOT_TABLE = "load_snapshot"
BATCH_TABLE = "batch"
SNAPSHOT_VERSION = 1
BATCH_COLUMNS = ("table_name", "file", "sha256", "added_at", "rows", "inserted", "updated", "skipped", "unchanged")

# schema.yaml의 type → 적재 뒤 information_schema.columns의 data_type(로더의 CREATE TABLE과 짝이다)
INFO_TYPES = {
    "text": "text",
    "enum": "text",
    "integer": "bigint",
    "number": "numeric",
    "boolean": "boolean",
    "date": "date",
    "timestamp": "timestamp with time zone",
}


# ---------------------------------------------------------------- 지문(순수)

def canonical(value: Any) -> str | None:
    """값 하나의 정규 표현. 같은 값은 표기가 달라도 같은 문자열이 된다(1.50 = 1.5, TRUE = true).

    빈 값(None)은 None. 시간대가 있는 시각은 UTC로 맞춘다. 시간대가 없는 시각은 그대로 둔다.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        if value == 0:
            return "0"
        # normalize()는 컨텍스트 정밀도(기본 28자리)로 반올림하므로 쓰지 않는다. 전체 자릿수를 그대로 펼친 뒤
        # 소수부 끝의 0만 지운다.
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).isoformat()
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def row_key(columns: Sequence[str], primary_key: Sequence[str], row: Sequence[Any]) -> str:
    """기본 키의 해시: 기본 키 값의 정규 표현을 primary_key 순서로 담은 JSON 배열의 SHA-256.

    원본 키 값은 기록에 남기지 않는다(dash_reader가 읽는 기록에 raw의 원본 값이 없어야 한다).
    """
    values = [canonical(row[columns.index(c)]) for c in primary_key]
    return hashlib.sha256(_dump(values).encode("utf-8")).hexdigest()


def row_label(columns: Sequence[str], primary_key: Sequence[str], row: Sequence[Any]) -> str:
    """기본 키를 사람이 읽는 글로(복합 키는 ", "로 잇는다). CSV 쪽 행을 화면에 보여 줄 때만 쓰고 저장하지 않는다."""
    values = [canonical(row[columns.index(c)]) for c in primary_key]
    return ", ".join("" if v is None else v for v in values)


def row_hash(columns: Sequence[str], row: Sequence[Any]) -> str:
    """행 해시. 열 이름순으로 (열, 정규 표현)을 직렬화한 것의 SHA-256이라 열 순서와 무관하다."""
    pairs = sorted(zip(columns, (canonical(v) for v in row)))
    return hashlib.sha256(_dump(pairs).encode("utf-8")).hexdigest()


def fingerprints(table: Mapping[str, Any], rows: Iterable[Sequence[Any]]) -> list[tuple[str, str]]:
    """테이블의 행들 → [(row_key, row_hash)]. table은 schema.yaml의 정규화된 테이블 정의, 행의 열 순서는 table["columns"]."""
    columns = list(table["columns"])
    pk = list(table["primary_key"])
    return [(row_key(columns, pk, row), row_hash(columns, row)) for row in rows]


def labels(table: Mapping[str, Any], rows: Iterable[Sequence[Any]]) -> dict[str, str]:
    """{row_key(해시): 사람이 읽는 기본 키}. plan이 CSV 쪽 행(추가·변경)의 예시 키를 보여 주는 데 쓴다."""
    columns = list(table["columns"])
    pk = list(table["primary_key"])
    return {row_key(columns, pk, row): row_label(columns, pk, row) for row in rows}


def table_columns(table: Mapping[str, Any]) -> list[tuple[str, str]]:
    """schema.yaml의 테이블 정의 → 적재 뒤 raw 테이블의 열 [(이름, data_type)](= `table:` 데이터셋 뷰의 열)."""
    return [(name, INFO_TYPES[spec["type"]]) for name, spec in table["columns"].items()]


def definition_hash(dataset: Mapping[str, Any]) -> str:
    """데이터셋의 뷰 정의(table 또는 sql) 해시. sql은 로더가 뷰를 만들 때와 같은 다듬기를 거친다."""
    if "table" in dataset:
        text = "table:" + dataset["table"]
    else:
        text = "sql:" + dataset["sql"].strip().rstrip(";").strip()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def table_rules_hash(table: Mapping[str, Any]) -> str:
    """테이블 정의(정규화한 값: 열·규칙·방식·충돌 정책·default·checks)의 해시."""
    text = json.dumps(table, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def data_revision(schema: Mapping[str, Any], dashboard: Mapping[str, Any]) -> str:
    """데이터 설정 지문: DB의 내용을 정하는 설정만으로 구한 해시(contracts/ingest.md "데이터 설정 지문").

    schema.yaml의 tables 전체(열, 규칙, 방식, 충돌 정책, checks)와 dashboard.yaml의 datasets(뷰 정의: table·sql)·
    app.as_of. 탭·그래프 배치, 데이터셋의 지표·표시 이름처럼 DB와 상관없는 설정은 넣지 않는다.
    정규화한 설정(config.load_project의 결과)에서 구한다.
    """
    views = {k: {"table": ds["table"]} if "table" in ds else {"sql": ds.get("sql")}
             for k, ds in (dashboard.get("datasets") or {}).items()}
    doc = {"tables": schema["tables"], "datasets": views, "as_of": (dashboard.get("app") or {}).get("as_of")}
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot(project_name: str, schema: Mapping[str, Any], dashboard: Mapping[str, Any],
             row_counts: Mapping[str, int], revision: str | None = None) -> dict[str, Any]:
    """적재 스냅샷(JSON으로 저장). plan이 "구조가 바뀌었는지", 추가 적재가 "설정이 DB에 적용됐는지" 판단하는 데 쓴다."""
    return {
        "version": SNAPSHOT_VERSION,
        "project": project_name,
        "data_revision": revision if revision is not None else data_revision(schema, dashboard),
        "tables": {
            name: {
                "columns": [list(c) for c in table_columns(table)],
                "primary_key": list(table["primary_key"]),
                "rows": int(row_counts.get(name, 0)),
                "mode": table.get("mode", "replace"),
                # 누적 테이블은 배치를 어떻게 해석하는지(충돌 정책, default, 규칙)도 남긴다. 파일이 같아도 이것이 바뀌면
                # 다시 만든 결과가 달라질 수 있다(plan이 비교한다)
                **({"rules": table_rules_hash(table)} if table.get("mode") == "append" else {}),
            }
            for name, table in schema["tables"].items()
        },
        "datasets": {
            key: {"kind": "table" if "table" in ds else "sql", "definition": definition_hash(ds)}
            for key, ds in dashboard["datasets"].items()
        },
    }


# ---------------------------------------------------------------- 쓰기(로더의 트랜잭션 안)

def write(cur: Any, snap: Mapping[str, Any], prints: Mapping[str, Sequence[tuple[str, str]]], reader: str,
          batches: Iterable[Sequence[Any]] = (), readers: Sequence[str] = ()) -> None:
    """`dashkit_meta`를 통째로 다시 만들고 기록을 넣는다. 로더의 적재 트랜잭션 안에서 부른다(실패하면 함께 롤백).

    batches는 배치 기록의 행(BATCH_COLUMNS 순서). 권한은 dash 스키마와 같은 방식이다: 적재할 때마다 dash_reader(와
    readers의 다른 읽기 계정)에게 USAGE와 SELECT만 준다(초기화 스크립트에 기대지 않으므로 이미 있는 DB 볼륨에서도 된다).
    """
    from psycopg import sql
    from psycopg.types.json import Jsonb

    meta = sql.Identifier(META)
    marks = sql.Identifier(FINGERPRINT_TABLE)
    shot = sql.Identifier(SNAPSHOT_TABLE)
    cur.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(meta))
    cur.execute(sql.SQL("CREATE SCHEMA {}").format(meta))
    cur.execute(sql.SQL(
        "CREATE TABLE {}.{} (table_name text NOT NULL, row_key text NOT NULL, row_hash text NOT NULL, "
        "PRIMARY KEY (table_name, row_key))").format(meta, marks))
    cur.execute(sql.SQL(
        "CREATE TABLE {}.{} (id integer PRIMARY KEY CHECK (id = 1), loaded_at timestamptz NOT NULL DEFAULT now(), "
        "snapshot jsonb NOT NULL)").format(meta, shot))
    batch = sql.Identifier(BATCH_TABLE)
    cur.execute(sql.SQL(
        "CREATE TABLE {}.{} (table_name text NOT NULL, file text NOT NULL, sha256 text NOT NULL, "
        "added_at timestamptz, rows integer NOT NULL, inserted integer NOT NULL, updated integer NOT NULL, "
        "skipped integer NOT NULL, unchanged integer NOT NULL, PRIMARY KEY (table_name, file))").format(meta, batch))
    with cur.copy(sql.SQL("COPY {}.{} (table_name, row_key, row_hash) FROM STDIN").format(meta, marks)) as copy:
        for name, pairs in prints.items():
            for key, digest in pairs:
                copy.write_row((name, key, digest))
    with cur.copy(sql.SQL("COPY {}.{} ({}) FROM STDIN").format(
            meta, batch, sql.SQL(", ").join(map(sql.Identifier, BATCH_COLUMNS)))) as copy:
        for row in batches:
            copy.write_row(tuple(row))
    cur.execute(sql.SQL("INSERT INTO {}.{} (id, snapshot) VALUES (1, %s)").format(meta, shot), (Jsonb(dict(snap)),))
    cur.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(meta))
    for name in (reader, *readers):
        role = sql.Identifier(name)
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(meta, role))
        cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(meta, role))
