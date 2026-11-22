# 테이블 색인 선언 및 스키마 진화(열 이름 변경·삭제·유형 좁히기) 설계 (#41, #43)

> 대상: [이슈 #41](https://github.com/ohyounjo/dashboard/issues/41) (`[6] 테이블 색인 선언`), [이슈 #43](https://github.com/ohyounjo/dashboard/issues/43) (`[6] 열 이름 변경·삭제·유형 좁히기`)
> 기준 커밋: `main` (`90b0fd6`)
> 상위 설계 기준: [데이터 설계서](data-design.md) 4-5, 6-2, 6-3, 6-4, 7-2, 8-2, 10-3, 14, 15절
> 관련 계약: [`contracts/schema.schema.json`](../contracts/schema.schema.json) · [`contracts/README.md`](../contracts/README.md) · [`contracts/ingest.md`](../contracts/ingest.md) · [`contracts/manage.md`](../contracts/manage.md) · [`contracts/studio.md`](../contracts/studio.md) · [`contracts/plan.md`](../contracts/plan.md)

---

## 1. 배경과 통합 설계 범위

6단계(후순위) 항목 중 스키마 정의(`schema.yaml`)와 구조 변경·로더 경로를 확장하는 두 이슈를 설계한다.

| 이슈 | 핵심 목표 | 난이도 / 영향도 | 설계 원칙 |
|---|---|---|---|
| **[#41 테이블 색인 선언](https://github.com/ohyounjo/dashboard/issues/41)** | 대용량 테이블에서 특정 필터·정렬 조건이 반복적으로 시간 제한(`MANAGE_STATEMENT_TIMEOUT_MS`)에 걸릴 때 `schema.yaml`에 `indexes: [[열, …]]`를 선언해 로더가 색인을 생성하도록 지원 | 하~중 / 낮음 | ① 선택 키 추가(`CONFIG_VERSION = 1` 유지)<br>② 쓰기 경로(`dash_owner` 로더·재생성)에서만 생성, 읽기 경로(`dash_inspect`, `dash_reader`) 자동 생성 금지<br>③ 데이터 적재 완료 후 색인 일괄 생성, 조회 결과 불변 |
| **[#43 열 이름 변경·삭제·유형 좁히기](https://github.com/ohyounjo/dashboard/issues/43)** | 옛 배치 파일에 이전 열 이름·삭제된 열·넓은 유형의 값이 남아 재생성이 실패하는 한계를 해결하기 위해, 배치 합치기(정규형 재작성)와 결합하여 열 이름 변경(`column.rename`), 열 삭제(`column.remove`), 유형 좁히기(`column.update` 확장)를 원자적으로 지원 | 상 / 높음 | ① 실행 전 영향받는 데이터셋·그래프와 되돌리기 제약(배치 합치기 동반) 사전 표시<br>② 변환 불가 값은 원본 위치와 이유를 보고하고 전체 작업 실패(무변경)<br>③ 설정·데이터 파일·배치 기록을 묶음 기록(`config`)으로 원자적 변경 및 끊김 복구<br>④ 변경 후 전체 `make apply`(`load`) 결과가 현재 DB와 100% 일치 |

- 두 이슈의 요구사항과 기존 `contracts/` 문서 사이에 충돌은 없다(기존 계약이 "열 이름 변경·삭제는 없다", "유형은 넓히기만"으로 막아 둔 4단계 제약을 6단계 설계서 6-2·6-4·15절에 맞춰 확장한다).
- 두 이슈는 공통으로 `contracts/schema.schema.json`, `dashkit/config.py`, `dashkit/project/ops.py`, `dashkit/loader/`, `dashkit/ingest/structure.py`, `web/admin/structure.js`를 수정하므로 본 설계서에서 함께 정의하되, 필요 시 #41 단독 PR과 #43 후속 PR로 분리 구현할 수 있도록 모듈 경계를 명확히 나눈다.

---

## 2. 이슈 #41: 테이블 색인 선언 (`indexes`) 상세 설계

### 2-1. `schema.yaml` 문법과 JSON Schema 계약 (`contracts/schema.schema.json`)

테이블 정의(`$defs.table.properties`)에 선택 키 `indexes`를 추가한다. 선택 키 추가이므로 설정 형식 버전(`CONFIG_VERSION = 1`)은 올리지 않는다.

```yaml
tables:
  entries:
    mode: append
    on_conflict: update
    primary_key: id
    columns:
      id:       { type: text }
      group_id: { type: text, ref: groups.id }
      state:    { type: enum, values: [open, done] }
      seen_on:  { type: date }
      amount:   { type: number }
    indexes:
      - [state, seen_on]
      - [amount]
```

- **`contracts/schema.schema.json` (`$defs.table.properties.indexes`):**
  ```json
  "indexes": {
    "description": "조회 필터·정렬용 보조 B-tree 색인 목록(선택). 각 항목은 열 이름(1개 이상, 중복 없음)의 배열이다. 로더가 데이터를 모두 넣은 뒤 쓰기 경로에서만 만든다.",
    "type": "array",
    "items": {
      "type": "array",
      "minItems": 1,
      "uniqueItems": true,
      "items": { "$ref": "#/$defs/ident" }
    }
  }
  ```
- **정규화 (`dashkit/config.py` `_normalize_schema`):**
  - `table.setdefault("indexes", [])`로 기본 빈 배열을 채운다. 각 항목은 `list(idx)`로 정규화한다.

### 2-2. 정적 참조 및 중복 검증 (`dashkit/config.py` `_check_schema_refs`)

JSON Schema 통과 후 `_check_schema_refs`에서 다음을 검사한다(오류 형식: `schema.yaml:tables.<테이블>.indexes[<i>]: <이유>`).

| 검사 항목 | 오류 조건 | 오류 메시지 형식 |
|---|---|---|
| 존재하지 않는 열 | `col not in table["columns"]` | `schema.yaml:tables.<t>.indexes[<i>][<j>]: 열 '<col>'가 columns에 없음` |
| 한 색인 안의 중복 열 | `len(set(idx)) != len(idx)` (JSON Schema `uniqueItems`가 1차 차단하며 방어적으로 재확인) | `schema.yaml:tables.<t>.indexes[<i>]: 같은 열 '<col>'가 두 번 있음` |
| 기본 키와 동일한 색인 | `list(idx) == list(table["primary_key"])` (PostgreSQL이 PK에 이미 고유 B-tree 색인을 생성함) | `schema.yaml:tables.<t>.indexes[<i>]: primary_key와 같은 색인 ([<열, …>])` |
| 중복 색인 선언 | `tuple(idx)`가 같은 테이블의 앞선 `indexes` 항목과 완전히 같음(열 순서가 다른 `[a, b]`와 `[b, a]`는 서로 다른 색인이므로 허용) | `schema.yaml:tables.<t>.indexes[<i>]: 중복된 색인 선언 ([<열, …>])` |

### 2-3. 로더의 색인 생성 및 명명 규칙 (`dashkit/loader/__init__.py`)

- **쓰기 경로 전용:** 색인 생성은 오직 `dash_owner` 계정으로 실행되는 `dashkit/loader/__init__.py`의 `_rebuild()` 트랜잭션 안에서만 수행한다.
  - `dashkit/paging.py`, `dashkit/manage/routes/rows.py`, `dashkit/api/data.py` 등 **어떠한 읽기 경로(`dash_inspect`, `dash_reader`)에서도 색인을 자동 생성하거나 DDL을 실행하지 않는다.**
- **생성 시점 (적재 후 일괄 생성):**
  - `_rebuild()`에서 `CREATE TABLE` 직후가 아니라 **해당 테이블의 모든 데이터(`replace` CSV `COPY` 또는 `append` 배치들의 `apply_batch` 루프)를 적재한 직후**에 `CREATE INDEX`를 실행한다. 대량 행 적재 후 B-tree를 한 번에 빌드하는 편이 행마다 색인을 갱신하는 것보다 빠르고 조각화가 적다.
  - 이후 추가 적재(`dashkit/ingest/apply.py`의 온라인 `ingest`)에서는 PostgreSQL이 테이블에 이미 만들어진 색인을 자동으로 유지한다.
  - 빈 DB에 최초 `load`할 때와 `rebuild_locked`(구조 변경, 배치 되돌리기·되살리기, 재적재)로 재생성할 때 모두 같은 `_rebuild()`를 타므로 **언제나 동일한 색인 집합**이 만들어진다.
- **결정적(deterministic) 색인 이름:**
  - PostgreSQL 식별자 길이 한도(63바이트)를 절대 넘지 않도록 `_index_name(tname: str, i: int, cols: Sequence[str]) -> str`로 이름을 만든다.
  - 기본 형식: `ix_{tname}_{i}_{"_".join(cols)}` (`i`는 `schema.yaml`의 `indexes` 0-기반 번호).
  - 길이가 63자를 넘으면 앞부분을 자르고 `hashlib.sha1(f"{tname}:{i}:{','.join(cols)}".encode()).hexdigest()[:8]`을 붙여 `f"{prefix[:54]}_{digest}"`(63자 이하)로 결정적 유일성을 보장한다.
- **생성 SQL (`_create_index_sql`):**
  ```python
  def _create_index_sql(tname: str, index_no: int, cols: Sequence[str]) -> sql.Composed:
      idx_name = _index_name(tname, index_no, cols)
      return sql.SQL("CREATE INDEX {} ON {}.{} ({})").format(
          sql.Identifier(idx_name),
          sql.Identifier(RAW),
          sql.Identifier(tname),
          sql.SQL(", ").join(map(sql.Identifier, cols)),
      )
  ```
  - 모든 식별자는 검증된 이름에 `psycopg.sql.Identifier`만 사용한다.
  - `_step(cur, f"schema.yaml:tables.{tname}.indexes[{i}]: 색인 생성", _create_index_sql(tname, i, idx))`로 실행한다.

### 2-4. 적재 기록(`data_revision`), 설정 연산, 관리 API 연동

1. **`data_revision` 포함 (`dashkit/loader/record.py`):**
   - `indexes`는 DB의 `raw` 스키마에 실제 색인 객체를 생성해야 반영되므로 `DB_IRRELEVANT_KEYS`에 넣지 않고 **`db_relevant()`(`data_revision` 및 `table_rules_hash`)에 포함**한다.
   - 효과: `schema.yaml`에서 `indexes`만 수정하고 아직 `make apply`하지 않은 상태에서는 `data_revision` 불일치로 감지되어 `make plan`이 변경을 알리고, 온라인 적재 전에 적용을 요구해 DB와 `schema.yaml`의 색인 상태가 어긋나지 않는다.
2. **설정 변경 연산 (`dashkit/project/ops.py`):**
   - `TABLE_KEYS`에 `"indexes"` 추가(`table.add`에서 초기 색인 선언 가능).
   - `TABLE_UPDATE_KEYS`에 `"indexes"` 추가(`table.update`의 `set: {"indexes": [...]}` 또는 `null`로 색인 목록 변경·삭제 가능).
3. **관리 서비스 API (`dashkit/manage/routes/tables.py`):**
   - `GET /api/manage/tables/{table}` 응답에 `"indexes": [list(idx) for idx in spec.get("indexes", [])]`를 포함한다.

---

## 3. 이슈 #43: 열 이름 변경·삭제·유형 좁히기 상세 설계

### 3-1. 문제 정의와 핵심 해결 원리

현재 구조에서 열 이름 변경, 열 삭제, 유형 좁히기를 단순히 `schema.yaml`만 고치고 `load`하면 실패하는 이유는 다음과 같다.

1. **`append` 테이블:** `data/<테이블>/` 아래의 과거 정규형 배치 파일(`000001-….csv.gz` 등) 머리글에 **이전 열 이름**이나 **삭제된 열**이 남아 있어 로더의 `strict=True` 머리글 검사(`schema.yaml에 없는 열`, `기본 키 열이 머리글에 없음`)에서 즉시 거절된다.
2. **`replace` 테이블:** `data/<테이블>.csv` 머리글이 `schema.yaml`의 열 집합과 정확히 같아야 하므로 CSV 파일을 함께 재작성해야 한다.
3. **유형 좁히기(예: `text` → `integer`):** 기존 행의 값이 `"1,000"`(천 단위 쉼표)이거나 `"2026/11/20"`처럼 다듬기(`normalize`)를 거쳐야 하는 표기이거나, 아예 새 유형으로 변환할 수 없는 값(`"abc"`)일 수 있다.

**해결 원리 (설계서 6-2, 6-4절):**
- 열 이름 변경·열 삭제·유형 좁히기 연산이 포함되면, 현재 테이블의 데이터를 읽어 **새 스키마 구조의 정규형 파일 하나로 변환·합치기(compaction + rewrite)**하고, 설정 파일(`schema.yaml`, 필요 시 `dashboard.yaml`)과 데이터 파일(`data/<t>.csv` 또는 `data/<t>/<번호>-<시각>-merged.csv.gz`)을 **`config` 묶음 기록과 단일 `load` 트랜잭션으로 원자적으로 교체**한다.

### 3-2. 설정 변경 연산 확장 (`dashkit/project/ops.py`, `contracts/studio.md`)

`schema.yaml` 열 조작을 위해 2개 연산을 신설하고 `column.update`의 유형 변경 규칙을 확장한다.

| 연산 | 필드 | 동작 및 제약 |
|---|---|---|
| `column.rename` (신규) | `table`, `column`(이전 이름), `to`(새 이름) | - `table["columns"]`에서 `column`의 **열 자리(순서)를 그대로 유지**한 채 키만 `to`로 바꾼다.<br>- `to`가 이미 존재하거나 SQL 예약어이거나 식별자 규칙(`^[a-z][a-z0-9_]{0,62}$`)에 맞지 않으면 거부.<br>- **기본 키 열은 이름 변경 불가**(`column in table["primary_key"]`이면 400 `invalid_op`: "기본 키 열은 이름을 바꿀 수 없음").<br>- 다른 테이블의 `ref`가 `<table.column>`을 가리키지 않아야 함(단일 열 PK만 `ref` 대상이므로 PK 변경 금지로 자연 차단).<br>- 같은 테이블의 `indexes` 안에 `column`이 있으면 그 자리의 열 이름을 `to`로 함께 갱신한다.<br>- 같은 테이블의 다른 열 `aliases`에 `to`가 들어 있으면 제거한다. |
| `column.remove` (신규) | `table`, `column` | - `table["columns"]`에서 `column`을 삭제한다.<br>- **기본 키 열은 삭제 불가**(`column in table["primary_key"]`이면 400 `invalid_op`: "기본 키 열은 지울 수 없음").<br>- 마지막 남은 열은 삭제 불가.<br>- 같은 테이블의 `indexes`에서 `column`을 포함한 단일 열 색인은 제거하고, 복합 색인에서 `column`을 뺀 뒤 중복/빈 색인이 되면 정리한다. |
| `column.update` (확장) | `table`, `column`, `set: {type?, values?, min?, max?, pattern?, default?, required?, description?, aliases?}` | - 기존 넓히기(`_WIDER`)에 더해 **유형 좁히기(`_NARROWER`)**를 허용한다.<br>- **기본 키 열은 유형 좁히기 불가**(넓히기도 기존 UI와 동일하게 차단하거나 기본 키 유일성 변형 방지를 위해 유형 변경에서 제외).<br>- 참조 열(`ref`가 있는 열) 또는 다른 테이블이 `ref`로 가리키는 열은 대상 열과 `type`이 같아야 하므로 단독 유형 변경 불가(`config.py` 참조 검사).<br>- `type`을 바꿀 때 새 유형에 허용되지 않는 이전 속성(예: `enum` → 비-`enum` 시 `values`, 숫자가 아닌 유형으로 갈 때 `min`·`max`, `text`·`enum`이 아닌 유형으로 갈 때 `pattern`)은 `set`에서 `null`로 지우거나 자동 정리한다. |

#### 지원하는 유형 변환 매트릭스 (`_WIDER` 및 `_NARROWER`)

| 변환 구분 | 출발 유형 → 도착 유형 | 변환·검증 규칙 |
|---|---|---|
| **넓히기 (`_WIDER`, 기존)** | `integer` → `number`<br>모든 유형 → `text` | 정규형 표현이 그대로 유효함. `text`로 넓힐 때도 기존 배치 파일을 새 스키마로 읽을 수 있으나, 일관성을 위해 그대로 두거나 합치기와 결합 가능. |
| **좁히기 (`_NARROWER`, 신규)** | `text` → `integer`, `number`, `boolean`, `date`, `timestamp`, `enum` | 각 셀의 문자열에 `normalize.normalize(raw, target_type)`(앞뒤 공백, 천 단위 쉼표, `Y`/`N`·`1`/`0`, `YYYY/MM/DD` 등 뜻이 하나인 표기 다듬기)를 적용한 뒤 `validate._parse` 및 `_check_value`(`values`, `min`, `max`, `pattern`)로 검증하고 정규형(`record.canonical`)으로 기록한다. |
| | `number` → `integer` | 소수부가 0인 유한 숫자(예: `10`, `10.0`, `10.00`, `1,000.0`)만 정수로 변환(`d == int(d)` 및 64비트 정수 범위). `10.5`처럼 소수부가 있으면 변환 불가 오류로 거절한다. |
| | `enum` → `integer`, `number`, `boolean`, `date`, `timestamp` | `text`에서 좁히는 것과 동일(기존 enum 값들이 모두 목표 유형으로 파싱·검증되어야 함). |
| | `timestamp` → `date` | ISO 8601 시각에서 날짜 부분(`dt.date().isoformat()`)으로 변환 후 `min`·`max` 검사. |
| **미지원 (`invalid_op`)** | `date` ↔ `integer`/`number`/`boolean`<br>`boolean` → `date`/`timestamp`<br>`integer`/`number` → `date`/`timestamp` | 의미가 모호한 변환이므로 400 `invalid_op`로 거절한다. |

- **Studio에서의 제한 (`contracts/studio.md`):**
  - `column.add`(replace 테이블)와 마찬가지로, **`column.rename`, `column.remove`, 그리고 유형을 좁히는 `column.update`는 데이터 파일(CSV 또는 배치 폴더)을 함께 변환·합치기해야 하므로** YAML만 고치는 Studio의 `PATCH /studio/api/projects/{p}/config`에서는 400 `invalid_op`("데이터 파일을 함께 변환해야 하는 연산입니다. 운용 화면에서 실행하세요")로 거절한다.
  - 관리 서비스(`PATCH /api/manage/config` 및 `dashkit.ingest.structure.change_config`)가 설정과 데이터 파일 변환을 한 번에 처리한다.

### 3-3. 사전 영향 분석과 되돌리기 제약 확인 (완료 조건 2)

열 이름 변경·열 삭제·유형 변경을 실행하기 전에 운용자에게 **영향받는 데이터셋·그래프·필터·규칙(`checks`)**과 **되돌리기 제약**을 명확히 계산해 보여 준다.

#### 1) 영향 범위 계산 (`dashkit/ingest/lineage.py` 확장 및 `GET /api/manage/tables/{t}/column_impact`)

`lineage.column_impact(project, graph, table, column, *, op="remove" | "rename" | "retype", to=None)` 순수 함수를 추가하고, 관리 서비스에 조회 경로 또는 `PATCH /config` 사전 검사·응답을 둔다.

| 영향 대상 | 판정 방법 | 처리 방식 |
|---|---|---|
| **`table:` 데이터셋 (`datasets.<ds>.table == table`)** | - `datasets.<ds>.labels`에 `column`이 있는지<br>- 그 데이터셋을 쓰는 차트(`chart_registry.column_refs`), 대시보드 필터(`flt.column`), 위젯 섹션(`widget.column_refs`)이 `column`을 참조하는지 | - **`column.rename`:** 같은 `PATCH /config` 요청에 `dataset.update` 등 후속 연산이 없어도, 옵션을 켜거나 자동으로 **`table:` 데이터셋의 `labels`·차트·필터·위젯 섹션에서 `column` 참조를 `to`로 함께 갱신**할 수 있게 지원하거나, 참조가 남아 있으면 `409 in_use`(또는 `422 invalid_config`)로 정확한 위치 목록을 알린다.<br>  *(설계 결정: `table:` 데이터셋에 연결된 `labels`는 `column.rename` 시 자동 치환 옵션을 제공하고, 차트·필터·위젯·`sql:` 뷰·`checks` 참조는 목록으로 명확히 표시하며 깨지는 참조가 있으면 `422 invalid_config` / `409 in_use`로 차단해 반쪽 변경을 막는다.)*<br>- **`column.remove`:** 차트·필터·위젯이 해당 열을 참조 중이면 **409 `in_use`**(`errors`: `["차트 <키> (<필드>)", "필터 <대시보드>.<키>", "섹션 <탭>#<번호>"]`)로 삭제를 거절한다(먼저 차트/필터를 고치게 함). `table:` 데이터셋의 `labels.<column>`만 있는 경우에는 `labels`에서 함께 제거한다. |
| **`sql:` 데이터셋 (`datasets.<ds>.sql`)** | 적재 스냅샷 계보 `graph["datasets"][ds]["sources"].get(table)`에 `column`이 포함되어 있는지 (`sources is None`이면 `unknown_datasets`로 표시) | - 해당 `sql:` 데이터셋과 그 데이터셋을 쓰는 차트·위젯·탭을 영향 목록(`affected`)에 표시한다.<br>- 만약 `column.rename`이나 `column.remove`로 인해 `sql:` 뷰의 `CREATE VIEW`가 실패하면 `load` 단계의 뷰 생성에서 잡혀 **모든 파일과 배치 합치기가 변경 전으로 100% 자동 롤백**되고 422 `load_failed`로 쿼리 오류를 알린다. |
| **테이블 `checks` (`tables.<t>.checks`)** | `checks`는 임의 SQL이므로 영향 안내에 테이블의 `checks` 이름 목록을 표시하고, 실제 실행 시 `run_checks`가 검증(실패 시 전체 롤백). | `CREATE VIEW`와 동일하게 `load` 트랜잭션 안에서 검증된다. |

#### 2) 되돌리기 제약 (배치 합치기 동반 안내)

`append` 테이블에서 `column.rename`, `column.remove`, 또는 유형 좁히기(`column.update`)를 실행하면 다음 제약이 발생한다.

1. **개별 배치 되돌리기 불가:** 기존 배치 파일들이 새 구조의 단일 합친 배치(`<다음 번호>-<UTC 시각>-merged.csv.gz`)로 합쳐지므로, 변경 이전의 개별 배치를 하나씩 되돌릴 수 없다(`DELETE /tables/{t}/batches/{file}`은 합친 배치에 대해 409 `merged_batch`).
2. **설정 이력(`GET /history`) 되돌리기 제약:**
   - **`replace` 테이블:** `data/<테이블>.csv` 원본 사본이 `.state/history/<프로젝트>/<이력 id>/before/data/<테이블>.csv`에 함께 저장되므로 **설정 이력 되돌리기(`POST /history/{id}/restore`)로 열 이름·삭제된 열의 값·이전 유형 표현까지 완벽히 되돌릴 수 있다.**
   - **`append` 테이블:** 배치 파일들은 설정 이력(`.state/history/`)에 복사하지 않는다(설계서 6-3절: 수백 MB 배치 폴더 복제 방지). 따라서 `append` 테이블에서 열을 삭제하거나 유형을 좁혀 합친 뒤에는 **설정 이력 되돌리기(`POST /history/{id}/restore`)로 삭제된 열의 데이터나 합치기 전 개별 배치를 되살릴 수 없다.**
   - 이를 위해 `history`의 `meta.json`에 `"irreversible": true`(또는 `"reason": " 테이블 <t>의 배치를 새 구조로 합쳐 되돌릴 수 없습니다"`)를 기록하고, `POST /history/{id}/restore` 호출 시 409 `irreversible_history`("배치 합치기를 동반한 구조 변경(열 이름 변경·삭제·유형 좁히기)은 이전 판으로 되돌릴 수 없습니다")로 명확히 거절하며 운용 화면의 설정 이력 목록에서도 `[되돌리기 불가]` 배지를 표시한다.

### 3-4. 값 변환·검증 및 변환 불가 값 위치 보고 (완료 조건 3)

테이블 데이터를 새 스키마로 변환할 때 **어느 한 행, 한 칸이라도 변환하거나 새 규칙을 통과하지 못하면 전체 작업을 실패(422 `invalid_rows` 또는 `load_failed`)시키고 어떤 파일도 바꾸지 않는다.**

#### 1) 원본 기준 위치 추적 및 변환 파이프라인

변환 불가 값의 위치를 운용자가 정확히 찾을 수 있도록 다음과 같이 읽고 검증한다.

- **`replace` 테이블 (`data/<테이블>.csv`):**
  - `data/<테이블>.csv`를 줄 번호(머리글 = 1행)와 함께 읽는다.
  - 각 행에 대해:
    - 유지되는 열: 기존 값 그대로(규칙이 좁혀졌으면 새 규칙 검사).
    - 이름 변경 열(`old_col → new_col`): 머리글만 `new_col`로 바꾸고 행의 값을 그대로 유지(함께 규칙·유형이 바뀌었으면 변환·검사).
    - 삭제된 열(`removed_col`): 머리글과 각 행의 해당 칸에서 제거.
    - 유형이 좁혀진 열(`col: old_type → new_type`):
      1. 빈 칸(`""`)이면 `required` 여부 확인(`required: true`이면 `값이 필요함`).
      2. 비어 있지 않으면 `normalize.normalize(raw, new_type, counts)`로 다듬는다(`number → integer`에서 `"10.00"`은 `"10"`으로, `"10.5"`는 그대로 두어 정수 파싱 오류 유발).
      3. `validate._parse(norm_val, new_spec)`으로 파싱하고 `validate._check_value(parsed, norm_val, new_spec)`으로 `values`·`min`·`max`·`pattern`을 검사한다.
      4. 성공하면 `record.canonical(parsed)` 문자열을 새 CSV 칸에 쓴다.
      5. 실패하면 **`data/<테이블>.csv:<줄 번호>:<열 이름>: <이유>`** 형식으로 오류를 수집한다(파일당 최대 200개 수집 + 총 `error_count` 집계).
- **`append` 테이블 (`data/<테이블>.csv` + `data/<테이블>/<배치들>`):**
  - **전제 확인:** 쓰기 잠금(`locked`) 안에서 `session.diff[tname]`을 확인한다. 반영되지 않았거나 바뀐 배치 파일이 있으면 합치기와 동일하게 409 `rebuild_required`로 먼저 거절한다.
  - **1단계 — 원본 배치별 위치 추적 검증(오류 위치 정확성 보장):**
    - 만약 DB의 현재 행(`raw.<t>`)에서만 변환을 시도하면, 실패한 값이 **어느 배치 파일의 몇 번째 줄**에서 왔는지 알거나 아니면 기본 키로만 보고해야 한다. 또한 `update` 정책이 아닌 테이블이나 과거 배치 파일에 있던 값의 출처를 분명히 하기 위해 두 가지 위치를 모두 활용한다:
      - 현재 DB에 반영된 최종 유효 행(`SELECT <cols> FROM raw.<t> ORDER BY <pk>`)을 기준으로 변환을 수행하되, 변환 불가 값이 발견되면 **`<테이블> (기본 키 <pk_label>):<열 이름>: <이유>`** 에 더해 원본 배치 파일들을 스캔해 해당 기본 키의 값을 마지막으로 넣은 **`data/<테이블>/<배치 파일>:<줄 번호>:<열 이름>: <이유>`** 위치를 함께 찾아 보고한다.
      - 이로써 `update` 정책으로 과거 배치의 잘못된 값이 나중 배치에서 이미 정상 값으로 덮어써진 경우에는 불필요하게 실패하지 않으면서(현재 유효한 행만 합친 배치에 남으므로), 현재 유효한 행의 값이 변환 불가할 때는 **정확한 파일·줄·기본 키와 이유**를 모두 알려 준다.
  - **2단계 — 새 정규형 합친 배치 파일 생성 (`data/_pending/incoming/`):**
    - 모든 행이 변환·검증을 통과하면 기본 키 오름차순으로 새 스키마의 모든 열 머리글을 가진 정규형 `.csv.gz` 임시 파일(`CanonicalWriter`)을 `data/_pending/incoming/`에 생성하고 `sha256`과 행 수를 구한다.
    - 만약 테이블에 행이 0건이고 배치 파일도 0개이면 빈 배치 파일을 만들지 않고 설정(`schema.yaml`)만 갱신한다. 배치 파일이 1개 이상 있었던 `append` 테이블은(1개였더라도 옛 머리글·옛 값을 새 스키마로 덮어써야 하므로) 새 합친 배치 파일(`<다음 번호>-<시각>-merged.csv.gz`) 하나로 교체한다.

### 3-5. 원자적 파일·데이터 변경과 끊김 복구 (완료 조건 4, 5)

열 이름 변경·삭제·유형 좁히기는 설정 파일(`schema.yaml`, `dashboard.yaml`)과 데이터 파일(`replace` CSV 또는 `append` 배치 폴더)을 **동시에** 바꾸므로, 기존 `config` 묶음 기록과 `compact` 묶음 기록의 장점을 결합해 **`config` 묶음 기록 하나로 원자적 변경과 끊김 복구를 수행**한다.

#### 1) 묶음 기록(`kind: "config"`) 스키마 확장 (`dashkit/ingest/structure.py`)

기존 `config` 묶음 기록에 선택 필드 `compactions`와 `irreversible`을 추가한다.

```json
{
  "id": "<묶음 id>",
  "kind": "config",
  "project": "devtracker",
  "data_revision": "<새 설정의 data_revision>",
  "history": "<이력 id>",
  "action": "column-rename",
  "loaded_at": "<작업 전 적재 시각 ISO | null>",
  "irreversible": true,
  "files": [
    {
      "path": "schema.yaml",
      "before": "<변경 전 base64>",
      "after": "<새 내용 sha256>"
    }
  ],
  "compactions": [
    {
      "table": "entries",
      "name": "000008-20261122T1300Z-merged.csv.gz",
      "incoming": "tmp_abc123.csv.gz",
      "sha256": "<새 합친 배치의 내용 해시>",
      "rows": 1250,
      "merged": "_merged/20261122T130000Z",
      "olds": [
        { "name": "entries.csv", "first": true },
        { "name": "000001-20261120T0910Z.csv.gz", "first": false }
      ]
    }
  ]
}
```

- `replace` 테이블의 열 이름 변경·삭제·유형 좁히기는 변환된 새 CSV 바이트를 기존 `files` 배열(`path: "data/<테이블>.csv"`, `before: <변경 전 base64>`, `after: <새 sha256>`)에 그대로 넣는다.
- `append` 테이블의 열 이름 변경·삭제·유형 좁히기는 `compactions` 배열에 합치기 이동 정보를 넣는다.

#### 2) 실행 순서 (`change_config` 잠금 안)

```
1. 쓰기 잠금(locked) + store.project_lock 획득 → revision 확인
2. 남은 묶음 기록 복구(recover) 및 원본 파일 ↔ 배치 기록 대조(reconcile)
   - 재작성 대상 append 테이블이 clean하지 않으면 409 rebuild_required 거절
3. 새 설정(schema.yaml, dashboard.yaml) 생성 및 정적 설정 검증
4. 데이터 변환 및 임시 파일 준비(잠금 안, 파일 교체 전):
   - replace 테이블: 변환된 CSV 바이트 생성 및 validate_csv 검증(참조 무결성 포함)
   - append 테이블: raw.<t> 행을 새 스키마로 변환·검증하여 _pending/incoming/<임시>.csv.gz 작성
   - 변환 불가 값이 하나라도 있으면 임시 파일을 지우고 422 invalid_rows(+ 위치·이유 errors)로 즉시 중단(원본 파일·DB 무변경)
5. 묶음 기록(data/_pending/<id>.json, kind: "config") 기록 및 fsync
6. 파일 교체:
   - YAML 및 replace CSV 원자적 교체(_replace_atomic)
   - append 테이블(compactions): 옛 배치들을 data/<t>/_merged/<시각>/으로 이동(os.replace)하고
     _pending/incoming/의 새 합친 배치를 data/<t>/<번호>-<시각>-merged.csv.gz로 이동
7. 같은 접속·한 트랜잭션에서 rebuild_locked(new_project, conn) 실행 → COMMIT
   - 새 raw 테이블(색인 포함) 생성 → 새 CSV/합친 배치 적재 → dash 뷰 생성 → 계보 기록 → checks 실행 → 뷰 열 참조 검사 → dashkit_meta(load_snapshot, batch, row_fingerprint) 갱신
   - 이 단계에서 rebuild_locked가 방금 만든 새 정규형 파일들을 처음부터 다시 읽어 적재하므로,
     "변경 후 전체 apply 결과가 DB와 같은지"가 구조적으로 100% 보장된다!
8. load가 실패(checks 위반, sql: 뷰 오류 등)하면:
   - DB 트랜잭션은 자동 롤백됨
   - recover_config를 즉시 실행해 YAML, replace CSV,そして _merged/<시각>/으로 옮긴 옛 배치들을 모두 원래 자리로 되돌리고 새 합친 배치 파일을 삭제함 → 422 load_failed 반환
9. load가 커밋되면:
   - 설정 이력(_history_from_record) 기록 → 묶음 기록 삭제 → clear_merged(project)로 _merged/ 정리 → (잠금 밖) Superset sync
```

#### 3) 끊김 복구 규칙 (`recover_config` 확장)

6~9단계 도중 어느 지점(`after_move`, `before_commit`, `after_commit` 등)에서 프로세스가 죽더라도 `recover_before_read` 및 `recover`가 다음 두 상태로 완벽히 수렴시킨다.

| 남은 `config` 기록의 상태 | 복구 동작 (`recover_config`) |
|---|---|
| **커밋 완료 상태**<br>(`DB loaded_at != record.loaded_at` **AND** `DB data_revision == record.data_revision` **AND** 모든 `files`가 새 판 **AND** 모든 `compactions`의 새 합친 배치 파일이 존재하고 해시 일치) | - 아직 `_merged/`로 옮기지 못한 옛 배치가 있으면 마저 옮기고 `clear_merged`로 정리한다.<br>- 설정 이력(`.state/history/`)이 아직 쓰이지 않았으면 기록에서 마저 쓴다.<br>- 묶음 기록을 지운다 (`Outcome(..., result="committed")`). |
| **커밋 전 중단 상태**<br>(위 조건 중 하나라도 불충족: 파일 이동 중 중단, `load` 중 중단, `COMMIT` 전 중단, 다른 프로젝트 적재 등) | - **1) `compactions` 역복구:** 각 테이블의 `_merged/<시각>/`에 옮겨진 옛 배치 파일(과 첫 배치 `data/<t>.csv`)을 원래 자리로 `os.replace`로 되돌리고, 새로 만든 합친 배치 파일(`final` 및 `incoming`)을 삭제한 뒤 빈 `_merged/<시각>/` 폴더를 지운다.<br>- **2) `files` 역복구:** `schema.yaml`, `dashboard.yaml`, `replace` CSV를 `before`(base64) 사본으로 원자적으로 되돌린다(새로 만든 파일은 삭제).<br>- 묶음 기록을 지운다 (`Outcome(..., result="discarded")`). |

이 설계의 핵심 장점은 **`rebuild_locked(new_project, conn)`을 파일 교체 직후 같은 잠금 안에서 실행**하기 때문에:
1. 커밋에 성공했다는 것 자체가 곧 **"새로 쓰인 정규형 데이터 파일들로 `make apply`(`load`)를 처음부터 돌린 결과가 성공했고 지금 DB가 바로 그 결과물임"**을 뜻한다.
2. 별도의 인메모리 DB 수정과 파일 쓰기 사이의 불일치 가능성이 원천적으로 제거된다.

### 3-6. 운용 화면 UI 설계 (`web/admin/structure.js`, `web/shared/structure.js`, `web/admin/menus/tables.js`, `web/strings.js`)

운용 화면(`#table/<이름>`의 **"구조"** 탭, admin 전용)에서 직관적이고 안전하게 스키마 진화를 실행할 수 있도록 기존 **"규칙 고치기"** 카드(`columnRuleCard`)와 열 동작을 확장한다.

1. **`GET /api/manage/tables/{t}/column_impact?column=<c>` (또는 `used_by` + 클라이언트 순수 계산):**
   - `GET /api/manage/tables/{t}/used_by` 응답에 `column_refs: {<열 이름>: {"datasets": [...], "charts": [{"key", "title", "dashboard", "tabs"}], "filters": [{"dashboard", "key", "label"}], "sections": [{"tab", "index", "type"}], "labels": [<데이터셋 키>]}}`를 함께 내려주어, 추가 왕복 없이 열별 영향 범위를 즉시 표시한다.
2. **열 규칙·이름 변경 카드 (`columnRuleCard` 확장):**
   - 기본 키가 아닌 열은 카드 상단에 **"열 이름"** 입력 칸(기본값: 현재 `col.name`)을 활성화한다(기본 키 열은 읽기 전용 + `"기본 키 열은 이름을 바꾸거나 지울 수 없습니다"` 도움말).
   - **"유형"** 선택 드롭다운에 넓히기 후보(`St.widenTargets(col.type)`)뿐 아니라 **좁히기 후보(`St.narrowTargets(col.type)`)**도 함께 표시한다(참조 `ref` 열이나 기본 키 열은 유형 변경 비활성화).
   - 이름 변경 또는 유형 변경이 감지되면:
     - **영향받는 데이터셋·그래프·필터 상자(`columnImpactBox`)**를 표시한다.
     - 이름 변경이거나 유형 좁히기이고 테이블이 `append` 방식이면 **되돌리기 제약 경고 상자**(예: `"이 변경은 기존 배치를 새 구조의 파일 하나로 합칩니다. 합친 뒤에는 이전 배치를 하나씩 되돌리거나 이 변경을 설정 이력에서 되돌릴 수 없습니다."`)와 확인 체크박스(`"영향받는 그래프와 되돌리기 제약을 확인했습니다"`)를 필수로 요구한다.
   - 변환 불가 값이 있어 서버가 422 `invalid_rows` / `load_failed`를 돌려주면 `configFail` 상자에 파일·줄·열·이유 목록을 그대로 보여 주고 `"파일과 데이터는 변경 전 그대로입니다"` 안내를 표시한다.
3. **열 삭제 카드 (`dropColumnCard` 신규, 또는 구조 탭의 "삭제" 버튼):**
   - 기본 키가 아니고 테이블에 열이 2개 이상일 때 각 열 행에 **"삭제"** 버튼(`small danger`)을 둔다.
   - 클릭 시 `dropColumnCard(t, col)`을 열어:
     - 그 열을 참조하는 차트·필터·위젯이 있으면(`blocking` 참조) **삭제 버튼을 비활성화**하고 정리해야 할 차트·필터·섹션 목록을 먼저 보여 준다.
     - 차단 참조가 없으면 영향받는 데이터셋(`table:` 뷰, `sql:` 뷰)과 **되돌리기 제약 경고**(특히 `append` 테이블의 배치 합치기 및 열 데이터 영구 삭제 안내)를 보여 주고, 확인용 열 이름 입력(또는 확인 체크) 후 `PATCH /api/manage/config` `{ops: [{op: "column.remove", table: t.name, column: col.name}], revision}`을 실행한다.

---

## 4. 모듈별 변경 파일 범위

### 4-1. 계약 문서 (`contracts/`)
1. [`contracts/schema.schema.json`](../contracts/schema.schema.json): `$defs.table.properties`에 `indexes` 스키마 추가.
2. [`contracts/README.md`](../contracts/README.md): 정적 검사 표에 `schema indexes`(존재하지 않는 열, 한 색인 내 중복 열, 기본 키와 동일한 색인, 중복 색인 선언) 추가.
3. [`contracts/ingest.md`](../contracts/ingest.md):
   - "배치 기록과 데이터 설정 지문": `data_revision`에 `indexes` 포함 명시.
   - "구조 변경": `column.rename`, `column.remove`, `column.update` 유형 좁히기 규칙, 배치 합치기 결합 동작, 변환 불가 값 실패 처리, `config` 묶음 기록의 `compactions` 복구 절차와 `append` 테이블 합치기 이력의 되돌리기 제약 명시.
4. [`contracts/manage.md`](../contracts/manage.md):
   - `GET /tables/{t}` 응답에 `indexes` 추가.
   - `GET /tables/{t}/used_by` 응답에 열 단위 참조(`column_refs`) 추가.
   - `PATCH /config` 허용 연산에 `column.rename`, `column.remove` 추가 및 유형 좁히기·영향 목록·409 `in_use`·409 `irreversible_history` 명세.
5. [`contracts/studio.md`](../contracts/studio.md):
   - 변경 연산 표에 `table.add`/`table.update`의 `indexes` 지원 및 `column.rename`·`column.remove`·유형 좁히기의 Studio 단독 저장 거절(관리 서비스 전용) 명시.

### 4-2. 백엔드 (`dashkit/`)
1. [`dashkit/config.py`](../dashkit/config.py):
   - `_normalize_schema`: `table.setdefault("indexes", [])` 정규화.
   - `_check_schema_refs`: `_check_indexes(f, base, table)` 추가(없는 열, 색인 내 중복 열, PK와 동일한 색인, 중복 색인 검사).
2. [`dashkit/loader/__init__.py`](../dashkit/loader/__init__.py):
   - `_index_name(tname, index_no, cols)` 및 `_create_index_sql(tname, index_no, cols)` 추가.
   - `_rebuild()`에서 각 테이블의 데이터 적재(`COPY` 또는 `apply_batch` 루프) 직후 선언된 `indexes`를 순회하며 `CREATE INDEX` 실행(쓰기 경로 `dash_owner` 전용).
3. [`dashkit/loader/record.py`](../dashkit/loader/record.py):
   - `db_relevant()`가 `indexes`를 유지하는지 확인(`DB_IRRELEVANT_KEYS`에 넣지 않음).
4. [`dashkit/project/ops.py`](../dashkit/project/ops.py):
   - `TABLE_KEYS`, `TABLE_UPDATE_KEYS`에 `"indexes"` 추가(`_check_indexes_op` 형식 검사).
   - `_NARROWER` 유형 좁히기 쌍 정의 및 `column.update` 확장(PK/참조 열 보호, 목표 유형에 맞지 않는 기존 `values`/`min`/`max`/`pattern` 정리 지원).
   - `@op("column.rename", SCHEMA_FILE)` 및 `@op("column.remove", SCHEMA_FILE)` 구현(열 순서 유지, PK 보호, `indexes` 내 열 이름 동기화).
5. [`dashkit/ingest/lineage.py`](../dashkit/ingest/lineage.py):
   - `used_by(lineage, table, project=None)` 확장 또는 `column_refs_by_table(project, lineage, table)` 추가: 테이블의 각 열을 참조하는 데이터셋(`table:`/`sql:`/`labels`), 차트, 필터, 위젯 섹션 매핑 반환.
6. [`dashkit/ingest/structure.py`](../dashkit/ingest/structure.py):
   - 스키마 진화 연산(`column.rename`, `column.remove`, 유형 좁히기 `column.update`) 감지 및 데이터 변환 함수 `_rewrite_table_data(...)` 구현:
     - `replace` 테이블: `data/<t>.csv` 열 이름 변경·삭제·값 다듬기·파싱·규칙 검증 및 재작성(오류 시 `data/<t>.csv:<줄>:<열>: <이유>`).
     - `append` 테이블: 원본 배치 대조 확인 → 현재 행 및 원본 배치 위치 추적으로 값 변환·검증(오류 시 배치 파일·줄·기본 키와 이유 보고) → `_pending/incoming/`에 새 정규형 합친 배치 작성.
   - `table:` 데이터셋의 `labels`에 삭제/이름 변경된 열이 있을 때 함께 정리·치환 지원.
   - `_write_record`와 `recover_config`에 `compactions` 및 `irreversible` 지원 추가(커밋 전 중단 시 `_merged/<시각>/`의 옛 배치를 원위치로 복원하고 새 합친 배치 삭제, 커밋 후 중단 시 마무리 및 `clear_merged`).
   - `history_files`: `meta.json`에 `irreversible: true`가 있으면 409 `irreversible_history`로 되돌리기 거절.
7. [`dashkit/manage/routes/tables.py`](../dashkit/manage/routes/tables.py) & [`dashkit/manage/routes/structure.py`](../dashkit/manage/routes/structure.py):
   - `GET /tables/{t}`에 `indexes` 추가.
   - `GET /tables/{t}/used_by`에 열별 참조 정보 `columns` 추가.
   - `STRUCT_OPS`에 `"column.rename"`, `"column.remove"` 추가.
   - `_check_ops`에서 `column.remove` / `column.rename` 시 차트·필터·위젯 섹션이 참조 중이면 409 `in_use`(+ 상세 위치 `errors`)로 거절.
   - `_affected`에서 `column.rename`, `column.remove`, `column.update`(유형 변경)에 영향받는 데이터셋·차트·섹션 반환.

### 4-3. 프런트엔드 (`web/`)
1. [`web/shared/structure.js`](../web/shared/structure.js):
   - `NARROWER`, `narrowTargets(type)` 추가.
   - `OPS`에 `columnRename: "column.rename"`, `columnRemove: "column.remove"` 추가.
   - `columnEditOps(table, before, form, existing)`: 이름 변경(`column.rename`)과 규칙·유형 변경(`column.update`) 연산 목록 생성 및 유효성 검사.
   - `columnRemoveOp(table, column)` 및 `columnUsage(used, column)` 순수 헬퍼 추가.
2. [`web/admin/structure.js`](../web/admin/structure.js), [`web/admin/menus/tables.js`](../web/admin/menus/tables.js), [`web/strings.js`](../web/strings.js):
   - `columnRuleCard`: 열 이름 변경 입력, 유형 넓히기·좁히기 선택, 열별 영향 목록 및 배치 합치기·되돌리기 제약 경고 + 확인 체크박스 연동.
   - `dropColumnCard`: 열 삭제 시 영향 목록·차단 안내·되돌리기 제약 확인 및 삭제 실행.
   - `menus/history.js`: `irreversible` 이력 항목에 되돌리기 불가 표시.

---

## 5. 테스트 및 검증 계획

### 5-1. 이슈 #41 (테이블 색인 선언) 검증

| 구분 | 테스트 파일 | 검증 시나리오 |
|---|---|---|
| **설정·검증 (단위)** | `tests/test_config.py` | - 단일 열 색인 `[[state]]`, 복합 색인 `[[state, seen_on]]` 정상 로드 및 정규화<br>- 빈 배열 `[]`, 숫자·문자열 등 잘못된 형식 JSON Schema 거부<br>- 존재하지 않는 열 `[[nope]]` → `열 'nope'가 columns에 없음`<br>- 한 색인 내 중복 열 `[[state, state]]` 거부<br>- `primary_key`와 동일한 색인(`[[id]]` 또는 복합 PK와 동일 순서) 거부<br>- 중복 색인 선언(`[[state], [state]]`) 거부, 열 순서가 다른 `[[a, b], [b, a]]`는 허용<br>- `table.add` / `table.update` 연산으로 `indexes` 추가·수정·삭제(`ops.py`)<br>- `indexes` 변경 시 `record.data_revision` 및 `table_rules_hash` 변경 확인 |
| **로더·재생성·권한 (통합)** | `tests/test_loader.py`, `tests/test_ingest_db.py` | - 빈 DB에 `load()` 실행 시 `pg_indexes`(`schemaname = 'raw'`)에 선언된 단일·복합 색인이 정확한 열 순서로 생성됨<br>- `rebuild_locked()` 및 재적재(`load()`) 후에도 동일한 색인 이름·정의가 유지됨<br>- 색인 유무와 관계없이 행 조회(`paging.fetch_page`), `/api/data`, `checks` 결과가 100% 동일함<br>- 63자를 넘는 긴 테이블·열 이름 조합에서도 색인 이름이 63자 이하로 결정적으로 생성됨<br>- 읽기 경로(`dash_inspect`, `dash_reader`)에서 조회·정렬·필터 실행 시 새 색인이 자동 생성되지 않고 `dash_owner`가 열리지 않음(`test_read_paths_never_open_the_owner_account`)<br>- 대표 필터·정렬 쿼리에 대해 `EXPLAIN (FORMAT JSON)`에서 선언한 색인(`Index Scan` / `Bitmap Index Scan`)이 사용 가능한 구조인지 확인 및 기존 `apply` 회귀 통과 |

### 5-2. 이슈 #43 (열 이름 변경·삭제·유형 좁히기) 검증

| 구분 | 테스트 파일 | 검증 시나리오 |
|---|---|---|
| **연산·프런트 순수 로직 (단위)** | `tests/test_studio_yaml.py`, `tests/js/structure.test.mjs` | - `column.rename`: 열 순서 보존, 기본 키 열 거부, 이미 있는 이름·예약어 거부, `indexes` 내 열 이름 자동 갱신<br>- `column.remove`: 기본 키 열 거부, 단일/복합 `indexes` 정리<br>- `column.update`: `_NARROWER`(`text → integer/number/boolean/date/timestamp/enum`, `number → integer`, `timestamp → date`) 허용 및 미지원 변환(`date → integer` 등) 거부, Studio 단독 YAML 저장 경로에서는 데이터 변환 연산 거부<br>- `web/shared/structure.js`: `narrowTargets`, `columnEditOps`, `columnUsage` 단위 테스트 |
| **변환·합치기·실패 보호 (DB 통합)** | `tests/test_ingest_structure.py` | - **`replace` 및 `append` 열 이름 변경 (`column.rename`):** 여러 배치가 쌓인 `append` 테이블(및 `replace` 테이블)에서 열 이름을 바꾸면 합친 정규형 배치 하나(`-merged.csv.gz`)로 변환되고 값과 행 수가 보존되며, 이후 옛 머리글 파일이나 새 머리글 행 적재·`rebuilt_equals_current`가 일치함<br>- **`replace` 및 `append` 열 삭제 (`column.remove`):** 삭제된 열이 정규형 파일과 DB에서 사라지고 나머지 열 값이 보존되며 `rebuilt_equals_current`가 일치함<br>- **유형 좁히기 성공 (`column.update`):** `"1,000"`, `"2026/11/01"`, `"Y"`, `"10.00"` 등 다듬기 가능한 값들이 담긴 옛 배치들이 `integer`·`date`·`boolean`으로 정상 변환·합치기되고 `rebuilt_equals_current`가 일치함<br>- **변환 불가 값 거절:** `"abc"`가 든 `text` 열을 `integer`로 좁히거나 `"10.5"`가 든 `number` 열을 `integer`로 좁히면 **정확한 위치(파일·줄·열·기본 키)와 이유**를 담은 422 오류를 반환하고, 설정 파일·원본 배치 파일·DB 행이 변경 전과 바이트 단위로 동일함<br>- **`sql:` 뷰 / `checks` 실패 시 전체 롤백:** 이름 변경·삭제된 열을 참조하는 `sql:` 데이터셋 뷰나 `checks`가 실패하면 422 `load_failed`와 함께 YAML과 `_merged/`로 옮겼던 원본 배치 파일들이 모두 변경 전으로 복원됨 |
| **끊김 복구 (Fault Injection)** | `tests/test_ingest_structure.py` | - `append` 테이블의 열 이름 변경·삭제·유형 좁히기 도중 `after_move`(옛 배치를 `_merged/`로 옮긴 직후, `load` 전), `before_commit`, `after_commit` 지점에서 `SimulatedCrash` 주입<br>- 다음 `recover_before_read` / `load` / `add` 실행 시 커밋 전 중단이면 **옛 배치 파일들과 `schema.yaml`이 모두 원래대로 복원**되고, 커밋 후 중단이면 **새 합친 배치와 이력이 유지**되며, 어느 경우든 `rebuilt_equals_current(root, scratch_db)`가 통과함 |
| **관리 서비스 API·영향·되돌리기 제약 (통합)** | `tests/test_manage_structure.py` | - `GET /tables/{t}/used_by`가 열별 참조(`columns`)를 반환함<br>- 차트·필터·위젯이 참조 중인 열에 `column.remove` 요청 시 409 `in_use`와 참조 위치 목록 반환<br>- `PATCH /config`로 `column.rename`·`column.remove`·유형 좁히기 실행 시 `affected` 목록과 합쳐진 배치 결과가 반환됨<br>- `replace` 테이블의 변경은 `POST /history/{id}/restore`로 되돌려지고, `append` 테이블의 합치기 동반 변경은 `irreversible: true`로 표시되며 `POST /history/{id}/restore` 시 409 `irreversible_history`로 거절됨<br>- `operator` 등급의 호출은 403 `forbidden`, 읽기 경로는 `dash_inspect`만 사용 |

---

## 6. 완료 조건 체크리스트

### 이슈 #41 (`[6] 테이블 색인 선언`)
- [ ] `schema.yaml` 테이블에 `indexes: [[열, …]]` 선언을 계약(`contracts/schema.schema.json`, `contracts/README.md`)·검증(`dashkit/config.py`)·로더(`dashkit/loader/__init__.py`)·연산(`dashkit/project/ops.py`)에 추가한다.
- [ ] 존재하지 않는 열, 한 색인 내 중복 열, 기본 키와 동일한 색인, 중복 색인 선언과 복합 색인을 검증한다.
- [ ] 빈 DB 적재(`load`)와 재생성(`rebuild_locked`) 시 동일한 색인이 만들어지고 조회 결과는 변하지 않는다.
- [ ] 색인 생성은 쓰기 경로(`dash_owner`)에서만 수행하며, 조회할 때 자동으로 색인을 만들지 않는다.
- [ ] 대표 필터·정렬의 색인 적용과 기존 `apply` 회귀를 테스트한다.

### 이슈 #43 (`[6] 열 이름 변경·삭제·유형 좁히기`)
- [ ] 배치 합치기와 함께 현재 내용을 새 구조의 정규형 파일로 변환해 열 이름 변경(`column.rename`)·삭제(`column.remove`)·유형 좁히기(`column.update`)를 지원한다.
- [ ] 실행 전 영향받는 데이터셋·그래프(`used_by` 열 단위 참조 및 `affected`)와 되돌리기 제약(배치 합치기 동반 및 `append` 이력 되돌리기 불가)을 표시한다.
- [ ] 변환 불가 값은 위치(파일·줄·열·기본 키)와 이유를 알리고 전체 작업을 실패시킨다(파일·DB 무변경).
- [ ] 설정·데이터 파일을 묶음 기록(`config` + `compactions`)으로 원자적으로 변경하고 기존 묶음 기록/끊김 복구 절차를 따른다.
- [ ] 실패·중단(`after_move`, `before_commit`, `after_commit`) 복구와 변경 후 전체 `apply`(`load`) 결과가 현재 DB와 같은지(`rebuilt_equals_current`) 검증한다.
