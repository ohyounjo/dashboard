# 로그인·사용자 관리 계약 (`dashkit/auth`)

웹앱의 로그인, 사용자 등급, 세션, 요청 보호, 인증 DB의 약속이다. 설계 배경과 단계는 `reference/auth-design.md`(설계서)에 있고, 이 문서가 구현의 기준이다. 기존 웹앱 API(`/api/app` 등)에 생긴 변화는 [api.md](api.md)에 적는다.

- 코드: `dashkit/auth/`. 인증 수단(비밀번호, 세션, 일회용 토큰, 메일)은 이 패키지 안에 가둔다(나중에 사내 SSO를 붙일 때 바꾸는 곳은 여기뿐이다).
- 하지 않는 것: 사내 SSO·2단계 인증, Studio의 인증(Studio는 지금처럼 `127.0.0.1` 전용, [studio.md](studio.md)), Superset 계정 연동(웹앱 사용자는 guest token으로만 본다), 행 단위 권한(guest token의 `rls_rules`는 빈 배열).
- 오류 형식과 공통 규칙은 [api.md](api.md)와 같다: `{"detail": "<영문 코드>", "message": "<한국어 설명>"}`, `Cache-Control: no-store`. 웹앱은 `detail`로 분기하고 문구는 `web/strings.js`에 둔다.
- 비밀번호, 임시 비밀번호, 토큰, 해시는 로그·오류 응답·CLI 출력에 싣지 않는다.

## 등급과 계정 상태

| 등급 | 뜻 |
|---|---|
| `admin` | 어드민. 모든 대시보드를 보고 모든 계정을 다룬다 |
| `operator` | 운용진. 모든 대시보드를 보고 `user` 계정을 다룬다 |
| `user` | 일반사용자 |

이 문서에서 **운용자**는 `admin`과 `operator`를 함께 가리킨다.

| 상태 | 뜻 | 로그인 |
|---|---|---|
| `pending` | 스스로 가입했고 메일 인증 전 | 불가(비밀번호가 맞으면 403 `email_not_verified`) |
| `invited` | 운용자가 만들었고 비밀번호 설정 전 | 불가(비밀번호가 없으므로 401 `invalid_credentials`) |
| `active` | 정상 | 가능 |
| `suspended` | 운용자가 정지 | 불가(비밀번호가 맞으면 403 `account_suspended`) |

- 첫 admin은 화면으로 만들지 않는다. `make admin EMAIL=<주소>`(아래 "CLI")로 만들고 비밀번호는 프롬프트로 받는다. 코드와 `.env`에 넣지 않는다.

## 로그인 확인

`/api/auth/*`와 `/api/health`를 뺀 경로는 로그인이 필요하다. 세션 쿠키로 요청마다 DB의 세션과 계정을 읽어 판정한다(등급·상태 변경은 다음 요청부터 반영된다).

| 판정(`dashkit/auth/routes.py`) | 통과 조건 | 실패 |
|---|---|---|
| `require_session` | 유효한 세션이고 계정이 `active` | 401 `unauthenticated` |
| `require_user` | `require_session` + `must_change_password`가 꺼져 있음 | 403 `password_change_required` |
| `require_operator` | `require_user` + 등급 `admin`·`operator` | 403 `forbidden` |
| `require_admin` | `require_user` + 등급 `admin` | 403 `forbidden` |

- 쿠키가 없거나, 형식이 틀리거나, DB에 없는 세션이면 401 `unauthenticated`.
- 만료된 세션(아래 "세션")이나 `active`가 아닌 계정의 세션은 그 요청에서 행을 지우고 401 `unauthenticated`.
- 인증 DB에 닿지 못하거나 스키마가 없으면 503 `auth_unavailable`(로그에 원인 종류만 남긴다. 스키마가 없으면 `make auth-init`).

## 세션

- 서버 저장 세션이다. 로그인하면 256비트 임의 값(`secrets.token_urlsafe(32)`)을 쿠키 `dashkit_session`에 넣고, DB(`sessions.token_hash`)에는 그 SHA-256만 저장한다.
- 쿠키 속성: `HttpOnly`, `SameSite=Lax`, `Path=/`, `Max-Age=<AUTH_SESSION_MAX_DAYS × 86400>`. `AUTH_COOKIE_SECURE=1`이면 `Secure`(HTTPS로 배포할 때는 반드시 1).
- 만료: 마지막 사용(`last_seen_at`) 후 `AUTH_SESSION_IDLE_MINUTES`(기본 480분), 로그인 후 `AUTH_SESSION_MAX_DAYS`(기본 7일, `expires_at`). 둘 중 먼저 오는 때.
- `last_seen_at`은 1분에 한 번만 갱신한다(요청마다 쓰지 않는다).
- 로그아웃은 그 세션 행을 지우고 쿠키를 만료시킨다.

## 요청 보호

`dashkit/auth/guard.py`(ASGI 미들웨어). Studio의 `guard.py`와 같은 방식이고 backend의 모든 경로(없는 경로 포함)에 적용된다. 상태를 바꾸는 요청(POST, PUT, PATCH, DELETE)만 본다. `SameSite=Lax` 쿠키와 함께 CSRF를 막는다.

| 규칙 | 실패 |
|---|---|
| `Origin`이 있으면 `WEB_ORIGINS`(와 `WEB_PUBLIC_URL`의 출처) 중 하나 | 403 `forbidden_origin` |
| `Sec-Fetch-Site: cross-site`가 아님 | 403 `forbidden_origin` |
| `Content-Type`이 `application/json`(매개변수 허용). 본문이 없는 요청도 이 형식으로 보낸다 | 415 `unsupported_media_type` |
| `Origin`·`Content-Type` 헤더가 하나씩 | 400 `bad_request` |

- 출처 검사를 먼저 하고 형식 검사를 나중에 한다(다른 사이트의 요청은 형식과 상관없이 403).
- backend 포트는 밖에 열려 있지 않다. 클라이언트 IP는 nginx가 넣는 `X-Real-IP`를 쓴다.

## 비밀번호

- 저장은 Argon2id(`argon2-cffi`의 기본 매개변수)뿐이다. 평문과 복호화 가능한 형태는 어디에도 저장하지 않는다. 매개변수가 바뀌면 다음 로그인 성공 때 다시 해시한다.
- 정책(`passwords.policy_violation`): 10자 이상 128자 이하, 이메일의 `@` 앞부분과 같으면 안 됨(대소문자 무시). 위반 사유 코드는 `too_short` · `too_long` · `same_as_email`.
- 로그인 요청의 비밀번호는 1024자, 이메일은 320자까지 받는다(넘으면 422 `invalid_request`).

## 인증 API (`/api/auth`, 로그인 불필요)

| 메서드·경로 | 본문 → 결과 |
|---|---|
| `GET /api/auth/config` | → 200 `{"email_domains": [...], "mail_mode": "test", "password_min": 10}` |
| `POST /api/auth/login` | `{email, password}` → 200 `{email, role, must_change_password}` + 세션 쿠키 |
| `POST /api/auth/logout` | `{}` → 204. 세션이 없어도 204. 쿠키를 만료시킨다 |

### `POST /api/auth/login`

1. 이메일을 앞뒤 공백을 빼고 소문자로 정규화한다.
2. 계정이 없거나, 형식이 틀리거나, 비밀번호가 없는 계정(`invited`)이면 **해시 검증 한 번을 수행한 뒤** 401 `invalid_credentials`(응답 시간을 맞춘다).
3. 비밀번호가 틀리면 401 `invalid_credentials`. 없는 계정과 같은 응답이다.
4. 비밀번호가 맞은 뒤에만 상태를 알려 준다: `pending` → 403 `email_not_verified`, `suspended` → 403 `account_suspended`.
5. 성공하면 세션을 만들고 쿠키를 준다. `login_count`를 1 올리고 `last_login_at`을 기록하고 `failed_count`·`locked_until`을 지운다.
6. 응답의 `must_change_password`가 `true`면 웹앱은 비밀번호 변경 화면으로만 간다. 그동안 `require_user` 경로는 403 `password_change_required`.

| 상황 | 상태 | detail |
|---|---|---|
| 본문 형식 오류 | 422 | `invalid_request` |
| 없는 계정 · 틀린 비밀번호 · 비밀번호 없는 계정 | 401 | `invalid_credentials` |
| 비밀번호는 맞고 메일 인증 전 | 403 | `email_not_verified` |
| 비밀번호는 맞고 정지된 계정 | 403 | `account_suspended` |
| 인증 DB를 쓸 수 없음 | 503 | `auth_unavailable` |

## DB 계정

`AGENTS.md`의 owner/reader 구분을 지키려고 인증 전용 계정을 하나 더 둔다.

| 계정 | 권한 | 쓰는 곳 |
|---|---|---|
| `dash_owner` | raw·dash 스키마 소유 | 로더(tools). backend에는 주지 않는다 |
| `dash_reader` | dash 스키마 SELECT | 대시보드 데이터 읽기(`/api/data`, plan) |
| `dash_auth` | `dashkit_auth` 스키마의 표에 대한 SELECT·INSERT·UPDATE·DELETE만(`schema_version`은 SELECT만) | backend의 인증·권한·운용 API, `dashkit auth` CLI(tools) |

- `dash_auth`는 raw·dash·dashkit_meta를 읽을 수 없고 표·스키마를 만들거나 바꿀 수 없다. `dash_reader`와 `dash_owner`는 `dashkit_auth`를 읽을 수 없다(`tests/test_auth_db.py`).
- `dashkit/db.py`의 `auth_conn()`이 `DATA_DB_AUTH_USER`, `DATA_DB_AUTH_PASSWORD`로 접속한다. backend는 이 계정과 `dash_reader`만 갖는다(owner 계정·Superset 계정 없음).
- 스키마 소유자는 superuser(`postgres`)다. 로더 계정이 DB 소유자여도 `dashkit_auth`를 지우지 못하고, 로더의 재적재(`DROP SCHEMA`)는 raw·dash·dashkit_meta만 지운다. 프로젝트를 바꿔도 계정과 그룹은 남는다.
- 사용자 입력이 SQL에 들어가는 곳은 전부 매개변수 바인딩이다.

### 스키마 만들기와 올리기

- `data-db/init/02-auth.sh`가 한다. 새 볼륨은 initdb 때 자동으로(01-reader.sh 다음), 이미 있는 볼륨은 `make auth-init`이 data-db 컨테이너 안에서 `postgres` 계정(로컬 소켓)으로 실행한다. 여러 번 실행해도 된다.
  1. 역할 `DATA_DB_AUTH_USER`가 없으면 만들고(LOGIN, superuser·CREATEDB·CREATEROLE 없음), 비밀번호를 환경변수 값으로 맞춘다.
  2. 스키마 `dashkit_auth`와 `schema_version` 표가 없으면 만든다.
  3. `dashkit/auth/sql/`(data-db에 `/dashkit-auth-sql`로 읽기 전용 마운트)의 번호 붙은 SQL(`001_init.sql` …) 중 `schema_version`에 없는 것을 번호 순서대로, 파일 하나를 한 트랜잭션으로 적용하고 기록한다.
  4. 권한을 다시 준다(위 표). `PUBLIC`에는 이 스키마의 어떤 권한도 없다.
- `make auth-init`은 그다음 tools 컨테이너에서 `python -m dashkit auth init`으로 `dash_auth` 접속과 스키마 버전(= `dashkit/auth/sql/`의 최대 번호)을 확인한다.
- 볼륨을 다시 만들지 않는다. data-db 컨테이너는 마운트·환경변수가 바뀌었을 때만 다시 만들어진다(데이터는 볼륨에 그대로 있다).

## 데이터 모델

스키마 `dashkit_auth`(`dashkit/auth/sql/001_init.sql`). 시각은 전부 `timestamptz`, `id`가 uuid인 표는 `gen_random_uuid()`.

| 표 | 열 |
|---|---|
| `users` | `id` uuid PK, `email` text UNIQUE(소문자), `password_hash` text NULL, `role` text(`admin`·`operator`·`user`), `status` text(`pending`·`invited`·`active`·`suspended`), `must_change_password` bool, `source` text(`signup`·`invite`), `created_at`, `created_by` uuid NULL(만든 사람이 지워지면 NULL), `email_verified_at`, `password_changed_at`, `last_login_at`, `login_count` int, `failed_count` int, `locked_until` |
| `sessions` | `id` uuid PK, `user_id` FK(계정 삭제 시 함께 삭제), `token_hash` bytea UNIQUE, `created_at`, `last_seen_at`, `expires_at`, `ip` inet, `user_agent` text(400자까지) |
| `tokens` | `id` uuid PK, `user_id` FK(함께 삭제), `purpose` text(`verify_email`·`reset_password`·`invite`), `token_hash` bytea UNIQUE, `created_at`, `expires_at` |
| `groups` | `id` uuid PK, `name` text UNIQUE, `description` text, `is_default` bool, `created_at`, `updated_at` |
| `group_members` | `group_id` FK, `user_id` FK(둘 다 함께 삭제), `added_at`, `added_by`(지워지면 NULL). PK(`group_id`, `user_id`) |
| `group_tabs` | `group_id` FK(함께 삭제), `project` text, `tab_key` text. PK(`group_id`, `project`, `tab_key`) |
| `view_events` | `id` bigserial PK, `user_id` FK NULL(삭제 시 NULL), `project`, `tab_key`, `viewed_at`. 색인(`project`, `tab_key`, `viewed_at`) |
| `audit_log` | `id` bigserial PK, `at`, `actor_id` uuid NULL, `actor_email` text, `action` text, `target_type` text, `target_id` text, `target_label` text, `detail` jsonb, `ip` inet |
| `outbox` | `id` bigserial PK, `user_id` FK(함께 삭제), `to_email`, `kind`(`verify_email`·`reset_password`·`invite`·`already_registered`), `link` text NULL, `created_at`. 메일 테스트 모드 전용 |
| `schema_version` | `version` int PK, `applied_at` |

새 열·표는 다음 번호의 SQL 파일(`002_….sql`)로 더한다. 적용한 파일은 고치지 않는다.

## 환경변수

`.env.example`과 compose의 backend·tools·data-db가 쓴다.

| 이름 | 기본 | 뜻 |
|---|---|---|
| `DATA_DB_AUTH_USER` | `dash_auth` | 인증 DB 계정. owner·reader·superuser와 다른 이름 |
| `DATA_DB_AUTH_PASSWORD` | `__GENERATE__` | 그 비밀번호. 예전에 만든 `.env`에 없으면 `make up`·`make auth-init`이 임의 값으로 덧붙인다(`make env-secrets`). 값이 `__GENERATE__` 그대로면 접속하지 않는다 |
| `AUTH_EMAIL_DOMAINS` | (없음, 필수) | 가입·초대·admin 생성을 허용하는 메일 도메인(쉼표 구분, 대소문자 무시, 앞의 `@` 무시). 없거나 형식이 틀리면 backend가 시작하지 않는다. `.env.example`에는 `example.com` |
| `AUTH_MAIL_MODE` | `test` | `test` · `smtp` |
| `WEB_PUBLIC_URL` | `http://localhost:8080` | 메일 링크의 주소. 그 출처는 요청 보호의 허용 출처에도 들어간다 |
| `WEB_ORIGINS` | (기존 값) | 요청 보호의 허용 출처 |
| `AUTH_COOKIE_SECURE` | `0` | `1`이면 쿠키에 `Secure` |
| `AUTH_SESSION_IDLE_MINUTES` / `AUTH_SESSION_MAX_DAYS` | `480` / `7` | 세션 만료(5~43200분 / 1~90일) |

## CLI

| 명령 | 내용 |
|---|---|
| `make auth-init` | 위 "스키마 만들기와 올리기". `.env`에 없는 비밀값을 먼저 채운다 |
| `make admin EMAIL=<주소>` = `python -m dashkit auth create-admin --email <주소>` | `active` admin 계정을 만든다. 비밀번호는 프롬프트로 두 번 받는다(`PASSWORD_STDIN=1`이면 표준 입력의 첫 줄, 자동화용). 도메인·형식·정책을 검사하고, 같은 이메일의 계정이 있으면 아무것도 바꾸지 않고 종료 코드 1 |
| `python -m dashkit auth init` | `dash_auth`로 접속해 스키마 버전을 확인한다(종료 코드 0/1) |

## 화면

바닐라 JS, 빌드 단계 없음, 문구는 `web/strings.js`, 인라인 스크립트 없음(CSP `script-src 'self'`), 기존 디자인 토큰.

| 파일 | 내용 |
|---|---|
| `web/login.html` + `web/auth.js` + `web/auth.css` | 이메일, 비밀번호, 로그인 버튼. 실패·정지·미인증·잠금 안내. 성공하면 `?next=`로 간다. `next`는 브라우저와 같은 URL 파서(`new URL(next, 지금 출처)`)로 읽어 출처가 지금 출처와 같을 때만 그 경로·쿼리·해시를 쓰고, 아니면(다른 출처, 탭·줄바꿈·역슬래시를 섞은 우회, 로그인 전 화면) `/`로 간다(`web/common.js`의 `safeNext`, 테스트 `tests/js/`). `?reason=expired`면 "로그인이 만료되었습니다", `?reason=logout`이면 "로그아웃했습니다" |
| `web/index.html`(웹앱 셸) | `/api/app`이 401이면 `/login.html?next=<지금 주소>`로 보낸다. 섹션의 guest token·데이터 요청이 401이면 `?reason=expired`를 붙여 보낸다. 레일 아래(테마 버튼 위)에 사용자 메뉴: 이메일, 등급, 로그아웃 |
| `web/common.js` | 셸과 로그인 화면이 함께 쓰는 도우미(요소 생성, `next` 검사, JSON POST) |
