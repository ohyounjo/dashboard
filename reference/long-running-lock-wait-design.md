# 장시간 작업의 잠금 대기와 대용량 미리보기 안내 설계

[이슈 #34](https://github.com/ohyounjo/dashboard/issues/34)의 구현 설계안이다. 200MB에 가까운 파일(209.7MB·177만 행) 실측에서 쓰기 잠금 구간이 38.2초 지속되어 다른 쓰기 작업이 기본 5초(`MANAGE_LOCK_WAIT_MS`) 뒤 409 `apply_running`으로 실패했다. 이 문서는 비동기(202) 작업과 즉시 응답(200)의 잠금 대기 한도를 분리하고, 모든 202 작업의 잠금 대기 단계(`lock`) 표시와 50만 행 이상 대용량 미리보기 안내를 추가하기 위한 상세 사양과 검증 계획을 정의한다.

참조: [데이터 설계서 10-3·12·14·15절](./data-design.md#10-3-동시-쓰기) · [관리 서비스 계약](../contracts/manage.md) · [적재 계약](../contracts/ingest.md) · [오케스트레이션 프롬프트](./long-running-lock-wait-prompt.md)

## 현재 구현 분석

현재 코드에서 확인된 동작과 보완 지점은 다음과 같다.

| 영역 | 현재 상태 | 보완 필요 사항 |
|---|---|---|
| 한도 설정 (`dashkit/manage/context.py`, `.env.example`, `docker-compose.yml`) | `Limits`에 `lock_wait_ms`(`MANAGE_LOCK_WAIT_MS`, 기본 5000ms) 하나만 존재 | 202 작업 전용 `job_lock_wait_ms`(`MANAGE_JOB_LOCK_WAIT_MS`, 기본 600000ms)를 분리한다. |
| 202 작업 라우트 (`dashkit/manage/routes/tables.py`, `structure.py`) | `_ingest_job`, `delete_batch`(rebuild), `post_restore`, `patch_config`, `post_table`, `post_compact`, `post_history_restore` 모두 `limits.lock_wait_ms` 사용 | 202 작업은 모두 `limits.job_lock_wait_ms`를 사용하고, 즉시 응답(`rows` 직접 입력·붙여넣기, 빠른 취소)은 `limits.lock_wait_ms`를 유지한다. |
| 배치 되돌리기 진입 (`dashkit/manage/routes/tables.py` `delete_batch`) | 202 작업 진입 전에 `remove_batch(..., fast_only=True, wait_ms=limits.lock_wait_ms)`를 먼저 호출 | 빠른 길이 불가능한 이전 배치·수정 포함 배치 되돌리기조차 다른 쓰기 중 5초 뒤 409로 실패해 202 `remove` 작업으로 진입하지 못한다. 읽기 전용 사전 판별로 비후보를 바로 202 작업으로 넘긴다. |
| 대기 중 진행 단계 (`dashkit/ingest/add.py`, `remove.py`, `compact.py`, `structure.py`) | `add.py`만 잠금 직전 `progress("lock", {})` 호출. `remove`·`restore`는 시작 시 `"rebuild"`, `_config_job`(`config`·`create`·`compact`·`restore_config`)은 `"apply"`를 바로 설정 | 모든 202 작업이 잠금 대기 중 `{"step": "lock"}`을 보고하고, 잠금을 잡은 직후 본 작업 단계(`rebuild`·`apply`)로 전이한다. |
| 구조 변경 잠금 순서 (`dashkit/ingest/structure.py` `change_config`) | `with store.project_lock(project.name), locked(..., wait_ms=wait_ms)` 순서로 프로세스 내 잠금을 먼저 점유 | DB 잠금을 최대 10분 기다리는 동안 DB 잠금과 무관한 `POST /tables/{t}/aliases`가 막히지 않도록 DB `locked()` 확보 후 `store.project_lock`을 잡는다. |
| 대용량 미리보기 안내 (`web/shared/ingest.js`, `web/admin/menus/tables.js`, `web/strings.js`) | 50만 행 이상 미리보기 안내 로직과 문구가 없음 | 단일·여러 파일 미리보기 공통으로 넣을 행 수 합계 ≥ 500,000일 때 안내 문구를 표시한다. |

## 확정 설계 사양

### 1. 잠금 대기 한도 분리 (`MANAGE_JOB_LOCK_WAIT_MS` vs `MANAGE_LOCK_WAIT_MS`)

환경 변수와 `dashkit/manage/context.py`의 `Limits`를 다음과 같이 분리한다.

| 환경 변수 | `Limits` 필드 | 기본값 | 용도 |
|---|---|---|---|
| `MANAGE_LOCK_WAIT_MS` | `lock_wait_ms` | `5000` (5초) | 즉시 응답 요청(직접 입력·붙여넣기, 빠른 취소 후보)의 쓰기 잠금 대기 한도. 초과 시 HTTP 409 `apply_running`. |
| `MANAGE_JOB_LOCK_WAIT_MS` | `job_lock_wait_ms` | `600000` (10분) | 202 비동기 작업의 쓰기 잠금 대기 한도. 초과 시 작업 `status: "failed"`, `error: {"status": 409, "detail": "apply_running", ...}`. |

- `Limits.public()`(`GET /api/manage/config`의 `limits`)에는 기존과 동일하게 UI 입력 한도(`inline_max_rows`, `max_upload_mb`, `max_rows`, `max_request_mb`, `compact_hint`, `page_max`)만 노출한다. 서버 대기 시간은 노출하지 않는다.
- CLI·Studio의 `load`(`LOCK_WAIT_MS=10_000`), `dashkit ingest` CLI의 기본 대기(`operation.lock_wait_ms()`), 커밋 결과 불명 판정(`operation.resolve()`)은 기존 동작을 유지한다.

엔드포인트별 적용 기준은 다음과 같다.

| 메서드·경로 | 요청 유형 | 응답 | 잠금 대기 한도 |
|---|---|---|---|
| `POST /tables/{t}/batches` | JSON `rows` (`form` / `paste`, `dry_run` 포함) | 200 (즉시) | `MANAGE_LOCK_WAIT_MS` (5초) |
| `DELETE /tables/{t}/batches/{file}` | 빠른 취소 후보 (`fast_only=True`) | 200 (즉시) | `MANAGE_LOCK_WAIT_MS` (5초) |
| `POST /tables/{t}/batches` | multipart `file` / JSON `upload_id` (`dry_run` 포함) | 202 (`ingest`) | `MANAGE_JOB_LOCK_WAIT_MS` (10분) |
| `POST /batches` | multipart `files` / JSON `upload_id` (`dry_run` 포함) | 202 (`ingest`) | `MANAGE_JOB_LOCK_WAIT_MS` (10분) |
| `DELETE /tables/{t}/batches/{file}` | 다시 만들기 경로 | 202 (`remove`) | `MANAGE_JOB_LOCK_WAIT_MS` (10분) |
| `POST /tables/{t}/batches/{file}/restore` | 배치 되살리기 | 202 (`restore`) | `MANAGE_JOB_LOCK_WAIT_MS` (10분) |
| `PATCH /config` | 구조 변경 연산 | 202 (`config`) | `MANAGE_JOB_LOCK_WAIT_MS` (10분) |
| `POST /tables` | 새 테이블 만들기 | 202 (`create`) | `MANAGE_JOB_LOCK_WAIT_MS` (10분) |
| `POST /tables/{t}/compact` | 배치 합치기 | 202 (`compact`) | `MANAGE_JOB_LOCK_WAIT_MS` (10분) |
| `POST /history/{id}/restore` | 설정 이력 되돌리기 | 202 (`restore_config`) | `MANAGE_JOB_LOCK_WAIT_MS` (10분) |

#### `DELETE /tables/{t}/batches/{file}` 빠른 취소 사전 판별

`delete_batch`에서 빠른 길이 명백히 불가능한 배치를 202 `remove` 작업으로 바로 보내기 위해, 잠금을 잡기 전에 폴더 목록(`batches.list_batches`)과 읽기 전용 계정(`dash_inspect`, `_recorded()`)만으로 사전 판별한다.

1. `file`이 현재 테이블 배치 폴더에 존재하고 합친 배치(`batches.is_merged(file)`)가 아닐 때:
   - `file`이 폴더 내 가장 큰 번호의 배치가 아니거나,
   - 배치 기록(`_recorded()`)에 해당 파일이 있고 `rec.inserted != rec.rows or rec.updated or rec.skipped or rec.unchanged`이면
   → 빠른 길 비후보로 판단하고 `fast_only=True`를 건너뛰어 바로 202 `remove` 작업(`wait_ms=limits.job_lock_wait_ms`)을 시작한다.
2. 그 외(빠른 길 후보이거나, 존재하지 않는 배치·합친 배치로 404 `unknown_batch`·409 `merged_batch`를 즉시 반환해야 하는 경우)는 기존처럼 `remove_batch(..., fast_only=True, wait_ms=limits.lock_wait_ms)`를 먼저 시도한다.
   - 빠른 취소 성공 시 200, 잠금 5초 초과 시 409 `apply_running`.
   - 잠금을 잡았으나 해시 불일치·폴더 차이 등으로 `NeedsRebuild`가 발생하면 202 `remove` 작업(`wait_ms=limits.job_lock_wait_ms`)으로 전환한다.
3. 사전 판별은 라우팅 최적화일 뿐이며, 최종 유효성·프로젝트·복구·대조·삭제 판정은 기존과 동일하게 쓰기 잠금 안에서 `remove_batch`가 권위 있게 수행한다. 사전 판별은 `dash_inspect`만 사용하며 `dash_owner` 연결을 열지 않는다.

#### `change_config`의 잠금 순서

`dashkit/ingest/structure.py`의 `change_config`에서 DB 쓰기 잠금(`locked(..., wait_ms=wait_ms)`)을 먼저 잡은 뒤 `with store.project_lock(project.name):`에 진입한다.

- `POST /tables/{t}/aliases`는 `store.project_lock`만 사용하고 DB 쓰기 잠금을 잡지 않으므로 잠금 순서 역전에 따른 교착 위험이 없다.
- 구조 변경 작업이 DB 쓰기 잠금을 최대 10분 기다리는 동안에도 `aliases` 저장이 불필요하게 차단되지 않는다.
- `locked()`의 `revision` 검사는 `store.project_lock` 밖에서 실행되므로, `post_aliases`가 프로젝트 잠금을 보유한 채 `schema.yaml`을 갱신하는 경쟁을 막기 위해 **`store.project_lock(project.name)`을 획득한 직후(파일을 읽거나 `plan_files`·`history_files`를 실행하기 전) `store.revision(project.root) != revision`을 다시 검사해 어긋나면 `StaleConfig`(409 `stale_config`)로 거절**한다.

### 2. 진행 단계(`step: "lock"`)와 한도 초과 409 처리

#### 서버 측 진행 단계 보고

`dashkit/ingest/operation.py`의 `locked()`에 선택 콜백 `on_acquired: Callable[[], None] | None = None`을 추가해 `locks.acquire(...)` 성공 직후 호출한다. 각 작업 함수(`remove_batch`, `restore_batch`, `compact`, `change_config`)도 선택 콜백 `progress: Callable[[str, Mapping[str, Any]], None] | None = None`을 받는다.

| 작업 `kind` | 잠금 대기 전·중 `progress.step` | 잠금 획득 직후 `progress.step` | 비고 |
|---|---|---|---|
| `ingest` | `"read"` → `"lock"` | `"lock"` 유지 | 기존 `contracts/manage.md` 및 `test_manage_data.py` 호환. `StaleConfig` 재시도 시 다시 `"read"` → `"lock"`. |
| `remove`, `restore` | `"lock"` | `"rebuild"` | 작업 시작 시 `ctx.progress("lock")` 후 `on_acquired`에서 `progress("rebuild", {})`. |
| `config`, `create`, `compact`, `restore_config` | `"lock"` | `"apply"` | `_config_job` 시작 시 `ctx.progress("lock")` 후 `on_acquired`에서 `progress("apply", {})`. |

- 한도(`MANAGE_JOB_LOCK_WAIT_MS`) 안에 잠금을 잡지 못하면 `locks.acquire` → `LockBusy` → `IngestError(409, "apply_running", ...)`가 발생하고, `Runs._run`이 작업 결과에 `status: "failed"`, `error: {"status": 409, "detail": "apply_running", "message": ...}`를 남긴다.

#### UI 표시

- `web/strings.js`의 `S.admin.data.jobStep.lock`은 이미 `"다른 쓰기가 끝나기를 기다리는 중…"`으로 정의되어 있다.
- `web/admin/datakit.js`의 `jobText(job)` · `jobBox(job)` · `runJob(...)`이 모든 202 작업(단일·여러 파일 적재, 배치 되돌리기·되살리기, 열 추가·규칙·삭제, 새 테이블, 합치기, 설정 이력 되돌리기)의 폴링 표시를 공통 처리하므로, 서버가 `step: "lock"`을 반환하면 모든 202 작업 화면에 대기 안내가 일관되게 표시된다.

### 3. 50만 행 이상 대용량 미리보기 안내

- **기준**: `LARGE_BATCH_ROWS = 500_000` (209.7MB·177만 행 실측 38.2초에서 단순 비례로 추정한 안내 기준이며, 실제 대기 시간은 환경과 `checks`에 따라 다르다).
- **순수 함수 (`web/shared/ingest.js`)**:
  - `LARGE_BATCH_ROWS = 500_000` 상수를 export한다.
  - `previewInsertRows(batches)`: 미리보기 응답의 `batches` 배열을 순회해 각 배치의 넣을 행 수 `Math.max(0, (Number(b.rows.total) || 0) - (Number(b.rows.invalid) || 0))`를 합산한다.
    - 정상 미리보기(`invalid === 0`)에서는 `total`과 동일하다.
    - 오류 행 빼고 넣기(`skip_invalid`)가 가능한 미리보기에서는 실제로 넣을 행 수(`total - invalid`)를 기준으로 판정한다.
    - 여러 파일 미리보기는 한 트랜잭션으로 반영되어 잠금 구간이 합산되므로 모든 파일의 행 수를 합산한다.
  - `isLargeBatchPreview(batches, threshold = LARGE_BATCH_ROWS)`: `previewInsertRows(batches) >= threshold` 여부를 반환한다.
- **UI 표시 (`web/admin/menus/tables.js`, `web/strings.js`)**:
  - `S.admin.data.largeBatchHint` 문구를 `web/strings.js`에 추가한다(예: `"넣을 행이 50만 행 이상입니다. 반영하는 동안 다른 쓰기가 기다릴 수 있으며, 나눠 올리면 대기가 짧아집니다."`).
  - `runNotes(r, { preview })`에서 `preview === true`이고 `I.isLargeBatchPreview(r.batches)`가 참일 때 `D.largeBatchHint`를 포함한다.
  - `resultBox`(단일 파일)와 `bulkResult`(여러 파일) 모두 `runNotes(r, { preview })`를 호출하므로 두 경로에 일관되게 표시되며, 확정 결과(`preview: false`)에는 표시되지 않는다.
  - `web/admin/`의 모든 한글 문구는 `web/strings.js`에만 두어 `tests/test_admin_ui_static.py`를 만족한다.

### 4. 유지하는 원칙(변경하지 않는 것)

- 데이터 DB 전역 단일 세션 advisory lock(`dashkit/project/locks.py`, `WRITE_LOCK_KEY`)을 유지한다. 테이블별·프로젝트별 잠금으로 나누지 않는다.
- 공통 순서(잠금 → 프로젝트 확인 → 설정 `revision`·`data_revision` 확인 → 복구 → 대조 → 작업 → `checks` → 커밋)와 배치 단일 트랜잭션 원자성을 유지한다. 배치를 여러 트랜잭션으로 분할하지 않는다.
- 읽기 경로는 `dash_inspect` 또는 기존 읽기 전용 계정만 사용하고, `dash_owner`는 쓰기 작업에서만 연다. 비밀값은 환경 변수로만 관리한다.

## 변경 파일 범위

1. **백엔드 설정·잠금·라우트**
   - `dashkit/manage/context.py`: `Limits.job_lock_wait_ms` 추가 (`MANAGE_JOB_LOCK_WAIT_MS`, 기본 `600000`).
   - `dashkit/ingest/operation.py`: `locked()`에 `on_acquired` 콜백 지원.
   - `dashkit/ingest/remove.py`, `dashkit/ingest/compact.py`, `dashkit/ingest/structure.py`: `progress` 콜백 수용, 잠금 대기 전 `"lock"` 및 획득 직후 본 단계(`"rebuild"`/`"apply"`) 보고, `change_config`의 `locked()` → `store.project_lock` 순서 조정 및 프로젝트 잠금 획득 직후 `revision` 재검사 추가.
   - `dashkit/manage/routes/tables.py`: `_ingest_job`, `delete_batch`(rebuild 경로 + 읽기 전용 빠른 길 사전 판별), `post_restore`에 `limits.job_lock_wait_ms` 및 `progress` 연결. `rows` 즉시 입력과 `fast_only=True`는 `limits.lock_wait_ms` 유지.
   - `dashkit/manage/routes/structure.py`: `_config_job`의 `change_config`·`compact`에 `limits.job_lock_wait_ms` 및 `progress` 연결, 초기 단계를 `"lock"`으로 변경.
2. **프런트엔드**
   - `web/shared/ingest.js`: `LARGE_BATCH_ROWS`, `previewInsertRows`, `isLargeBatchPreview` 추가.
   - `web/strings.js`: `S.admin.data.largeBatchHint` 추가.
   - `web/admin/menus/tables.js`: `runNotes(r, { preview })`, `resultBox`, `bulkResult`에서 대용량 미리보기 안내 표시.
3. **환경·계약 문서**
   - `.env.example`, `docker-compose.yml`: `MANAGE_JOB_LOCK_WAIT_MS=600000` 추가.
   - `contracts/manage.md`: "한도" 표에 `MANAGE_JOB_LOCK_WAIT_MS`(기본 `600000`) 추가, "작업"·"데이터 더하기" 절에 모든 202 작업의 `lock` 대기 단계와 50만 행 미리보기 안내 규칙 반영.
   - `contracts/ingest.md`: "쓰기 잠금" 절에 관리 서비스의 즉시 요청(`MANAGE_LOCK_WAIT_MS`) vs 202 작업(`MANAGE_JOB_LOCK_WAIT_MS`) 구분 반영.
4. **테스트**
   - `tests/test_manage_routes.py` 및/또는 `tests/test_manage_data.py`·`tests/test_manage_structure.py`: 잠금 대기·성공, `step: "lock"` 관측, `MANAGE_JOB_LOCK_WAIT_MS` 초과 409, 즉시 응답(직접 입력·빠른 취소) `MANAGE_LOCK_WAIT_MS` 초과 409, 비후보 배치 되돌리기의 202 대기 성공 테스트 추가.
   - `tests/js/ingest.test.mjs`: `499_999` / `500_000` 행 경계(단일 파일, 여러 파일 합산, `invalid` 제외) 테스트 추가.

## 검증 계획

| 검증 항목 | 확인 내용 및 기대 결과 |
|---|---|
| 단위·정적 테스트 | `make test-unit`, `node --test tests/js/ingest.test.mjs`, `pytest tests/test_admin_ui_static.py`, `git diff --check` 통과. |
| 202 작업 잠금 대기 및 단계 표시 | 선행 쓰기가 DB advisory lock을 잡은 상태에서 후속 파일 적재(및 `remove`/`restore`/`compact`/`config`) 202 작업을 시작하면 `GET /api/manage/jobs/{id}`의 `progress.step`이 `"lock"`으로 보이고, 선행 잠금 해제 후 409 없이 `status: "succeeded"`로 완료됨. |
| 긴 대기 한도 초과 409 | `MANAGE_JOB_LOCK_WAIT_MS`를 짧게(예: 200ms) 두고 잠금이 유지되는 동안 202 작업을 실행하면 `status: "failed"`, `error.status == 409`, `error.detail == "apply_running"`. |
| 즉시 응답 5초(짧은 한도) 초과 409 | 잠금이 유지되는 동안 직접 입력·붙여넣기(`POST /tables/{t}/batches` JSON `rows`)와 빠른 취소(`DELETE /tables/{t}/batches/{latest}`)가 `MANAGE_LOCK_WAIT_MS` 초과 시 즉시 HTTP 409 `apply_running`을 반환함. |
| 499,999 / 500,000행 경계 | `isLargeBatchPreview`: 단일 파일 499,999행 `false`, 500,000행 `true`; 여러 파일 `250,000 + 249,999` `false`, `250,000 + 250,000` `true`; `total: 500,000, invalid: 1` `false`, `total: 500,001, invalid: 1` `true`. |
| 동시 쓰기·복구·프로젝트 전환 회귀 | `tests/test_ingest_db.py`, `tests/test_ingest_remove.py`, `tests/test_ingest_structure.py`, `tests/test_manage_*.py`와 `AUTH_COOKIE_SECURE=0 make test` 전체 통과. 읽기 경로에서 `dash_owner` 미사용 확인(`test_read_paths_never_open_the_owner_account`). |

## 완료 조건

- [ ] `MANAGE_JOB_LOCK_WAIT_MS`(기본 600000ms)와 `MANAGE_LOCK_WAIT_MS`(기본 5000ms)가 202 작업과 즉시 응답으로 분리되었다.
- [ ] 큰 파일(또는 선행 쓰기) 작업 중 다른 202 파일 작업이 기다렸다가 성공하고, 대기 중 진행 단계가 `lock`("다른 쓰기가 끝나기를 기다리는 중")으로 보인다.
- [ ] 긴 대기 한도 초과(202 작업 결과 409 `apply_running`)와 즉시 응답의 대기 한도 초과(HTTP 409 `apply_running`)가 각각 검증되었다.
- [ ] 499,999 / 500,000행 경계에서 대용량 미리보기 안내 여부가 검증되었다.
- [ ] `contracts/manage.md`, `contracts/ingest.md`, `.env.example`, `docker-compose.yml`과 단위·통합 테스트(동시 쓰기·복구·프로젝트 전환·기존 테스트)가 갱신되고 모두 통과했다.
