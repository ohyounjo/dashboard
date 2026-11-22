# 보안·자동화 핵심 과제 통합 구현 오케스트레이션 프롬프트 (#38, #35)

설계 PR이 병합된 뒤 `ohyounjo/dashboard` 저장소에서 새 세션을 열고 아래 블록을 붙여 넣는다. [통합 설계서](security-automation-design.md)

````text
# 목표

ohyounjo/dashboard의 이슈 #38(`[5] 자동 적재용 API 토큰`)과 이슈 #35(`[5] 읽기 전용 SQL 콘솔`)을 통합 설계서(reference/security-automation-design.md)대로 구현·검증하고 PR을 올린다. 실제 확인하지 않은 결과를 성공으로 기록하지 않는다.

# 시작과 격리

- AGENTS.md, GitHub 이슈 #38·#35의 최신 본문·댓글, reference/security-automation-design.md, reference/data-design.md(8-3, 9-1, 9-2, 10-4, 10-5, 10-6, 11, 12절)를 읽는다.
- contracts/manage.md, contracts/auth.md, contracts/ingest.md와 관련 코드(dashkit/auth/store.py, dashkit/auth/routes.py, dashkit/auth/guard.py, dashkit/auth/sessions.py, dashkit/auth/sql/001_init.sql, data-db/init/02-auth.sh, dashkit/manage/app.py, dashkit/manage/routes/base.py, dashkit/manage/routes/tables.py, dashkit/manage/routes/jobs.py, dashkit/manage/routes/rows.py, dashkit/manage/uploads.py, dashkit/project/runs.py, web/admin/admin.js, web/admin/menus/tables.js, web/admin/menus/datasets.js, web/strings.js)를 확인한다. 이슈와 계약이 다르면 계약을 기준으로 해석하고 차이를 보고한다.
- 최신 origin/main을 가져와 기준 SHA를 기록하고 그 커밋에서 새 격리 worktree와 작업 브랜치(예: feat/security-automation-38-35)를 준비한다.
- 만약 origin/main에 `dashkit/features.py`가 이미 존재하면 기존 `KNOWN_MANAGE_FEATURES`에 `"sql"`, `"tokens"`를 합치고, 없으면 설계서대로 새로 만든다.
- 비밀번호·토큰 원문·해시는 로그·감사 로그·오류 응답·후속 조회 응답·커밋에 절대 남기지 않는다. 읽기 경로는 `dash_inspect`만 사용한다.

# 구현

1. 공통 기반 (`MANAGE_FEATURES`):
   - `dashkit/features.py`: `KNOWN_MANAGE_FEATURES`에 `"sql"`, `"tokens"` 등록(기존 기능이 있으면 합집합 유지), `manage_features()`, `has_feature(name)` 구현.
   - `dashkit/manage/routes/base.py`: `GET /config`에 `"features": sorted(manage_features())` 추가.
   - `.env.example`, `docker-compose.yml`: `MANAGE_FEATURES`를 추가하고 `manager` 서비스에 전달한다.

2. 이슈 #38: 자동 적재용 API 토큰 (`MANAGE_FEATURES=tokens`):
   - `dashkit/auth/sql/002_api_tokens.sql`: `dashkit_auth.api_tokens`(`id`, `name`, `project`, `token_hash`, `tables`, `created_by`, `created_at`, `expires_at`, `last_used_at`, `revoked_at`, `revoked_by`)와 인덱스 추가(`02-auth.sh` 자동 마이그레이션 및 권한 재부여 확인). 원문·접두사 조각은 DB에 저장하지 않는다.
   - `dashkit/auth/store.py`: `ApiToken` 데이터클래스, `create_api_token`, `list_api_tokens`, `api_token_by_hash`, `touch_api_token`(1분 간격), `revoke_api_token`(소프트 폐기) 구현.
   - `dashkit/manage/auth.py`: `dkt_` + `secrets.token_urlsafe(32)` 생성, `plausible_api_token`, `ManageActor`, `require_ingest_actor` 구현(`tokens` 비활성 시 Bearer 요청 401 `unauthenticated`, 무효·만료·폐기 시 401 `invalid_token`).
   - `dashkit/manage/app.py` (`ManageGuard`):
     - `Authorization` 헤더 중복 시 400 `bad_request`.
     - 모든 메서드·경로에서 `Authorization` 헤더와 `dashkit_session` 쿠키가 함께 오면 최상단에서 400 `ambiguous_auth` 거절.
     - 쿠키가 없고 `Bearer ` 헤더가 있는 `POST /api/manage/tables/{t}/batches` 요청에만 `Origin`/`Sec-Fetch-Site` 검사 예외 적용(`Content-Type` 검사는 유지).
   - `dashkit/project/runs.py` & `dashkit/manage/routes/tables.py` & `jobs.py`:
     - `Run.token_id` 필드 추가(`public()`에는 미노출).
     - `POST /tables/{t}/batches`: `require_ingest_actor` 적용, 토큰의 `project`·`tables` 일치 검증(불일치 시 403 `forbidden`), `uploads` 소유자 격리(`f"token:{tok.id}"`), 적재 감사 로그(`data.batch.create`·`data.batch.failed`)는 `actor_id=None`, `actor_email=f"token:{tok.name}"`, `detail`에 `auth_type="api_token"`, `api_key_id`, `api_key_name` 기록(`record_audit`의 `token` 키 필터에 걸리지 않도록 주의).
     - `GET /jobs/{id}`: `require_ingest_actor` 적용, 토큰 요청은 `run.token_id == tok.id`인 자기 작업만 허용(타 작업 403 `forbidden`, 없는 작업 404 `unknown_job`). 그 외 경로(`GET /jobs`, `GET /jobs/{id}/rejects`, `POST /batches`, 조회, 구조 변경, 삭제 등)는 세션 전용 유지.
   - `dashkit/manage/routes/tokens.py` (`admin` 세션 전용, `tokens` 비활성 시 404 `not_found`):
     - `GET /tokens`, `POST /tokens`(201, 원문 `token` 1회만 반환; `append` 테이블 및 미래~365일 이내 만료일 검증), `DELETE /tokens/{id}`(204 멱등 소프트 폐기).
     - 감사 로그: `data.token.create`, `data.token.revoke` (`target_type="api_token"`, `target_id`, `target_label`, 원문·해시 미포함).
   - 운용 화면(`web/admin/menus/tokens.js`, `web/admin/menus/tables.js`, `web/admin/admin.js`, `web/strings.js`):
     - `tokens` 활성 + `admin`일 때 `#tokens` 메뉴 표시(발급 폼, 1회 표시 배너·복사, 목록·상태·폐기).
     - `tokens` 활성 + `append` 테이블 상세에 `curl` 명령 복사 버튼 추가.

3. 이슈 #35: 읽기 전용 SQL 콘솔 (`MANAGE_FEATURES=sql`):
   - `dashkit/manage/sql_guard.py`: PostgreSQL 렉서(문자열·이중 따옴표·달러 인용·줄/중첩 블록 주석) 기반 사전 검사:
     - 후행 세미콜론 1개만 제거, 문장 중간 `;` 거절(422).
     - 괄호 균형 검사(도중 음수·미종결 시 422), 미종결 문자열·주석 거절(422).
     - 첫 유효 토큰 `select`, `with`, `values`, `table`, `(` 외 거절(422).
     - 부작용/DoS 시스템 함수(`pg_sleep`, `pg_terminate_backend`, `pg_cancel_backend`, `pg_reload_conf`, `pg_rotate_logfile`, `lo_import`, `lo_export`, `pg_read_file`, `pg_read_binary_file`, `pg_ls_dir`, `dblink*`) 호출 거절(422).
     - 정제된 사용자 SQL의 모든 `%`를 `%%`로 이스케이프(`cleaned.replace("%", "%%")`)한 뒤 줄바꿈으로 감싸 `SELECT * FROM (\n<escaped>\n) AS _dashkit_q LIMIT %s OFFSET %s`로 래핑한다.
   - `dashkit/manage/routes/sql_console.py` (`POST /api/manage/sql`, `require_operator` 세션 전용, `sql` 비활성 시 404 `not_found`):
     - `sql`(1~20,000자), `limit`(1..200, 기본 50), `offset`(0..100000, 기본 0) 검증.
     - `db.inspect_conn()` 읽기 전용 접속(`default_transaction_read_only=on`, `statement_timeout=MANAGE_STATEMENT_TIMEOUT_MS`, `lock_timeout=min(timeout, 5000)`, `search_path=raw,dash,dashkit_meta`). `dash_owner` 절대 미사용.
     - `cur.execute(wrapped, (limit + 1, offset))` 확장 쿼리 프로토콜 실행, `has_more` 판정, `rows`는 `columns` 순서에 대응하는 2차원 배열(`list[list[Any]]`)로 반환해 중복 열 이름(`SELECT 1 AS id, 2 AS id` 등)을 손실 없이 보존, 2,000자 초과 셀 절단 시 `truncated_cells`에 `[row_index, col_index]` 기록.
     - 오류 매핑: `QueryCanceled` → 504 `query_timeout`, `InsufficientPrivilege` → 403 `forbidden` (`dashkit_auth` 및 #37 민감 열·민감 뷰 차단), 기타 DB 오류 → 422 `invalid_query`.
     - 감사 로그: 모든 실행(성공·실패·차단)을 `data.query.run`(실행자, 쿼리 앞 2,000자, `rows`, `elapsed_ms`, 결과 데이터 제외)으로 기록.
   - 운용 화면(`web/admin/menus/sql.js`, `web/admin/menus/datasets.js`, `web/admin/admin.js`, `web/strings.js`):
     - `sql` 활성 시 `#sql` 메뉴 표시(`admin`·`operator`), 편집기(`Ctrl/Cmd+Enter`), 페이지 크기·이전/다음 페이지, 2차원 배열 기반 결과 표 렌더링.
     - `admin` 전용 "이 쿼리로 데이터셋 만들기" 버튼 → `session.pendingDatasetSql`로 `#datasets` 생성 폼(`kind: "sql"`) 자동 채움(URL 해시에 SQL 미노출).
     - `web/strings.js`에 `data.token.create`, `data.token.revoke`, `data.query.run` 감사 로그 라벨 추가.

4. 계약 문서 갱신:
   - `contracts/auth.md`, `contracts/manage.md`를 구현과 일치시킨다.

# 테스트 및 검증

1. 테스트 추가:
   - `tests/test_manage_tokens.py`:
     - `MANAGE_FEATURES` 비활성 시 `/tokens*` 404 및 Bearer 적재/작업 401.
     - 발급·목록·폐기(`admin` 허용, `operator`/`user` 403), 원문이 DB·감사 로그·후속 응답에 남지 않음, `replace`/없는 테이블/잘못된 만료일 422.
     - 허용 테이블 JSON `rows`·multipart `file`·`upload_id` 적재 성공, `last_used_at` 갱신, 자기 `GET /jobs/{id}` 조회 성공, 쿠키 없는 Bearer 요청의 cross-site Origin 통과.
     - 허용 외 테이블(403), 타 프로젝트 토큰(403), 만료·폐기·무효 토큰(401), 쿠키+Bearer 혼용(400 `ambiguous_auth`), 타 작업 조회(403), 조회·구조 변경·배치 삭제·작업 목록·SQL 콘솔·토큰 관리에 Bearer 거절(401).
     - 감사 로그(`data.token.create`, `data.token.revoke`, `data.batch.create`, `data.batch.failed`) 검증.
   - `tests/test_manage_sql.py`:
     - `MANAGE_FEATURES` 비활성 시 404, `admin`/`operator` 허용, `user` 403, 비로그인·API 토큰 401.
     - `raw`/`dash`/`dashkit_meta` 정상 조회, 후행 세미콜론 1개, CTE, 문자열 안 `;`, 마지막 줄 `--` 주석, `%` 포함 쿼리(`5 % 2`, `LIKE 'a%'`, `'%s'`, `'%(x)s'`), 중복 열 이름(`SELECT 1 AS id, 2 AS id`, `SELECT 1 + 1, 2 + 2`) 보존, `limit`(1..200)·`offset`(0..100000) 경계 및 `has_more` 검증.
     - `INSERT`/`UPDATE`/`DELETE`/DDL/쓰기 CTE/다중 문장/래핑 탈출(`SELECT 1) AS q; ...`)/`pg_sleep` 차단(422), `dashkit_auth` 조회 차단(403), 타임아웃(504 `query_timeout`), `dash_owner` 미사용 검증.
     - 감사 로그 `data.query.run`(쿼리 앞 2,000자 절단, `rows`, `elapsed_ms`, 데이터 값 미포함) 검증.
   - `tests/test_admin_ui_static.py`: `tokens.js`, `sql.js`, `strings.js` 키, 인라인 스크립트 없음 검증.
2. 실행 및 확인:
   - `make test-unit`
   - `node --test tests/js/*.test.mjs`
   - `pytest tests/test_admin_ui_static.py tests/test_domain_words.py`
   - 격리된 테스트 스택에서 `AUTH_COOKIE_SECURE=0 make test` 전체 통과 확인 후 `make test-down`
   - `git diff --check`

# PR 및 최종 보고

- 커밋·푸시 후 `.github/pull_request_template.md` 형식에 맞춰 한국어 PR을 연다(`Closes #38`, `Closes #35`).
- 리뷰 지적이나 CI 실패가 있으면 수정 후 관련 테스트를 재실행한다.
- PR 링크, 기준 SHA, 변경 요약, 단위·DB·보안 경계·통합 테스트 결과를 간결하게 보고한다.
````
