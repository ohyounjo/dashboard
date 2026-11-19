# 완료 보고

- 작성일: 2026-11-19
- 확인 환경: WSL2 Ubuntu 24.04, Docker Engine 29.10.0, Docker Compose v5.8.0
- 실행: `make env && make up && make apply` 후 http://localhost:8080 에서 devtracker 대시보드가 동작합니다.
- 최종 테스트 로그: `.build/make-test.log`(커밋 대상 아님)

## 체크리스트

### [x] 모든 기능을 구현했고 테스트를 통과했는가? 테스트 코드가 남아 있는가?

`make test` 최종 결과(종료 코드 0):

```
== 1단계: PROJECT=devtracker 전체 테스트
43 passed, 5 skipped in 15.72s
== 2단계: PROJECT=sales-sample 로 전환해 범용성 검사
5 passed in 2.45s
== 복원: PROJECT=devtracker 로 다시 apply
✔ make test 통과
```

- 1단계에서 건너뛴 5건은 `tests/test_switch.py`입니다. 이 파일은 2단계에서 따로 실행되며 거기서 5건 모두 통과했습니다.
- 그 밖에 건너뛰거나 숨긴 실패는 없습니다.

| 요구 테스트 | 파일(테스트 수) | 확인 내용 |
|---|---|---|
| 설정 검사 | `tests/test_config.py` (20) | 두 예시가 check를 통과한다. 없는 데이터셋 참조, 배치에 없는 차트 키, 뷰에 없는 필터 열을 각각 잡는다. 예약어, ref 순환, 지표 이름, 행 너비, 오류를 모두 모아 보고하는지도 확인한다 |
| 로더 | `tests/test_loader.py` (4) | 임시 DB 사용. 행 수 = CSV 행 수. 깨진 CSV 5종(없는 참조, 잘못된 enum, 범위 밖 숫자, 패턴 불일치, 중복 키)의 오류 5건을 `파일:행:열`로 모두 보고하고 DB는 이전 상태. checks 위반(created > updated) 롤백. dash_reader는 raw 조회와 쓰기·생성이 모두 `InsufficientPrivilege` |
| sync | `tests/test_sync.py` (3) | 두 번 실행해도 Superset 객체 수·UUID·임베드 UUID가 같다. 차트 키가 모두 생성된다. 설정에서 지운 차트는 Superset에서도 사라진다(임시 프로젝트 이름과 임시 STATE_DIR을 쓰고, 끝나면 정리) |
| 지표 정합성 | `tests/test_metrics.py` (6) | `POST /api/v1/chart/data`로 조회: 가중 진척률 55, 다음 마일스톤까지 29, 열린 이슈 16, Critical 미해결 3, 스냅샷 마지막 주 (16, 3, 55), 🆕 문서 D2·D3·D5·D6·D7 |
| 백엔드 | `tests/test_api.py` (8) | `/api/app`·`/api/health` 계약. guest token으로 임베드 페이지·대시보드·차트 데이터 조회 성공. 같은 토큰으로 다른 대시보드 404, 다른 대시보드의 차트 데이터 403, 쓰기 5종 거부 후 객체 변경 없음, CSRF 토큰 발급 거부. 허용 출처가 아닌 Referer는 403. `/api/data`는 위젯이 참조하지 않은 데이터셋에 404. 토큰 claim(type, rls_rules, 리소스 1개, 수명 300초) |
| 범용성(전환) | `tests/test_switch.py` (5) | `PROJECT=sales-sample`로 apply하면 코드 변경 없이 탭·제목·위젯 데이터가 바뀐다. devtracker의 Superset 객체(데이터셋·차트·대시보드·DB)는 0개, Superset 차트 수 = sales-sample 차트 수 |
| 범용성(grep) | `tests/test_domain_words.py` (2) | 아래 "도메인 하드코딩" 참고 |

UI 화면은 요청대로 자동 테스트에서 뺐습니다. 대신 헤드리스 Chromium으로 다음 화면을 직접 확인했습니다.
- devtracker 탭 4개를 1280px 라이트·다크, 390px에서 확인했습니다.
- sales-sample 탭 4개를 1280px에서 확인했습니다.

### [x] plan에 없는 의존성을 추가하지 않았는가? 모든 버전을 고정했는가?

- **이미지:**
  - `apache/superset:6.1.0`(+ `psycopg2-binary==2.9.9`)
  - `postgres:16.10`
  - `nginx:1.29.3-alpine`
  - `python:3.12.16-slim-bookworm`
- **파이썬:** `dashkit/requirements.txt`에 8개를 모두 `==`로 고정했습니다.
  - PyYAML 6.0.3, jsonschema 4.26.0, psycopg[binary] 3.3.9, fastapi 0.165.0, uvicorn 0.54.0, requests 2.34.2, PyJWT 2.15.1, pytest 9.2.0
- **웹:** `@superset-ui/embedded-sdk@0.4.0` 번들을 `web/vendor/`에 넣었습니다. 출처와 sha256은 `web/vendor/README.md`에 있습니다.
- **floating 태그:** `latest`나 태그 없는 이미지는 0건입니다(`docker-compose.yml`, `*/Dockerfile` grep).
- **plan에 이름이 없던 패키지:** FastAPI를 뺀 7개입니다(설정 읽기·검증, DB 드라이버, ASGI 서버, Superset REST 호출, JWT 서명, 테스트). 모두 plan의 기능을 구현하는 데 필요한 최소 구성이고, Plan 단계 보고에서 미리 알렸습니다. Redis와 UI 프레임워크, 빌드 도구는 없습니다.
- **테스트 화면 확인용 도구:** Playwright 이미지는 저장소와 `make test`에 포함하지 않았습니다. 확인할 때만 일회성으로 썼습니다.

### [x] 템플릿 코드에 도메인 하드코딩이 0건인가(grep 테스트 결과)?

- `tests/test_domain_words.py::test_no_domain_words_in_template_code`가 통과했습니다(0건).
- **검사 대상:** `dashkit/`, `web/`(`web/vendor/` 제외), `superset/`의 모든 파일
- **금지어:** 두 예시의 테이블명, 열 이름, enum 값, 데이터셋, 대시보드, 탭 키와 라벨을 설정에서 자동으로 모은 것에 고정 단어를 더했습니다.
  - 고정 단어: milestone, issue, severity, 마일스톤, 이슈, 심각도, task, kpi, customer, deal, campaign 등
  - 고정 단어 목록 파일: `tests/domain_words.txt`
- **제외(일반 단어):** id, title, name, type, status, email 등은 어느 도메인에나 쓰이는 단어라 제외했습니다. 아이콘 이름 `doc`(contracts/icons.md)도 제외했습니다. 목록은 `tests/domain_words.txt`의 `[allow]`에 있습니다.
- **처음 실행 때 걸린 것:** 주석 2곳의 '문서'(devtracker 탭 라벨과 같은 단어)가 걸려서 표현을 바꿨습니다.

### [x] 두 예시 프로젝트가 PROJECT 전환만으로 동작하는가?

- `make test` 2단계가 `PROJECT=sales-sample make apply`로 전환한 뒤 검사 5건을 통과했고, `PROJECT=devtracker make apply`로 복원했습니다.
- 전환할 때 sync가 이전 프로젝트 객체를 지웠습니다(로그 `· prune: 19개 삭제`).
- **devtracker:** 탭 4개(개요, 일정, 이슈, 문서), 차트 10개, 대시보드 3개, 네이티브 필터 4개, 간트와 칸반
- **sales-sample:** 탭 4개(요약, 파이프라인, 캠페인, 고객사), 차트 9개, 대시보드 2개, 칸반과 간트, 청록 테마
- 두 예시의 테이블명과 열 이름 겹침은 0건입니다(B 담당 검사).

### [x] 비밀값이 커밋 대상에 없는가? backend에 Superset 계정이 없는가?

- **커밋 대상:** `.env.example`만 커밋 대상이고, 비밀값 자리는 `__GENERATE__`입니다. `.gitignore`가 `.env`, `.state/`, `.build/`를 뺍니다.
- **빌드 컨텍스트:** `.dockerignore`가 `.env`를 뺍니다.
- **48자 hex 검색:** `.env` 밖에서는 이미지 digest(superset-notes 1절)만 걸립니다.
- **backend 컨테이너 환경변수:**
  - 있음: `DATA_DB_READER_*`, `GUEST_TOKEN_JWT_SECRET`/`AUDIENCE`, `PROJECT`, `STATE_DIR`, `SUPERSET_PUBLIC_URL`, `DATA_MAX_ROWS`, `TZ`
  - 없음: `SUPERSET_ADMIN_*`, `DATA_DB_OWNER_*`, `DATA_DB_ADMIN_*`
- **guest token:** backend가 직접 서명합니다(`dashkit/api/app.py`). Superset에는 요청하지 않습니다.
- **DB 계정:**
  - `dash_owner`: superuser가 아니고 DB 소유자이며 CREATEDB 권한만 있습니다. 로더만 씁니다.
  - `dash_reader`: dash 스키마 SELECT만 있습니다. Superset과 backend가 씁니다.
  - superuser `postgres`: 어떤 서비스에도 주지 않았습니다.

### [x] Explore에서 확인한 내용과 다르게 구현한 부분, 남은 한계, 모호한 부분을 알렸는가?

아래에 모았습니다. Explore 기록에서 틀린 두 곳은 `contracts/superset-notes.md`에 "정정"으로 표시했습니다.

## Explore와 달라진 점

| 항목 | Explore 기록 | 실제 | 조치 |
|---|---|---|---|
| guest 요청 변조 | 지표를 바꾸면 403 | **200**. Explore의 403은 다른 데이터셋 id를 함께 보내서 생긴 거부였다. guest는 차트 목록 권한이 없어 Superset의 payload 비교 검사가 건너뛰어진다 | notes 4절 정정, 테스트는 실제 경계로 변경, 아래 "남은 한계" 1번 |
| CORS | 켜지 않았다 | 6.1.0 기본값이 `ENABLE_CORS = True`(지도 타일 출처) | `superset_config.py`에 `ENABLE_CORS = False` 명시, notes 6절 정정 |
| Big Number 부제 키 | 대응표에 `subheader` | 6.1.0 controlPanel 이름은 `subtitle` | 대응표와 sync 수정 |
| PostgreSQL 공식 이미지 | — | `POSTGRES_USER`는 항상 superuser라서 로더 계정이 superuser가 됨 | superuser를 `postgres`로 분리하고 `dash_owner`는 일반 역할로 |

## 계약에서 바뀐 점(진행 중 승인 또는 보고)

- **데이터셋 `labels`(승인받음):** 열·지표 이름을 표시 이름으로 바꿉니다. Superset `verbose_name`이 되어 표 머리글, 필터·범례 라벨에 쓰입니다.
- **표 날짜 형식(승인받음):** 표 차트 기본값에 `table_timestamp_format: "%Y-%m-%d"`를 넣었습니다.
- **sync 보강:**
  - import 전에 `managed.json`을 (이전 ∪ 이번)으로 한 번 먼저 씁니다.
  - prune에 실패한 UUID는 목록에 남겨 다음 실행에서 다시 지웁니다.
- **백엔드:**
  - 공통 오류 코드를 추가했습니다: `not_found`, `method_not_allowed`, `internal_error`, `token_unavailable`
  - DB 쿼리 시간 제한 `DATA_STATEMENT_TIMEOUT_MS`를 두었습니다.
  - OpenAPI 문서 경로는 만들지 않습니다.
- **`make apply`:** backend 이미지를 다시 빌드한 뒤 재시작합니다. 템플릿 코드가 바뀌어도 apply 하나로 반영되고, 바뀐 게 없으면 캐시로 즉시 끝납니다.
- **nginx 설정:** `web/nginx.conf.template`(envsubst)이고, 웹앱용 CSP를 둡니다(`frame-src`는 Superset 주소만).
- **devtracker 검증 규칙:** "프로젝트 기간 밖" 규칙을 checks 대신 열 규칙(min, max)으로 옮겼습니다. 같은 규칙이 CSV 단계에서 줄 번호와 함께 잡힙니다.

## 남은 한계

1. **guest token의 조회 범위:** 토큰을 가진 사람은 **임베드한 대시보드가 쓰는 데이터셋 안에서** 차트에 없는 열과 집계도 조회할 수 있습니다(Superset 6.1.0 동작).
   - 막히는 것: 다른 데이터셋, 대시보드에 없는 차트, SQL 서브쿼리, 모든 쓰기(테스트로 확인).
   - guest 역할에 데이터셋 접근 권한을 주면 이 검사가 켜지지만, 대신 다른 대시보드의 차트 데이터가 열려서 택하지 않았습니다.
   - 대응: `dash` 뷰에는 보여도 되는 열만 두고, 외부 공개 시 인증 프록시를 둡니다(README).
2. **iframe 첫 렌더:** SDK 0.4.0에는 임베드할 때 테마를 정하는 인자가 없습니다. 임베드가 끝난 뒤 `setThemeMode`를 보내기 때문에 다크모드에서 iframe이 처음 잠깐 라이트로 보일 수 있습니다. 웹앱 자체도 저장된 테마가 시스템 설정과 다르면 첫 화면이 잠깐 깜빡일 수 있습니다(CSP 때문에 인라인 스크립트를 쓸 수 없음).
3. **Big Number 증감 표시:** 직전 주 대비를 퍼센트(예: +14.3%)로 보여 줍니다. reference의 절대값(▲ +2) 표시와 다릅니다. iframe 안이라 카운트업 같은 웹앱 모션도 적용되지 않습니다.
4. **Superset 연결 실패 판정:** 웹앱 CSP가 `connect-src 'self'`라서 미리 접속을 시험할 수 없습니다. 30초 시간 초과로만 판정하므로 그동안 스켈레톤이 보입니다.
5. **콘솔 메시지:** Superset iframe 안에서 Service Worker 등록 실패(404) 오류가 콘솔에 남습니다(Superset 이미지 동작, 화면 영향 없음). 의도된 4xx 응답에도 브라우저가 "Failed to load resource"를 남깁니다.
6. **import 원자성:** Superset의 번들 import는 검증 오류를 500 HTML로만 돌려줍니다. 중간에 실패하면 DB 객체만 남는 식의 부분 적용이 생길 수 있습니다. 같은 UUID로 덮어쓰므로 원인을 고치고 `make apply`를 다시 실행하면 수렴합니다.
7. **한 번에 한 프로젝트:** 데이터 DB의 `raw`·`dash`를 매번 다시 만들고, Superset도 한 프로젝트 객체만 남깁니다(설계대로).
8. **DB 비밀번호 변경:** `.env`의 DB 비밀번호를 바꾸려면 `make reset CONFIRM=1`이 필요합니다(계정은 initdb 때만 만들어짐).
9. **prune의 기준:** `.state/managed.json`을 지우면 이전 객체를 알 수 없어 prune되지 않습니다. 이때는 `make reset CONFIRM=1`로 처음부터 다시 만듭니다.

## 모호했던 부분과 정한 방향

- **"뷰에 없는 필터 열"을 잡는 위치:** `sql:` 데이터셋의 열은 DB가 있어야 알 수 있습니다. 그래서 `make check`는 정적으로 검사할 수 있는 것만 하고(`table:` 데이터셋은 schema로 즉시 확인), load가 뷰를 만든 직후 같은 트랜잭션에서 확인해 없으면 롤백합니다.
- **지표 정합성 조회:** `query_context`를 만들지 않기로 해서 `GET /api/v1/chart/<id>/data/`는 쓸 수 없습니다. 차트의 데이터셋과 지표로 `POST /api/v1/chart/data`를 부릅니다(Plan 결정).
- **허용 출처:** `localhost:8080`과 `127.0.0.1:8080`을 둘 다 기본값에 넣었습니다(`WEB_ORIGINS`).

## 이 컴퓨터에서 바꾼 것(저장소 밖)

- **Docker 설정 파일:** `~/.docker/config.json`에 Docker Desktop용 `credsStore: desktop.exe`가 남아 있어 이미지를 받을 수 없었습니다. `{}`로 바꿨고 원본은 `~/.docker/config.json.bak-desktop`에 있습니다.
- **Explore 단계 이미지:** `ssexplore-superset:6.1.0`(1.28GB)은 요청에 따라 지웠습니다.

- **저장소의 `AGENTS.md`:** 이 작업에서 만든 파일이 아니라서 건드리지 않았습니다.
