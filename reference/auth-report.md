# 로그인·사용자 관리 구현 보고서

> 작성일: 2026-11-20 · 설계서: `reference/auth-design.md` · 실행 프롬프트: `reference/auth-prompt.md` · 계약: `contracts/auth.md`

## 1. 단계별 상태

| 단계 | 상태 | PR |
|---|---|---|
| 준비(설계서 PR) | 이미 병합돼 있었음 | ohyounjo/dashboard#15 |
| 1. 기반 | 병합됨 | ohyounjo/dashboard#16 |
| 2. 접근 제어 | 병합됨 | ohyounjo/dashboard#17 |
| 3. 가입과 메일 | 병합됨 | ohyounjo/dashboard#18 |
| 4. 운용 화면 | **멈춤**(PR 열어 둠, 병합하지 않음) | ohyounjo/dashboard#19 |
| 5. 통계와 기록 | **시작 안 함**(PR 없음. 구현은 브랜치 `feat/auth-stats`에 있음, 아래 1-1) | — |

### 1-1. 멈춘 이유

4단계 PR #19에서 리뷰 반복을 프롬프트의 한도(PR당 3번)까지 했다. 세 번째 재리뷰(1f8bd04)가 "사용자 PATCH의 등급·상태·그룹 변경을 한 트랜잭션으로" 지적을 냈다. Codex 등급은 P2지만 권한(operator 제한) 관련이라 규칙대로 P1로 다뤘고, **반복 3번 뒤에도 P1이 남은 상태**라 멈춤 조건에 해당한다. 그래서 #19를 병합하지 않았고 5단계 PR도 올리지 않았다.

- 그 지적은 고쳐서 푸시해 두었다(5f6805d, `store.update_user` 한 트랜잭션, 회귀 테스트 `test_user_patch_is_all_or_nothing`). 이 커밋은 4번째 리뷰를 받지 않았다. 리뷰를 받으려면 PR에 `@codex review`를 남기면 된다.
- 5단계는 4단계 위에서 이미 구현해 두었다: 브랜치 `feat/auth-stats`(원격에 푸시, PR 없음). 확인용 스택에서 `tests/test_auth_stats.py`를 포함한 인증 통합 테스트가 통과했고 단위 테스트도 통과했지만, **이 브랜치의 전체 `make test`는 끝까지 돌리지 않았다**(4단계가 바뀔 때마다 다시 시작해야 해서 중간에 멈췄다). 4단계를 병합한 뒤 이 브랜치를 main 위로 옮기고 `make test-unit`·`make test`를 돌려 PR을 올리면 된다.


## 2. 완료 조건 확인(테스트 출력 요약)

병합한 단계(1~3)는 모두 마지막 커밋 기준으로 `make test-unit`(앱 이미지·웹앱 JS·Studio 이미지)과 `make test`(별도 테스트 스택: devtracker 전체 → Studio → sales-sample 전환 → chart-gallery → 새 프로젝트)가 통과한 뒤 병합했다. GitHub Actions(단위 테스트, 화면 캡처 base·head와 head의 `make test`, 비교 댓글)도 모두 성공했다.

| 단계 | 완료 조건(13절) | 확인한 테스트 | 마지막 커밋의 결과 |
|---|---|---|---|
| 1 | admin으로 로그인해 기존 웹앱을 그대로 본다. 로그인 없이 `/api/app`·`/api/data`·`/api/guest-token`이 401. DB 계정 권한 경계 | `tests/test_auth_login.py`, `tests/test_auth_db.py`, 기존 통합 테스트 전부(로그인 도우미) | 2ca83eb: 단위 364 · JS 4 · Studio 322, 통합 431 passed / 40 skipped(기존 조건부) · Studio 11 · 전환 15 · 갤러리 45 · 새 프로젝트 3 |
| 2 | user 계정이 허용된 탭만 보고, 다른 탭의 guest token·데이터 요청이 403·404 | `tests/test_auth_access.py`, `tests/test_auth_access_unit.py` | 085f1d5: 단위 370 · JS 4 · Studio 322, 통합 447 / 40 · 11 · 15 · 45 · 3 |
| 3 | 테스트 메일함의 링크만으로 가입 → 인증 → 로그인 → 찾기 → 재설정 | `tests/test_auth_signup.py`, `tests/test_auth_mail_unit.py` | 8cf6e94: 단위 377 · JS 4 · Studio 322, 통합 471 / 40 · 11 · 15 · 45 · 3 |
| 4 | 3절의 권한 표가 테스트로 고정된다(operator가 admin을 건드리지 못함, 마지막 admin 보호) | `tests/test_auth_users.py` | 5f6805d(병합 안 함): 단위 377 · JS 4 · Studio 322, 통합 493 passed / 40 skipped · Studio 11 · 전환 15 · 갤러리 45 · 새 프로젝트 3 |
| 5 | 통계 화면에 탭별·사용자별 수치, 9-2의 모든 동작이 감사 로그에 남는다 | `tests/test_auth_stats.py`(브랜치 `feat/auth-stats`) | 확인용 스택(웹앱·backend·DB만, Superset 없음)에서 `test_auth_stats.py`·`test_auth_users.py`·`test_auth_signup.py`·`test_auth_db.py` 37 passed(조회 동시성 테스트를 더하기 전), 단위 377 passed. 전체 `make test`는 아직 |

13절 "반드시 포함할 것":

| 항목 | 테스트 |
|---|---|
| 가입·찾기 응답으로 계정 존재 여부를 구분할 수 없음 | `test_signup_and_forgot_do_not_reveal_accounts`, 로그인은 `test_failed_login_does_not_reveal_accounts`·`test_missing_account_takes_as_long_as_wrong_password`·`test_missing_accounts_lock_the_same_way` |
| 토큰 재사용·만료 거절 | `test_tokens_are_single_use_expire_and_replace_older` |
| 비밀번호 변경 후 다른 세션 무효 | `test_temporary_password_forces_change`, `test_signup_verify_login_forgot_reset_with_outbox_links` |
| 권한 없는 탭의 guest token 거절 | `test_user_sees_only_allowed_tabs`, `test_guest_token_and_data_follow_allowed_tabs` |
| `dash_reader`가 `dashkit_auth`를 읽지 못함 | `test_reader_and_owner_cannot_read_auth_schema` |
| operator가 admin·operator 주소로 비밀번호 찾기를 요청해도 테스트 메일함에서 그 링크를 볼 수 없음 | `test_operator_cannot_see_mail_for_admins_or_operators` |
| 등급 변경 뒤 이전에 발급된 토큰 거절 | `test_admin_manages_operators_and_roles_kill_sessions_and_tokens` |

화면은 단계마다 헤드리스 브라우저(Playwright)로 1280px·390px × 라이트·다크를 직접 확인했고, PR마다 `ui-screenshots` 워크플로가 변경 전·후 캡처를 댓글로 달았다(이 PR부터 캡처용 admin으로 로그인해 찍고 로그인 전 화면·운용 화면·내 정보도 찍는다).

## 3. 리뷰에서 받은 지적과 처리

리뷰어는 Codex(`chatgpt-codex-connector[bot]`)다. 인증·권한·비밀값 관련 지적은 등급과 상관없이 P1로 다뤘다. 고치지 않은 지적은 없다.

| PR | 등급 | 지적 | 처리 |
|---|---|---|---|
| #16 | P2→P1 | `safeNext()`가 원문 접두사만 봐서 `/\t/evil.example` 같은 열린 리디렉션 | 고침(720228c): 브라우저 URL 파서로 출처 검사, `tests/js/` node 테스트 추가 |
| #16 | P2→P1 | 허용 출처의 기본 포트(`:443`·`:80`)를 지우지 않아 같은 출처 요청이 403 | 고침(2ca83eb) |
| #17 | P2→P1 | 허용 탭 교체가 그룹 행을 잠그지 않아 동시 저장이 합쳐짐 | 고침(085f1d5): 그룹 행 `FOR UPDATE` |
| #17 | P2→P1 | 없는 탭 하나를 빼면 같은 프로젝트의 다른 없는 탭도 지워짐 | 고침(085f1d5): 항목별 `DELETE` 경로, PUT은 설정에 있는 탭만 바꿈 |
| #18 | P1 | 재설정이 토큰 소비·비밀번호·세션 삭제를 따로 커밋 | 고침(a1cd2e7): 한 트랜잭션 |
| #18 | P2→P1 | 같은 사용자·용도의 토큰 동시 발급 | 고침(a1cd2e7): 사용자 행 잠금 |
| #18 | P1 | 인증 전 계정의 다시 가입과 옛 링크 인증이 겹침 | 고침(8a8b936): `resign_pending` 한 트랜잭션, 인증도 같은 행 잠금 |
| #18 | P1 | 신규 가입의 계정 생성과 첫 토큰 사이 틈 | 고침(8cf6e94): `create_pending_user` |
| #18 | P1 | 옛 비밀번호로 시작한 로그인이 재설정 뒤 세션을 만듦 | 고침(8cf6e94): `start_session`이 잠금 안에서 해시 재확인 |
| #19 | P1 | 내 비밀번호 변경이 동시 리셋을 덮어씀 | 고침(eb02202): `change_own_password` |
| #19 | P2→P1 | 운용 화면이 그룹을 200개까지만 읽어 소속이 지워질 수 있음 | 고침(eb02202): 끝 페이지까지 읽고 못 읽으면 저장 안 함 |
| #19 | P1 | operator 권한 검사가 트랜잭션 밖이라 검사 뒤 승급된 계정을 다룰 수 있음 | 고침(6d1c00d): 모든 운용 저장이 대상 행을 잠근 뒤 지금 등급·상태로 다시 검사 |
| #19 | P2→P1 | 그룹 없는 사용자의 그룹 교체가 직렬화되지 않음 | 고침(6d1c00d): 사용자 행 잠금 |
| #19 | P2→P1 | 일괄 그룹 지정이 그룹마다 따로 커밋돼 중간 권한 실패 때 일부만 반영 | 고침(1f8bd04): `bulk_change_groups` 한 트랜잭션 |
| #19 | P2 | 구성원 추가와 사용자 그룹 저장의 잠금 순서가 달라 교착 가능 | 고침(1f8bd04): 어디서나 사용자 행 → 그룹 행 |
| #19 | P2→P1 | 사용자 PATCH의 등급·상태·그룹 변경이 따로 커밋돼 뒤쪽 권한 실패 때 앞쪽 변경이 남음 | 고침(5f6805d): `store.update_user` 한 트랜잭션. **리뷰 반복 한도 뒤의 지적이라 멈춤**(1-1) |

같은 종류의 지적이 이어져서, 리뷰가 오기 전에 미리 막은 것도 있다: 마지막 활성 admin 판정을 활성 admin 행 잠금 안에서 다시 하기(두 admin이 동시에 서로를 정지해도 0명이 되지 않음), 초대의 계정·그룹·토큰을 한 트랜잭션으로, 조회 기록의 30분 규칙을 사용자·탭별 트랜잭션 잠금으로.

## 4. 설계서와 달라진 점과 이유

| 설계서 | 구현 | 이유 |
|---|---|---|
| 흔한 비밀번호 목록 `dashkit/auth/common_passwords.txt` | `dashkit/auth/vendor/common_passwords.txt`(+ 출처·라이선스 README) | 제3자 데이터를 `vendor/`에 모으는 기존 방식(`web/vendor/`). 도메인 단어 검사(`tests/test_domain_words.py`)가 `vendor/`를 건너뛰므로 목록 속 일반 영어 단어에 걸리지 않음 |
| 요청 보호 위반은 403 `forbidden_origin` | 출처 위반은 403 `forbidden_origin`, Content-Type 위반은 415 `unsupported_media_type` | Studio guard와 같은 코드. 원인을 더 정확히 알려 줌. 출처 검사가 먼저 |
| `PUT /groups/{id}/tabs`가 그 프로젝트의 허용 탭을 통째로 바꿈 | 설정에 있는 탭만 바꾸고, 없는 탭 기록은 `DELETE /groups/{id}/tabs/{project}/{tab_key}`로 하나씩 지움 | 리뷰 지적: 없는 탭 기록이 저장만으로 사라지지 않게(4-3 "운용자가 정리한다") |
| `PUT /groups/{id}/members`의 `not_found`는 이메일 | 찾지 못한 이메일과 id 모두 | id로 넣거나 빼는 경우도 알려 주려고 |
| 가입 응답은 202(본문 미정) | 202 `{"status": "accepted"}` | 본문 형식을 정함 |
| (설계서에 없음) | `GET /api/me`의 등급 표, 감사 로그 `actions` 목록, 통계 `never`·`inactive` 표시, `make env-secrets` | 화면 구현과 예전 `.env` 호환에 필요 |
| `last_admin`과 `self_target`의 순서 미정 | `last_admin`을 먼저 | 활성 admin이 하나뿐이면 그를 바꾸려는 사람은 그 자신이라 반대 순서면 `last_admin`에 닿지 않음 |
| 운용자 경로의 메일 실패 | smtp 모드에서는 계정을 만들기 전에 503 `mail_unavailable` | 로그인 전 경로는 계정 존재를 숨기려고 같은 202를 주고, 운용자 경로는 결과를 알려 줌 |

## 5. 스스로 정한 판단

1. `<사내 도메인>`이 정해지지 않아 개발용 `AUTH_EMAIL_DOMAINS`는 `.env.example`과 같은 `example.com`이다(프롬프트의 지시).
2. 10절의 표를 1단계에서 모두 만들었다(이후 단계는 스키마를 바꾸지 않음, 버전 1).
3. `dashkit_auth`의 소유자는 superuser(postgres)다. 로더 계정이 DB 소유자라서 스키마까지 갖게 하면 지울 수 있기 때문이다. 역할 비밀번호는 명령줄이 아니라 `\getenv`로 넘긴다.
4. 예전 `.env`에 없는 비밀값(`DATA_DB_AUTH_PASSWORD`)은 `make up`·`make auth-init`이 임의 값으로 덧붙인다(`env-secrets`). 기존 값은 건드리지 않는다.
5. 비밀번호가 맞을 때만 `email_not_verified`·`account_suspended`를 알려 준다(틀리면 늘 `invalid_credentials`).
6. 연속 5번째 실패에서 바로 423이고, 잠길 때 실패 횟수를 0으로 돌린다. 없는 주소의 잠금과 IP 제한은 backend 메모리에서 센다. IP 제한은 `/api/auth/*`의 POST만 센다.
7. 인증 전 계정에 다시 가입하면 비밀번호를 새 값으로 바꾸고 새 인증 링크를 보낸다(메일함을 가진 사람이 계정을 가져감).
8. operator는 그룹 구성원으로도 operator·admin을 넣거나 빼지 못한다(3절을 단순하게 지킴).
9. 임시 비밀번호는 `active`·`suspended`, 재설정 메일은 `active`, 초대 재발송은 `invited`만 받는다.
10. 통합 테스트의 인증 POST는 backend에 바로 보내고 요청마다 다른 `X-Real-IP`를 붙인다(IP 제한 때문). 운영에서 backend는 내부망에만 열려 있고 nginx가 이 헤더를 덮어쓴다.
11. 사용자·그룹·토큰·세션을 바꾸는 저장은 모두 대상 행을 잠근 한 트랜잭션에서 하고, 권한·상태·해시를 그 안에서 다시 본다(리뷰에서 이어진 동시성 지적을 한 규칙으로 정리).
12. 통계는 현재 프로젝트의 탭만 센다. "본 사람 수"는 지금 있는 계정만 센다.

## 6. 직접 하실 것

0. **4단계 PR #19 결정:** 마지막 커밋(5f6805d)에 `@codex review`로 리뷰를 한 번 더 받거나 직접 검토한 뒤 병합할지 정하세요. 병합하면 `feat/auth-stats`를 main 위로 옮겨 5단계 PR을 올리면 됩니다(위 1-1).
1. **첫 admin 만들기:** 개발 스택에는 인증 DB가 준비돼 있고(`make auth-init` 완료, 스키마 버전 1) 계정은 없습니다.
   ```bash
   make admin EMAIL=<내 주소@example.com>
   ```
   도메인은 `.env`의 `AUTH_EMAIL_DOMAINS`(지금 `example.com`)에 있어야 합니다. 회사 도메인으로 바꾸면 backend를 다시 띄우세요(`make up`).
2. **테스트 메일함으로 흐름 확인:** http://localhost:8080/signup.html 에서 가입 → 운용 화면(http://localhost:8080/admin/#outbox) 또는 `make outbox`에서 인증 링크 → 로그인 → 비밀번호 찾기 → 메일함 링크로 재설정.
3. **화면 확인:** 지금 main(3단계까지)의 운용 화면은 그룹·테스트 메일함 메뉴만 있습니다. 사용자·통계·감사 로그 메뉴와 내 정보(`/account.html`)는 4·5단계 브랜치에 있습니다. 그룹을 하나 만들고 일반사용자에게 탭을 허용해 보세요(그룹이 없는 일반사용자는 아무 탭도 보지 못합니다).

## 7. 회사에서 정해야 할 것(설계서 14절) 중 남은 항목

| # | 항목 | 상태 |
|---|---|---|
| 1 | 가입을 허용할 사내 메일 도메인(`AUTH_EMAIL_DOMAINS`) | 미정. 지금 `example.com` |
| 2 | 사내 메일 발송 방식과 `Mailer` 구현 담당 | 미정. `AUTH_MAIL_MODE=smtp`는 "구현되지 않음"까지만 있음(`dashkit/auth/mailer.py`의 `SmtpMailer`) |
| 3 | 사내 공개 주소와 HTTPS 인증서 | 미정. 공개할 때 `WEB_ORIGINS`, `WEB_PUBLIC_URL`, `SUPERSET_PUBLIC_URL`, `AUTH_COOKIE_SECURE=1` |
| 4 | 보존 기간(감사 로그 1년, 접속 기록 180일)이 사내 규정에 맞는지 | 확인 필요. 값은 `dashkit/auth/maintenance.py` |

## 8. 작업 중 알게 된 것

- 같은 기계의 다른 Claude 세션이 같은 이름의 테스트 스택(`dashkit-test`)과 같은 이미지 태그(`dashkit-app:local`)로 `make test`를 돌려 서로의 실행이 섞였다. 그 세션과 순서를 맞췄고, 이후 그 세션은 별도 이름의 테스트 스택을 쓴다. 이미지 태그는 여전히 공유되므로 두 작업 폴더에서 동시에 빌드하지 않는 것이 안전하다.
- 개발 스택(웹앱 8080, Superset 8088, PROJECT=devtracker)은 단계를 병합할 때마다 main으로 `make up && make apply`를 해 두었다(지금 3단계까지의 main). 볼륨과 `.state/`는 지우지 않았다.
- `dashkit-app:local` 이미지는 테스트 때문에 4단계 브랜치로 빌드된 상태일 수 있다. 개발 스택을 main 코드로 확실히 맞추려면 main에서 `make up`을 한 번 하세요(지금 돌고 있는 컨테이너는 main으로 만든 것이다).
