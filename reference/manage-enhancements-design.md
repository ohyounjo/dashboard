# 관리 서비스 보완·선택 기능 통합 설계 (#34, #36, #33, #32)

이 문서는 다음 4개 이슈를 한 번의 통합 구현·검증 사이클로 진행하기 위한 상세 설계서다.

- [이슈 #34](https://github.com/ohyounjo/dashboard/issues/34): `[4+] 장시간 작업의 잠금 대기와 대용량 미리보기 안내`
- [이슈 #36](https://github.com/ohyounjo/dashboard/issues/36): `[5] 데이터 신선도 표시` (`MANAGE_FEATURES=freshness`)
- [이슈 #33](https://github.com/ohyounjo/dashboard/issues/33): `[5] 행 조회 결과 CSV 내보내기` (`MANAGE_FEATURES=export`)
- [이슈 #32](https://github.com/ohyounjo/dashboard/issues/32): `[5] 열 통계 조회` (`MANAGE_FEATURES=profile`)

참조: [데이터 설계서](./data-design.md) · [이슈 #34 단독 설계서](./long-running-lock-wait-design.md) · [관리 서비스 계약](../contracts/manage.md) · [적재 계약](../contracts/ingest.md) · [백엔드 API 계약](../contracts/api.md) · [인증·감사 로그 계약](../contracts/auth.md) · [오케스트레이션 프롬프트](./manage-enhancements-prompt.md)

## 통합 추진 배경과 범위

4개 이슈를 함께 구현하는 이유는 다음과 같다.

1. **공통 기반 공유:** #32(`profile`), #33(`export`), #36(`freshness`)은 모두 선택 기능 플래그 `MANAGE_FEATURES`(데이터 설계서 10-6절)와 `GET /api/manage/config`의 `features` 응답에 의존하며, #34와 함께 `dashkit/manage/context.py`, `dashkit/manage/routes/tables.py`, `web/admin/menus/tables.js`, `web/strings.js`, `.env.example`, `docker-compose.yml`을 공통으로 수정한다.
2. **검증 비용 절감:** 격리된 테스트 스택(`AUTH_COOKIE_SECURE=0 make test`) 기동·통합 검증을 이슈마다 4회 반복하지 않고 단일 통합 검증으로 일괄 확인한다.
3. **이슈 #34 기존 설계·PR과의 정합성:** #34의 단독 설계서([long-running-lock-wait-design.md](./long-running-lock-wait-design.md))가 이미 `main`에 병합되어 있다. 본 문서는 #34의 확정 사양을 그대로 유지하며, 구현 시점에 #34가 `main`에 이미 반영되어 있으면 그 위에서 #32·#33·#36을 얹고, 미반영 상태라면 4개 이슈를 통합 브랜치에서 일괄 구현한다.

## 공통 기반: 선택 기능 플래그 (`MANAGE_FEATURES`)

데이터 설계서 10-6절에 따라 선택 기능을 환경 변수 하나로 제어한다.

- **모듈 위치:** `dashkit/features.py` (표준 라이브러리만 사용하는 소형 공용 모듈). `backend`(`target: app`)와 `manager`(`target: studio`)가 이미지 의존성 결합 없이 함께 import한다.
- **환경 변수:** `MANAGE_FEATURES` (쉼표 구분, 기본 빈 문자열 `""`).
- **지원 기능 목록:** `KNOWN_MANAGE_FEATURES = frozenset({"export", "profile", "freshness"})` (추후 `sql`, `tokens` 구현 시 추가).
- **파싱 규칙 (`manage_features() -> frozenset[str]`):**
  - `os.environ.get("MANAGE_FEATURES", "")`를 `,`로 분리하고 앞뒤 공백 제거 후 소문자화한다.
  - `KNOWN_MANAGE_FEATURES`에 속하는 값만 반환한다. 알 수 없는 토큰은 무시해 서비스 기동을 막지 않는다.
  - 요청 시점 또는 호출 시점의 환경 변수를 읽어 테스트에서 `monkeypatch.setenv("MANAGE_FEATURES", ...)`로 즉시 전환할 수 있게 한다.
- **`GET /api/manage/config` 확장 (`dashkit/manage/routes/base.py`):**
  - 기존 `{"project", "role", "limits"}`에 `"features": sorted(manage_features())`를 추가한다.
  - 운용 화면(`web/admin/`)은 `session.manage.features`를 보고 켜진 기능의 탭·버튼·경고만 렌더링한다.
- **비활성 경로 응답 규칙:**
  - `GET /api/manage/tables/{table}/profile` (`profile`)과 `GET /api/manage/tables/{table}/export` (`export`)는 권한 확인(`Depends(require_operator)`) 후 기능이 꺼져 있으면 HTTP 404 `{"detail": "not_found", "message": "비활성화된 기능입니다"}`를 반환한다.
  - 비인증 요청은 401 `unauthenticated`, `user` 등급 요청은 403 `forbidden`으로 기존 인증·권한 경계를 유지한다.
- **Compose 및 환경 파일:**
  - `.env.example`: `MANAGE_FEATURES=`(기본 빈 값), `EXPORT_MAX_ROWS=1000000`, `MANAGE_JOB_LOCK_WAIT_MS=600000` 추가.
  - `docker-compose.yml`:
    - `manager` 서비스에 `MANAGE_FEATURES`, `EXPORT_MAX_ROWS`, `MANAGE_JOB_LOCK_WAIT_MS` 전달.
    - `backend` 서비스에 `MANAGE_FEATURES` 전달(`GET /api/app`의 `freshness` 활성 여부 판정용).

---

## 1. 이슈 #34: 장시간 작업의 잠금 대기와 대용량 미리보기 안내

[long-running-lock-wait-design.md](./long-running-lock-wait-design.md)의 확정 사양을 그대로 적용한다. 핵심 요약은 다음과 같다.

1. **잠금 대기 한도 분리 (`dashkit/manage/context.py`):**
   - `MANAGE_LOCK_WAIT_MS` (`Limits.lock_wait_ms`, 기본 `5000`ms): 즉시 응답 요청(`POST /tables/{t}/batches` JSON `rows`, `DELETE /tables/{t}/batches/{file}` 빠른 취소 후보). 초과 시 HTTP 409 `apply_running`.
   - `MANAGE_JOB_LOCK_WAIT_MS` (`Limits.job_lock_wait_ms`, 기본 `600000`ms): 모든 202 비동기 작업(`ingest`, `remove`, `restore`, `config`, `create`, `compact`, `restore_config`). 초과 시 작업 `status: "failed"`, `error: {"status": 409, "detail": "apply_running", ...}`.
   - `Limits.public()`은 기존 UI 입력 한도만 노출하고 서버 잠금 대기 시간은 노출하지 않는다.
2. **`DELETE /tables/{t}/batches/{file}` 빠른 취소 사전 판별:**
   - 잠금 전 폴더 목록(`batches.list_batches`)과 읽기 전용 계정(`dash_inspect`, `_recorded()`)으로 비후보(가장 큰 번호가 아니거나 `inserted != rows` / `updated`·`skipped`·`unchanged > 0`)를 판별해 `fast_only=True` 5초 대기 없이 바로 202 `remove` 작업으로 넘긴다. 없는 배치(404)와 합친 배치(409)의 즉시 오류 응답은 유지한다.
3. **`change_config` 잠금 순서 및 `revision` 재검사 (`dashkit/ingest/structure.py`):**
   - `locked(..., wait_ms=wait_ms)`(DB advisory lock) 확보 후 `store.project_lock(project.name)`에 진입해 대기 중 `POST /tables/{t}/aliases`가 막히지 않게 한다.
   - `store.project_lock` 획득 직후 파일을 읽거나 변경하기 전에 `store.revision(project.root) != revision`을 다시 검사해 어긋나면 409 `stale_config`로 거절한다.
4. **모든 202 작업의 `step: "lock"` 통일:**
   - `dashkit/ingest/operation.py`의 `locked()`에 `on_acquired` 콜백을 추가하고, `remove_batch`·`restore_batch`·`compact`·`change_config`가 잠금 대기 중 `{"step": "lock"}`을 보고한 뒤 획득 직후 본 단계(`"rebuild"` 또는 `"apply"`)로 전이한다. `ingest`는 기존 계약(`"read"` → `"lock"`)을 유지한다.
5. **50만 행 이상 대용량 미리보기 안내:**
   - `web/shared/ingest.js`에 `LARGE_BATCH_ROWS = 500_000`, `previewInsertRows(batches)`(`total - invalid` 합산), `isLargeBatchPreview(batches, threshold = LARGE_BATCH_ROWS)`를 추가한다.
   - `web/strings.js`에 `S.admin.data.largeBatchHint`를 추가하고, `web/admin/menus/tables.js`의 `runNotes(r, { preview })`에서 `preview === true`일 때만 표시한다(단일·여러 파일 공통).
6. **유지 원칙:** 데이터 DB 전역 단일 advisory lock, 잠금→프로젝트 확인→설정 확인→복구→대조→작업→`checks`→커밋 순서와 단일 트랜잭션 원자성을 변경하지 않는다.

---

## 2. 이슈 #32: 열 통계 조회 (`MANAGE_FEATURES=profile`)

### 2-1. API 계약 (`GET /api/manage/tables/{table}/profile`)

- **경로:** `GET /api/manage/tables/{table}/profile` (`dashkit/manage/routes/rows.py` 또는 전용 모듈 `dashkit/manage/routes/profile.py`에 배치하고 `routes/__init__.py`에 등록)
- **권한·활성 조건:** `Depends(require_operator)` (`admin`·`operator` 허용). `profile` 기능이 비활성이면 404 `not_found`.
- **대상 확인:** `_table(project, table)`로 `schema.yaml`의 테이블 목록과 대조(없으면 404 `unknown_table`).
- **DB 접속:** `dash_inspect` 읽기 전용 접속(`rows._connect()` 재사용: `default_transaction_read_only=on`, `statement_timeout=MANAGE_STATEMENT_TIMEOUT_MS`). `dash_owner` 연결을 열지 않는다.
- **응답 (200):**

```json
{
  "table": "issues",
  "total_rows": 120,
  "columns": [
    {
      "name": "id",
      "type": "text",
      "sensitive": false,
      "null_count": 0,
      "null_ratio": 0.0,
      "distinct_count": 120,
      "min": "ISS-001",
      "max": "ISS-120"
    },
    {
      "name": "is_closed",
      "type": "boolean",
      "sensitive": false,
      "null_count": 2,
      "null_ratio": 0.016667,
      "distinct_count": 2,
      "min": null,
      "max": null
    }
  ]
}
```

### 2-2. 집계 SQL, 열 유형·경계 조건, 민감 열 보호

- **단일 쿼리 집계:** 테이블 스캔 횟수를 1회로 제한하기 위해 `SELECT COUNT(*)`와 비민감 열별 집계 표현식을 단일 `SELECT` 문으로 구성한다. 모든 테이블·열 이름은 `schema.yaml` 대조 후 `psycopg.sql.Identifier`로만 삽입한다.
- **열 유형별 계산 규칙:**

| 열 유형 | `null_count` / `null_ratio` | `distinct_count` | `min` / `max` |
|---|---|---|---|
| `integer`, `number`, `date`, `timestamp`, `text`, `enum` | `total_rows - COUNT(c)` / `round(null_count / total_rows, 6)` (빈 테이블이면 `null`) | `COUNT(DISTINCT c)` (NULL 제외) | `MIN(c)`, `MAX(c)` (`rows._to_json` 직렬화) |
| `boolean` | 동일 | `COUNT(DISTINCT c)` | `null`, `null` (PostgreSQL은 `MIN(boolean)`/`MAX(boolean)` 미지원) |

- **긴 텍스트 절단:** `text`·`enum` 열의 `min`/`max` 문자열이 `paging.CELL_CHARS`(2,000자)를 넘으면 2,000자에서 절단해 거대한 단일 셀이 응답을 비대화하지 않도록 한다.
- **빈 테이블 (`total_rows == 0`):** 모든 비민감 열은 `null_count: 0`, `null_ratio: null`, `distinct_count: 0`, `min: null`, `max: null`을 반환한다.
- **모두 NULL인 열 (`total_rows > 0`, `COUNT(c) == 0`):** `null_count == total_rows`, `null_ratio == 1.0`, `distinct_count == 0`, `min: null`, `max: null`.
- **민감 열 보호(#37 연동):**
  - `schema.yaml` 열 정의에 `sensitive: true`가 있으면 해당 열을 **집계 SQL에 포함하지 않는다**(DB에서 아예 읽지 않아 `dash_inspect` 열 권한 제한과 충돌하지 않으며 통계로 값 유추도 차단).
  - 응답 항목에는 `{"name": col, "type": col_type, "sensitive": true, "null_count": null, "null_ratio": null, "distinct_count": null, "min": null, "max": null}`을 반환한다.
  - 테이블의 모든 열이 민감 열인 경우에도 `SELECT COUNT(*) FROM raw.<table>`만 실행해 정상 응답한다.
- **오류 처리:**
  - `psycopg.errors.QueryCanceled` → 504 `query_timeout` ("통계 조회가 시간 제한을 넘었습니다")
  - `UndefinedTable` / `InvalidSchemaName` → 409 `rebuild_required`
  - DB 연결 불가 → 503 `db_unavailable`

### 2-3. 운용 화면 UI (`web/admin/menus/tables.js`, `web/strings.js`)

- `session.manage.features`에 `"profile"`이 있을 때만 테이블 상세 탭에 **"통계"**(`key: "profile"`) 탭을 표시한다.
- 탭 진입 시 자동 쿼리하지 않고 **"통계 계산"** 버튼(설계서 9-1절: "누를 때 계산")을 클릭했을 때 `GET /api/manage/tables/{t}/profile`을 호출한다.
- 결과 표에는 열(표시 이름 + 키), 유형, 빈 값(`null_count`와 백분율), 고유값 수, 최소, 최대를 표시한다. `sensitive: true` 열은 모든 통계 칸에 `"***"`을 표시한다. 504 `query_timeout` 발생 시 안내 메시지와 다시 시도 버튼을 제공한다.

---

## 3. 이슈 #33: 행 조회 결과 CSV 내보내기 (`MANAGE_FEATURES=export`)

### 3-1. API 계약 (`GET /api/manage/tables/{table}/export`)

- **경로:** `GET /api/manage/tables/{table}/export`
- **쿼리 파라미터:**
  - `format`: 선택, 기본 `"csv"`. `"csv"`가 아니면 422 `invalid_query`.
  - `filters`: 선택, JSON 배열 문자열(예: `[{"column":"status","op":"eq","value":"open"}]`). 생략 또는 빈 문자열이면 `[]`.
  - `sort`: 선택, JSON 배열 문자열(예: `[{"column":"updated","dir":"desc"}]`). 생략 또는 빈 문자열이면 `[]`.
  - `columns`: 선택, JSON 배열 문자열(예: `["id","title"]`). 생략 또는 빈 문자열이면 테이블의 전체 열(스키마 순서).
- **권한·활성 조건:** `Depends(require_operator)` (`admin`·`operator` 허용). `export` 기능이 비활성이면 404 `not_found`.
- **필터·정렬·열 검증:**
  - JSON 파싱 실패 시 422 `invalid_query` (또는 `filters` 파싱 실패 시 422 `invalid_filter`).
  - 파싱된 객체는 `paging.parse_query({"filters": ..., "sort": ..., "columns": ...}, columns)`를 **그대로 재사용**해 열 이름, 연산자(`eq`~`not_null`), 유형별 값 검증을 행 조회와 100% 일치시킨다.
  - WHERE 절은 `paging.where_clause(q.filters)`를 그대로 재사용하며, 모든 식별자는 `psycopg.sql.Identifier`, 모든 값은 매개변수 바인딩으로 처리한다.
- **응답 헤더 (200):**
  - `Content-Type: text/csv; charset=utf-8`
  - `Content-Disposition: attachment; filename="<table_name>.csv"` (`table_name`은 `schema.yaml` 식별자라 안전)
  - `Cache-Control: no-store`
  - `X-Export-Max-Rows: <EXPORT_MAX_ROWS>`

### 3-2. 단일 쿼리 스트리밍, 한도(`EXPORT_MAX_ROWS`), 민감 열, 감사 로그

- **한도 설정 (`dashkit/manage/context.py`):**
  - `Limits.export_max_rows` (`EXPORT_MAX_ROWS`, 기본 `1_000_000`, 1 이상 정수 보정).
- **DB 접속 및 단일 쿼리 일관성:**
  - `dash_inspect` 읽기 전용 접속(`default_transaction_read_only=on`, `statement_timeout=MANAGE_STATEMENT_TIMEOUT_MS`). `dash_owner` 미사용.
  - 단일 `SELECT <non_sensitive_cols> FROM raw.<table> WHERE <where> ORDER BY <order> LIMIT %s` 쿼리를 서버사이드 커서(named cursor, `itersize=2000`, `LIMIT export_max_rows + 1`)로 실행해 전체 결과가 PostgreSQL의 **단일 문장 스냅샷(한 시점)**에 해당하도록 보장한다.
  - 정렬 기준이 지정되지 않으면 기본 키 오름차순(`NULLS LAST`)으로 안정된 순서를 사용한다. 지정된 정렬 뒤에 기본 키를 tie-breaker로 덧붙인다.
  - **초기 오류의 정상 JSON 응답 보장:** `StreamingResponse`를 반환하기 전에 `conn` 연결 → 트랜잭션 시작 → 서버사이드 커서 `execute` → 첫 배치(`fetchmany(2000)`) 읽기까지를 먼저 수행한다. 이 단계에서 `UndefinedTable`(409 `rebuild_required`), `QueryCanceled`(504 `query_timeout`), `db_unavailable`(503)가 발생하면 `conn`을 닫고 정확한 HTTP 오류 JSON을 반환한다.
  - 첫 배치가 성공하면 `StreamingResponse` 제너레이터가 UTF-8 BOM(`\ufeff`) + CSV 머리글 + 첫 배치 + 이후 `fetchmany(2000)` 배치를 순차 yield한다.
  - `export_max_rows`행을 출력한 뒤 추가 행이 존재하면(`LIMIT export_max_rows + 1`의 마지막 1행 감지) `truncated = True`로 표시하고 즉시 스트리밍을 종료한다.
  - 제너레이터의 `finally` 블록에서 서버사이드 커서와 `dash_inspect` 커넥션을 반드시 닫고 감사 로그를 기록한다.
- **CSV 값 포맷:**
  - `None` → `""`, `bool` → `"true"` / `"false"`, `date`/`datetime` → ISO 문자열, `Decimal` → `format(v, "f")` 또는 정규 숫자 문자열, `int` → `str(v)`.
- **민감 열 보호(#37 연동):**
  - `sensitive: true` 열은 **SELECT 대상에서 제외**하고, 출력 CSV에서 해당 열 위치에는 `"***"`을 기록한다(설계서 10-4절).
  - `filters`나 `sort`에 민감 열이 포함되면 422 `invalid_filter` / `invalid_query`로 거절해 필터·정렬을 통한 값 유추를 차단한다.
- **감사 로그 (`data.export`):**
  - `auth_store.record_audit("data.export", ..., target_type="table", target_id=name, target_label=name, detail=...)`.
  - `detail`: `{"project": project.name, "table": name, "format": "csv", "columns": selected_cols, "filter_count": len(q.filters), "rows": exported_rows, "truncated": truncated}`. **필터 값이나 행 데이터 값은 절대 기록하지 않는다.**

### 3-3. 운용 화면 UI (`web/admin/rows.js`, `web/admin/menus/tables.js`, `web/strings.js`)

- `rowsView`에 선택 옵션 `exportPath`(문자열 또는 `null`)를 추가한다.
- `session.manage.features`에 `"export"`가 있을 때만 테이블 상세 미리보기(`previewPanel`)에서 `exportPath: "${tablePath(t.name)}/export"`를 전달한다(`datasets.js` 뷰 조회는 이번 이슈 범위가 아니므로 전달하지 않는다).
- `rowsView` 필터 도구 모음에 **"CSV 내보내기"** 버튼을 두고, 클릭 시 현재 적용된 필터(`Q.buildFilters`)와 정렬(`sort`)을 쿼리 문자열(`format=csv&filters=...&sort=...`)로 직렬화해 다운로드를 시작한다.
- `web/shared/query.js`에 순수 함수 `buildExportUrl(path, { filters, sort, columns })`를 추가해 JS 단위 테스트로 URL 직렬화를 검증한다.

---

## 4. 이슈 #36: 데이터 신선도 표시 (`MANAGE_FEATURES=freshness`)

### 4-1. 설정 및 스키마 계약 (`schema.yaml`)

- `contracts/schema.schema.json`: `$defs.table.properties`에 선택 키 `freshness` 추가.
  ```json
  "freshness": {
    "description": "데이터 갱신 기한(선택. 예: 30m, 12h, 7d). MANAGE_FEATURES=freshness일 때 기한을 넘기면 운용 화면 테이블 목록에 경고한다.",
    "type": "string",
    "pattern": "^[1-9][0-9]*[mhd]$"
  }
  ```
- `dashkit/config.py`: `parse_freshness(value: str) -> timedelta` 제공(`m`→분, `h`→시간, `d`→일). JSON Schema 검증 외에 `freshness` 문자열이 양의 기간인지 확인한다.
- `dashkit/project/ops.py`: `TABLE_KEYS`와 `TABLE_UPDATE_KEYS`에 `"freshness"` 추가(`table.add`, `table.update` 지원, `null`로 해제 가능).
- `dashkit/loader/record.py`: `freshness`는 DB 내용과 무관한 운용 메타데이터이므로 `db_relevant()`(`data_revision`)에는 포함하지 않는다.

### 4-2. 신선도 계산 규칙 (`dashkit/freshness.py`)

공용 순수 모듈 `dashkit/freshness.py`에 시간대 정규화, 테이블별 마지막 배치 시각, 탭별 `data_as_of`, 기한 초과(`is_stale`) 판정을 한 곳에 둔다.

1. **시간대 정규화:**
   - 모든 시각 계산은 UTC aware `datetime`(`timezone.utc`)으로 수행한다. naive `datetime`은 UTC로 간주해 정규화하고, API 출력은 항상 `YYYY-MM-DDTHH:MM:SSZ` 형식의 UTC ISO 문자열로 직렬화한다.
2. **테이블의 마지막 배치 시각 (`table_last_added_at(recorded, table)`):**
   - `dashkit_meta.batch`에서 `table`의 기록 중 `added_at is not None`인 레코드의 `max(added_at)`을 반환한다.
   - 기록이 없거나 모든 기록의 `added_at`이 `None`이면 `None`을 반환한다.
3. **탭의 `data_as_of` 계산 (`tab_data_as_of(project, tab, graph, recorded)`):**
   - 탭의 모든 섹션에서 참조하는 데이터셋 집합을 구한다:
     - `superset` 섹션: 해당 `dashboard`에 속한 모든 차트의 `dataset`.
     - 위젯 섹션: `sec.get("dataset")` (존재하는 경우).
   - 각 데이터셋의 계보(`graph["datasets"][ds]["sources"]`)에서 읽는 테이블 집합을 수집한다.
   - **명확한 경계 조건 처리:**

| 상황 | `data_as_of` 결과 | 이유 |
|---|---|---|
| 탭이 데이터셋/테이블을 하나도 참조하지 않음 | `null` | 기준이 되는 데이터 원본 없음 |
| 참조 데이터셋 중 하나라도 `sources is None` (계보 미기록/미적용 `sql:` 뷰) | `null` | 불완전한 계보로 오해 소지 있는 시각 노출 방지 |
| 참조 테이블 중 하나라도 마지막 배치 시각이 `None` (미적재 또는 배치 시각 없음) | `null` | 일부 테이블의 갱신 시점을 알 수 없어 부정확한 기준 시각 노출 방지 |
| 참조 테이블 모두 마지막 배치 시각이 존재 | `min(last_added_at for t in tables)` (UTC ISO 문자열) | 탭의 모든 그래프가 의존하는 테이블 중 가장 오래된 최신 배치 시각 |
| DB 적재 스냅샷의 `project != project.name` | `null` | 다른 프로젝트의 배치 기록 혼입 방지 |
| DB 접속/조회 실패 (`backend` `/api/app`) | `null` | 대시보드 셸 부팅 안정성 보장 |

4. **테이블 기한 초과 판정 (`is_stale(last_added_at, freshness, now_utc)`):**
   - `freshness`가 없거나 `last_added_at`이 `None`이면 `False` (미적재/시각 없음과 기한 초과를 구분).
   - `now_utc - last_added_at > parse_freshness(freshness)`일 때만 `True`.
   - **경계 시각:** `now_utc - last_added_at == delta`는 아직 초과 전이므로 `False`, 1초라도 넘으면 `True`.

### 4-3. API 반영

1. **백엔드 `GET /api/app` (`dashkit/api/app.py`):**
   - **읽기 계정:** `dash_reader` (`db.reader_conn()`)만 사용한다(읽기 전용 트랜잭션, 타임아웃 적용). `dash_inspect`나 `dash_owner`를 사용하지 않는다.
   - `freshness`가 `MANAGE_FEATURES`에 **없으면**: DB를 조회하지 않으며 탭 객체에 `data_as_of` 키를 추가하지 않는다(기존 계약·테스트 100% 하위 호환).
   - `freshness`가 `MANAGE_FEATURES`에 **있으면**: `dash_reader`로 `ledger.loaded(cur)`와 `ledger.read(cur)`를 읽어 `lineage.build(project, loaded.snapshot)`를 구성하고, 각 탭 객체에 `"data_as_of": <UTC ISO 문자열 | null>`을 추가한다.
2. **관리 서비스 `GET /api/manage/tables` 및 `GET /api/manage/tables/{t}` (`dashkit/manage/routes/tables.py`):**
   - **읽기 계정:** 기존 `dash_inspect` 그대로 사용한다.
   - `freshness`가 `MANAGE_FEATURES`에 **있으면** `GET /api/manage/tables`의 각 항목에 `"freshness": spec.get("freshness")`와 `"stale": bool`을 추가한다(비활성 시 키 미포함).
   - `GET /api/manage/tables/{t}`에는 스키마에 `freshness`가 설정돼 있을 때 `"freshness": spec["freshness"]`를 포함한다.

### 4-4. UI 반영

- **웹앱 대시보드 (`web/app.js`, `web/strings.js`, `web/styles.css`):**
  - 탭 객체에 `tab.data_as_of` 문자열이 있으면 해당 탭 뷰(`section.view`) 상단에 탭 머리 표시줄(`.tab-freshness`, `role="status"`)을 렌더링한다: `"데이터 기준: <로컬 포맷 시각>"` + `title="<원본 UTC ISO 시각>"`.
  - `tab.data_as_of`가 없거나 `null`이면 표시줄을 렌더링하지 않는다.
- **운용 화면 테이블 목록 (`web/admin/menus/tables.js`, `web/strings.js`):**
  - `session.manage.features`에 `"freshness"`가 포함되고 `t.stale === true`이면 "마지막 배치" 셀에 경고 배지 `pill(D.staleHint(t.freshness), "warn")`을 표시한다.

---

## 변경 파일 범위

1. **공통 기반 & 설정·계약**
   - `dashkit/features.py` (신규): `KNOWN_MANAGE_FEATURES`, `manage_features()`, `has_manage_feature(name)`
   - `dashkit/freshness.py` (신규): `parse_freshness()`, `table_last_added_at()`, `tab_data_as_of()`, `is_stale()`, UTC ISO 직렬화
   - `dashkit/config.py`: `freshness` 검증 및 헬퍼 연동
   - `dashkit/project/ops.py`: `TABLE_KEYS`·`TABLE_UPDATE_KEYS`에 `"freshness"` 추가
   - `dashkit/manage/context.py`: `Limits.job_lock_wait_ms`(`MANAGE_JOB_LOCK_WAIT_MS`), `Limits.export_max_rows`(`EXPORT_MAX_ROWS`) 추가
   - `dashkit/manage/routes/base.py`: `GET /config`에 `features` 추가
   - `.env.example`, `docker-compose.yml`: `MANAGE_FEATURES`, `EXPORT_MAX_ROWS`, `MANAGE_JOB_LOCK_WAIT_MS` 추가(`backend`에 `MANAGE_FEATURES`, `manager`에 셋 모두 전달)
2. **이슈 #34 (잠금 대기·미리보기 안내)**
   - `dashkit/ingest/operation.py`, `dashkit/ingest/remove.py`, `dashkit/ingest/compact.py`, `dashkit/ingest/structure.py`
   - `dashkit/manage/routes/tables.py`, `dashkit/manage/routes/structure.py`
   - `web/shared/ingest.js`, `web/admin/menus/tables.js`, `web/strings.js`
3. **이슈 #32·#33 (열 통계·CSV 내보내기)**
   - `dashkit/manage/routes/rows.py` (또는 `profile.py`·`export.py` 분리 후 `routes/__init__.py` 등록): `GET /tables/{table}/profile`, `GET /tables/{table}/export`
   - `web/shared/query.js`: `buildExportUrl` 추가
   - `web/admin/rows.js`: `exportPath` 옵션과 CSV 내보내기 버튼
   - `web/admin/menus/tables.js`, `web/strings.js`: 통계 탭("통계 계산" 버튼·표) 및 내보내기 연동
4. **이슈 #36 (데이터 신선도)**
   - `dashkit/api/app.py`: `GET /api/app` 탭별 `data_as_of` (`dash_reader` 전용, `freshness` 활성 시)
   - `dashkit/manage/routes/tables.py`: `GET /tables`의 `freshness`·`stale`, `GET /tables/{t}`의 `freshness`
   - `web/app.js`, `web/styles.css`, `web/strings.js`: 탭 머리 데이터 기준 시각 표시
   - `web/admin/menus/tables.js`, `web/strings.js`: 테이블 목록 기한 초과 경고 배지
5. **계약 문서**
   - `contracts/schema.schema.json`: 테이블 `freshness` 속성 추가
   - `contracts/manage.md`: `GET /config`의 `features`, `GET /tables/{t}/profile`, `GET /tables/{t}/export`, `GET /tables`의 `freshness`·`stale`, 한도(`MANAGE_JOB_LOCK_WAIT_MS`, `EXPORT_MAX_ROWS`, `MANAGE_FEATURES`), 202 작업 `lock` 단계 및 50만 행 미리보기 안내
   - `contracts/ingest.md`: `MANAGE_LOCK_WAIT_MS` vs `MANAGE_JOB_LOCK_WAIT_MS` 구분
   - `contracts/api.md`: `GET /api/app` 탭의 선택 필드 `data_as_of` (`MANAGE_FEATURES=freshness`)
   - `contracts/auth.md`: 감사 로그 표에 `data.export` 추가

## 검증 계획

| 영역 | 검증 항목 및 기대 결과 |
|---|---|
| 공통·단위·정적 | `make test-unit`, `node --test tests/js/*.test.mjs`, `pytest tests/test_admin_ui_static.py tests/test_domain_words.py`, `git diff --check` 통과. `MANAGE_FEATURES` 파싱(빈 값, 공백, 대소문자, 미지정 값 무시) 단위 테스트. |
| #34 잠금 대기·안내 | 선행 잠금 중 202 작업이 `step: "lock"`으로 대기 후 성공; `MANAGE_JOB_LOCK_WAIT_MS` 초과 시 작업 실패 409 `apply_running`; 즉시 응답(직접 입력·빠른 취소) `MANAGE_LOCK_WAIT_MS` 초과 시 HTTP 409; 비후보 `DELETE`의 202 대기 성공; `499_999` / `500_000` 행 경계 JS 테스트. |
| #32 열 통계 (`profile`) | `MANAGE_FEATURES` 비활성 시 404 `not_found`; `user` 403 / 비인증 401; `dash_inspect`만 사용(`dash_owner` 미호출); 빈 테이블, NULL 포함/전부 NULL, `integer`·`number`·`date`·`timestamp`·`text`·`enum`·`boolean` 유형별 결과; `sensitive: true` 열 SQL 제외 및 null 마스킹; `statement_timeout` 초과 504 `query_timeout`. |
| #33 CSV 내보내기 (`export`) | `MANAGE_FEATURES` 비활성 시 404 `not_found`; `user` 403 / 비인증 401; 필터·정렬·열 검증 및 SQL 인젝션 방지(`paging.parse_query`·`where_clause`); `dash_inspect`만 사용; `EXPORT_MAX_ROWS` 절단 및 스트리밍 응답(UTF-8 BOM·헤더·행); `sensitive: true` 열 `***` 마스킹 및 필터·정렬 차단; 감사 로그 `data.export` 기록(데이터·필터 값 미포함). |
| #36 데이터 신선도 (`freshness`) | 여러 테이블·그래프 중 가장 오래된 시각 선택; 빈 테이블·계보 없음(`sources: null`)·배치 시각 없음(`added_at: null`)·다른 프로젝트 스냅샷 시 `data_as_of: null`; UTC 정규화 및 기한 경계(`== delta` 정상 vs `> delta` 경고); `MANAGE_FEATURES=freshness` 활성/비활성 분기; `/api/app`이 `dash_reader`만 사용. |
| 통합·회귀 | 격리된 테스트 스택에서 `AUTH_COOKIE_SECURE=0 make test` 전체 통과(동시 쓰기·복구·프로젝트 전환·관리 API·읽기 경로 계정 격리 `test_read_paths_never_open_the_owner_account` 포함) 후 `make test-down`. |

## 완료 조건

- [ ] `MANAGE_FEATURES`(`export`, `profile`, `freshness`)와 `GET /api/manage/config`의 `features`가 동작하며, 비활성 선택 기능 경로는 404 `not_found`를 반환한다.
- [ ] 이슈 #34의 잠금 대기 한도 분리, `step: "lock"` 통일, 499,999/500,000행 미리보기 안내가 검증되었다.
- [ ] 이슈 #32의 열 통계 API·UI가 `dash_inspect` 전용, 유형별/빈 테이블/NULL/시간 초과/민감 열 보호 조건을 모두 만족한다.
- [ ] 이슈 #33의 CSV 내보내기 API·UI가 `dash_inspect` 전용, 필터 검증 재사용, 단일 쿼리 스트리밍, `EXPORT_MAX_ROWS`, 민감 열 보호, `data.export` 감사 로그 조건을 모두 만족한다.
- [ ] 이슈 #36의 탭별 `data_as_of`(`/api/app`, `dash_reader` 전용)와 테이블 `freshness` 기한 경고(`/api/manage/tables`, `schema.yaml`)가 경계 시각·시간대·누락 케이스와 함께 검증되었다.
- [ ] 관련 `contracts/` 문서, `.env.example`, `docker-compose.yml`, 단위·JS·통합 테스트가 모두 갱신되고 통과했다.
