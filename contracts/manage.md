# 관리 서비스 API (`/api/manage`)

설계: `reference/data-design.md` 9~11절. 구현: `dashkit/manage/`(라우팅과 권한만), 로직은 공용 코어(`dashkit/project/`, `dashkit/ingest/`).
이 문서는 경로·권한·요청 보호만 적는다. 데이터 더하기의 형식과 동작은 [ingest.md](ingest.md), 설정 변경 연산과 저장 방식은 [studio.md](studio.md), 추론은 [scaffold.md](scaffold.md)를 그대로 쓴다.

## 서비스

- `python -m dashkit manage`(기본 포트 8100), compose 서비스 `manager`(프로필 `manage`, `make manage`·`make manage-down`). 이미지는 Studio와 같은 것(`dashkit/Dockerfile`의 `studio` 단계)이다.
- 포트를 밖에 열지 않는다. 브라우저는 nginx(web)의 `/api/manage/`로만 닿는다. 관리 서비스가 떠 있지 않으면 nginx가 502를 돌려주고 운용 화면은 데이터 메뉴를 숨긴다.
- 쓰는 계정: `dash_owner`(쓰기 작업에서만 연다), `dash_inspect`(읽기 경로), `dash_auth`(로그인 확인·감사 로그), Superset 관리자(apply의 sync). backend에는 owner·Superset 계정을 주지 않는다.
- 프로세스는 하나만 띄운다(작업 상태와 미리보기한 파일이 메모리에 있다).

## 현재 프로젝트

backend와 같은 규칙이다: `.state/embed.json`의 `project`(마지막으로 sync에 성공한 프로젝트), 쓸 수 없으면 env `PROJECT`. 경로에 프로젝트 이름이 없다.

## 로그인과 권한

로그인 확인은 `dashkit/auth`의 의존성을 그대로 쓴다([auth.md](auth.md) "로그인 확인"). 세션 쿠키는 웹앱과 같은 출처라 그대로 온다. 권한 검사는 서버에서 한다.

| 할 수 있는 일 | admin | operator | user |
|---|---|---|---|
| 테이블·배치·데이터셋·계보 보기, 행 조회 | ✓ | ✓ | |
| 누적 테이블에 데이터 더하기(미리보기, 오류 행 빼고 넣기 포함), 머리글 맞추기(그 적재에만) | ✓ | ✓ | |
| 배치 되돌리기, 방금 추가 취소 | ✓ | 자기가 더한 배치만 | |
| 테이블 추가·삭제, 열 추가·변경(`aliases` 저장 포함), 데이터셋 만들기, 방식 변경, 합치기, 설정 되돌리기 | ✓ | | |

로그인하지 않았으면 401 `unauthenticated`, 등급이 모자라면 403 `forbidden`, 비밀번호를 바꿔야 하는 계정은 403 `password_change_required`다.

## 요청 보호

- `dashkit/auth/guard.py`를 그대로 쓴다: 상태를 바꾸는 요청은 `Origin`이 웹앱 출처여야 하고(`Sec-Fetch-Site: cross-site` 거절), 본문은 `application/json`이다. 업로드 경로(`…/tables/{t}/batches`, `/batches`, `…/tables/infer`)만 `multipart/form-data`도 받는다(그 경로를 더하는 단계에서 이 표에 적는다).
- 응답은 모두 `Cache-Control: no-store`, 오류는 `{"detail", "message"}`(+ `errors` 등)로 [api.md](api.md)와 같다.
- 경로 값(`{table}`·`{dataset}`·`{file}`)은 현재 프로젝트의 설정과 폴더 목록에 대조한 뒤에만 쓴다. 사용자 입력이 들어가는 SQL은 전부 매개변수 바인딩이고 식별자는 목록과 대조한 뒤 `psycopg.sql.Identifier`로만 넣는다.
- 로그·오류 응답·감사 로그에 비밀값과 데이터 값을 싣지 않는다.

## 경로

| 메서드·경로 | 로그인 | 내용 |
|---|---|---|
| `GET /health` | 불필요 | `{"status": "ok"}` |
| `GET /config` | 운용자 | `{"project", "role", "limits"}`. 운용 화면이 데이터 메뉴를 그리는 데 쓴다 |

경로는 `dashkit/manage/routes/`의 모듈 하나씩이다. 경로를 더하는 일은 모듈 하나와 `routes/__init__.py`의 목록 한 줄이다. 설계서 11절의 나머지 경로는 그 단계에서 이 표에 더한다.

## 한도

`GET /config`의 `limits`와 같은 값이다(환경변수, `.env.example`).

| 이름 | 기본 | 뜻 |
|---|---|---|
| `INGEST_INLINE_MAX_ROWS` | `500` | 직접 입력·붙여넣기 한 번의 행 수 |
| `INGEST_MAX_UPLOAD_MB` / `INGEST_MAX_ROWS` | `200` / `2000000` | 파일 하나의 한도 |
| `INGEST_MAX_REQUEST_MB` | `500` | 여러 파일을 한 번에 올릴 때의 합계 |
| `INGEST_COMPACT_HINT` | `200` | 이 수를 넘으면 합치기를 권한다 |
| `MANAGE_STATEMENT_TIMEOUT_MS` | `15000` | 조회 시간 제한 |
| `MANAGE_LOCK_WAIT_MS` | `5000` | 쓰기 잠금을 기다리는 시간. 넘으면 409 `apply_running` |

`limits.page_max`(200)는 한 번에 돌려주는 행의 최대(고정)다.
