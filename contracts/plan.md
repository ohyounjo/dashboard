# 적용 전 차이 보기 계약 (`python -m dashkit plan`, `dashkit/plan.py`)

`make apply`가 무엇을 바꿀지 미리 보여 준다(보강설계서 5-2). **DB, Superset, 상태 파일(`.state/`), 빌드 폴더(`.build/`)를 바꾸지 않는다.** `make apply`는 plan 없이도 그대로 적용한다. plan은 선택이다.

```
$ make plan
데이터   members  +1행 −0행 ~1행
         변경 없음: milestones, tasks
뷰       dash.kpi_snapshots  열 추가: age_days
         dash.docs  적용해야 알 수 있음 (쿼리가 바뀜)
Superset 데이터셋 ~1 (kpi_snapshots: 열 추가: age_days)
         차트 +1 (extra_total)  ~1 (kpi_dday: 제목 변경)
         대시보드 ~1 (overview: 배치 변경)

→ 바뀌는 것이 있음. 적용: make apply
```

- 종료 코드: 차이가 있든 없든 0. CSV 검증 실패·설정 오류·DB 접속 실패는 1(오류를 한 줄에 하나씩 stderr에 출력).
- `--json`: 아래 "결과 형식"의 JSON을 stdout에 쓴다.
- 환경변수: `PROJECT`, `PROJECTS_DIR`, `DATA_DB_HOST`·`PORT`·`NAME`, `DATA_DB_READER_USER`·`PASSWORD`, `BUILD_DIR`, `STATE_DIR`. **owner 계정(`DATA_DB_OWNER_*`)과 Superset 계정은 쓰지 않는다.** 없어도 돈다.

## 권한

plan은 조회 전용이므로 **`dash_reader` 계정만 쓴다**(AGENTS.md "읽기 경로에서는 reader 계정만"). owner 계정으로는 읽기 전용 트랜잭션으로도 접속하지 않는다.

| 읽는 것 | 어디서 |
|---|---|
| 지금 `dash` 뷰의 열 | `information_schema.columns`(dash_reader) |
| 지난 적재의 내용과 구조 | 적재 기록 `dashkit_meta`(dash_reader, SELECT만) |
| 지난 번들, 상태 파일 | `.build/<프로젝트>/applied/`(마지막으로 import에 성공한 번들), `.state/managed.json`(파일) |

`dash_reader`는 `raw` 스키마를 읽을 수 없고(`tests/test_loader.py`), plan을 위해 그 권한을 주지도 않는다. 그래서 plan은 raw 테이블을 직접 보지 않고, 로더가 적재할 때 남긴 **적재 기록**과 CSV를 비교한다. 기록으로 알 수 없는 것은 추측하지 않고 "비교할 수 없음"·"적용해야 알 수 있음"으로 표시한다.

## 적재 기록 (`dashkit_meta` 스키마, `dashkit/loader/record.py`)

`dashkit load`가 raw·dash를 다시 만드는 **같은 트랜잭션**에서 `dashkit_meta` 스키마도 통째로 다시 만든다(적재가 롤백되면 기록도 이전 그대로다). `dash` 스키마는 뷰 = 데이터셋의 이름 공간이라 섞지 않는다.

```sql
CREATE TABLE dashkit_meta.row_fingerprint (
  table_name text NOT NULL,          -- schema.yaml의 테이블 이름
  row_key    text NOT NULL,          -- 기본 키의 해시(SHA-256 16진수 64자). 원본 키 값은 저장하지 않는다
  row_hash   text NOT NULL,          -- 행 해시(SHA-256 16진수 64자)
  PRIMARY KEY (table_name, row_key)
);
CREATE TABLE dashkit_meta.load_snapshot (
  id        integer PRIMARY KEY CHECK (id = 1),     -- 항상 한 건
  loaded_at timestamptz NOT NULL DEFAULT now(),
  snapshot  jsonb NOT NULL
);
CREATE TABLE dashkit_meta.batch (                 -- 원본 데이터 파일마다 한 줄(contracts/ingest.md "배치 기록")
  table_name text NOT NULL,
  file       text NOT NULL,                       -- replace 테이블·첫 배치는 "<테이블>.csv", 배치는 폴더 안의 이름
  sha256     text NOT NULL,                       -- 내용 해시(압축을 푼 내용의 SHA-256)
  added_at   timestamptz,                         -- 파일 이름의 시각. 없으면 NULL
  rows       integer NOT NULL,
  inserted   integer NOT NULL, updated integer NOT NULL, skipped integer NOT NULL, unchanged integer NOT NULL,
  PRIMARY KEY (table_name, file)
);
```

`snapshot`:

```json
{
  "version": 1,
  "project": "devtracker",
  "data_revision": "<sha256>",
  "tables": {
    "members": { "columns": [["id", "text"], ["name", "text"], ["role", "text"]], "primary_key": ["id"], "rows": 6, "mode": "replace" }
  },
  "datasets": {
    "kpi_snapshots": { "kind": "table", "definition": "<sha256>" },
    "docs":          { "kind": "sql",   "definition": "<sha256>" }
  }
}
```

- `tables.<이름>.columns`: 적재 뒤 raw 테이블의 열(이름, `information_schema`의 `data_type`), schema.yaml 순서. `enum`은 `text`다.
- `data_revision`: 데이터 설정 지문(DB의 내용을 정하는 설정만으로 구한 해시. [ingest.md](ingest.md) "배치 기록과 데이터 설정 지문"). 추가 적재가 "설정이 DB에 적용됐는지" 확인하는 데 쓴다.
- `tables.<이름>.mode`: `replace` | `append`. `rows`는 적재 뒤 테이블의 행 수다. `append` 테이블은 `rules`(정규화한 테이블 정의의 SHA-256)도 남긴다.
- `batch`: **프로젝트의 모든 원본 데이터 파일**(replace 테이블의 CSV, append 테이블의 배치마다)이 한 줄씩. 이름·해시·행 수뿐이다. 누적(append) 테이블은 행 지문을 남기지 않는다(배치 단위로 비교한다).
- `datasets.<키>.definition`: 뷰 정의의 해시. `table:`은 `"table:<테이블>"`, `sql:`은 로더가 뷰를 만들 때처럼 다듬은 쿼리(앞뒤 공백과 끝의 `;` 제거)의 SHA-256.
- **raw의 원본 값은 하나도 저장하지 않는다.** 지문 테이블의 두 열은 모두 해시이고(기본 키 값도 해시로만), 스냅샷에는 이름·타입·행 수·정의 해시만, 배치 기록에는 파일 이름·내용 해시·행 수만 있다. raw의 기본 키(이메일 등)는 dash 뷰에 나오지 않는 값일 수 있어, `dash_reader`가 읽는 기록에 두면 권한 경계가 넓어진다.
- 권한: `dash` 스키마와 같은 방식으로, 적재할 때마다 로더가 `REVOKE ALL ON SCHEMA dashkit_meta FROM PUBLIC`, `GRANT USAGE`·`GRANT SELECT ON ALL TABLES`를 `dash_reader`(와 있으면 `dash_inspect`)에게 준다. 초기화 스크립트(`data-db/init/01-reader.sh`)에 기대지 않으므로 이미 있는 DB 볼륨에서도 다음 적재부터 생긴다. `dash_reader`는 이 스키마에 쓰거나 지울 수 없다(`tests/test_plan.py`).

### 행 지문

지문은 DB 값이 아니라 **CSV를 검증하며 파싱한 값**에서 계산한다(`record.fingerprints`). 로더가 적재할 때와 plan이 CSV를 읽을 때 같은 함수를 쓰므로 DB의 타입 표현이 끼어들지 않는다.

| 값 | 정규 표현 |
|---|---|
| 빈 칸(NULL) | `null`(빈 문자열 `""`과 다르다) |
| `integer` | 10진수(`+7` = `7`) |
| `number` | 뒤의 0과 지수 표기를 없앤 10진수(`1.50` = `1.5`, `1.0` = `1`, `1E+2` = `100`, `-0.00` = `0`) |
| `boolean` | `true` / `false`(`TRUE` = `true`) |
| `date` | `YYYY-MM-DD` |
| `timestamp` | 시간대가 있으면 UTC로 바꾼 ISO 8601(`09:00+09:00` = `00:00+00:00`), 없으면 쓴 그대로 |
| `text`, `enum` | 글자 그대로(`1`과 `1.0`은 다르다) |

- `row_key` = 기본 키 열들의 정규 표현을 `primary_key` 순서로 담은 JSON 배열의 SHA-256.
- `row_hash` = `[열 이름, 정규 표현]` 쌍을 **열 이름순**으로 늘어놓은 JSON의 SHA-256. 열 순서만 바꾼 것은 해시를 바꾸지 않고, 열 이름을 바꾼 것은 바꾼다.

## 비교하는 것

| 줄 | 새 상태 | 지금 상태 | 비교 |
|---|---|---|---|
| 데이터 | `data/<테이블>.csv`(검증 통과분)의 행 지문 | 적재 기록의 행 지문 | 기본 키로 행을 맞춘다. `+` 새 키, `−` 없어진 키, `~` 키는 같고 해시가 다른 행 |
| 뷰 | 새 설정으로 만들어질 `dash.<데이터셋>`의 열 | 지금 `dash.<데이터셋>`의 열 | 열 추가·삭제·타입 변경·순서 변경 |
| Superset | 새 번들 | 마지막으로 import에 성공한 sync가 남긴 `.build/<프로젝트>/applied/` | 파일 단위. `metadata.yaml`(타임스탬프)은 무시. sync가 import 전에 쓰는 `bundle/`은 실패한 실행의 것일 수 있어 기준으로 쓰지 않는다. `applied/`가 없으면(이 기록이 생기기 전에 적용한 프로젝트 포함) "처음 적용"으로 본다 |

### 데이터

- CSV는 `dashkit load`와 같은 검증(`loader/validate.py`)을 먼저 거친다. 오류가 하나라도 있으면 차이를 계산하지 않고 오류를 보여 준다.
- 테이블마다:

  | 상황 | 결과 |
  |---|---|
  | 적재 기록이 없음(한 번도 적재하지 않았거나, 기록을 남기기 전의 dashkit으로 적재한 DB) | `status: unrecorded`. **"지난 적재 기록 없음: 비교할 수 없음(한 번 apply하면 생김)"**을 표시하고 CSV 행 수만 보여 준다. `added`·`removed`·`changed`는 `null` |
  | 기록에 그 테이블이 없음 | `new`. 전부 추가 |
  | 열 구성(이름·타입의 집합)이 기록과 다름 | 행을 맞추지 않는다(`comparable: false`). **+CSV 행 수, −기록된 행 수**. 열 차이는 따로 적는다 |
  | 기본 키 정의가 기록과 다름 | 위와 같음. 새 키로 지난 행을 맞추면 키가 겹쳐 삭제가 가려질 수 있다 |
  | 기록이 다른 프로젝트의 것 | 위와 같음(전부 교체). 그 프로젝트에만 있던 테이블은 "테이블 삭제" |
  | 그 밖(열 순서만 다른 경우 포함) | 지문을 기본 키로 비교 |
  | schema에서 빠진 테이블 | `removed`. −기록된 행 수 |

- 어느 쪽이든 같은 키가 두 번 나오면 조용히 덮어쓰지 않고 `comparable: false`(전부 추가·전부 삭제)로 돌려준다(`diff_rows`).

#### 누적 테이블(`mode: append`)

배치 단위로 비교한다(행 지문이 없다). 항목에 `"mode": "append"`와 `"batches": {"added": [파일], "removed": [파일], "changed": [파일]}`가 더해진다.

| 상황 | 결과 |
|---|---|
| 배치 기록에 없는 파일 | `batches.added`(배치 +1). 엄격하게 검증해 행 수를 `added`에 센다 |
| 기록에 있는데 폴더에 없는 파일 | `batches.removed`(배치 −1). 그 배치가 넣었던 새 행 수(`inserted`)를 `removed`에 센다 |
| 이름이 같고 내용 해시가 다른 파일 | `batches.changed`. 새 행 수를 `added`에, 기록된 `inserted`를 `removed`에 센다 |
| 이름과 해시가 같은 배치 | 다시 읽지 않는다 |
| 테이블 정의(충돌 정책·`default`·검증 규칙·`checks`)가 지난 적재와 다름(스냅샷의 `tables.<이름>.rules`, 정의의 해시) | 파일이 같아도 `status: changed`, `rules_changed: true`. "테이블 규칙이 바뀜: 다시 만든 결과가 달라질 수 있음" |
| 이 테이블을 가리키는 묶음 기록이 `data/_pending/`에 남아 있음 | `status: pending`. 기록을 고치지 않고 "끝나지 않은 폴더 작업이 있음"을 알리며 비교하지 않는다 |

`rows`는 지금 행 수(배치 기록의 `inserted` 합. 추가 적재분 포함) + 새 배치의 행 수 − 없어지거나 바뀐 배치의 `inserted`(어림값. 키가 겹치는 배치는 적용해야 정확히 안다), `changed`·`unchanged`는 `null`, `comparable`은 `false`다. 사람이 읽는 출력은 `배치 +2 −1 ~0 (+120행 −30행)`이다. 배치 파일의 이름 오류와 검증 오류는 CSV 검증 실패와 같게 다룬다.
- `load`는 테이블을 통째로 다시 만든다. plan의 행 수는 "적용 뒤 내용이 지난 적재와 얼마나 다른가"이지 실행될 SQL의 수가 아니다.
- 기록은 로더만 쓴다. owner 계정으로 raw를 직접 고친 내용은 plan에 나오지 않는다(다음 적재에서 덮어써진다).

### 뷰

- `table:` 데이터셋의 열은 schema.yaml에서 바로 안다(뷰가 `SELECT * FROM raw.<테이블>`이다). 테이블에 열을 더하면 적용 전에 "열 추가: …"로 나온다.
- `sql:` 데이터셋의 **새 열은 적용해 봐야 안다**(쿼리를 해석하려면 raw를 읽어야 하는데 plan은 reader만 쓴다). 다만 "바뀐 것이 없다"는 것은 적재 스냅샷으로 안다. 아래가 모두 참이면 지금 뷰의 열이 곧 새 열이다(변경 없음).
  1. 쿼리 정의의 해시가 스냅샷과 같다.
  2. 쿼리 글에 이름이 나오는 raw 테이블의 구조(열 이름·타입·순서)가 스냅샷과 같다(새로 생기거나 없어지는 테이블도 "다르다").
  3. 쿼리 글에 이름이 나오는 앞선 데이터셋의 새 열이 지금 뷰의 열과 같다.

  하나라도 아니면 **"적용해야 알 수 있음"**이고 이유를 적는다(`쿼리가 바뀜`, `쿼리에 나오는 raw.x의 구조가 바뀜`, `새 데이터셋`, `지난 적재 기록 없음`). 이름은 글자 단위로 찾으므로 실제로 읽지 않아도 걸릴 수 있다(틀린 답보다 모른다는 답을 고른다).
- `checks`(위반 행 SQL)와 뷰가 돌려줄 **값**의 변화는 plan이 보지 않는다. `make apply`의 load 단계가 확인한다.

### Superset

- 새 번들은 sync와 같은 함수(`sync/bundle.py`의 `build_bundle`)로 만든다. 데이터셋 YAML에 들어가는 뷰 열은 위에서 구한 열을 쓰고, 모르는 데이터셋은 지금 뷰의 열을 그대로 쓴다(결과의 `assumed_columns`에 적고 출력에도 알린다). 지금 뷰도 없으면(또는 지금 뷰가 다른 프로젝트의 것이면) 그 데이터셋과 그 데이터셋을 쓰는 차트는 "적용해야 알 수 있음"(`?`)이다.
- 지난 번들 폴더가 없으면 "처음 적용"이고 전부 추가다.
- 한 줄 설명: 대시보드는 `제목 변경`·`배치 변경`·`필터 변경`·`교차 필터 변경`, 차트는 `제목 변경`·`유형 변경`·`데이터셋 변경`·`옵션 변경`·`설명 변경`, 데이터셋은 `열 추가: …`·`열 삭제: …`·`열 속성 변경`·`지표 변경`. 공통으로 `화면 편집 잠금 변경`(`SYNC_LOCK_UI`), 그 밖은 `그 밖의 설정 변경`.
- 비교 대상은 Superset에 있는 객체가 아니라 **지난 sync가 만든 번들**이다. 관리 객체는 Superset 화면에서 고칠 수 없으므로(7절 D3, [state-and-uuid.md](state-and-uuid.md) "표식") 둘은 같다. `SYNC_LOCK_UI=0`으로 화면에서 고친 내용은 plan에 나오지 않고 다음 적용에서 덮어써진다.
- `−`로 나온 차트·대시보드·데이터셋은 다음 sync의 prune 대상이다.
- `.state/managed.json`이 다른 프로젝트의 것이면(프로젝트 전환) 그 프로젝트의 객체 수를 `replaces`에 싣고 "지금 적용된 프로젝트 '<이름>'의 객체 N개 삭제"로 알린다. 이때는 이 프로젝트의 지난 번들 폴더가 남아 있어도 비교하지 않고 전부 추가로 본다(Superset에는 이 프로젝트의 객체가 없다. `first: true`). 상태 파일은 읽기만 한다.

## 결과 형식 (`plan.make_plan(project) -> dict`, `--json`)

```json
{
  "project": "devtracker",
  "changed": true,
  "load_record": { "exists": true, "project": "devtracker" },
  "data": [
    { "table": "members", "rows": 7, "status": "changed",
      "columns": { "added": [], "removed": [], "retyped": [], "reordered": false },
      "added": 1, "removed": 0, "changed": 1, "unchanged": 5,
      "comparable": true, "reason": null,
      "keys": { "added": ["m77"], "removed": [], "changed": ["m2"] } }
  ],
  "views": [
    { "dataset": "docs", "status": "unknown", "exists": true, "reason": "쿼리가 바뀜",
      "columns": { "added": [], "removed": [], "retyped": [], "reordered": false } }
  ],
  "superset": {
    "first": false,
    "database": "unchanged",
    "datasets":   { "added": [], "changed": [ { "key": "kpi_snapshots", "detail": "열 추가: age_days" } ], "removed": [], "unknown": [], "unchanged": 4 },
    "charts":     { "added": ["extra_total"], "changed": [], "removed": [], "unknown": [], "unchanged": 10 },
    "dashboards": { "added": [], "changed": [ { "key": "overview", "detail": "배치 변경" } ], "removed": [], "unknown": [], "unchanged": 2 },
    "other": [],
    "assumed_columns": ["docs"],
    "replaces": null,
    "pending_prune": 0
  }
}
```

| 필드 | 값 |
|---|---|
| `changed` | 바뀌는 것(또는 비교할 수 없거나 적용해야 알 수 있는 것)이 하나라도 있으면 `true` |
| `load_record` | 적재 기록이 있는지와 그 기록의 프로젝트 |
| `data[].status` | `unrecorded`(적재 기록 없음) · `new` · `changed` · `unchanged` · `removed` |
| `data[].rows` | CSV의 행 수 |
| `data[].comparable`, `reason` | 행을 기본 키로 맞췄으면 `true`. 못 맞췄으면 `false`와 이유(이때 `added` = CSV 행 수, `removed` = 기록된 행 수. `unrecorded`면 둘 다 `null`) |
| `data[].keys` | `added`·`changed`마다 앞에서부터 20개의 키(문자열. 복합 키는 `", "`로 잇는다). CSV의 행에서 만든다. **`removed`는 항상 빈 목록이다**: 삭제될 행은 CSV에 없고 기록에는 키의 해시뿐이라 원본을 알 수 없다(개수는 `removed`) |
| `data[].columns`, `views[].columns` | `added`·`removed`는 열 이름, `retyped`는 `{column, from, to}`, `reordered`는 같은 열의 순서만 바뀜 |
| `views[].status` | `new` · `changed` · `unchanged` · `unknown`(적용해야 알 수 있음. `reason`에 이유) · `removed` |
| `superset.first` | 비교할 지난 번들이 없음(처음 적용, 또는 다른 프로젝트가 적용된 상태) |
| `superset.database` | `added` · `changed` · `unchanged` · `removed` |
| `superset.<종류>.unknown` | `{key, reason}`. 적용해야 내용을 알 수 있는 파일 |
| `superset.assumed_columns` | 열을 몰라 지금 뷰의 열로 번들을 만든 데이터셋 |
| `superset.pending_prune` | 같은 프로젝트의 `.state/managed.json`에 남아 있지만 이번 설정이 관리하지 않는 객체 수. 지난 sync가 삭제에 실패했거나 prune 전에 끊긴 것이고 다음 sync가 다시 지운다. 0보다 크면 `changed`는 참이고 "지난 적용에서 지우지 못한 객체 N개 삭제 재시도"로 알린다 |
| `superset.replaces` | `null`, 또는 `{project, objects}`: 상태 파일에 남은 다른 프로젝트와 그 Superset 객체 수(이번 적용에서 지워진다) |

## 함수 (Studio가 그대로 쓴다, 보강설계서 6-3 "데이터" 화면)

순수 함수(DB·파일에 닿지 않는다)와 읽어 오는 부분을 나눈다.

```python
# dashkit/loader/record.py — 순수(지문과 스냅샷). write만 로더의 트랜잭션에서 DB에 쓴다
def canonical(value) -> str | None
def fingerprints(table: dict, rows) -> list[tuple[str, str]]          # [(row_key, row_hash)] 둘 다 해시
def labels(table: dict, rows) -> dict[str, str]                       # {row_key: 사람이 읽는 기본 키}. 저장하지 않는다
def table_columns(table: dict) -> list[tuple[str, str]]               # schema의 테이블 → 적재 뒤 열(이름, 타입)
def definition_hash(dataset: dict) -> str
def snapshot(project_name, schema, dashboard, row_counts) -> dict
def write(cur, snapshot, fingerprints_by_table, reader_role) -> None

# dashkit/plan.py — 순수
def diff_rows(new, old, *, names=None, max_keys=20) -> dict           # 지문 두 묶음 비교. names는 예시 키용 labels
def diff_columns(new, old) -> dict
def table_diff(name, table, new_rows, recorded, old_prints=(), *, replaced_project=None) -> dict   # data[] 한 항목
def unrecorded_table(name, row_count) -> dict
def predict_view_columns(project, snapshot, views_now) -> dict[str, dict]
def diff_views(predicted, views_now) -> list[dict]
def diff_bundles(new: dict[str, str], old: dict[str, str] | None, unknown=None) -> dict
def format_plan(plan: dict) -> str
# dashkit/plan.py — 읽기 포함
def read_bundle_dir(path: Path) -> dict[str, str] | None
def read_other_project(state_dir: Path, project: str) -> dict | None
def make_plan(project, *, dbname=None, build_dir=None, state_dir=None, env=None) -> dict   # 실패 시 PlanError(title, errors)

# dashkit/loader/current.py — 읽어 오는 부분(dash_reader만)
def reader(dbname=None) -> ContextManager[psycopg.Connection]         # 읽기 전용, 끝나면 롤백
def read_view_columns(conn, names=None) -> dict[str, list[tuple[str, str]]]
def read_load_record(conn, tables=None) -> LoadRecord | None          # .snapshot, .fingerprints, .project
```

"업로드한 CSV 하나와 지금 테이블의 차이"는 이렇게 구한다: CSV를 `loader/validate.py`로 검증해 `TableData`를 얻고, `reader()` 안에서 `read_load_record(conn, [테이블])`로 기록을 읽어 `table_diff(테이블, 정의, TableData.rows, record.snapshot["tables"].get(테이블), record.fingerprints.get(테이블, []))`에 넘긴다. 기록이 없으면(`None`) `unrecorded_table`이다. owner 계정은 필요 없다.
