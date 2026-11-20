# 설계서: 로그인·사용자 관리·대시보드 접근 제어

> 작성일: 2026-11-20 · 대상: `main`(커밋 `56e9997`) · 상태: 기능 범위 승인 완료(2절), 구현 전
> 관련 문서: `contracts/api.md`(백엔드 API), `contracts/studio.md`(요청 보호 방식), `reference/enhancement-plan.md`(보강설계서)

## 1. 요약

지금 웹앱은 인증이 없고 `127.0.0.1`에만 열려 있다. 이 설계는 웹앱에 **로그인, 사용자 등급, 대시보드 그룹**을 넣어 사내에서 여러 사람이 쓸 수 있게 한다.

- 사용자는 사내 이메일과 비밀번호로 가입하고 로그인한다.
- 등급은 `admin`(어드민), `operator`(운용진), `user`(일반사용자) 셋이다.
- 일반사용자는 자기가 속한 **대시보드 그룹**이 허용한 탭만 본다. 그룹은 여러 개에 속할 수 있다.
- 운용자(어드민·운용진)는 운용 화면에서 사용자와 그룹을 관리한다.
- 메일 발송은 회사 시스템 연동 전까지 **테스트 모드**로 동작한다(6절).

용어: 이 문서에서 **운용자**는 `admin`과 `operator`를 함께 가리킨다. **대시보드**는 웹앱 왼쪽의 탭 하나다(4-1).

## 2. 기능 범위

### 2-1. 요청 기능

| # | 기능 | 절 |
|---|---|---|
| R1 | 가입: 이메일(사내 이메일 안내 문구) + 비밀번호 | 5-1 |
| R2 | 비밀번호 찾기: 사내 메일 인증 → 변경 화면 링크. 지금은 테스트 모드 | 5-3, 6 |
| R3 | 운용자의 비밀번호 리셋(메일이 안 될 때의 백업) | 5-4 |
| R4 | 등급 3종(어드민, 운용진, 일반사용자) | 3 |
| R5 | 대시보드 그룹: 일반사용자에게 여러 개 지정, 그룹의 대시보드·사용자 편집 | 4 |
| R6 | 운용 화면의 사용자 관리(가입일, 접속 횟수, 최근 접속일, 등급 등) | 8-2 |
| R7 | 운용자의 임의 가입 + 비밀번호 설정 링크가 든 메일 발송 | 5-5 |
| R8 | 운용자의 사용자 삭제 | 5-7 |

### 2-2. 추가 기능(승인 결과)

| # | 기능 | 결정 | 절 |
|---|---|---|---|
| A1 | 가입 이메일 인증 | **승인** | 5-1 |
| A2 | 계정 정지(비활성화) | **승인** | 5-6 |
| A3 | 일괄 초대·일괄 그룹 지정 | **승인** | 5-5, 8-2 |
| A4 | 대시보드 접속 통계 | **승인** | 9-1 |
| A5 | 신규 가입자 기본 그룹 | **승인** | 4-3 |
| A6 | 로그인 실패 잠금 | **승인**(작성자 제안) | 7-3 |
| A7 | 비밀번호 정책·강제 변경 | **승인**(작성자 제안) | 7-1 |
| A8 | 세션 관리(만료, 일괄 무효화) | **승인**(작성자 제안) | 7-2 |
| A9 | 감사 로그 | **승인**(작성자 제안) | 9-2 |
| — | 가입 승인 대기 | 제외 | |
| — | 장기 미접속 자동 휴면 | 제외 | |
| — | 사용자 목록 검색·필터·CSV 내보내기 | 제외(목록은 정렬과 페이지 나눔만) | |

A6~A9는 작성자가 제안해 승인받은 것이다. R3·R7이 이 기능에 기댄다: 운용자가 임시 비밀번호를 발급하면 첫 로그인 때 바꾸게 해야 하고(A7), 리셋하면 기존 로그인을 끊어야 하며(A8), 누가 리셋·삭제했는지 남아야 한다(A9). A6은 비밀번호 로그인을 사내망에 열 때의 최소 방어다.

### 2-3. 하지 않는 것

- 사내 SSO(SAML, OIDC), 2단계 인증. 나중에 붙일 수 있게 인증 수단을 `dashkit/auth/` 안에 가둔다.
- Studio의 인증. Studio는 지금처럼 인증 없이 `127.0.0.1` 전용이다(`contracts/studio.md`).
- Superset 자체 계정과의 연동. 웹앱 사용자는 Superset 계정을 갖지 않고 지금처럼 guest token으로만 본다.
- 행 단위 권한(같은 대시보드에서 사용자마다 다른 행). guest token의 `rls_rules`는 계속 빈 배열이다.

## 3. 등급과 권한

| 할 수 있는 일 | admin | operator | user |
|---|---|---|---|
| 대시보드 보기 | 전부 | 전부 | 속한 그룹이 허용한 탭 |
| 내 정보 보기, 내 비밀번호 변경 | ✓ | ✓ | ✓ |
| 사용자 목록·상세 보기 | ✓ | ✓ | |
| 일반사용자 임의 가입·초대, 리셋, 정지, 삭제 | ✓ | ✓ | |
| 그룹 만들기·편집·삭제 | ✓ | ✓ | |
| 접속 통계, 감사 로그, 테스트 메일함 보기 | ✓ | ✓ | |
| 등급 변경 | ✓ | | |
| operator·admin 계정의 리셋, 정지, 삭제 | ✓ | | |

지켜야 할 규칙(서버에서 검사한다. 화면에서 버튼을 숨기는 것만으로 끝내지 않는다):

- operator는 `user` 등급만 다룬다. operator·admin 계정을 대상으로 한 변경 요청은 403 `forbidden`.
- 활성 상태인 admin이 한 명뿐이면 그 계정은 삭제·정지·강등할 수 없다(409 `last_admin`).
- 자기 자신은 삭제·정지·강등할 수 없다(409 `self_target`).
- 첫 admin은 화면으로 만들지 않는다. `make admin EMAIL=<주소>`(= `python -m dashkit auth create-admin`)로 만들고 비밀번호는 프롬프트로 받는다. 코드와 `.env`에 넣지 않는다.

## 4. 대시보드 그룹과 접근 제어

### 4-1. 접근 단위: 탭

접근을 허용하는 단위는 `dashboard.yaml`의 **탭**(`tabs[].key`)이다. 탭 하나에 Superset 대시보드와 위젯 섹션이 함께 들어가므로, 탭을 허용하면 그 안의 섹션이 전부 열린다. 권한은 `(프로젝트 이름, 탭 키)`로 저장한다. 현재 프로젝트가 바뀌어도 다른 프로젝트의 권한이 남아 있어서 되돌아오면 그대로 쓴다.

### 4-2. 판정 규칙

- admin·operator는 현재 프로젝트의 모든 탭을 본다.
- user는 속한 그룹들이 허용한 탭의 **합집합**을 본다. 그룹이 없거나 허용 탭이 없으면 빈 화면과 안내 문구("접근할 수 있는 대시보드가 없습니다. 운용자에게 요청하세요")를 본다.
- 판정은 요청마다 DB에서 읽는다. 그룹을 고치면 다음 요청부터 반영된다.

기존 API에 적용하는 방식:

| 경로 | 변경 |
|---|---|
| `GET /api/app` | 로그인 필요. `tabs`에서 허용되지 않은 탭을 뺀다. 응답에 `user: {email, role}`을 더한다. `applied`는 남은 탭만으로 계산한다 |
| `POST /api/guest-token` | 로그인 필요. 요청한 대시보드 키가 **허용된 탭의 superset 섹션**에 있어야 한다. 아니면 403 `forbidden` |
| `GET /api/data/{dataset}` | 로그인 필요. 데이터셋이 **허용된 탭의 위젯 섹션**에 있어야 한다. 아니면 지금과 같은 404 `unknown_dataset` |
| `GET /api/health` | 그대로(인증 없음. compose healthcheck) |

- 세션이 없거나 만료됐으면 401 `unauthenticated`. 웹앱은 `/login.html`로 보낸다.
- 알려진 한계: guest token은 발급 후 300초 동안 유효하다. 그룹에서 빠진 사용자가 이미 받은 토큰으로 최대 5분 더 볼 수 있다. 수명을 줄이지 않고 받아들인다.
- 알려진 한계: 같은 Superset 대시보드를 두 탭에서 쓰면, 한 탭만 허용돼도 그 대시보드의 토큰은 발급된다(탭이 아니라 대시보드에 대한 토큰이므로). 의도한 동작이다.

### 4-3. 그룹

- 그룹은 이름(중복 불가), 설명, 허용 탭 목록, 구성원 목록, `is_default` 표시를 가진다.
- **기본 그룹(A5):** `is_default`인 그룹(여러 개 가능)에 새 계정을 자동으로 넣는다. 넣는 시점은 계정이 `active`가 되는 순간(가입 메일 인증 완료, 초대 수락)이다. 이후에 운용자가 빼면 다시 넣지 않는다.
- 설정 파일에서 탭이 없어지면 그룹의 그 항목은 지우지 않고 편집 화면에 "없는 탭"으로 표시한다. 운용자가 정리한다. 없는 탭은 판정에서 무시된다.
- 그룹을 삭제하면 구성원·허용 탭 연결만 지워진다. 사용자 계정은 남는다.

## 5. 계정 흐름

### 5-0. 계정 상태

| 상태 | 뜻 | 로그인 |
|---|---|---|
| `pending` | 스스로 가입했고 메일 인증 전 | 불가(403 `email_not_verified`) |
| `invited` | 운용자가 만들었고 비밀번호 설정 전 | 불가 |
| `active` | 정상 | 가능 |
| `suspended` | 운용자가 정지 | 불가(403 `account_suspended`) |

잠금(7-3)은 상태가 아니라 `locked_until` 시각이다.

### 5-1. 가입 (R1, A1)

1. 가입 화면에서 이메일, 비밀번호, 비밀번호 확인을 입력한다. 이메일 칸 아래에 **"사내 이메일(@<도메인>)로 가입해야 합니다"** 문구를 둔다.
2. 서버는 이메일을 소문자로 정규화하고, 도메인이 `AUTH_EMAIL_DOMAINS`(쉼표 구분, 필수 환경변수)에 있는지와 비밀번호 정책(7-1)을 검사한다. 도메인이 다르면 422 `email_domain_not_allowed`.
3. 통과하면 `pending` 계정을 만들고 인증 메일(24시간 유효 링크)을 보낸다. 응답은 항상 202이고 화면은 "메일을 확인하세요"를 보여 준다.
4. 링크를 누르면 `active`가 되고 기본 그룹에 들어간다. 로그인 화면으로 간다.

- 이미 있는 이메일로 가입하면 **같은 202 응답**을 주고, 메일로 "이미 가입된 주소입니다. 비밀번호를 잊었다면 비밀번호 찾기를 쓰세요"를 보낸다. 가입 화면으로 어떤 주소가 가입돼 있는지 알아낼 수 없게 한다.
- `pending` 계정이 인증 없이 7일 지나면 지운다(로그인 요청 처리 중에 하루 한 번 정리).
- 인증 메일 재발송: 로그인 실패 응답이 `email_not_verified`일 때 화면에 재발송 버튼을 보인다.

### 5-2. 로그인·로그아웃

- 이메일 + 비밀번호. 실패 사유(없는 계정, 틀린 비밀번호)는 구분하지 않고 401 `invalid_credentials` 하나다. 없는 계정일 때도 해시 검증 한 번을 수행해 응답 시간을 맞춘다.
- 성공하면 세션 쿠키를 주고(7-2) `login_count`를 1 올리고 `last_login_at`을 기록한다.
- `must_change_password`가 켜진 계정은 로그인 직후 비밀번호 변경 화면으로만 갈 수 있다. 그동안 다른 API는 403 `password_change_required`.
- 로그아웃은 서버의 세션 행을 지우고 쿠키를 만료시킨다.

### 5-3. 비밀번호 찾기 (R2)

1. 로그인 화면의 "비밀번호를 잊으셨나요?" → 이메일 입력.
2. 서버는 계정이 있든 없든 202를 준다. `active` 계정이면 재설정 메일(30분 유효, 한 번만 사용)을 보낸다. 새로 요청하면 이전 링크는 무효다.
3. 링크(`/reset.html#token=…`)의 화면에서 새 비밀번호를 정한다. 토큰은 URL 조각(`#`)에 넣어 서버 접근 로그와 Referer에 남지 않게 한다.
4. 변경에 성공하면 그 사용자의 모든 세션을 지우고 로그인 화면으로 보낸다. 잠금도 푼다.

실제 사내 메일 발송은 회사 쪽에서 붙인다. 그 전까지는 6절의 테스트 모드로 이 흐름 전체를 확인할 수 있다.

### 5-4. 운용자의 비밀번호 리셋 (R3)

사용자 상세 화면에서 두 가지 중 하나를 고른다.

| 방법 | 동작 |
|---|---|
| 재설정 메일 보내기 | 5-3의 2~4와 같다. 운용자는 링크를 보지 못한다(테스트 모드 제외) |
| 임시 비밀번호 발급 | 서버가 임의 비밀번호(16자)를 만들어 **응답에 한 번만** 보여 준다. 운용자가 사내 메신저 등으로 전달한다. 계정에 `must_change_password`가 켜진다 |

어느 쪽이든 그 사용자의 기존 세션은 즉시 전부 지우고 감사 로그에 남긴다. 임시 비밀번호는 감사 로그와 서버 로그에 남기지 않는다.

### 5-5. 임의 가입과 초대 (R7, A3)

- 운용자가 이메일, 등급(admin만 `user` 외 등급 지정 가능), 그룹을 넣어 계정을 만든다. 상태는 `invited`이고 비밀번호는 없다.
- 만들면서 초대 메일을 보낸다. 메일의 링크(72시간 유효)는 비밀번호 설정 화면(`/reset.html#token=…`, 5-3과 같은 화면)으로 간다. 설정하면 `active`가 된다. 링크를 가진 것이 곧 메일 수신 확인이므로 따로 메일 인증을 하지 않는다.
- 초대 재발송: `invited` 계정의 상세 화면에서 다시 보낸다(이전 링크 무효).
- 도메인 검사는 가입과 같다. 운용자도 사내 도메인 밖의 주소는 만들 수 없다.
- **일괄 초대:** 이메일을 줄바꿈·쉼표로 여러 개 붙여 넣고 그룹을 고른다. 한 번에 200개까지. 응답은 주소마다 결과(`created` · `exists` · `invalid_email` · `email_domain_not_allowed`)를 돌려주고, 일부가 실패해도 나머지는 만든다. 일괄 초대의 등급은 항상 `user`다.

### 5-6. 계정 정지 (A2)

- 운용자가 `active` 계정을 `suspended`로 바꾼다. 세션을 즉시 지운다. 그룹 구성, 접속 기록은 그대로다.
- 해제하면 `active`로 돌아간다. 비밀번호는 그대로다.
- 정지된 계정은 비밀번호 찾기 메일도 받지 않는다(응답은 같은 202).

### 5-7. 사용자 삭제 (R8)

- 계정 행, 그룹 구성원 연결, 세션, 토큰을 지운다. 되돌릴 수 없으므로 화면에서 이메일을 다시 입력받아 확인한다. 되살릴 가능성이 있으면 정지(5-6)를 쓰라고 안내한다.
- 감사 로그에는 삭제한 사람과 대상 이메일이 남는다. 접속 통계의 행은 사용자 연결만 끊고(`user_id = NULL`) 조회 수 집계에는 남긴다.
- 같은 이메일로 다시 가입할 수 있다(새 계정이다).

### 5-8. 내 정보

모든 등급이 쓴다. 이메일, 등급, 가입일, 속한 그룹, 최근 접속일을 보여 주고 비밀번호를 바꾼다(현재 비밀번호 확인 필요). 바꾸면 지금 세션만 남기고 나머지 세션을 지운다.

## 6. 메일과 테스트 모드

메일 발송은 `dashkit/auth/mailer.py`의 인터페이스 하나 뒤에 둔다.

```python
class Mailer(Protocol):
    def send(self, to: str, kind: MailKind, link: str | None) -> None: ...
```

`MailKind`는 `verify_email` · `reset_password` · `invite` · `already_registered` 넷이다. 본문 문구는 서버의 템플릿에 두고, 링크의 주소는 `WEB_PUBLIC_URL`로 만든다.

| `AUTH_MAIL_MODE` | 동작 |
|---|---|
| `test`(기본) | 실제로 보내지 않고 `dashkit_auth.outbox` 표에 쌓는다 |
| `smtp` | 회사 메일 시스템으로 보낸다. **이 설계의 구현 범위 밖**이다. 회사에서 `Mailer` 구현을 추가하고 접속 정보를 환경변수로 넣는다 |

테스트 모드에서 메일을 확인하는 방법:

- **테스트 메일함:** 운용 화면의 한 메뉴. 받는 사람, 종류, 시각, 링크를 최근 순으로 보여 주고 링크를 바로 열 수 있다. 운용자만 본다.
- **CLI:** `make outbox`(= `python -m dashkit auth outbox`)가 최근 메일과 링크를 출력한다. 운용자 자신이 비밀번호를 잊었을 때 쓴다.
- 테스트 모드일 때 로그인·가입·비밀번호 찾기 화면과 운용 화면 위에 "메일 테스트 모드: 메일이 실제로 발송되지 않습니다" 띠를 보인다(`GET /api/auth/config`의 `mail_mode`).
- 비밀번호 찾기를 요청한 화면에 링크를 바로 보여 주는 방식은 쓰지 않는다. 로그인하지 않은 사람이 남의 계정 링크를 얻게 되기 때문이다.
- outbox는 테스트 모드에서만 쓴다. `smtp` 모드에서는 쌓지 않고 테스트 메일함 API는 404다. 행은 7일 지나면 지운다.

## 7. 보안 설계

### 7-1. 비밀번호 (기본 + A7)

- 저장: Argon2id 해시(`argon2-cffi`, 라이브러리 기본 매개변수). 평문과 복호화 가능한 형태는 어디에도 저장하지 않는다. 매개변수가 바뀌면 다음 로그인 성공 때 다시 해시한다.
- 정책: 10자 이상 128자 이하, 이메일의 `@` 앞부분과 같으면 안 됨, 흔한 비밀번호 목록(`dashkit/auth/common_passwords.txt`, 상위 1만 개)에 있으면 안 됨. 문자 종류 조합은 강제하지 않는다. 위반은 422 `weak_password`와 사유 코드.
- 강제 변경: 임시 비밀번호로 리셋된 계정은 `must_change_password`가 켜진다(5-2).
- 로그·감사 로그·오류 응답에 비밀번호, 토큰, 해시를 싣지 않는다.

### 7-2. 세션 (기본 + A8)

- 서버 저장 세션이다. 로그인하면 256비트 임의 값을 만들어 쿠키 `dashkit_session`에 넣고, DB에는 그 SHA-256만 저장한다.
- 쿠키 속성: `HttpOnly`, `SameSite=Lax`, `Path=/`, `AUTH_COOKIE_SECURE=1`이면 `Secure`. HTTPS로 배포할 때는 반드시 1로 둔다.
- 만료: 마지막 사용 후 8시간(`AUTH_SESSION_IDLE_MINUTES`), 로그인 후 최대 7일(`AUTH_SESSION_MAX_DAYS`). `last_seen_at`은 1분에 한 번만 갱신한다.
- 일괄 무효화: 비밀번호 변경·리셋, 정지, 삭제, 등급 변경 때 그 사용자의 세션을 지운다. 운용자는 사용자 상세에서 "모든 기기에서 로그아웃"을 누를 수 있다.

### 7-3. 로그인 실패 잠금 (A6)

- 같은 계정에 연속 5번 실패하면 15분 잠근다(423 `account_locked`). 성공하거나 비밀번호를 재설정하면 횟수를 지운다. 운용자가 상세 화면에서 바로 풀 수 있다.
- IP별 요청 제한: 인증 경로(`/api/auth/*`)는 IP당 1분에 20번(429 `rate_limited`). backend가 프로세스 하나이므로 메모리에 센다. IP는 nginx가 넣는 `X-Real-IP`를 쓴다.
- 없는 계정에 대한 시도도 같은 응답과 같은 지연을 준다.

### 7-4. 요청 보호

Studio의 `guard.py`와 같은 방식을 backend에 넣는다.

- 상태를 바꾸는 요청(POST, PUT, PATCH, DELETE)은 `Content-Type: application/json`만 받고, `Origin`이 있으면 `WEB_ORIGINS` 중 하나여야 하며, `Sec-Fetch-Site`가 `cross-site`면 거절한다(403 `forbidden_origin`). `SameSite=Lax` 쿠키와 함께 CSRF를 막는다.
- 일회용 토큰(메일 인증, 재설정, 초대)은 256비트 임의 값이고 DB에는 SHA-256만 저장한다. 한 번 쓰면 지운다. 같은 사용자·같은 용도로 새로 만들면 이전 것은 무효다.
- 사용자 입력이 SQL에 들어가는 곳은 전부 매개변수 바인딩이다.

### 7-5. DB 계정

`AGENTS.md`의 owner/reader 구분을 지키기 위해 인증 전용 계정을 하나 더 둔다.

| 계정 | 권한 | 쓰는 곳 |
|---|---|---|
| `dash_owner` | raw·dash 스키마 소유 | 로더(tools). **backend에는 여전히 주지 않는다** |
| `dash_reader` | dash 스키마 SELECT | 대시보드 데이터 읽기(`/api/data`, plan). 그대로 |
| `dash_auth`(신규) | `dashkit_auth` 스키마의 표에 대한 SELECT·INSERT·UPDATE·DELETE만 | backend의 인증·권한·운용 API |

- `dash_auth`는 raw·dash를 읽을 수 없고, `dash_reader`는 `dashkit_auth`를 읽을 수 없다. 테스트로 고정한다.
- `dashkit/db.py`에 `auth_conn()`을 더한다. 환경변수 `DATA_DB_AUTH_USER`, `DATA_DB_AUTH_PASSWORD`.
- 스키마와 계정은 `make auth-init`이 만든다(여러 번 실행해도 된다). 역할 생성에는 superuser가 필요하므로 data-db 컨테이너 안에서 `postgres` 계정으로 실행하고, 번호가 붙은 SQL 파일(`dashkit/auth/sql/001_init.sql` …)을 `dashkit_auth.schema_version`에 기록하며 순서대로 적용한다. 새로 만드는 DB는 `data-db/init/`에서도 같은 일을 한다.
- 로더의 재적재(`DROP SCHEMA`)는 raw·dash만 지운다. `dashkit_auth`는 프로젝트를 바꿔도 남는다. 구현할 때 테스트로 확인한다.

## 8. 화면

화면은 기존 웹앱과 같은 방식(바닐라 JS, 빌드 단계 없음, 문구는 `web/strings.js`, 인라인 스크립트 없음)으로 만든다.

### 8-1. 로그인 전 화면

| 파일 | 내용 |
|---|---|
| `web/login.html` | 이메일, 비밀번호, 로그인 버튼. 아래에 "가입하기", "비밀번호를 잊으셨나요?" 링크. 실패·잠금·미인증 안내 |
| `web/signup.html` | 이메일(사내 이메일 안내 문구), 비밀번호, 비밀번호 확인, 정책 안내. 제출 후 "메일을 확인하세요" |
| `web/forgot.html` | 이메일 입력. 제출 후 항상 "가입된 주소라면 메일을 보냈습니다" |
| `web/reset.html` | 새 비밀번호, 확인. 재설정과 초대 수락이 같이 쓴다. 토큰이 만료·사용됐으면 다시 요청하라는 안내 |
| `web/verify.html` | 메일 인증 링크의 도착 화면. 성공·만료 안내와 로그인 링크 |

### 8-2. 웹앱 셸과 운용 화면

- 웹앱 셸(`web/index.html`): 왼쪽 레일 아래에 사용자 메뉴(이메일, 내 정보, 로그아웃, 운용자에게만 "운용")를 더한다. `/api/app`이 401이면 로그인 화면으로 보낸다.
- 운용 화면(`web/admin/`)은 메뉴 다섯 개다. `user` 등급이 주소를 직접 열면 API가 403이고 화면은 웹앱으로 돌려보낸다.

| 메뉴 | 내용 |
|---|---|
| 사용자 | 표: 이메일, 등급, 상태(잠금 표시 포함), 그룹, 가입일, 접속 횟수, 최근 접속일, 가입 경로(직접·초대). 열 제목으로 정렬, 50명씩 페이지. 체크박스로 여러 명을 골라 그룹에 추가·제거(A3). 위쪽에 "사용자 추가"·"일괄 초대" 버튼 |
| 사용자 상세 | 위 항목 + 초대한 사람, 마지막 비밀번호 변경일, 실패 횟수. 동작: 그룹 편집, 등급 변경(admin만), 재설정 메일, 임시 비밀번호, 초대 재발송, 잠금 해제, 모든 기기 로그아웃, 정지·해제, 삭제 |
| 그룹 | 그룹 목록(이름, 구성원 수, 탭 수, 기본 그룹 여부). 편집 화면은 왼쪽에 현재 프로젝트의 탭 체크 목록(다른 프로젝트·없는 탭은 따로 묶어 표시), 오른쪽에 구성원 목록과 이메일로 추가 |
| 통계 | 9-1 |
| 감사 로그 / 테스트 메일함 | 9-2, 6절. 테스트 메일함은 테스트 모드에서만 보인다 |

## 9. 통계와 기록

### 9-1. 접속 통계 (R6, A4)

- 사용자별: `login_count`, `last_login_at`(로그인 성공 때 갱신). 사용자 표에 나온다.
- 대시보드별: 웹앱이 탭을 열 때 `POST /api/views {"tab": "<키>"}`를 보낸다. 서버는 허용된 탭인지 확인하고 `view_events`에 한 줄 남긴다. 같은 사용자·같은 탭은 30분에 한 번만 센다.
- 통계 화면: 기간(7일·30일·90일)을 골라 탭별 조회 수·본 사람 수·마지막 조회일, 사용자별 조회 수를 본다. 한 번도 열리지 않은 탭과 30일 넘게 접속이 없는 사용자를 따로 표시한다.
- `view_events`는 180일 지나면 지운다.

### 9-2. 감사 로그 (A9)

남기는 일: 로그인 성공·실패·잠금, 로그아웃, 가입, 메일 인증, 비밀번호 변경·재설정 요청·재설정, 운용자의 계정 생성·초대·리셋·정지·해제·삭제·등급 변경·잠금 해제, 그룹 생성·수정·삭제, 구성원·허용 탭 변경.

- 항목: 시각, 한 사람(id와 이메일 사본), 동작 코드, 대상(종류, id, 이름 사본), 변경 내용(JSON), IP.
- 이메일·이름을 사본으로 남겨 계정이나 그룹이 지워진 뒤에도 읽을 수 있게 한다.
- 화면은 최근 순 목록에 동작·기간 선택만 둔다. 수정·삭제 API는 없다. 1년 지나면 지운다.

## 10. 데이터 모델

스키마 `dashkit_auth`. 시각은 전부 `timestamptz`.

| 표 | 열 |
|---|---|
| `users` | `id` uuid PK, `email` text UNIQUE(소문자), `password_hash` text NULL, `role` text(`admin`·`operator`·`user`), `status` text(`pending`·`invited`·`active`·`suspended`), `must_change_password` bool, `source` text(`signup`·`invite`), `created_at`, `created_by` uuid NULL, `email_verified_at`, `password_changed_at`, `last_login_at`, `login_count` int, `failed_count` int, `locked_until` |
| `sessions` | `id` uuid PK, `user_id` FK(삭제 시 함께 삭제), `token_hash` bytea UNIQUE, `created_at`, `last_seen_at`, `expires_at`, `ip` inet, `user_agent` text |
| `tokens` | `id` uuid PK, `user_id` FK(함께 삭제), `purpose` text(`verify_email`·`reset_password`·`invite`), `token_hash` bytea UNIQUE, `created_at`, `expires_at` |
| `groups` | `id` uuid PK, `name` text UNIQUE, `description` text, `is_default` bool, `created_at`, `updated_at` |
| `group_members` | `group_id` FK, `user_id` FK(둘 다 함께 삭제), `added_at`, `added_by`. PK(`group_id`, `user_id`) |
| `group_tabs` | `group_id` FK(함께 삭제), `project` text, `tab_key` text. PK(`group_id`, `project`, `tab_key`) |
| `view_events` | `id` bigserial PK, `user_id` FK NULL(삭제 시 NULL), `project`, `tab_key`, `viewed_at`. 색인(`project`, `tab_key`, `viewed_at`) |
| `audit_log` | `id` bigserial PK, `at`, `actor_id` uuid NULL, `actor_email` text, `action` text, `target_type` text, `target_id` text, `target_label` text, `detail` jsonb, `ip` inet |
| `outbox` | `id` bigserial PK, `to_email`, `kind`, `link` text NULL, `created_at`. 테스트 모드 전용 |
| `schema_version` | `version` int PK, `applied_at` |

## 11. API

오류 형식과 공통 규칙은 `contracts/api.md`와 같다(`{"detail", "message"}`, `no-store`). 운용 API의 목록 응답은 `{"items": [...], "total": n}`이고 `?page=&size=&sort=`를 받는다.

### 11-1. 인증 (`/api/auth`, 로그인 불필요)

| 메서드·경로 | 본문 → 결과 |
|---|---|
| `GET /api/auth/config` | → `{"email_domains": [...], "mail_mode": "test", "password_min": 10}` |
| `POST /api/auth/signup` | `{email, password}` → 202. 422 `email_domain_not_allowed`·`weak_password` |
| `POST /api/auth/verify` | `{token}` → 204. 410 `token_expired` |
| `POST /api/auth/verify/resend` | `{email}` → 202(항상) |
| `POST /api/auth/login` | `{email, password}` → 200 `{email, role, must_change_password}` + 쿠키. 401 `invalid_credentials`, 403 `email_not_verified`·`account_suspended`, 423 `account_locked` |
| `POST /api/auth/logout` | → 204 |
| `POST /api/auth/password/forgot` | `{email}` → 202(항상) |
| `POST /api/auth/password/reset` | `{token, password}` → 204. 재설정과 초대 수락 공용. 410 `token_expired`, 422 `weak_password` |

### 11-2. 내 정보 (로그인 필요)

| 메서드·경로 | 본문 → 결과 |
|---|---|
| `GET /api/me` | → `{email, role, created_at, last_login_at, groups: [{id, name}], must_change_password}` |
| `POST /api/me/password` | `{current, new}` → 204. 401 `invalid_credentials`, 422 `weak_password` |
| `POST /api/views` | `{tab}` → 204. 허용되지 않은 탭은 403 `forbidden` |

### 11-3. 운용 (`/api/admin`, operator 이상)

| 메서드·경로 | 내용 |
|---|---|
| `GET /users`, `GET /users/{id}` | 목록, 상세 |
| `POST /users` | `{email, role, group_ids}` → 201. 초대 메일 발송. 409 `user_exists` |
| `POST /users/bulk-invite` | `{emails: [...], group_ids}` → 200 `{results: [{email, result}]}` |
| `PATCH /users/{id}` | `{role?, status?, group_ids?}`. `role`은 admin만. `status`는 `active`↔`suspended`만 |
| `DELETE /users/{id}` | → 204 |
| `POST /users/{id}/reset-password` | `{method: "mail" \| "temporary"}` → `temporary`면 200 `{password}`(한 번만), `mail`이면 204 |
| `POST /users/{id}/resend-invite` · `/unlock` · `/logout-all` | → 204 |
| `POST /users/bulk-groups` | `{user_ids, add: [group_id], remove: [group_id]}` → 204 |
| `GET /groups`, `POST /groups`, `GET·PATCH·DELETE /groups/{id}` | 그룹 CRUD. `PATCH`는 `{name?, description?, is_default?}` |
| `PUT /groups/{id}/tabs` | `{project, tab_keys: [...]}`. 그 프로젝트의 허용 탭을 통째로 바꾼다. 설정에 없는 키는 422 `unknown_tab` |
| `PUT /groups/{id}/members` | `{add: [email \| id], remove: [id]}` → 200 `{not_found: [email]}` |
| `GET /tabs` | 현재 프로젝트의 탭 목록(`key`, `label`)과 그룹이 참조하는 다른 프로젝트·없는 탭 |
| `GET /stats/tabs?days=`, `GET /stats/users?days=` | 9-1 |
| `GET /audit?action=&from=&to=` | 9-2 |
| `GET /outbox` | 테스트 모드 전용. 그 밖에는 404 |

공통 오류: 401 `unauthenticated`, 403 `forbidden`·`password_change_required`, 404 `unknown_user`·`unknown_group`, 409 `last_admin`·`self_target`·`group_exists`, 429 `rate_limited`.

## 12. 구성 변경

- 코드: `dashkit/auth/`(`store.py` DB, `passwords.py`, `sessions.py`, `tokens.py`, `mailer.py`, `access.py` 탭 판정, `routes.py`, `admin_routes.py`, `guard.py`, `sql/`), `dashkit/db.py`의 `auth_conn()`, `dashkit/__main__.py`의 `auth` 하위 명령(`init`, `create-admin`, `outbox`).
- 의존성: `argon2-cffi` 하나를 `dashkit/requirements.txt`에 고정 버전으로 더한다.
- 환경변수(`.env.example`, compose의 backend):

  | 이름 | 기본 | 뜻 |
  |---|---|---|
  | `DATA_DB_AUTH_USER` | `dash_auth` | 인증 DB 계정 |
  | `DATA_DB_AUTH_PASSWORD` | `__GENERATE__` | 그 비밀번호 |
  | `AUTH_EMAIL_DOMAINS` | (없음, 필수) | 가입을 허용하는 메일 도메인. 없으면 backend가 시작하지 않는다 |
  | `AUTH_MAIL_MODE` | `test` | `test` · `smtp` |
  | `WEB_PUBLIC_URL` | `http://localhost:8080` | 메일 링크의 주소 |
  | `AUTH_COOKIE_SECURE` | `0` | HTTPS 배포에서는 `1` |
  | `AUTH_SESSION_IDLE_MINUTES` / `AUTH_SESSION_MAX_DAYS` | `480` / `7` | 세션 만료 |

- nginx: `/api/` 프록시는 그대로다. `X-Real-IP`를 이미 넘기고 있다. `/admin/`은 정적 파일로 나간다(권한은 API가 지킨다).
- 계약 문서: `contracts/auth.md`를 새로 쓰고(이 설계서의 3·4·5·7·10·11절이 원본), `contracts/api.md`의 네 경로에 4-2의 변경을 반영하고, `contracts/README.md` 색인에 더한다. 구현보다 계약을 먼저 고친다.
- 사내망에 열 때: 지금 compose는 포트를 `127.0.0.1`에만 연다. 사내에 공개하려면 HTTPS를 끝내는 프록시를 앞에 두고 `WEB_ORIGINS`, `WEB_PUBLIC_URL`, `SUPERSET_PUBLIC_URL`, `AUTH_COOKIE_SECURE=1`을 맞춘다. 이 작업은 배포 환경이 정해진 뒤에 한다.

## 13. 단계와 완료 조건

| 단계 | 내용 | 완료 조건 |
|---|---|---|
| 1. 기반 | `contracts/auth.md`, `dashkit_auth` 스키마와 `dash_auth` 계정, `make auth-init`, `make admin`, 로그인·로그아웃·세션, 요청 보호, 로그인 화면, 웹앱 셸의 401 처리 | admin으로 로그인해 기존 웹앱을 그대로 본다. 로그인 없이 `/api/app`·`/api/data`·`/api/guest-token`이 401. DB 계정 권한 경계 테스트 통과 |
| 2. 접근 제어 | 그룹 표, 탭 판정(`access.py`), 세 경로의 필터, 그룹 API와 편집 화면 | user 계정이 허용된 탭만 보고, 다른 탭의 guest token·데이터 요청이 403·404 |
| 3. 가입과 메일 | 가입, 메일 인증, 비밀번호 찾기·재설정, 테스트 모드와 테스트 메일함, 기본 그룹, 잠금, 비밀번호 정책 | 테스트 메일함의 링크만으로 가입 → 인증 → 로그인 → 찾기 → 재설정을 끝까지 한다 |
| 4. 운용 화면 | 사용자 목록·상세, 임의 가입·초대·일괄 초대, 리셋 두 방식, 정지, 삭제, 일괄 그룹 지정, 내 정보 | 3절의 권한 표가 테스트로 고정된다(operator가 admin을 건드리지 못함, 마지막 admin 보호) |
| 5. 통계와 기록 | 접속 통계, 감사 로그, 보존 기간 정리 | 통계 화면에 탭별·사용자별 수치가 나오고 9-2의 모든 동작이 감사 로그에 남는다 |

테스트는 단계마다 같이 넣는다(`tests/test_auth_*.py`). 반드시 포함할 것: 가입·찾기 응답으로 계정 존재 여부를 구분할 수 없음, 토큰 재사용·만료 거절, 비밀번호 변경 후 다른 세션 무효, 권한 없는 탭의 guest token 거절, `dash_reader`가 `dashkit_auth`를 읽지 못함.

## 14. 회사에서 정해야 할 것

| # | 항목 | 필요한 시점 |
|---|---|---|
| 1 | 가입을 허용할 사내 메일 도메인(`AUTH_EMAIL_DOMAINS`) | 단계 3 전 |
| 2 | 사내 메일 발송 방식(SMTP 릴레이 주소, 인증, 발신 주소)과 `Mailer` 구현 담당 | 사내 공개 전. 그때까지 테스트 모드 |
| 3 | 사내 공개 주소와 HTTPS 인증서 | 사내 공개 전 |
| 4 | 보존 기간(감사 로그 1년, 접속 기록 180일)이 사내 규정에 맞는지 | 단계 5 전 |
