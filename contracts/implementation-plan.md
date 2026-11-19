# 구현 계획 (Plan 단계 결과)

## 확정한 결정

| 항목 | 결정 | 근거 |
|---|---|---|
| Superset | `apache/superset:6.1.0` + `psycopg2-binary==2.9.9` | superset-notes 1절 |
| SDK | `@superset-ui/embedded-sdk@0.4.0` `bundle/index.js` → `web/vendor/superset-embedded-sdk-0.4.0.js` | superset-notes 1, 8절 |
| `query_context` | 만들지 않는다. 지표 정합성 테스트는 `POST /api/v1/chart/data` | 사용자 결정(권장안) |
| 허용 출처 | `.env`의 `WEB_ORIGINS`(기본 `http://localhost:8080,http://127.0.0.1:8080`)를 임베드 `allowed_domains`와 `frame-ancestors`에 같이 쓴다 | 사용자 결정(권장안) |
| guest 역할 | `EmbedGuest` = `can_read` on `CurrentUserRestApi`, `Dashboard`, `Chart` | superset-notes 5절 |
| 프로젝트 전환 | `.env`의 `PROJECT`를 바꾸고 `make apply` → backend 재시작, 이전 프로젝트 객체 prune | plan 6-1 |

## 컨테이너와 이미지

| 서비스 | 이미지 | 포트 | 비고 |
|---|---|---|---|
| `data-db` | `postgres:16.10` | 없음(내부) | `initdb`에서 `dash_reader` 로그인 역할을 만든다(비밀번호는 env). 소유자 계정은 `POSTGRES_USER` |
| `superset-db` | `postgres:16.10` | 없음 | |
| `superset-init` | `superset/Dockerfile` | — | `db upgrade` → `fab create-admin`(이미 있으면 통과) → `superset init` → `superset/init_guest_role.py`. 끝나면 종료 |
| `superset` | `superset/Dockerfile` | `127.0.0.1:8088` | healthcheck `/health` |
| `backend` | `dashkit/Dockerfile` | 없음 | `uvicorn dashkit.api.app:app`. `projects/` ro, `.state/` ro |
| `web` | `nginx:1.29.3-alpine` | `127.0.0.1:8080` | `web/` ro, `web/nginx.conf`. `/api/` → `backend:8000` |
| `tools` | `dashkit/Dockerfile` | — | profile `tools`. `projects/` ro, `.state/` rw, `.build/` rw, `tests/` ro |

Python 베이스는 `python:3.12.16-slim-bookworm`이다(Docker Hub, 2026-11-05).

## 파이썬 의존성 (`dashkit/requirements.txt`, 모두 `==` 고정)

```
PyYAML==6.0.3
jsonschema==4.26.0
psycopg[binary]==3.3.9
fastapi==0.165.0
uvicorn==0.54.0
requests==2.34.2
PyJWT==2.15.1
pytest==9.2.0
```

이 밖의 패키지는 추가하지 않는다(전이 의존성은 pip가 해결). 버전은 2026-11-19 PyPI 최신이다.

## 모듈 사이 함수 계약

| 모듈 | 공개 함수 | 실패 |
|---|---|---|
| `dashkit.loader` | `load(project: Project, *, dbname: str \| None = None) -> dict[str, int]` (테이블 → 적재 행 수) | `LoadError(errors: list[str])` |
| `dashkit.sync` | `sync(project: Project, *, state_dir: Path \| None = None, build_dir: Path \| None = None) -> dict` (종류 → UUID 목록) | `SyncError(message)` |
| `dashkit.api.app` | 모듈 전역 `app: FastAPI` | — |

`dashkit/__main__.py`가 `load_project`로 설정을 읽어 넘기고, 예외의 오류 목록을 한 줄씩 출력한 뒤 종료 코드 1로 끝낸다.

`dash.as_of()`: 고정 날짜면 `SELECT DATE 'YYYY-MM-DD'`, `today`면 `SELECT (now() AT TIME ZONE '<적재 시 TZ 값>')::date`. DB 세션 시간대와 무관하게 `TZ` 기준 날짜가 된다.

## 환경변수 (`.env.example`)

| 변수 | 기본/생성 | 쓰는 곳 |
|---|---|---|
| `PROJECT` | `devtracker` | backend, tools |
| `WEB_PORT` / `SUPERSET_PORT` | `8080` / `8088` | compose |
| `WEB_ORIGINS` | `http://localhost:8080,http://127.0.0.1:8080` | superset_config(frame-ancestors), sync(allowed_domains) |
| `SUPERSET_PUBLIC_URL` | `http://localhost:8088` | backend(`/api/app`) |
| `SUPERSET_ADMIN_USER` | `admin` | superset-init, tools(sync, 테스트) |
| `SUPERSET_ADMIN_PASSWORD` | `make env`가 생성 | 위와 같음. backend에는 **주지 않는다** |
| `SUPERSET_SECRET_KEY` | 생성 | superset |
| `GUEST_TOKEN_JWT_SECRET` | 생성 | superset, backend |
| `GUEST_TOKEN_JWT_AUDIENCE` | `dashkit-embed` | superset, backend |
| `SUPERSET_DB_PASSWORD` | 생성 | superset-db, superset |
| `DATA_DB_NAME` | `dash` | data-db, tools, backend, sync |
| `DATA_DB_OWNER_PASSWORD` | 생성 | data-db, tools(loader). backend·superset에는 주지 않는다 |
| `DATA_DB_READER_PASSWORD` | 생성 | data-db(init), backend, tools(sync가 import passwords로 전달) |
| `DATA_DB_OWNER_USER` / `DATA_DB_READER_USER` | `dash_owner` / `dash_reader` | data-db(`POSTGRES_USER` = owner), dashkit/db.py |
| `DATA_DB_HOST` / `DATA_DB_PORT` | `data-db` / `5432` | dashkit/db.py |
| `SUPERSET_URL` | `http://superset:8088` | tools(sync, 테스트). 컨테이너 내부 주소 |
| `STATE_DIR` / `BUILD_DIR` | `/app/.state` / `/app/.build` | sync(rw), backend(STATE_DIR ro) |
| `DATA_MAX_ROWS` | `5000` | backend |
| `TZ` | `Asia/Seoul` | backend, tools(`as_of: today`) |

## Makefile

| 대상 | 동작 |
|---|---|
| `env` | `.env`가 없으면 `.env.example`을 복사하고 `__GENERATE__` 자리를 임의 값으로 채운다(호스트 `od`/`tr` 또는 일회성 컨테이너. 파이썬을 호스트에서 쓰지 않는다). 있으면 건드리지 않는다 |
| `up` | `docker compose up -d --build` 후 superset healthy까지 대기(시간 제한 300초) |
| `check` | `tools: python -m dashkit check` |
| `apply` | `check` → `load` → `sync`. backend는 `projects/` 변경을 스스로 다시 읽는다(재빌드·재시작 없음. 프로젝트를 바꿨을 때만 컨테이너를 다시 만든다) |
| `plan` | `tools: python -m dashkit plan`. 적용 전 차이(데이터, 뷰, Superset). 아무것도 바꾸지 않는다(`contracts/plan.md`) |
| `new NAME= FROM=` | CSV 폴더에서 프로젝트 초안 생성(`contracts/scaffold.md`). 스택 불필요 |
| `migrate` | 설정 파일을 최신 설정 버전으로(`CHECK=1`이면 확인만) |
| `test-unit` | 스택 없이 `pytest -m "not integration" tests/`(tools 이미지를 `docker run`으로 직접 실행) |
| `test` | 별도 compose 프로젝트 `dashkit-test`(웹앱 18080, Superset 18088, 별도 볼륨, 상태 `.test/`)를 띄워 `pytest tests/` → 프로젝트 전환 검사 → 스택 삭제(`KEEP=1`이면 남김). 개발 스택의 `.state/`가 바뀌면 실패 |
| `golden` | 번들 스냅샷 `tests/golden/` 갱신 |
| `reset` | `down -v` 후 `.state/`, `.build/` 삭제(확인 문구 출력) |
| `down`, `logs` | 그대로 |

## 서브에이전트 분담 (Implement)

먼저 내가 `dashkit/__init__.py`, `dashkit/__main__.py`(하위 명령 분기), `dashkit/config.py`, `dashkit/reserved_words.py`를 만들고 단위 테스트(`tests/test_config.py`)를 붙인다. 그다음 아래 5개를 병렬로 맡긴다. **각자 자기 파일만 만든다.** 계약 변경이 필요하면 구현하지 말고 보고한다.

| 담당 | 만드는 파일 | 읽기만 하는 것 |
|---|---|---|
| A 인프라 | `docker-compose.yml`, `.env.example`, `Makefile`, `.gitignore`, `superset/Dockerfile`, `superset/superset_config.py`, `superset/init_guest_role.py`, `superset/init.sh`, `dashkit/Dockerfile`, `dashkit/requirements.txt`, `data-db/init/01-reader.sh`, `web/nginx.conf` | contracts, superset-notes |
| B 로더·예시 데이터 | `dashkit/loader/`, `projects/devtracker/**`, `projects/sales-sample/**` | config.py, reference/index.html, core.test.js |
| C sync | `dashkit/sync/` | config.py, chart-shorthand, state-and-uuid, superset-notes |
| D 백엔드 | `dashkit/api/` | config.py, api.md, state-and-uuid |
| E 웹앱 | `web/index.html`, `web/app.js`, `web/strings.js`, `web/icons.js`, `web/styles.css`, `web/widgets/gantt.js`, `web/widgets/kanban.js`, `web/vendor/` | api.md, widgets.md, icons.md, reference/index.html, artifact-plan 6·7절 |

- B·C·D는 실행 확인을 위해 A의 compose가 필요하다. A가 끝나기 전에는 단위 수준으로 만들고, 통합은 내가 한다.
- C와 D가 쓰는 DB 접속 함수는 각자 만들지 않고 `dashkit/db.py`(내가 config.py와 함께 만든다: `owner_conn()`, `reader_conn()`, 환경변수에서 DSN 생성)를 쓴다.

## 통합 후 Verify 계획 (`tests/`, `make test`)

| 파일 | 내용 | 상태 보존 방법 |
|---|---|---|
| `test_config.py` | 두 예시가 check 통과, 없는 데이터셋·배치에 없는 차트 키·뷰에 없는 필터 열 | DB 불필요(필터 열은 `check_view_columns`에 가짜 뷰 열을 넘긴다) + 로더 단계 확인은 `test_loader.py` |
| `test_loader.py` | 행 수, 깨진 CSV 5종 오류 전체 보고·롤백, checks 롤백, dash_reader 거부 | 임시 DB `dash_test`(테스트가 만들고 지운다) |
| `test_sync.py` | 두 번 실행 동일, 차트 키 전부 생성, 차트 삭제 → prune | 임시 프로젝트 이름 `zz_synctest`와 임시 `STATE_DIR`. 끝에 빈 목록으로 prune해 정리 |
| `test_metrics.py` | 16 / 3 / 55 / 29 / 🆕 D2·D3·D5·D6·D7 | 현재 적용된 devtracker를 읽기만 함 |
| `test_api.py` | guest token으로 대시보드·차트 데이터 조회, 쓰기·다른 대시보드 거부, `/api/data` 404 | 읽기만 함 |
| `test_generic.py` | `PROJECT=sales-sample` apply → 탭·차트·위젯 데이터 변경, devtracker 객체 없음 → 원래 프로젝트로 다시 apply / 템플릿 코드 grep | 마지막에 원래 `PROJECT`로 복원 |

grep 금지어 목록은 `tests/domain_words.txt`에 둔다(두 예시의 테이블명·열 이름·상태값·탭 키와 라벨을 `projects/*/` 설정에서 자동 수집 + 고정 단어 milestone, issue, severity, 마일스톤). 일반 단어(`id`, `title`, `name`, `status`, `type`, `date`, `value` 등)는 제외 목록에 둔다.
