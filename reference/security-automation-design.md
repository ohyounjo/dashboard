# 보안·자동화 핵심 과제 통합 설계 (#38, #35)

이 문서는 다음 2개 이슈를 구현·검증하기 위한 상세 설계서다.

- [이슈 #38](https://github.com/ohyounjo/dashboard/issues/38): `[5] 자동 적재용 API 토큰` (`MANAGE_FEATURES=tokens`)
- [이슈 #35](https://github.com/ohyounjo/dashboard/issues/35): `[5] 읽기 전용 SQL 콘솔` (`MANAGE_FEATURES=sql`)

참조: [데이터 설계서 8-3·9·10-4·10-5·10-6·11절](./data-design.md) · [관리 서비스 보완·선택 기능 통합 설계](./manage-enhancements-design.md) · [관리 서비스 계약](../contracts/manage.md) · [인증·감사 로그 계약](../contracts/auth.md) · [적재 계약](../contracts/ingest.md) · [오케스트레이션 프롬프트](./security-automation-prompt.md)

---

## 1. 추진 배경과 계약 정합성

### 1-1. 통합 범위

두 이슈는 모두 관리 서비스(`/api/manage`)의 선택 기능 플래그(`MANAGE_FEATURES`)와 보안 경계(인증·권한·읽기 전용 DB·감사 로그)를 공유한다.

1. **공통 기반 (`MANAGE_FEATURES`):** `dashkit/features.py`와 `GET /api/manage/config`의 `features` 응답, `web/admin/`의 기능 플래그 기반 메뉴 노출을 함께 사용한다.
2. **보안 경계 집중 검증:** #38은 `ManageGuard`(CSRF·출처 검사·쿠키/Bearer 혼용 차단)와 `dashkit_auth` 스키마를, #35는 `dash_inspect` 읽기 전용 실행·다층 SQL 방어·#37(민감 열 보호) 정합성을 다룬다. 두 과제 모두 감사 로그(`dashkit_auth.audit_log`)와 권한 경계 회귀 검증이 핵심이다.

### 1-2. 현행 계약과의 차이 및 반영 계획

현재 `contracts/manage.md`와 `contracts/auth.md`에는 5단계 선택 기능(`MANAGE_FEATURES`, `api_tokens`, `/api/manage/tokens`, `/api/manage/sql`, `data.token.*`, `data.query.run`)이 아직 반영되어 있지 않다. 구현 PR에서 코드와 함께 아래 계약 문서를 선행 갱신한다.

- `contracts/auth.md`: `dashkit_auth.api_tokens` 테이블(`002_api_tokens.sql`), 감사 로그 동작 코드(`data.token.create`, `data.token.revoke`, `data.query.run`), API 토큰 적재 시 감사 로그 행위자 표기 규칙 추가.
- `contracts/manage.md`: `GET /config`의 `features`, `MANAGE_FEATURES` 비활성 시 404 규칙, Bearer 토큰 인증·쿠키 혼용 거절·출처 검사 예외 범위, `GET/POST/DELETE /tokens`, `POST /sql` 경로 명세 추가.

**기존 계약 구현 시 주의할 제약 2가지:**
1. `dashkit/auth/store.py`의 `record_audit`는 키 이름에 `password`, `token`, `hash`, `secret`이 포함된 `detail` 항목을 자동 제거한다. 따라서 감사 로그 `detail`에 `token_id`·`token_name` 같은 키를 쓰지 않고, 토큰 관리 로그는 `target_type="api_token"`·`target_id`·`target_label`에, 토큰 적재 로그는 `auth_type="api_token"`·`api_key_id`·`api_key_name`에 기록한다(원문·해시는 절대 포함하지 않음).
2. `dashkit_auth.audit_log.actor_id`는 `uuid NULL` 타입이다. API 토큰 요청은 사용자 세션이 아니므로 `actor_id=None`, `actor_email=f"token:{token.name}"`으로 기록한다.

---

## 2. 공통 기반: 선택 기능 플래그 (`MANAGE_FEATURES`)

[manage-enhancements-design.md](./manage-enhancements-design.md)의 공통 기반 설계와 동일한 인터페이스를 유지한다.

- **모듈 위치:** `dashkit/features.py` (표준 라이브러리만 사용).
  - `KNOWN_MANAGE_FEATURES`: 구현이 존재하는 기능만 등록한다. 본 과제 단독 반영 시 `frozenset({"sql", "tokens"})`, #32·#33·#36과 통합 시 `frozenset({"sql", "export", "profile", "freshness", "tokens"})`.
  - `manage_features() -> frozenset[str]`: `os.environ.get("MANAGE_FEATURES", "")`를 쉼표로 분리하고 공백 제거·소문자화한 뒤 `KNOWN_MANAGE_FEATURES`와 교집합을 반환한다. 알 수 없는 이름은 무시한다. 호출 시점의 환경 변수를 읽어 테스트에서 `monkeypatch.setenv`로 즉시 전환할 수 있다.
  - `has_feature(name: str) -> bool`: `name in manage_features()`.
- **`GET /api/manage/config` 확장 (`dashkit/manage/routes/base.py`):**
  - 기존 `{"project", "role", "limits"}`에 `"features": sorted(manage_features())`를 추가한다.
- **비활성 경로 응답 규칙:**
  - `tokens` 비활성: `/api/manage/tokens`, `/api/manage/tokens/{id}`는 인증·권한 확인(`require_admin`) 후 HTTP 404 `{"detail": "not_found", "message": "비활성화된 기능입니다"}`. Bearer 토큰 인증 자체도 비활성화되어 기존 적재/작업 경로의 Bearer 요청은 401 `unauthenticated`로 거절한다(세션 요청은 정상 유지).
  - `sql` 비활성: `POST /api/manage/sql`은 인증·권한 확인(`require_operator`) 후 HTTP 404 `{"detail": "not_found", "message": "비활성화된 기능입니다"}`.
- **환경 변수·Compose:**
  - `.env.example`에 `MANAGE_FEATURES=`(기본 빈 값) 추가.
  - `docker-compose.yml`의 `manager` 서비스에 `MANAGE_FEATURES: ${MANAGE_FEATURES:-}` 전달.

---

## 3. 이슈 #38: 자동 적재용 API 토큰 (`MANAGE_FEATURES=tokens`)

### 3-1. 데이터 모델 (`dashkit/auth/sql/002_api_tokens.sql`)

`data-db/init/02-auth.sh`는 `AUTH_SQL_DIR`의 `[0-9][0-9][0-9]_*.sql`을 번호순으로 단일 트랜잭션 적용하고 `dashkit_auth.schema_version`에 기록한 뒤, `dash_auth` 계정에 `dashkit_auth`의 모든 테이블 권한(`SELECT, INSERT, UPDATE, DELETE`)을 재부여하고 `PUBLIC` 권한을 회수한다.

```sql
CREATE TABLE dashkit_auth.api_tokens (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name         text NOT NULL CHECK (btrim(name) <> '' AND length(name) <= 100),
    project      text NOT NULL CHECK (project <> ''),
    token_hash   bytea NOT NULL UNIQUE,
    tables       text[] NOT NULL CHECK (cardinality(tables) > 0),
    created_by   uuid NULL REFERENCES dashkit_auth.users (id) ON DELETE SET NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    last_used_at timestamptz NULL,
    revoked_at   timestamptz NULL,
    revoked_by   uuid NULL REFERENCES dashkit_auth.users (id) ON DELETE SET NULL
);
CREATE INDEX api_tokens_project_created_idx ON dashkit_auth.api_tokens (project, created_at DESC);
```

- **원문 비저장:** DB에는 SHA-256 해시(`token_hash`)와 메타데이터만 저장한다. 원문이나 접두사 조각도 DB에 저장하지 않는다.
- **프로젝트 스코프(`project`):** 발급 시점의 현재 프로젝트(`context.current_project()`)를 함께 저장해, 프로젝트 전환 후 이전 프로젝트 토큰이 새 프로젝트의 동명 테이블에 잘못 적재하는 사고를 차단한다.
- **소프트 폐기(`revoked_at`, `revoked_by`):** 폐기 시 행을 삭제하지 않고 시각과 폐기자를 기록해 운용 화면·감사 추적을 유지한다.
- **`last_used_at` 갱신:** 세션의 `last_seen_at`과 동일하게 1분 간격(`TOUCH_INTERVAL = timedelta(minutes=1)`)으로 갱신해 매 요청 DB 쓰기 부하를 피한다.

### 3-2. 토큰 생성 및 검증 규칙

- **형식:** `dkt_` + `secrets.token_urlsafe(32)` (256비트 임의 값, 총 47자).
- **사전 형식 검사 (`plausible_api_token`):** `dkt_`로 시작하고 길이 36~128자, `[A-Za-z0-9_-]` 문자만으로 구성되었는지 확인한다. 형식이 틀리면 DB를 조회하지 않고 즉시 401 `invalid_token`.
- **해시:** `hashlib.sha256(token.encode("utf-8")).digest()` (`sessions.token_hash` 재사용).
- **유효 판정 (`store.api_token_by_hash`):**
  1. 해시 일치 행이 없거나
  2. `revoked_at IS NOT NULL`이거나
  3. `expires_at <= now()`이면
  → HTTP 401 `{"detail": "invalid_token", "message": "유효하지 않거나 만료된 API 토큰입니다"}`.

### 3-3. 요청 보호·쿠키 혼용 거절·출처 검사 예외 (`ManageGuard`)

`dashkit/manage/app.py`의 `ManageGuard`에서 기존 세션/CSRF 경계를 해치지 않도록 다음 순서로 검사한다.

1. **헤더 중복 검사 (모든 HTTP 메서드):**
   - `Authorization` 헤더가 2개 이상이면 400 `bad_request`.
   - `Origin`·`Content-Type` 중복은 기존과 같이 400 `bad_request`.
2. **세션 쿠키 + Bearer 혼용 거절 (모든 HTTP 메서드·모든 경로):**
   - 요청에 `Authorization` 헤더(비어 있지 않음)와 `dashkit_session` 쿠키가 **함께 존재하면** 즉시 HTTP 400 `{"detail": "ambiguous_auth", "message": "세션 쿠키와 API 토큰을 함께 보낼 수 없습니다"}`로 거절한다.
   - GET·POST 등 메서드나 기능 플래그 여부와 무관하게 최상단에서 차단한다.
3. **출처(Origin) 검사 예외 범위 (최소 허용):**
   - `dashkit_session` 쿠키가 **없고**, `Authorization` 헤더가 `Bearer `로 시작하며, 요청이 **토큰 허용 쓰기 경로**(`POST /api/manage/tables/<table_name>/batches`)일 때만 `Origin` 및 `Sec-Fetch-Site` 검사를 건너뛴다.
   - `Content-Type` 검사(`application/json` 또는 `multipart/form-data`)는 토큰 요청에도 그대로 유지한다.
   - 그 외 모든 쓰기 경로(`POST /tokens`, `DELETE /tokens/{id}`, `PATCH /config`, `POST /sql`, `POST /batches`, `DELETE /tables/{t}/batches/{file}` 등)는 출처 검사를 그대로 유지하며, 라우트 인증 의존성도 세션 전용(`require_operator`/`require_admin`)이므로 토큰으로 호출할 수 없다.

### 3-4. 토큰 허용 경로 및 권한 범위

토큰으로 호출할 수 있는 경로는 아래 **2개 경로**로 엄격히 제한한다.

| 메서드·경로 | 세션 운용자 | API 토큰 (`MANAGE_FEATURES=tokens`) | 거절 조건 |
|---|---|---|---|
| `POST /api/manage/tables/{table}/batches` | 기존과 동일 (`require_operator`) | 허용 (`rows` JSON, `file` multipart, `upload_id` JSON, `dry_run`·`skip_invalid`·`force`·`strict`·`columns` 지원) | `tokens` 비활성(401), 무효/만료/폐기(401), `token.project != current_project` 또는 `table not in token.tables`(403 `forbidden`) |
| `GET /api/manage/jobs/{job_id}` | 기존과 동일 (`require_operator`) | 허용 (단, **해당 토큰이 시작한 작업만**) | `tokens` 비활성(401), 무효/만료/폐기(401), 없는 작업(404 `unknown_job`), 다른 사용자·다른 토큰의 작업(`run.token_id != token.id`, 403 `forbidden`) |
| 그 외 모든 `/api/manage/*` 경로 (`GET /jobs`, `GET /jobs/{id}/rejects`, `POST /batches`, `GET /tables*`, `POST /rows/query`, `DELETE /batches/*`, `PATCH /config`, `POST /sql`, `/tokens*` 등) | 기존과 동일 | **불가** (세션 전용 의존성 → 401 `unauthenticated`) | 세션 쿠키 없음 |

- **인증 의존성 분리 (`dashkit/manage/auth.py` 신규):**
  - `ApiTokenPrincipal` 또는 `Principal` 확장:
    ```python
    @dataclass(frozen=True)
    class ManageActor:
        principal: Principal | None      # 세션 운용자일 때
        token: ApiToken | None           # API 토큰일 때
    ```
    - `require_ingest_actor(request: Request) -> ManageActor`:
      - `Authorization` 헤더가 있으면: `has_feature("tokens")` 확인(꺼져 있으면 401 `unauthenticated`) → `Bearer <값>` 파싱·형식 검사 → DB 해시 조회·만료/폐기 검사 → `last_used_at` 1분 간격 갱신 → `ManageActor(principal=None, token=tok)`.
      - `Authorization` 헤더가 없으면: 기존 `require_operator(request)` 실행 → `ManageActor(principal=p, token=None)`.
  - **`Run` 소유자 추적 (`dashkit/project/runs.py`):**
    - `Run`에 `token_id: str | None = None` 필드 추가(`public()` 응답에는 노출하지 않음).
    - `Runs.start(..., token_id: str | None = None)` 지원.
    - 토큰으로 시작한 적재 작업은 `actor_id=None`, `actor_email=f"token:{tok.name}"`, `token_id=tok.id`로 기록한다.
  - **`Uploads` 소유자 격리 (`dashkit/manage/uploads.py`):**
    - `uploads.hold` / `uploads.get`의 `actor_id`에 세션 사용자는 `principal.id`, API 토큰은 `f"token:{tok.id}"`를 전달해 서로의 `upload_id`를 사용할 수 없게 격리한다.
  - **적재 감사 로그 (`data.batch.create`, `data.batch.failed`):**
    - 토큰 적재 시 `actor_id=None`, `actor_email=f"token:{tok.name}"`, `detail`에 `auth_type="api_token"`, `api_key_id=tok.id`, `api_key_name=tok.name` 추가(`method`는 기존 `form`/`paste`/`upload` 유지).

### 3-5. 토큰 관리 API (`dashkit/manage/routes/tokens.py`, `admin` 세션 전용)

모든 경로는 `Depends(require_admin)`(세션 전용) 후 `has_feature("tokens")`를 검사해 비활성이면 404 `not_found`를 반환한다.

| 메서드·경로 | 요청 | 응답 | 오류 |
|---|---|---|---|
| `GET /api/manage/tokens` | — | 200 `{"items": [{id, name, project, tables, created_at, created_by_email, expires_at, last_used_at, revoked_at, status}], "total": n}` (최근 생성 순, 현재 프로젝트 기준 또는 전체+`project` 표시. 기본 현재 프로젝트 필터) | 401, 403, 404(`not_found`) |
| `POST /api/manage/tokens` | `{"name": str, "tables": [str, ...], "expires_at": str}` | 201 `{id, name, project, tables, created_at, expires_at, token}` (**`token` 원문 1회만 반환**) | 422 `invalid_request` · `unknown_table` · `not_append`, 401, 403, 404(`not_found`) |
| `DELETE /api/manage/tokens/{token_id}` | — | 204 No Content (소프트 폐기, 이미 폐기된 경우에도 204 멱등) | 404 `unknown_token` · `not_found`, 401, 403 |

- **`POST /api/manage/tokens` 입력 검증:**
  - `name`: 앞뒤 공백 정리 후 1~100자.
  - `tables`: 비어 있지 않은 문자열 리스트(중복 제거·정렬). 모든 항목이 현재 프로젝트 `schema.yaml`에 존재해야 하며(없으면 422 `unknown_table`), `mode == "append"`여야 한다(`replace`면 422 `not_append`).
  - `expires_at`: ISO 8601 타임스탬프 또는 `YYYY-MM-DD`(서버 `TZ` 기준 해당 날짜 `23:59:59`로 해석). 현재 시각보다 미래여야 하며 최대 365일 이내(초과·과거·형식 오류는 422 `invalid_request`).
- **상태 계산(`status`):** `revoked_at is not None` → `"revoked"`, `expires_at <= now` → `"expired"`, 그 외 → `"active"`.
- **감사 로그:**
  - 발급: `action="data.token.create"`, `actor_id=principal.id`, `actor_email=principal.email`, `target_type="api_token"`, `target_id=tok.id`, `target_label=tok.name`, `detail={"project": project.name, "tables": tables, "expires_at": expires_at.isoformat()}`.
  - 폐기(최초 폐기 시): `action="data.token.revoke"`, `target_type="api_token"`, `target_id=tok.id`, `target_label=tok.name`, `detail={"project": tok.project, "tables": tok.tables}`.
  - 원문 토큰과 해시는 감사 로그·서버 로그·후속 응답 어디에도 남기지 않는다.

### 3-6. 운용 화면 UI (`web/admin/menus/tokens.js`, `web/admin/menus/tables.js`)

- **메뉴 (`web/admin/admin.js`):** `tokens` 메뉴 추가 (`key: "tokens"`, `when: () => isManageFeature("tokens") && isAdmin()`).
- **발급 화면 (`#tokens`):**
  - 이름 입력, 현재 프로젝트의 `append` 테이블 체크박스 목록, 만료일(`input[type=date]`, 기본 30일 후, 최대 365일).
  - 발급 성공 시 상단에 **1회 표시 카드** 렌더링: 원문 토큰(모노스페이스) + 복사 버튼 + "이 값은 지금 한 번만 표시됩니다. 안전하게 보관하세요" 안내. 닫기 또는 화면 이탈 시 DOM·변수에서 즉시 제거.
  - 토큰 목록 표: 이름, 허용 테이블, 상태 배지(`활성`·`만료`·`폐기`), 만료일, 마지막 사용 시각, 발급자, [폐기] 버튼(확인 후 `DELETE /api/manage/tokens/{id}`).
- **테이블 상세 명령 복사 (`web/admin/menus/tables.js`, 설계서 9-2절):**
  - `isManageFeature("tokens")`이고 `mode === "append"`인 테이블 상세 화면에 **"명령 복사"** 버튼 추가.
  - 클릭 시 해당 테이블 적재용 `curl` 예시를 클립보드에 복사:
    ```bash
    curl -X POST "<location.origin>/api/manage/tables/<table>/batches" \
      -H "Authorization: Bearer $DASHKIT_API_TOKEN" \
      -F "file=@data.csv"
    ```

---

## 4. 이슈 #35: 읽기 전용 SQL 콘솔 (`MANAGE_FEATURES=sql`)

### 4-1. API 계약 (`POST /api/manage/sql`, `dashkit/manage/routes/sql_console.py`)

- **권한·활성 조건:** `Depends(require_operator)` (`admin`·`operator` 허용, 세션 전용 — API 토큰 불가). `has_feature("sql")`이 거짓이면 HTTP 404 `not_found`.
- **요청 본문 (JSON):**
  ```json
  {
    "sql": "SELECT id, status FROM raw.issues ORDER BY id",
    "limit": 50,
    "offset": 0
  }
  ```
  - `sql`: 필수 문자열. 앞뒤 공백 제거 후 비어 있거나 `20_000`자를 넘으면 422 `invalid_query`.
  - `limit`: 정수 `1..200` (기본 `50`, `bool` 제외). 범위 밖이면 422 `invalid_query`.
  - `offset`: 정수 `0..100000` (기본 `0`, `bool` 제외). 범위 밖이면 422 `invalid_query`.
- **응답 (200):**
  ```json
  {
    "columns": [{"name": "id", "type": "text"}, {"name": "status", "type": "text"}],
    "rows": [["ISS-1", "open"]],
    "has_more": true,
    "elapsed_ms": 12,
    "truncated_cells": []
  }
  ```
  - **행 표현(`rows`)과 중복 열 이름 보존:** 임의 SQL은 조인이나 별칭 없는 식(`SELECT 1 AS id, 2 AS id`, `SELECT 1 + 1, 2 + 2`)으로 동일한 이름의 열을 여러 개 반환할 수 있다. 키 덮어쓰기 없이 모든 열 값을 손실 없이 보존하기 위해 `rows`는 `columns` 배열과 동일한 열 순서의 2차원 배열(`list[list[Any]]`, `rows[row_idx][col_idx]` ↔ `columns[col_idx]`)로 반환한다.
  - 전체 행 수는 세지 않고 `limit + 1`행을 읽어 `len(fetched) > limit`로 `has_more`를 판정한다.
  - 열 유형은 `cur.description`의 `type_code`를 `dashkit/api/data.py`의 `_TYPE_BY_OID`로 매핑(미등록 타입은 `"text"`).
  - 값 직렬화는 `dashkit/manage/routes/rows.py`의 `_to_json`을 재사용하고, 문자열 길이가 `paging.CELL_CHARS`(2,000자)를 초과하면 2,000자에서 절단해 `truncated_cells`에 `[row_index, col_index]`를 기록한다.

### 4-2. 다층 SQL 방어 설계 (`dashkit/manage/sql_guard.py`)

임의의 사용자 SQL을 받으므로 단일 방어에 의존하지 않고 5개 계층으로 차단한다.

| 계층 | 방어 수단 | 차단 대상 |
|---|---|---|
| 1. SQL 렉서 사전 검사 (`sql_guard.prepare_sql`) | PostgreSQL 문자열(`'...'`, `''`), 이중 따옴표(`"..."`), 달러 인용(`$tag$...$tag$`), 줄 주석(`--`), 중첩 블록 주석(`/*...*/`)을 파싱해 토큰 검증 | 다중 문장(문장 중간 `;`), 괄호 탈출(`)` 깊이 < 0), 미종결 문자열·주석·괄호, 허용 외 시작 키워드(`INSERT`, `UPDATE`, `DELETE`, `CREATE`, `ALTER`, `DROP`, `GRANT`, `REVOKE`, `TRUNCATE`, `COPY`, `DO`, `CALL`, `SET`, `EXPLAIN`, `PREPARE`, `EXECUTE` 등), 부작용/DoS 시스템 함수(`pg_sleep`, `pg_terminate_backend`, `pg_cancel_backend`, `pg_reload_conf`, `pg_rotate_logfile`, `lo_import`, `lo_export`, `dblink*`) |
| 2. 서브쿼리 래핑 + `%` 이스케이프 + 줄바꿈 보호 | 정제된 사용자 SQL의 모든 `%`를 `%%`로 치환한 뒤 `SELECT * FROM (\n<이스케이프된 SQL>\n) AS _dashkit_q LIMIT %s OFFSET %s`로 래핑 | 비-SELECT 문장, 쓰기 CTE(`WITH ... INSERT/UPDATE/DELETE`), `SELECT ... INTO`, 마지막 줄 `--` 주석에 의한 래핑 탈출, 사용자 SQL 내 리터럴 `%`(나머지 연산·`LIKE 'a%'`·`'%s'`)의 플레이스홀더 오인 |
| 3. 확장 쿼리 프로토콜 | `psycopg` 매개변수 바인딩(`(limit + 1, offset)`) | PostgreSQL 서버 프로토콜 레벨에서 다중 문장 실행 원천 차단 |
| 4. `dash_inspect` 계정 및 스키마 권한 | `db.inspect_conn()`만 사용(`dash_owner` 절대 금지) | `dashkit_auth` 스키마 조회(`02-auth.sh`에서 `PUBLIC` 회수), 쓰기 DDL/DML, 민감 열(`#37`) 및 민감 열 참조 `sql:` 뷰 조회 |
| 5. 읽기 전용 트랜잭션 + 시간 제한 | `-c default_transaction_read_only=on -c statement_timeout=<MANAGE_STATEMENT_TIMEOUT_MS> -c lock_timeout=<min(timeout, 5000)> -c search_path=raw,dash,dashkit_meta` | 쓰기 부작용, 장시간 풀스캔·카테시안 조인에 의한 DB 자원 점유 |

- **세부 규칙 (`sql_guard.prepare_sql`):**
  1. 공백·주석을 제외한 토큰 스트림을 생성한다.
  2. 마지막 유효 토큰이 `;`이면 1개만 제거한다(끝에 `;`가 여러 개면 거절).
  3. 남은 토큰 중 `;`가 하나라도 있으면 422 `invalid_query`("한 번에 하나의 조회 문장만 실행할 수 있습니다").
  4. 괄호 깊이가 도중에 음수가 되거나 최종 깊이가 0이 아니면 422 `invalid_query`.
  5. 첫 유효 토큰(소문자)이 `select`, `with`, `values`, `table`, `(` 중 하나가 아니면 422 `invalid_query`("읽기 전용 SELECT 문장만 실행할 수 있습니다").
  6. 식별자 토큰이 차단 함수 목록(`pg_sleep`, `pg_terminate_backend`, `pg_cancel_backend`, `pg_reload_conf`, `pg_rotate_logfile`, `lo_import`, `lo_export`, `pg_read_file`, `pg_read_binary_file`, `pg_ls_dir`, `dblink`, `dblink_exec`, `dblink_connect`)에 해당하고 바로 다음 유효 토큰이 `(`이면 422 `invalid_query`.
  7. 원문에서 후행 세미콜론만 제거한 문자열의 `%`를 모두 `%%`로 이스케이프(`cleaned.replace("%", "%%")`)한 뒤 줄바꿈으로 감싸 `SELECT * FROM (\n<escaped>\n) AS _dashkit_q LIMIT %s OFFSET %s`로 조립한다. `psycopg`가 `(limit + 1, offset)`을 바인딩할 때 사용자 SQL의 나머지 연산(`5 % 2`), `LIKE 'a%'`, 문자열 리터럴 `'%s'`가 플레이스홀더로 오인되지 않고 그대로 PostgreSQL에 전달된다.
- **오류 매핑:**
  - `psycopg.errors.QueryCanceled` → 504 `query_timeout` (`"쿼리가 시간 제한을 넘었습니다. 조건을 좁혀 주세요"`)
  - `psycopg.errors.InsufficientPrivilege` → 403 `forbidden` (`"접근 권한이 없는 스키마·테이블 또는 열입니다"`)
  - `psycopg.errors.ReadOnlySqlTransaction` → 422 `invalid_query` (`"읽기 전용 모드에서는 데이터를 변경할 수 없습니다"`)
  - `psycopg.errors.UndefinedTable` / `InvalidSchemaName` / `SyntaxError` / 기타 `psycopg.Error` → 422 `invalid_query` (`exc.diag.message_primary` 요약, 비밀값·내부 연결 정보 제외)
  - DB 연결 불가 → 503 `db_unavailable`

### 4-3. 민감 열 보호(#37)와의 권한 경계

- SQL 콘솔은 `dash_inspect` 계정으로만 접속하고 `raw`·`dash`·`dashkit_meta`의 DB 권한을 그대로 따른다.
- #37(민감 열 보호)이 적용된 환경에서 `dash_inspect`는 민감 열에 대한 열 단위 `SELECT` 권한과 민감 열을 읽는 `sql:` 뷰의 `SELECT` 권한이 없으므로, 콘솔에서 해당 열이나 `SELECT *`를 실행하면 PostgreSQL이 `InsufficientPrivilege`를 발생시키고 API는 403 `forbidden`으로 차단한다.
- 콘솔 결과나 오류 메시지에 민감 열 값이 노출되지 않으며, `dash_owner` 연결을 열지 않는다.

### 4-4. 감사 로그 (`data.query.run`)

- 모든 SQL 콘솔 실행(성공 및 DB 실행 오류·권한 차단·타임아웃)마다 `auth_store.record_audit`로 기록한다(감사 로그 기록 실패가 조회 응답을 깨지 않도록 예외 보호).
- 필드:
  - `action="data.query.run"`
  - `actor_id=principal.id`, `actor_email=principal.email`
  - `target_type="sql"`, `target_id=None`, `target_label=project.name`
  - `detail`:
    - 성공: `{"project": project.name, "sql": sql_text[:2000], "rows": len(rows), "has_more": has_more, "elapsed_ms": elapsed_ms, "limit": limit, "offset": offset}`
    - 실패/차단: `{"project": project.name, "sql": sql_text[:2000], "rows": 0, "elapsed_ms": elapsed_ms, "error": detail_code}`
  - 조회 결과 데이터 값은 절대 기록하지 않는다.

### 4-5. 운용 화면 UI (`web/admin/menus/sql.js`, `web/admin/menus/datasets.js`)

- **메뉴 (`web/admin/admin.js`):** `sql` 메뉴 추가 (`key: "sql"`, `when: () => isManageFeature("sql")`). `admin`·`operator` 모두 표시.
- **SQL 콘솔 화면 (`#sql`):**
  - SQL 입력 영역(`<textarea class="input mono">`), `Ctrl+Enter` / `Cmd+Enter`로 실행.
  - 실행 버튼, 페이지 크기(`limit`: 50 / 100 / 200) 선택.
  - 실행 결과 영역: 반환 행 수·소요 시간(`elapsed_ms`ms)·이전/다음 페이지 버튼(`offset` 0~100,000, `has_more` 기반).
  - 결과 표: `columns` 기반 헤더와 `rows` 렌더링, 잘린 셀 표시, 빈 결과·오류 안내.
  - **admin 전용 "이 쿼리로 데이터셋 만들기" 버튼:**
    - 쿼리가 정상 실행된 뒤 `isAdmin()`일 때만 활성화.
    - 클릭 시 `session.pendingDatasetSql = lastExecutedSql`에 저장하고 `location.hash = "#datasets"`로 이동한다.
    - `web/admin/menus/datasets.js`의 `listView`는 `session.pendingDatasetSql`이 있으면 1회 소비해 `createCard`를 자동으로 열고 `kind = "sql"`, `sql = pendingSql`을 미리 채운다(URL 해시에 SQL을 넣지 않음).

---

## 5. 수정·추가 파일 목록

| 구분 | 파일 | 변경 내용 |
|---|---|---|
| 공통 기능 플래그 | `dashkit/features.py` (신규) | `KNOWN_MANAGE_FEATURES`, `manage_features()`, `has_feature()` |
| 인증 DB 스키마 | `dashkit/auth/sql/002_api_tokens.sql` (신규) | `dashkit_auth.api_tokens` 테이블·인덱스 |
| 인증 저장소 | `dashkit/auth/store.py` | `ApiToken` 데이터클래스, `create_api_token`, `list_api_tokens`, `api_token_by_hash`, `touch_api_token`, `revoke_api_token` |
| 관리 서비스 앱·보안 | `dashkit/manage/app.py` | `ManageGuard`에 `Authorization` 중복 검사, 쿠키+Bearer 혼용 거절(400 `ambiguous_auth`), 토큰 적재 경로 한정 Origin 검사 예외 |
| 관리 서비스 인증 | `dashkit/manage/auth.py` (신규) | `ManageActor`, `require_ingest_actor`, `plausible_api_token`, `new_api_token` |
| 관리 서비스 라우트 | `dashkit/manage/routes/base.py` | `GET /config`에 `features` 추가 |
| 관리 서비스 라우트 | `dashkit/manage/routes/tokens.py` (신규) | `GET /tokens`, `POST /tokens`, `DELETE /tokens/{id}` (`admin`, `tokens` 플래그) |
| 관리 서비스 라우트 | `dashkit/manage/routes/sql_console.py` (신규) | `POST /sql` (`operator`, `sql` 플래그, `dash_inspect`, 감사 로그) |
| SQL 방어 | `dashkit/manage/sql_guard.py` (신규) | SQL 렉서, 단일 문장·괄호 균형·허용 시작 키워드·차단 함수 검사, 줄바꿈 래핑 |
| 관리 서비스 라우트 | `dashkit/manage/routes/tables.py`, `jobs.py`, `__init__.py` | `POST /tables/{t}/batches`·`GET /jobs/{id}`에 `require_ingest_actor` 및 테이블·작업 소유자 검증 적용, `tokens`·`sql_console` 라우터 등록 |
| 작업 상태 | `dashkit/project/runs.py` | `Run.token_id` 필드 및 `Runs.start(..., token_id=...)` 추가 |
| 운용 화면 UI | `web/admin/menus/tokens.js` (신규), `web/admin/menus/sql.js` (신규), `web/admin/menus/datasets.js`, `web/admin/menus/tables.js`, `web/admin/admin.js`, `web/admin/kit.js`, `web/strings.js` | 토큰 발급·폐기 화면, 테이블 상세 `curl` 명령 복사, SQL 콘솔 화면, 데이터셋 만들기 연계, 감사 로그 동작 라벨(`data.token.create`·`data.token.revoke`·`data.query.run`) |
| 환경·배포 | `.env.example`, `docker-compose.yml` | `MANAGE_FEATURES` 추가 및 `manager` 서비스 전달 |
| 계약 문서 | `contracts/auth.md`, `contracts/manage.md` | `api_tokens`, `features`, 토큰 인증·경로, SQL 콘솔, 감사 로그 코드 반영 |
| 테스트 | `tests/test_manage_tokens.py` (신규), `tests/test_manage_sql.py` (신규), `tests/test_admin_ui_static.py` | 단위·API·DB·보안 경계·정적 UI 회귀 테스트 |

---

## 6. 검증 계획

### 6-1. 이슈 #38 (API 토큰) 테스트 매트릭스

1. **기능 플래그 비활성 (`MANAGE_FEATURES=""`):**
   - `GET/POST/DELETE /api/manage/tokens` → 404 `not_found` (`admin` 세션).
   - `POST /api/manage/tables/{t}/batches`, `GET /api/manage/jobs/{id}`에 Bearer 토큰 전송 → 401 `unauthenticated`.
   - `GET /api/manage/config`의 `features`에 `"tokens"` 없음.
2. **발급·목록·폐기 (`admin` vs `operator` vs `user`):**
   - `operator`·`user`의 `/tokens` 접근 → 403 `forbidden`.
   - `admin` 발급 → 201, 응답에 원문 `token` 1회 포함; 직후 `GET /tokens` 및 DB(`dashkit_auth.api_tokens`)와 감사 로그(`dashkit_auth.audit_log`)에 원문이 전혀 남지 않음 확인.
   - 존재하지 않는 테이블(422 `unknown_table`), `replace` 테이블(422 `not_append`), 과거/365일 초과 만료일(422 `invalid_request`) 거절.
   - 폐기(`DELETE /tokens/{id}`) → 204, 재호출 멱등 204, 감사 로그 `data.token.create`·`data.token.revoke` 기록 확인.
3. **토큰 적재 및 작업 조회:**
   - 허용된 `append` 테이블에 JSON `rows` 적재 → 200, `last_used_at` 갱신, 감사 로그 `data.batch.create`(`actor_id=None`, `actor_email="token:<name>"`) 기록.
   - 허용된 테이블에 multipart `file` 적재 → 202 `job`, 같은 토큰으로 `GET /jobs/{id}` 조회 → 200.
   - `Origin: https://evil.example` / `Sec-Fetch-Site: cross-site`가 있어도 쿠키 없는 정상 Bearer 적재는 허용(출처 검사 비대상).
4. **보안 경계 거절:**
   - 허용 외 테이블 적재 → 403 `forbidden`.
   - 만료된 토큰·폐기된 토큰·잘못된 토큰 → 401 `invalid_token`.
   - 세션 쿠키 + `Authorization: Bearer` 혼용 → 400 `ambiguous_auth` (GET·POST 모두).
   - 다른 토큰 또는 세션 사용자가 시작한 작업 `GET /jobs/{other_id}` → 403 `forbidden`.
   - 조회(`GET /tables`, `POST /tables/{t}/rows/query`), 구조 변경(`PATCH /config`), 배치 삭제(`DELETE /tables/{t}/batches/{file}`), 작업 목록(`GET /jobs`), SQL 콘솔(`POST /sql`), 토큰 관리(`/tokens`)에 Bearer 토큰 전송 → 401 거절.
   - 프로젝트 전환 후 이전 프로젝트용 토큰으로 적재 시도 → 403 `forbidden`.

### 6-2. 이슈 #35 (읽기 전용 SQL 콘솔) 테스트 매트릭스

1. **기능 플래그 비활성 (`MANAGE_FEATURES=""`):**
   - `POST /api/manage/sql` → 404 `not_found`.
   - `GET /api/manage/config`의 `features`에 `"sql"` 없음.
2. **권한:**
   - `admin`·`operator` → 200 정상 실행.
   - `user` → 403 `forbidden`, 비로그인 → 401 `unauthenticated`, API 토큰 → 401 `unauthenticated`.
3. **정상 조회 및 페이지 나눔:**
   - `raw.*`, `dash.*`, `dashkit_meta.*` 조회 성공, `columns`·`rows`(열 순서 2차원 배열)·`has_more`·`elapsed_ms`·`truncated_cells` 형식 검증.
   - 후행 세미콜론 1개(`SELECT 1;`), CTE(`WITH x AS (SELECT 1 AS a) SELECT * FROM x`), 문자열 안 세미콜론(`SELECT 'a;b' AS s`), 마지막 줄 주석(`SELECT 1 -- comment`) 정상 실행.
   - `%` 문자 포함 쿼리: 나머지 연산(`SELECT 5 % 2 AS rem`), `LIKE` 패턴(`SELECT 'abc' LIKE 'a%' AS matched`), 문자열 내부 플레이스홀더 유사 문자열(`SELECT '%s' AS fmt, '%(x)s' AS named`)이 `psycopg` 바인딩 오류 없이 정상 실행됨을 검증.
   - 중복 열 이름 보존: `SELECT 1 AS id, 2 AS id` 및 별칭 없는 복수 식(`SELECT 1 + 1, 2 + 2`)에서 `columns` 2개와 `rows`의 2개 위치 값이 손실 없이 반환됨을 검증.
   - `limit` 1~200 경계(0·201 → 422), `offset` 0~100000 경계(-1·100001 → 422), `has_more` 정확성 검증.
4. **SQL 보안 차단 (실 DB 포함):**
   - `INSERT`, `UPDATE`, `DELETE`, `CREATE TABLE`, `DROP TABLE`, `ALTER TABLE`, `TRUNCATE`, `GRANT`, `COPY`, `DO $$ ... $$` → 422 차단.
   - 쓰기 CTE (`WITH d AS (DELETE FROM raw.issues RETURNING *) SELECT * FROM d`) → 422 차단.
   - 다중 문장 (`SELECT 1; SELECT 2`, `SELECT 1; DROP TABLE raw.issues`) → 422 차단.
   - 래핑 탈출 시도 (`SELECT 1) AS q; SELECT 2 --`, `SELECT 1) AS q UNION SELECT 2 --`) → 422 차단.
   - `dashkit_auth` 조회 (`SELECT * FROM dashkit_auth.users`, `SELECT * FROM dashkit_auth.api_tokens`) → 403 `forbidden` (`dash_inspect` 권한 차단).
   - `pg_sleep(10)` 차단(422) 및 장시간 연산 쿼리(`MANAGE_STATEMENT_TIMEOUT_MS` 축소) → 504 `query_timeout`.
5. **감사 로그:**
   - 실행자(`actor_id`, `actor_email`), 쿼리 앞 2,000자 절단(`len(sql) > 2000` 케이스 포함), `rows`, `elapsed_ms`가 `dashkit_auth.audit_log`(`action="data.query.run"`)에 기록되고 결과 행 값은 기록되지 않음 확인.
6. **정적·통합 검증:**
   - `pytest` 전체 단위·DB 테스트 통과.
   - `tests/test_admin_ui_static.py`에 신규 메뉴 모듈(`tokens.js`, `sql.js`), `strings.js` 키, 인라인 스크립트 없음 검증 추가.
   - `MANAGE_FEATURES=sql,tokens` 환경에서 운용 화면(`#tokens`, `#sql`, `#table/<t>` 명령 복사, `#sql` → `#datasets` 연계) E2E 확인.
