# 적용 전 차이 보기 계약 (`python -m dashkit plan`, `dashkit/plan.py`)

`make apply`가 무엇을 바꿀지 미리 보여 준다(보강설계서 5-2). **DB, Superset, 상태 파일(`.state/`), 빌드 폴더(`.build/`)를 바꾸지 않는다.** `make apply`는 plan 없이도 그대로 적용한다. plan은 선택이다.

```
$ make plan
데이터   members  +1행 −0행 ~1행
         변경 없음: milestones, tasks
뷰       dash.docs  열 추가: age_days
         변경 없음: dash.milestones
Superset 데이터셋 ~1 (docs: 열 추가: age_days)
         차트 +1 (extra_total)  ~1 (kpi_dday: 제목 변경)
         대시보드 ~1 (overview: 배치 변경)

→ 바뀌는 것이 있음. 적용: make apply
```

- 종료 코드: 차이가 있든 없든 0. CSV 검증 실패·설정 오류·DB 접속 실패는 1(오류를 한 줄에 하나씩 stderr에 출력).
- `--json`: 아래 "결과 형식"의 JSON을 stdout에 쓴다.
- 환경변수는 다른 하위 명령과 같다(`PROJECT`, `PROJECTS_DIR`, `DATA_DB_*`, `BUILD_DIR`, `STATE_DIR`). Superset 계정은 쓰지 않는다.

## 비교하는 것

| 줄 | 새 상태 | 지금 상태 | 비교 |
|---|---|---|---|
| 데이터 | `data/<테이블>.csv`(검증 통과분) | `raw.<테이블>` | 기본 키로 행을 맞춘다. `+` 새 키, `−` 없어진 키, `~` 키는 같고 값이 다른 행 |
| 뷰 | 새 설정으로 만들어질 `dash.<데이터셋>`의 열 | 지금 `dash.<데이터셋>`의 열 | 열 추가·삭제·타입 변경·순서 변경 |
| Superset | 새 번들 | 지난 sync가 남긴 `.build/<프로젝트>/bundle/` | 파일 단위. `metadata.yaml`(타임스탬프)은 무시 |

### 데이터

- CSV는 `dashkit load`와 같은 검증(`loader/validate.py`)을 먼저 거친다. 오류가 하나라도 있으면 차이를 계산하지 않고 오류를 보여 준다.
- 값은 **양쪽에 다 있는 열**만 비교한다. 열이 추가·삭제·타입 변경된 것은 같은 줄에 따로 적는다.
- 값은 표기가 아니라 값으로 비교한다(`1.50` = `1.5`, 시각은 UTC로 맞춘 뒤 비교. 시간대 없는 시각은 DB 세션 시간대 기준으로 본다. 적재할 때 Postgres가 하는 해석과 같다).
- `raw.<테이블>`이 없으면(처음 적용, 새 테이블) 전부 추가다. schema에서 빠진 테이블은 "테이블 삭제"다.
- 새 기본 키 열이 지금 테이블에 없으면 행을 맞출 수 없으므로 전부 추가·전부 삭제로 센다(`comparable: false`).
- `load`는 테이블을 통째로 다시 만든다. plan의 행 수는 "적용 뒤 내용이 지금과 얼마나 다른가"이지 실행될 SQL의 수가 아니다.

### 뷰

- `table:` 데이터셋의 열은 schema.yaml에서 바로 안다(뷰가 `SELECT * FROM raw.<테이블>`이다).
- `sql:` 데이터셋의 열은 쿼리를 **실행하지 않고** 지금 DB에 물어 알아낸다(확장 프로토콜의 Parse/Describe. 문장 하나만 받고 아무것도 실행하지 않는다).
  이 답이 적용 뒤에도 맞으려면 쿼리가 읽는 것의 구조가 그대로여야 한다. 그래서 쿼리 글에 이름이 나오는 raw 테이블이나 앞선 데이터셋 뷰 가운데 구조(열 이름·타입·순서)가 바뀌는 것이 있으면 묻지 않고 **"적용해야 알 수 있음"**으로 둔다. 이름은 글자 단위로 찾으므로 실제로 읽지 않아도 걸릴 수 있다(틀린 답보다 모른다는 답을 고른다).
- 다음 경우도 "적용해야 알 수 있음"이고 이유를 함께 적는다: 아직 적재된 적이 없음, 쿼리가 지금 DB에서 해석되지 않음(없는 열·테이블. Postgres의 메시지를 그대로 싣는다).
- 임시로 뷰를 만들었다 롤백하는 방법은 쓰지 않는다. 쓰기 트랜잭션이 필요하고, `raw`·`dash`를 다시 만드는 동안 읽는 쪽(backend, Superset)을 잠금으로 막기 때문이다.
- `checks`(위반 행 SQL)와 뷰가 돌려줄 **값**의 변화는 plan이 보지 않는다. `make apply`의 load 단계가 확인한다.

### Superset

- 새 번들은 sync와 같은 함수(`sync/bundle.py`의 `build_bundle`)로 만든다. 데이터셋 YAML에 들어가는 뷰 열은 위에서 구한 열을 쓰고, 모르는 데이터셋은 지금 뷰의 열을 그대로 쓴다(결과의 `assumed_columns`에 적고 출력에도 알린다). 지금 뷰도 없으면 그 데이터셋과 그 데이터셋을 쓰는 차트는 "적용해야 알 수 있음"(`?`)이다.
- 지난 번들 폴더가 없으면 "처음 적용"이고 전부 추가다.
- 한 줄 설명: 대시보드는 `제목 변경`·`배치 변경`·`필터 변경`·`교차 필터 변경`, 차트는 `제목 변경`·`유형 변경`·`데이터셋 변경`·`옵션 변경`·`설명 변경`, 데이터셋은 `열 추가: …`·`열 삭제: …`·`열 속성 변경`·`지표 변경`. 공통으로 `화면 편집 잠금 변경`(`SYNC_LOCK_UI`), 그 밖은 `그 밖의 설정 변경`.
- 비교 대상은 Superset에 있는 객체가 아니라 **지난 sync가 만든 번들**이다. 관리 객체는 Superset 화면에서 고칠 수 없으므로(7절 D3, [state-and-uuid.md](state-and-uuid.md) "표식") 둘은 같다. `SYNC_LOCK_UI=0`으로 화면에서 고친 내용은 plan에 나오지 않고 다음 적용에서 덮어써진다.
- `−`로 나온 차트·대시보드·데이터셋은 다음 sync의 prune 대상이다.
- `.state/managed.json`이 다른 프로젝트의 것이면(프로젝트 전환) 그 프로젝트의 객체 수를 `replaces`에 싣고 "지금 적용된 프로젝트 '<이름>'의 객체 N개 삭제"로 알린다. 이때는 이 프로젝트의 지난 번들 폴더가 남아 있어도 비교하지 않고 전부 추가로 본다(Superset에는 이 프로젝트의 객체가 없다. `first: true`). 상태 파일은 읽기만 한다.

## 권한

| 읽는 것 | 계정 | 이유 |
|---|---|---|
| `raw` 테이블의 열과 행, `sql:` 데이터셋 쿼리의 Describe | **owner, 읽기 전용 연결** | `dash_reader`는 `raw`를 읽을 수 없다(`tests/test_loader.py`). reader에게 `raw` 권한을 주면 backend와 Superset까지 원본 테이블을 읽게 되어 권한 경계가 넓어진다. plan은 owner 계정을 원래 쓰는 tools 컨테이너의 로더 쪽 명령이므로 owner를 쓰되 쓰기를 막는다 |
| 지금 `dash` 뷰의 열(`information_schema`) | `dash_reader` | 읽기 경로는 reader만 쓴다 |
| 지난 번들 | 파일 | Superset에 접속하지 않는다 |

owner 읽기 전용 연결(`dashkit/loader/current.py`의 `readonly_owner`)의 조건:

- 세션 기본값 `default_transaction_read_only=on`, 트랜잭션 `READ ONLY` + `REPEATABLE READ`(모든 테이블을 같은 시점으로 읽는다), `statement_timeout`(기본 60초, `PLAN_STATEMENT_TIMEOUT_MS`).
- 끝나면 항상 롤백한다. INSERT·DELETE·CREATE·DROP·임시 테이블 생성은 Postgres가 거부한다(`tests/test_plan.py`가 확인).
- 이 모듈은 tools 컨테이너(로더, 뒤의 Studio)에서만 쓴다. **backend에는 owner 계정이 없고 plan 기능을 두지 않는다.**

## 결과 형식 (`plan.make_plan(project) -> dict`, `--json`)

```json
{
  "project": "devtracker",
  "changed": true,
  "data": [
    { "table": "members", "status": "changed", "rows": 9,
      "added": 1, "removed": 0, "changed": 1, "unchanged": 7,
      "comparable": true, "compared_columns": ["id", "name", "role"],
      "keys": { "added": ["m77"], "removed": [], "changed": ["m2"] },
      "columns": { "added": [], "removed": [], "retyped": [], "reordered": false } }
  ],
  "views": [
    { "dataset": "docs", "status": "changed", "exists": true, "reason": null,
      "columns": { "added": ["age_days"], "removed": [], "retyped": [], "reordered": false } }
  ],
  "superset": {
    "first": false,
    "database": "unchanged",
    "datasets":   { "added": [], "changed": [ { "key": "docs", "detail": "열 추가: age_days" } ], "removed": [], "unknown": [], "unchanged": 4 },
    "charts":     { "added": ["extra_total"], "changed": [], "removed": [], "unknown": [], "unchanged": 10 },
    "dashboards": { "added": [], "changed": [ { "key": "overview", "detail": "배치 변경" } ], "removed": [], "unknown": [], "unchanged": 2 },
    "other": [],
    "assumed_columns": [],
    "replaces": null
  }
}
```

| 필드 | 값 |
|---|---|
| `changed` | 바뀌는 것(또는 적용해야 알 수 있는 것)이 하나라도 있으면 `true` |
| `data[].status` | `new`(테이블이 없음) · `changed` · `unchanged` · `removed`(schema에서 빠짐) |
| `data[].rows` | CSV의 행 수 |
| `data[].keys` | 종류마다 앞에서부터 20개의 키(문자열. 복합 키는 `", "`로 잇는다) |
| `data[].columns`, `views[].columns` | `added`·`removed`는 열 이름, `retyped`는 `{column, from, to}`, `reordered`는 같은 열의 순서만 바뀜 |
| `views[].status` | `new` · `changed` · `unchanged` · `unknown`(적용해야 알 수 있음. `reason`에 이유) · `removed` |
| `superset.first` | 비교할 지난 번들이 없음(처음 적용, 또는 다른 프로젝트가 적용된 상태) |
| `superset.database` | `added` · `changed` · `unchanged` · `removed` |
| `superset.<종류>.unknown` | `{key, reason}`. 적용해야 내용을 알 수 있는 파일 |
| `superset.assumed_columns` | 열을 몰라 지금 뷰의 열로 번들을 만든 데이터셋 |
| `superset.replaces` | `null`, 또는 `{project, objects}`: 상태 파일에 남은 다른 프로젝트와 그 Superset 객체 수(이번 적용에서 지워진다) |

## 함수 (Studio가 그대로 쓴다, 보강설계서 6-3 "데이터" 화면)

순수 함수(DB·파일에 닿지 않는다)와 읽어 오는 부분을 나눈다.

```python
# dashkit/plan.py — 순수
def diff_rows(columns, primary_key, new_rows, old_columns, old_rows, *, tz=None, max_keys=20) -> dict
def diff_columns(new: list[tuple[str, str]], old: list[tuple[str, str]]) -> dict
def table_columns(table: dict) -> list[tuple[str, str]]             # schema의 테이블 → 적재 뒤 열(이름, 타입)
def table_diff(name, table, new_rows, current, *, tz=None) -> dict   # data[] 한 항목. current는 RawTable 또는 None
def predict_view_columns(project, raw_now, views_now, describe) -> dict[str, dict]
def diff_views(predicted, views_now) -> list[dict]
def diff_bundles(new: dict[str, str], old: dict[str, str] | None, unknown=None) -> dict
def format_plan(plan: dict) -> str
# dashkit/plan.py — 읽기 포함
def read_bundle_dir(path: Path) -> dict[str, str] | None
def read_other_project(state_dir: Path, project: str) -> dict | None
def make_plan(project, *, dbname=None, build_dir=None, state_dir=None, env=None) -> dict   # 실패 시 PlanError(title, errors)

# dashkit/loader/current.py — 읽어 오는 부분(owner 읽기 전용, reader)
def readonly_owner(dbname=None) -> ContextManager[psycopg.Connection]
def read_columns(conn, schema, names=None) -> dict[str, list[tuple[str, str]]]
def read_raw_table(conn, name, columns, want=None) -> RawTable        # columns, row_columns, rows
def reader_view_columns(dbname=None) -> dict[str, list[tuple[str, str]]]
def describe_query(conn, query) -> list[tuple[str, str]]              # 실패 시 DescribeError
def session_timezone(conn) -> tzinfo
```

"업로드한 CSV 하나와 지금 테이블의 차이"는 이렇게 구한다: CSV를 `loader/validate.py`로 검증해 `TableData`를 얻고, `readonly_owner()` 안에서 `read_columns(conn, "raw", [테이블])`와 `read_raw_table(...)`로 지금 테이블을 읽어 `table_diff(테이블, 정의, TableData.rows, RawTable, tz=session_timezone(conn))`에 넘긴다.
