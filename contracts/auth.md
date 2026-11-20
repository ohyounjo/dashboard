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

## 접근 제어

접근을 허용하는 단위는 `dashboard.yaml`의 **탭**(`tabs[].key`)이다. 탭을 허용하면 그 안의 섹션(Superset 대시보드, 위젯)이 전부 열린다. 권한은 그룹의 `(프로젝트 이름, 탭 키)`로 저장한다(`group_tabs`). 현재 프로젝트가 바뀌어도 다른 프로젝트의 권한은 남아 있어서 되돌아오면 그대로 쓴다.

| 등급 | 볼 수 있는 탭(`dashkit/auth/access.py`) |
|---|---|
| `admin`, `operator` | 현재 프로젝트의 모든 탭 |
| `user` | 속한 그룹들이 **현재 프로젝트 이름으로** 허용한 탭의 합집합. 설정에 없는 탭 키는 무시한다. 없으면 빈 목록 |

- 판정은 요청마다 DB에서 읽는다. 그룹·구성원·허용 탭을 고치면 다음 요청부터 반영된다.

| 경로 | 적용([api.md](api.md)) |
|---|---|
| `GET /api/app` | `tabs`에서 볼 수 없는 탭을 뺀다(설정 순서 유지). `applied`는 남은 탭만으로 계산한다(남은 탭이 없으면 `true`) |
| `POST /api/guest-token` | 설정에 없는 키는 지금처럼 404 `unknown_dashboard`. 운용자는 설정의 모든 대시보드, user는 **볼 수 있는 탭의 superset 섹션**이 참조하는 대시보드만. 아니면 403 `forbidden` |
| `GET /api/data/{dataset}` | 운용자는 지금과 같다. user는 **볼 수 있는 탭의 위젯 섹션**이 참조하는 데이터셋만. 아니면 지금과 같은 404 `unknown_dataset` |

- 알려진 한계: guest token은 발급 후 300초 동안 유효하다. 그룹에서 빠진 사용자가 이미 받은 토큰으로 최대 5분 더 볼 수 있다.
- 알려진 한계: 같은 Superset 대시보드를 두 탭에서 쓰면, 한 탭만 허용돼도 그 대시보드의 토큰은 발급된다(토큰은 탭이 아니라 대시보드에 대한 것이다). 의도한 동작이다.
- 행 단위 권한은 없다(guest token의 `rls_rules`는 빈 배열).

### 그룹

- 그룹은 이름(앞뒤·연속 공백을 정리한 뒤 유일, 100자까지), 설명(500자까지), 허용 탭 목록, 구성원 목록, `is_default`를 가진다.
- 설정 파일에서 탭이 없어지면 그룹의 그 항목은 지우지 않고 편집 화면에 "없는 탭"으로 표시한다. 판정에서는 무시한다(탭이 설정에 돌아오면 그대로 다시 쓰인다). 허용 탭을 다시 저장해도 없는 탭 기록은 남는다. 운용자가 편집 화면에서 항목을 **하나씩** 뺀다(`DELETE /groups/{id}/tabs/{project}/{tab_key}`).
- 그룹을 삭제하면 구성원·허용 탭 연결만 지워진다. 사용자 계정은 남는다.
- 구성원은 어느 등급이든 넣을 수 있다(운용자에게는 판정에 쓰이지 않는다). 단 operator는 `user` 계정만 넣고 뺀다(아래 "운용 API").

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
| `Origin`이 있으면 `WEB_ORIGINS`(와 `WEB_PUBLIC_URL`의 출처) 중 하나. 설정값은 `scheme://host[:port]`로 정규화하고 그 스킴의 기본 포트(`http` 80, `https` 443)는 지운다(브라우저가 `Origin`에서 생략하므로) | 403 `forbidden_origin` |
| `Sec-Fetch-Site: cross-site`가 아님 | 403 `forbidden_origin` |
| `Content-Type`이 `application/json`(매개변수 허용). 본문이 없는 요청도 이 형식으로 보낸다 | 415 `unsupported_media_type` |
| `Origin`·`Content-Type` 헤더가 하나씩 | 400 `bad_request` |

- 출처 검사를 먼저 하고 형식 검사를 나중에 한다(다른 사이트의 요청은 형식과 상관없이 403).
- backend 포트는 밖에 열려 있지 않다. 클라이언트 IP는 nginx가 넣는 `X-Real-IP`를 쓴다.

## 비밀번호

- 저장은 Argon2id(`argon2-cffi`의 기본 매개변수)뿐이다. 평문과 복호화 가능한 형태는 어디에도 저장하지 않는다. 매개변수가 바뀌면 다음 로그인 성공 때 다시 해시한다.
- 정책(`passwords.policy_violation`): 10자 이상 128자 이하, 이메일의 `@` 앞부분과 같으면 안 됨(대소문자 무시), 흔한 비밀번호 목록(`dashkit/auth/vendor/common_passwords.txt`, 상위 1만 개, 출처·라이선스는 같은 폴더의 README)에 있으면 안 됨(대소문자 무시). 문자 종류 조합은 강제하지 않는다. 위반은 422 `weak_password`이고 본문의 `reason`이 사유 코드다: `too_short` · `too_long` · `same_as_email` · `too_common`.
- 로그인 요청의 비밀번호는 1024자, 이메일은 320자까지 받는다(넘으면 422 `invalid_request`).

## 인증 API (`/api/auth`, 로그인 불필요)

| 메서드·경로 | 본문 → 결과 |
|---|---|
| `GET /api/auth/config` | → 200 `{"email_domains": [...], "mail_mode": "test", "password_min": 10}` |
| `POST /api/auth/login` | `{email, password}` → 200 `{email, role, must_change_password}` + 세션 쿠키 |
| `POST /api/auth/logout` | `{}` → 204. 세션이 없어도 204. 쿠키를 만료시킨다 |
| `POST /api/auth/signup` | `{email, password}` → 202 `{"status": "accepted"}`(항상 같은 본문). 422 `invalid_email` · `email_domain_not_allowed` · `weak_password` |
| `POST /api/auth/verify` | `{token}` → 204. 410 `token_expired` |
| `POST /api/auth/verify/resend` | `{email}` → 202(항상) |
| `POST /api/auth/password/forgot` | `{email}` → 202(항상) |
| `POST /api/auth/password/reset` | `{token, password}` → 204. 재설정과 초대 수락 공용. 410 `token_expired`, 422 `weak_password` |

- `/api/auth/*`의 POST는 IP당 1분에 20번까지(아래 "잠금과 요청 제한", 429 `rate_limited`).
- 본문의 `email`은 320자, `password`는 1024자, `token`은 200자까지(넘으면 422 `invalid_request`).

### `POST /api/auth/login`

1. 이메일을 앞뒤 공백을 빼고 소문자로 정규화한다. 하루 한 번 보존 기간 정리를 한다(아래 "보존 기간").
2. 잠긴 주소(계정의 `locked_until`이 지금보다 뒤, 또는 없는 주소의 메모리 잠금)는 비밀번호와 상관없이 해시 검증 한 번 뒤 423 `account_locked`.
3. 계정이 없거나, 형식이 틀리거나, 비밀번호가 없는 계정(`invited`)이면 **해시 검증 한 번을 수행한 뒤** 401 `invalid_credentials`(응답 시간을 맞춘다).
4. 비밀번호가 틀리면 401 `invalid_credentials`. 없는 계정과 같은 응답이다. 2~4의 실패는 그 주소의 실패 횟수를 1 올리고, 연속 5번째 실패면 15분 잠그고 그 응답부터 423 `account_locked`다.
5. 비밀번호가 맞은 뒤에만 상태를 알려 준다: `pending` → 403 `email_not_verified`, `suspended` → 403 `account_suspended`.
6. 성공하면 세션을 만들고 쿠키를 준다. `login_count`를 1 올리고 `last_login_at`을 기록하고 `failed_count`·`locked_until`을 지운다.
7. 응답의 `must_change_password`가 `true`면 웹앱은 비밀번호 변경 화면으로만 간다. 그동안 `require_user` 경로는 403 `password_change_required`.

| 상황 | 상태 | detail |
|---|---|---|
| 본문 형식 오류 | 422 | `invalid_request` |
| 없는 계정 · 틀린 비밀번호 · 비밀번호 없는 계정 | 401 | `invalid_credentials` |
| 비밀번호는 맞고 메일 인증 전 | 403 | `email_not_verified` |
| 비밀번호는 맞고 정지된 계정 | 403 | `account_suspended` |
| 잠긴 주소 · 이번이 연속 5번째 실패 | 423 | `account_locked` |
| IP별 요청 한도 초과 | 429 | `rate_limited`(`Retry-After` 헤더) |
| 인증 DB를 쓸 수 없음 | 503 | `auth_unavailable` |

## 운용 API (`/api/admin`, 운용자만)

`dashkit/auth/admin_routes.py`. 모든 경로가 `require_operator`다(user 403 `forbidden`, 로그인 없음 401). 권한 검사는 서버에서 한다.

- **operator는 `user` 등급 계정만 다룬다.** 요청의 대상(그룹 구성원 추가·제거 등)에 `operator`·`admin` 계정이 하나라도 있으면 요청 전체가 403 `forbidden`이고 아무것도 바꾸지 않는다.
- 목록 응답은 `{"items": [...], "total": n}`이고 `?page=`(1부터, 기본 1), `?size=`(1~200, 기본 50), `?sort=`(열 이름, 앞에 `-`면 내림차순)를 받는다. 정렬할 수 없는 열이나 범위 밖 값은 422 `invalid_request`. 정렬 값이 없는(null) 항목은 뒤로 간다.
- 경로의 id가 uuid 형식이 아니거나 없으면 404(`unknown_group` 등).

### 탭과 그룹

| 메서드·경로 | 본문 → 결과 |
|---|---|
| `GET /outbox` | 테스트 모드 전용(그 밖에는 404 `not_found`). → 목록(최근 순, 정렬 없음). 항목 `{id, to_email, kind, link, created_at, role}`(`role`: 받는 계정의 지금 등급). operator에게는 `role`이 `user`인 행만 |
| `GET /tabs` | → `{"project": "<현재>", "tabs": [{key, label, icon}], "others": [{project, tab_key, label, current, exists, groups}]}`. `others`는 그룹이 참조하지만 현재 프로젝트의 있는 탭이 아닌 것(다른 프로젝트, 설정에서 없어진 탭)과 그것을 참조하는 그룹 수 |
| `GET /groups` | → 목록. 항목 `{id, name, description, is_default, created_at, updated_at, member_count, tab_count}`(`tab_count`: 현재 프로젝트의 있는 탭 수). 정렬 `name`(기본)·`member_count`·`tab_count`·`is_default`·`created_at`·`updated_at` |
| `POST /groups` | `{name, description?, is_default?}` → 201 그룹 상세. 409 `group_exists` |
| `GET /groups/{id}` | → 그룹 상세: 위 항목(목록의 수 제외) + `tabs: [{project, tab_key, label, current, exists}]`, `members: [{id, email, role, status, added_at}]`(이메일 순) |
| `PATCH /groups/{id}` | `{name?, description?, is_default?}` → 200 그룹 상세. 409 `group_exists` |
| `DELETE /groups/{id}` | → 204 |
| `PUT /groups/{id}/tabs` | `{project, tab_keys: [...]}` → 200 그룹 상세. 그 프로젝트의 허용 탭 중 **설정에 있는 탭**을 통째로 바꾼다. 키는 그 프로젝트의 설정(`projects/<project>/`)에 있어야 한다. 아니면 422 `unknown_tab`(폴더가 없거나 설정이 유효하지 않은 프로젝트는 `[]`만 받고 아무것도 바꾸지 않는다). 설정에 없는 탭의 기존 기록은 건드리지 않는다 |
| `DELETE /groups/{id}/tabs/{project}/{tab_key}` | → 200 그룹 상세. 허용 탭 기록 하나만 지운다(없는 탭·다른 프로젝트 탭 정리). 없는 기록이면 아무것도 하지 않는다 |
| `PUT /groups/{id}/members` | `{add: [이메일 또는 id], remove: [id]}` → 200 `{"not_found": [...]}`(찾지 못한 이메일·id). 이미 있는 구성원 추가, 없는 구성원 제거는 무시한다 |

- 같은 그룹의 허용 탭 저장·삭제는 부모 `groups` 행을 먼저 잠가(`SELECT … FOR UPDATE`) 순서를 세운다. 동시에 저장해도 결과는 어느 한 요청의 목록이다.
- `label`은 그 프로젝트 설정의 탭 라벨이고 없는 탭이면 `null`. `current`는 현재 프로젝트의 것인지, `exists`는 그 프로젝트 설정에 지금 있는지.

## 감사 로그

`audit_log` 표에 남긴다(`store.record_audit`). 항목은 시각, 한 사람(id와 이메일 사본), 동작 코드, 대상(종류, id, 이름 사본), 변경 내용(`detail`, JSON), IP다. 이메일·이름을 사본으로 남겨 계정이나 그룹이 지워진 뒤에도 읽을 수 있다. `detail`에 비밀번호·토큰·해시를 넣지 않는다(키 이름에 `password`·`token`·`hash`·`secret`이 들어간 항목은 저장 전에 버린다).

| 동작 코드 | 언제 | `detail` |
|---|---|---|
| `auth.login` | 로그인 성공 | `{}` |
| `auth.login_failed` | 로그인 실패. 한 사람은 시도한 이메일(계정이 있으면 그 id) | `{"reason": "unknown_account" \| "no_password" \| "wrong_password" \| "locked" \| "email_not_verified" \| "account_suspended" \| "not_active"}` |
| `auth.logout` | 로그아웃(세션이 있었을 때) | `{}` |
| `auth.locked` | 연속 실패로 잠김 | `{"minutes": 15}` |
| `auth.signup` | 가입(새 `pending` 계정) | `{}` |
| `auth.verify_email` | 메일 인증 완료 | `{}` |
| `auth.password_reset_requested` | 비밀번호 찾기(재설정 메일을 보냈을 때) | `{}` |
| `auth.password_reset` | 재설정 링크로 비밀번호 변경 | `{}` |
| `auth.invite_accepted` | 초대 링크로 비밀번호 설정 | `{}` |
| `group.create` | 그룹 생성 | `{description, is_default}` |
| `group.update` | 그룹 이름·설명·기본 그룹 변경(바뀐 것이 있을 때) | `{<필드>: {"from", "to"}}` |
| `group.delete` | 그룹 삭제 | `{"members": <구성원 수>}` |
| `group.tabs` | 허용 탭 변경(바뀐 것이 있을 때) | `{project, added: [...], removed: [...]}` |
| `group.members` | 구성원 변경(바뀐 것이 있을 때) | `{added: [이메일], removed: [이메일]}` |

## 계정 흐름

### 가입과 메일 인증

1. `POST /api/auth/signup`: 이메일을 정규화하고 형식(422 `invalid_email`), 도메인(`AUTH_EMAIL_DOMAINS`, 422 `email_domain_not_allowed`), 비밀번호 정책(422 `weak_password`)을 순서대로 본다. 여기까지는 계정 존재와 상관없는 검사다.
2. 통과하면 비밀번호를 해시하고(어느 길이든 같은 비용) 계정에 따라:

   | 그 주소의 계정 | 동작 | 응답 |
   |---|---|---|
   | 없음 | `pending`·`signup` 계정을 만들고 인증 메일(`verify_email`, 24시간) | 202 |
   | `pending`(인증 전) | 비밀번호를 새 값으로 바꾸고 새 인증 메일(이전 링크 무효). 상태 확인·비밀번호·토큰 교체를 사용자 행을 잠근 한 트랜잭션으로 한다(`store.resign_pending`). 그 사이 인증돼 `pending`이 아니면 아래 줄처럼 처리한다 | 202 |
   | `invited`·`active`·`suspended` | 아무것도 바꾸지 않고 "이미 가입된 주소입니다" 메일(`already_registered`) | 202 |

3. 인증 링크는 `/verify.html#token=…`이다. 화면이 `POST /api/auth/verify`를 부르면 계정이 `active`가 되고(`email_verified_at`) 기본 그룹에 들어간다. 토큰이 없거나·만료됐거나·이미 쓰였거나·계정이 `pending`이 아니면 410 `token_expired`.
4. `pending` 계정이 인증 없이 7일 지나면 지운다(보존 기간 정리).
- 인증 메일 재발송: 로그인 실패 응답이 `email_not_verified`일 때 로그인 화면에 버튼이 나온다. `POST /api/auth/verify/resend`는 항상 202이고 그 주소가 `pending`일 때만 새 링크(이전 링크 무효)를 보낸다.

### 비밀번호 찾기·재설정

1. `POST /api/auth/password/forgot`은 계정이 있든 없든 202다. `active` 계정에만 재설정 메일(`reset_password`, 30분, 한 번만)을 보낸다. 새로 요청하면 이전 링크는 무효다. 정지된 계정은 받지 않는다.
2. 링크(`/reset.html#token=…`)의 화면이 `POST /api/auth/password/reset`을 부른다. 토큰의 용도와 계정 상태가 맞아야 한다(`reset_password`는 `active`, `invite`는 `invited`. 아니면 410 `token_expired`).
3. 정책 위반은 422 `weak_password`이고 **토큰은 그대로 남는다**(다시 입력할 수 있다).
4. 성공하면 토큰을 지우고, 새 비밀번호(`password_changed_at`), `must_change_password` 끄기, 실패 횟수·잠금 지우기, 그 사용자의 **모든 세션 삭제**. 초대(`invite`)면 계정이 `active`가 되고 기본 그룹에 들어간다(링크를 가진 것이 메일 수신 확인이다). 이 모두를 **한 트랜잭션**으로 한다(`store.use_reset_token`). 중간에 실패하면 토큰·비밀번호·세션이 모두 그대로다. 메일 인증(토큰 소비와 활성화)도 한 트랜잭션이다(`store.use_verify_token`). 재설정·인증·다시 가입·토큰 발급은 모두 그 사용자 행을 먼저 잠가(`SELECT … FOR UPDATE`) 서로 겹치지 않는다.

### 일회용 토큰

- 256비트 임의 값이고 DB(`tokens.token_hash`)에는 SHA-256만 저장한다. 한 번 쓰면 지운다(동시에 두 번 쓰면 하나만 성공한다).
- 같은 사용자·같은 용도로 새로 만들면 이전 것은 지운다(무효). 발급은 그 사용자 행을 먼저 잠가(`SELECT … FOR UPDATE`) 동시에 요청해도 유효한 토큰은 용도마다 하나만 남는다.
- 수명: `verify_email` 24시간, `reset_password` 30분, `invite` 72시간. 만료된 행은 보존 기간 정리가 지운다.
- 링크의 토큰은 URL 조각(`#token=…`)에 넣어 서버 접근 로그와 Referer에 남지 않게 한다. 화면은 토큰을 읽은 뒤 주소창에서 지운다(`history.replaceState`).

### 기본 그룹

`is_default`인 그룹(여러 개 가능)에 새 계정을 자동으로 넣는다. 넣는 시점은 계정이 `active`가 되는 순간(가입 메일 인증, 초대 수락)이다. 이후에 운용자가 빼면 다시 넣지 않는다.

## 메일과 테스트 모드

`dashkit/auth/mailer.py`의 인터페이스 하나 뒤에 둔다.

```python
class Mailer(Protocol):
    def send(self, to: str, kind: MailKind, link: str | None) -> None: ...
```

- `MailKind`: `verify_email` · `reset_password` · `invite` · `already_registered`. 제목과 본문 문구는 서버의 `TEMPLATES`에 둔다. 링크는 `WEB_PUBLIC_URL` + `/verify.html#token=…` 또는 `/reset.html#token=…`(재설정·초대 공용).

| `AUTH_MAIL_MODE` | 동작 |
|---|---|
| `test`(기본) | `OutboxMailer`: 실제로 보내지 않고 `dashkit_auth.outbox`에 쌓는다(받는 계정이 있을 때만) |
| `smtp` | `SmtpMailer`: 구현되지 않았다(`MailUnavailable`). 회사에서 `send`를 구현하고 접속 정보를 환경변수로 넣는다 |

- 로그인 전 경로(가입·재발송·찾기)는 메일을 보내지 못해도 응답을 바꾸지 않는다(계정 존재를 드러내지 않게). 실패는 ERROR 로그에 남긴다. 링크·토큰은 로그에 남기지 않는다.
- 비밀번호 찾기를 요청한 화면에 링크를 바로 보여 주는 방식은 쓰지 않는다.
- 테스트 모드일 때 로그인·가입·비밀번호 찾기 화면과 운용 화면 위에 "메일 테스트 모드: 메일이 실제로 발송되지 않습니다" 띠를 보인다(`GET /api/auth/config`의 `mail_mode`).

### 테스트 메일함

- 운용 API `GET /api/admin/outbox`(아래)와 운용 화면의 "테스트 메일함" 메뉴(테스트 모드에서만 보인다). 받는 사람, 종류, 시각, 링크를 최근 순으로 보여 주고 링크를 바로 열 수 있다.
- **등급 제한:** operator에게는 받는 계정의 **지금 등급이 `user`인 메일만** 돌려준다. admin·operator 계정 앞으로 간 메일은 목록에도 나오지 않고 링크도 돌려주지 않는다. admin은 전부 본다. 판정은 읽을 때의 등급으로 한다(`outbox.user_id`로 `users`와 묶는다). 비밀번호 찾기는 로그인 없이 아무 주소로나 요청할 수 있으므로, 이 제한이 없으면 operator가 admin 주소로 찾기를 요청하고 링크를 가져가 admin 계정을 차지할 수 있다.
- 계정이 지워지면 그 메일도 함께 지워진다(FK). 행은 7일 지나면 지운다. `smtp` 모드에서는 쌓지 않고 API는 404 `not_found`.
- CLI `make outbox`(= `python -m dashkit auth outbox`)는 등급 제한 없이 최근 메일과 링크를 출력한다(서버에 들어갈 수 있는 사람만 쓴다).

## 잠금과 요청 제한

`dashkit/auth/limits.py`. backend가 프로세스 하나이므로 IP 제한과 없는 주소의 잠금은 메모리에 센다(backend를 다시 시작하면 지워진다).

- **계정 잠금:** 같은 주소로 연속 5번 실패하면 15분 잠근다(`locked_until`, 그때 `failed_count`는 0으로). 잠긴 동안은 비밀번호가 맞아도 423 `account_locked`. 로그인에 성공하거나 비밀번호를 재설정하면 횟수와 잠금을 지운다.
- **없는 주소:** 같은 횟수에서 같은 423을 주도록 메모리에서 센다(잠금 응답으로 계정 존재를 알 수 없다). 해시 검증도 똑같이 한 번 한다.
- **IP별 요청 제한:** `/api/auth/*`의 POST는 IP당 최근 60초에 20번까지. 넘으면 429 `rate_limited`와 `Retry-After`. IP는 nginx가 넣는 `X-Real-IP`(backend는 내부 네트워크에만 열려 있다).

## 보존 기간

로그인·가입 요청을 처리하는 중에 하루 한 번(`dashkit/auth/maintenance.py`) 지운다: 인증 없이 7일 지난 `pending` 계정, 7일 지난 outbox 행, 만료된 일회용 토큰, 만료된 세션.

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
| `make outbox [LIMIT=20]` = `python -m dashkit auth outbox --limit <n>` | 테스트 메일함의 최근 메일과 링크(등급 제한 없음). admin 자신이 비밀번호를 잊었을 때, operator 계정의 링크가 필요한데 admin이 없을 때 쓴다 |
| `python -m dashkit auth init` | `dash_auth`로 접속해 스키마 버전을 확인한다(종료 코드 0/1) |

## 화면

바닐라 JS, 빌드 단계 없음, 문구는 `web/strings.js`, 인라인 스크립트 없음(CSP `script-src 'self'`), 기존 디자인 토큰.

| 파일 | 내용 |
|---|---|
| `web/signup.html` | 이메일(아래에 "사내 이메일(@<도메인>)로 가입해야 합니다"), 비밀번호(정책 안내), 비밀번호 확인. 제출 후 "메일을 확인하세요" |
| `web/forgot.html` | 이메일. 제출 후 항상 "가입된 주소라면 메일을 보냈습니다" |
| `web/reset.html` | 새 비밀번호, 확인. 재설정과 초대 수락이 같이 쓴다. 토큰이 만료·사용됐으면 다시 요청하라는 안내 |
| `web/verify.html` | 메일 인증 링크의 도착 화면. 열리면 바로 인증하고 성공·만료 안내와 로그인 링크 |
| `web/login.html` + `web/auth.js` + `web/auth.css` | 이메일, 비밀번호, 로그인 버튼, "가입하기"·"비밀번호를 잊으셨나요?" 링크. 실패·정지·미인증(인증 메일 다시 보내기 버튼)·잠금 안내. 성공하면 `?next=`로 간다. `next`는 브라우저와 같은 URL 파서(`new URL(next, 지금 출처)`)로 읽어 출처가 지금 출처와 같을 때만 그 경로·쿼리·해시를 쓰고, 아니면(다른 출처, 탭·줄바꿈·역슬래시를 섞은 우회, 로그인 전 화면) `/`로 간다(`web/common.js`의 `safeNext`, 테스트 `tests/js/`). `?reason=expired`면 "로그인이 만료되었습니다", `?reason=logout`이면 "로그아웃했습니다" |
| `web/index.html`(웹앱 셸) | `/api/app`이 401이면 `/login.html?next=<지금 주소>`로 보낸다. 섹션의 guest token·데이터 요청이 401이면 `?reason=expired`를 붙여 보낸다. 레일 아래(테마 버튼 위)에 사용자 메뉴: 이메일, 등급, 로그아웃 |
| `web/common.js` | 셸과 로그인 화면이 함께 쓰는 도우미(요소 생성, `next` 검사, JSON POST) |
| `web/index.html` 사용자 메뉴 | 운용자에게만 "운용"(`/admin/`) 링크. user 계정의 탭이 하나도 없으면 "접근할 수 있는 대시보드가 없습니다. 운용자에게 요청하세요." |
| `web/admin/`(`index.html`, `admin.js`, `admin.css`) | 운용 화면. 주소의 `#` 뒤로 메뉴를 고른다. 시작할 때 `/api/app`의 `user.role`이 운용자가 아니면(또는 API가 403 `forbidden`이면) 웹앱(`/`)으로 돌려보내고, 401이면 로그인 화면으로 보낸다. 테스트 모드면 위에 메일 테스트 모드 띠. 메뉴: **테스트 메일함**(`#outbox`, 테스트 모드에서만: 받는 사람·등급, 종류, 시각, 링크 열기, 다시 불러오기), **그룹**(`#groups`: 목록, 정렬, 50개씩, 그룹 만들기 / `#group/<id>`: 이름·설명·기본 그룹, 현재 프로젝트의 탭 체크 목록, 다른 프로젝트·없는 탭 묶음과 항목별 빼기(그 항목만), 구성원 목록과 빼기, 이메일로 추가(쉼표·줄바꿈 구분, 찾지 못한 주소 안내), 삭제 확인) |
