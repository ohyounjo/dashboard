# 관리 서비스 보완·선택 기능 통합 구현 오케스트레이션 프롬프트 (#34, #36, #33, #32)

설계 PR이 병합된 뒤 `ohyounjo/dashboard` 저장소에서 새 세션을 열고 아래 블록을 붙여 넣는다. [통합 설계서](manage-enhancements-design.md)

````text
# 목표

ohyounjo/dashboard의 이슈 #34, #36, #33, #32를 통합 설계서(reference/manage-enhancements-design.md)대로 구현·검증하고 PR을 올린다. 실제 확인하지 않은 결과를 성공으로 기록하지 않는다.

# 시작과 격리

- AGENTS.md, GitHub 이슈 #34·#36·#33·#32의 최신 본문·댓글, reference/manage-enhancements-design.md, reference/long-running-lock-wait-design.md, reference/data-design.md(8-1, 8-2, 8-4, 9-1, 9-3, 10-3, 10-4, 10-6, 12, 14~15절)를 읽는다.
- contracts/manage.md, contracts/ingest.md, contracts/api.md, contracts/auth.md, contracts/schema.schema.json과 관련 코드를 확인한다. 이슈와 계약이 다르면 계약을 기준으로 해석하고 차이를 보고한다.
- 최신 origin/main을 가져와 기준 SHA를 기록하고 새 격리 worktree와 작업 브랜치(예: feat/manage-issues-32-33-34-36)를 준비한다.
- 만약 origin/main에 이슈 #34 구현이 이미 병합되어 있으면 중복 변경 없이 #32·#33·#36과 공통 기반만 얹고, 미병합이면 4개 이슈를 모두 구현한다.
- 비밀값·토큰은 환경 변수로만 취급하고, 읽기 경로는 dash_inspect(관리 서비스) 또는 dash_reader(backend)만 사용한다.

# 구현

1. 공통 기반 (`MANAGE_FEATURES`):
   - `dashkit/features.py`: `KNOWN_MANAGE_FEATURES = frozenset({"export", "profile", "freshness"})`, `manage_features()`, `has_manage_feature(name)` 구현.
   - `dashkit/manage/routes/base.py`: `GET /config`에 `"features": sorted(manage_features())` 추가.
   - `dashkit/manage/context.py`: `Limits`에 `job_lock_wait_ms`(`MANAGE_JOB_LOCK_WAIT_MS`, 기본 600000), `export_max_rows`(`EXPORT_MAX_ROWS`, 기본 1000000) 추가.
   - `.env.example`, `docker-compose.yml`: `MANAGE_FEATURES`, `EXPORT_MAX_ROWS`, `MANAGE_JOB_LOCK_WAIT_MS` 반영(`backend`에 `MANAGE_FEATURES`, `manager`에 3개 모두 전달).
2. 이슈 #34 (미병합 시):
   - reference/long-running-lock-wait-design.md 사양대로 202 작업 vs 즉시 응답 잠금 대기 한도 분리, `delete_batch` 읽기 전용 사전 판별, `change_config` 잠금 순서 조정 및 `project_lock` 획득 직후 `revision` 재검사, 모든 202 작업 `step: "lock"` 통일, 50만 행 이상 대용량 미리보기 안내를 구현한다.
3. 이슈 #32 (`MANAGE_FEATURES=profile`):
   - `GET /api/manage/tables/{table}/profile`: `require_operator`, 비활성 시 404 `not_found`, `dash_inspect` 읽기 전용 접속 + `MANAGE_STATEMENT_TIMEOUT_MS` 타임아웃(504 `query_timeout`).
   - 단일 SELECT 집계로 `total_rows`와 비민감 열별 `null_count`, `null_ratio`, `distinct_count`, `min`, `max` 계산(`boolean`은 `min`/`max`를 `null`, `text`·`enum`은 2000자 절단).
   - `sensitive: true` 열은 집계 SQL에서 제외하고 통계 필드를 `null`로 반환한다.
   - 운용 화면(`web/admin/menus/tables.js`, `web/strings.js`): `features`에 `"profile"`이 있을 때만 "통계" 탭을 표시하고 "통계 계산" 버튼을 눌렀을 때만 조회한다.
4. 이슈 #33 (`MANAGE_FEATURES=export`):
   - `GET /api/manage/tables/{table}/export?format=csv`: `require_operator`, 비활성 시 404 `not_found`, `dash_inspect` 읽기 전용 접속.
   - `paging.parse_query`와 `paging.where_clause`를 재사용해 필터·정렬·열을 검증하고, 단일 쿼리 서버사이드 커서(`LIMIT EXPORT_MAX_ROWS + 1`)로 UTF-8 BOM CSV를 스트리밍한다(첫 배치 확인 후 `StreamingResponse` 반환, `finally`에서 커서·접속 종료 및 `data.export` 감사 로그 기록).
   - `sensitive: true` 열은 SQL에서 제외해 CSV에 `***`로 출력하고, 민감 열에 대한 필터·정렬은 422로 거절한다. 감사 로그에는 데이터 값과 필터 값을 싣지 않는다.
   - 운용 화면(`web/shared/query.js`, `web/admin/rows.js`, `web/admin/menus/tables.js`, `web/strings.js`): `features`에 `"export"`가 있을 때만 미리보기 필터 도구 모음에 "CSV 내보내기" 버튼을 노출한다.
5. 이슈 #36 (`MANAGE_FEATURES=freshness`):
   - `contracts/schema.schema.json`, `dashkit/config.py`, `dashkit/project/ops.py`: 테이블 선택 속성 `freshness` (`^[1-9][0-9]*[mhd]$`) 추가. `data_revision`(`db_relevant`)에는 넣지 않는다.
   - `dashkit/freshness.py`: `parse_freshness`, `table_last_added_at`, `tab_data_as_of`, `is_stale`, UTC ISO 직렬화 구현(계보 누락·배치 시각 누락·타 프로젝트 스냅샷은 `null`, 기한 경계 `== delta`는 정상 / `> delta`는 초과).
   - `dashkit/api/app.py`: `MANAGE_FEATURES=freshness`일 때만 `dash_reader`로 탭별 `data_as_of` 계산(DB 오류 시 `null` 폴백, 비활성 시 키 미포함).
   - `dashkit/manage/routes/tables.py`: `freshness` 활성 시 `GET /tables`에 `freshness`·`stale` 포함, `GET /tables/{t}`에 `freshness` 포함.
   - 웹앱(`web/app.js`, `web/styles.css`, `web/strings.js`)에 탭 머리 "데이터 기준" 표시, 운용 화면(`web/admin/menus/tables.js`, `web/strings.js`) 테이블 목록에 기한 초과 경고 배지 표시.
6. 계약 문서 갱신:
   - `contracts/manage.md`, `contracts/ingest.md`, `contracts/api.md`, `contracts/auth.md`, `contracts/schema.schema.json`을 구현과 일치시킨다.

# 테스트 및 검증

1. 테스트 추가:
   - 공통: `MANAGE_FEATURES` 파싱 및 비활성 404 / 권한(401·403) 테스트.
   - #34: 잠금 대기·`step: "lock"`·한도 초과 409(202 vs 즉시)·비후보 되돌리기·499,999/500,000행 경계 테스트.
   - #32: 빈 테이블·NULL·전부 NULL·열 유형(`boolean` 포함)·긴 텍스트 절단·`sensitive: true` 차단·`query_timeout`(504)·`dash_owner` 미사용 테스트.
   - #33: 필터/정렬/열 검증·SQL 인젝션 방어·단일 쿼리 스트리밍·`EXPORT_MAX_ROWS` 절단·`sensitive: true` 마스킹 및 필터/정렬 거절·`data.export` 감사 로그(값 미포함)·`dash_owner` 미사용·`buildExportUrl` JS 테스트.
   - #36: 다중 테이블/그래프 최소 시각 선택·계보/배치 시각 누락·타 프로젝트 스냅샷·UTC/경계 시각(`== delta` vs `> delta`)·활성/비활성 분기·`dash_reader` 전용 테스트.
2. 실행 및 확인:
   - `make test-unit`
   - `node --test tests/js/*.test.mjs`
   - `pytest tests/test_admin_ui_static.py tests/test_domain_words.py`
   - 격리된 테스트 스택에서 `AUTH_COOKIE_SECURE=0 make test` 전체 통과 확인 후 `make test-down`
   - `git diff --check`

# PR 및 최종 보고

- 커밋·푸시 후 `.github/pull_request_template.md` 형식에 맞춰 한국어 PR을 연다(`Closes #32`, `Closes #33`, `Closes #34`, `Closes #36` 또는 반영 범위에 맞게 기입).
- 리뷰 지적이나 CI 실패가 있으면 수정 후 관련 테스트를 재실행한다.
- PR 링크, 기준 SHA, 변경 요약, 단위·JS·통합 테스트 결과를 간결하게 보고한다.
````
