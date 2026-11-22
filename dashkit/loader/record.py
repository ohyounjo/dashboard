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
import hmac
import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

META = "dashkit_meta"
FINGERPRINT_TABLE = "row_fingerprint"
SNAPSHOT_TABLE = "load_snapshot"
BATCH_TABLE = "batch"
SNAPSHOT_VERSION = 1
FINGERPRINT_SCHEME = "hmac-sha256-v1"
FINGERPRINT_KEY_ENV = "PLAN_FINGERPRINT_KEY"
MIN_FINGERPRINT_KEY_LEN = 16
BATCH_COLUMNS = ("table_name", "file", "sha256", "added_at", "rows", "inserted", "updated", "skipped", "unchanged")


class FingerprintKeyError(ValueError):
    """행 지문 HMAC 비밀 키(PLAN_FINGERPRINT_KEY)가 없거나 유효하지 않음."""

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


def fingerprint_key(env: Mapping[str, str] | None = None) -> bytes:
    """환경변수 PLAN_FINGERPRINT_KEY에서 HMAC 비밀 키를 읽는다. 미설정·자리표시자·16자 미만이면 거부한다."""
    source = os.environ if env is None else env
    raw = (source.get(FINGERPRINT_KEY_ENV) or "").strip()
    if not raw or raw == "__GENERATE__":
        raise FingerprintKeyError(
            f"환경변수 {FINGERPRINT_KEY_ENV}가 설정되지 않았습니다(make env-secrets 또는 make env)")
    if len(raw) < MIN_FINGERPRINT_KEY_LEN:
        raise FingerprintKeyError(
            f"환경변수 {FINGERPRINT_KEY_ENV}는 {MIN_FINGERPRINT_KEY_LEN}자 이상이어야 합니다")
    return raw.encode("utf-8")


def try_fingerprint_key(env: Mapping[str, str] | None = None) -> bytes | None:
    """유효한 PLAN_FINGERPRINT_KEY가 있으면 바이트를, 없거나 유효하지 않으면 None을 돌려준다."""
    try:
        return fingerprint_key(env)
    except FingerprintKeyError:
        return None


def key_id(key: bytes) -> str:
    """비밀 키에서 단방향 파생한 16자 hex 식별자(키 원본은 노출하지 않고 회전 여부만 판별)."""
    return hmac.new(key, b"dashkit:fingerprint:key_id:v1", hashlib.sha256).hexdigest()[:16]


def _hmac_key(key: bytes, table_name: str, pk_values: Sequence[str | None]) -> str:
    payload = _dump(["v1", "row_key", table_name, list(pk_values)]).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _hmac_row(key: bytes, table_name: str, pairs: Sequence[tuple[str, str | None]]) -> str:
    payload = _dump(["v1", "row_hash", table_name, list(pairs)]).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def row_key(columns: Sequence[str], primary_key: Sequence[str], row: Sequence[Any],
            *, table_name: str = "", key: bytes | None = None) -> str:
    """기본 키의 HMAC-SHA256 지문(도메인·테이블 이름 분리). 원본 키 값은 기록에 남기지 않는다."""
    k = fingerprint_key() if key is None else key
    values = [canonical(row[columns.index(c)]) for c in primary_key]
    return _hmac_key(k, table_name, values)


def row_label(columns: Sequence[str], primary_key: Sequence[str], row: Sequence[Any]) -> str:
    """기본 키를 사람이 읽는 글로(복합 키는 ", "로 잇는다). CSV 쪽 행을 화면에 보여 줄 때만 쓰고 저장하지 않는다."""
    values = [canonical(row[columns.index(c)]) for c in primary_key]
    return ", ".join("" if v is None else v for v in values)


def row_hash(columns: Sequence[str], row: Sequence[Any],
             *, table_name: str = "", key: bytes | None = None) -> str:
    """행 HMAC-SHA256 지문. 열 이름순으로 (열, 정규 표현)을 직렬화하므로 열 순서와 무관하다."""
    k = fingerprint_key() if key is None else key
    pairs = sorted(zip(columns, (canonical(v) for v in row)))
    return _hmac_row(k, table_name, pairs)


def fingerprints(table: Mapping[str, Any], rows: Iterable[Sequence[Any]],
                 *, table_name: str = "", key: bytes | None = None) -> list[tuple[str, str]]:
    """테이블의 행들 → [(row_key, row_hash)]. table은 schema.yaml의 정규화된 테이블 정의, 행의 열 순서는 table["columns"]."""
    k = fingerprint_key() if key is None else key
    columns = list(table["columns"])
    pk = list(table["primary_key"])
    pk_idx = [columns.index(c) for c in pk]
    sorted_idx = sorted(range(len(columns)), key=columns.__getitem__)
    sorted_cols = [columns[i] for i in sorted_idx]
    out: list[tuple[str, str]] = []
    for row in rows:
        canon = [canonical(v) for v in row]
        rk = _hmac_key(k, table_name, [canon[i] for i in pk_idx])
        rh = _hmac_row(k, table_name, [(sorted_cols[j], canon[sorted_idx[j]]) for j in range(len(sorted_idx))])
        out.append((rk, rh))
    return out


def labels(table: Mapping[str, Any], rows: Iterable[Sequence[Any]],
           *, table_name: str = "", key: bytes | None = None) -> dict[str, str]:
    """{row_key(지문): 사람이 읽는 기본 키}. plan이 CSV 쪽 행(추가·변경)의 예시 키를 보여 주는 데 쓴다."""
    k = fingerprint_key() if key is None else key
    columns = list(table["columns"])
    pk = list(table["primary_key"])
    pk_idx = [columns.index(c) for c in pk]
    out: dict[str, str] = {}
    for row in rows:
        values = [canonical(row[i]) for i in pk_idx]
        rk = _hmac_key(k, table_name, values)
        out[rk] = ", ".join("" if v is None else v for v in values)
    return out


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


# DB의 내용과 상관없는 테이블·열 키(머리글 맞추기와 설명). 바꿔도 다시 만들 필요가 없으므로 지문에 넣지 않는다
DB_IRRELEVANT_KEYS = ("aliases", "description")


def db_relevant(table: Mapping[str, Any]) -> dict[str, Any]:
    """테이블 정의에서 DB의 내용을 정하는 부분만(aliases·description을 뺀다)."""
    out = {k: v for k, v in table.items() if k not in DB_IRRELEVANT_KEYS}
    out["columns"] = {c: {k: v for k, v in spec.items() if k not in DB_IRRELEVANT_KEYS}
                      for c, spec in table["columns"].items()}
    return out


def table_rules_hash(table: Mapping[str, Any]) -> str:
    """테이블 정의(정규화한 값: 열·규칙·방식·충돌 정책·default·checks. aliases·description은 빼고)의 해시."""
    text = json.dumps(db_relevant(table), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def data_revision(schema: Mapping[str, Any], dashboard: Mapping[str, Any]) -> str:
    """데이터 설정 지문: DB의 내용을 정하는 설정만으로 구한 해시(contracts/ingest.md "데이터 설정 지문").

    schema.yaml의 tables(열, 규칙, 방식, 충돌 정책, checks. aliases·description은 빼고)와 dashboard.yaml의
    datasets(뷰 정의: table·sql)·app.as_of. 탭·그래프 배치, 데이터셋의 지표·표시 이름, 머리글 맞추기(aliases)처럼
    DB와 상관없는 설정은 넣지 않는다.
    정규화한 설정(config.load_project의 결과)에서 구한다.
    """
    views = {k: {"table": ds["table"]} if "table" in ds else {"sql": ds.get("sql")}
             for k, ds in (dashboard.get("datasets") or {}).items()}
    doc = {"tables": {t: db_relevant(spec) for t, spec in schema["tables"].items()}, "datasets": views, "as_of": (dashboard.get("app") or {}).get("as_of")}
    text = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def next_replaced_projects(prev_snapshot: Mapping[str, Any] | None, current_project: str) -> list[str]:
    """지난 적재 스냅샷에서 현재 프로젝트를 제외한 전환 이력(순서 유지·중복 제거)을 계산한다."""
    if not isinstance(prev_snapshot, Mapping):
        return []
    out: list[str] = []
    raw_list = prev_snapshot.get("replaced_projects")
    if isinstance(raw_list, Sequence) and not isinstance(raw_list, (str, bytes)):
        for p in raw_list:
            if isinstance(p, str) and p and p != current_project and p not in out:
                out.append(p)
    prev_proj = prev_snapshot.get("project")
    if isinstance(prev_proj, str) and prev_proj and prev_proj != current_project and prev_proj not in out:
        out.append(prev_proj)
    return out


def snapshot(project_name: str, schema: Mapping[str, Any], dashboard: Mapping[str, Any],
             row_counts: Mapping[str, int], revision: str | None = None,
             sources: Mapping[str, Mapping[str, list[str]]] | None = None,
             replaced_projects: Sequence[str] | None = None,
             *, fingerprint_key_id: str | None = None) -> dict[str, Any]:
    """적재 스냅샷(JSON으로 저장). plan이 "구조가 바뀌었는지", 추가 적재가 "설정이 DB에 적용됐는지" 판단하는 데 쓴다."""
    doc: dict[str, Any] = {
        "version": SNAPSHOT_VERSION,
        "project": project_name,
        "data_revision": revision if revision is not None else data_revision(schema, dashboard),
        **({"replaced_projects": list(replaced_projects)} if replaced_projects else {}),
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
            key: {"kind": "table" if "table" in ds else "sql", "definition": definition_hash(ds),
                  **({"sources": {t: list(c) for t, c in sources.get(key, {}).items()}} if sources is not None else {})}
            for key, ds in dashboard["datasets"].items()
        },
    }
    if fingerprint_key_id is not None:
        doc["fingerprint"] = {"scheme": FINGERPRINT_SCHEME, "key_id": fingerprint_key_id}
    return doc


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
    reader_id = sql.Identifier(reader)
    cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(meta, reader_id))
    for tbl in (SNAPSHOT_TABLE, BATCH_TABLE):
        cur.execute(sql.SQL("GRANT SELECT ON TABLE {}.{} TO {}").format(meta, sql.Identifier(tbl), reader_id))
    for name in readers:
        if name == reader:
            continue
        role = sql.Identifier(name)
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(meta, role))
        cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(meta, role))
