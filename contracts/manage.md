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

- `dashkit/auth/guard.py`를 그대로 쓴다: 상태를 바꾸는 요청은 `Origin`이 웹앱 출처여야 하고(`Sec-Fetch-Site: cross-site` 거절), 본문은 `application/json`이다. 업로드 경로(`POST /tables/{t}/batches`, `POST /batches`, `POST /tables/infer`)만 `multipart/form-data`도 받는다(출처 검사는 같다). 업로드 규칙(파일 이름 `<이름>.csv`, 경로 문자 금지)은 [studio.md](studio.md)와 같고, 올린 파일의 이름은 경로로 쓰지 않는다.
- 응답은 모두 `Cache-Control: no-store`, 오류는 `{"detail", "message"}`(+ `errors` 등)로 [api.md](api.md)와 같다.
- 경로 값(`{table}`·`{dataset}`·`{file}`)은 현재 프로젝트의 설정과 폴더 목록에 대조한 뒤에만 쓴다. 사용자 입력이 들어가는 SQL은 전부 매개변수 바인딩이고 식별자는 목록과 대조한 뒤 `psycopg.sql.Identifier`로만 넣는다.
- 로그·오류 응답·감사 로그에 비밀값과 데이터 값을 싣지 않는다.

## 경로

| 메서드·경로 | 로그인 | 내용 |
|---|---|---|
| `GET /health` | 불필요 | `{"status": "ok"}` |
| `GET /config` | 운용자 | `{"project", "role", "limits"}`. 운용 화면이 데이터 메뉴를 그리는 데 쓴다 |
| `GET /tables` | 운용자 | `{"items", "total", "project"}`. 항목 `{name, mode, on_conflict, description, rows, batches, compact_hint, last_batch: {file, added_at, actor_email} \| null}`. `rows`는 배치 기록의 `inserted` 합(적용한 적이 없으면 `null`), `batches`는 배치 폴더의 파일 수, `compact_hint`는 그 수가 `INGEST_COMPACT_HINT`를 넘는지 |
| `GET /tables/{t}` | 운용자 | 입력 폼을 만드는 정의: `{name, mode, on_conflict, description, primary_key, revision, columns: [{name, type, required, primary_key, label, values?, ref?, min?, max?, pattern?, description?, default?, aliases?}], checks: [이름], datasets: [그 테이블을 그대로 보는 데이터셋]}`. `label`은 그 테이블을 보는 `table:` 데이터셋의 `labels` |
| `POST /tables/{t}/batches` | 운용자 | **데이터 더하기**([ingest.md](ingest.md) "더하기"). 아래 "데이터 더하기" |
| `GET /tables/{t}/batches?removed=1` | 운용자 | 배치 이력, 최근 것부터: `{"items", "total"}`. 항목 `{file, removed, first, added_at, recorded, rows, inserted, updated, skipped, unchanged, actor_email, method, source_name, mine}`. 누가·방법·원래 파일 이름은 감사 로그(`data.batch.create`)에서 온다. `mine`은 요청한 사람이 더한 배치인지. `removed=1`이면 `_removed/`의 배치도 싣는다 |
| `GET /tables/{t}/batches/{file}` | 운용자 | 배치 파일 내려받기(정규형 그대로, `.csv.gz`는 `application/gzip`) |
| `DELETE /tables/{t}/batches/{file}` | admin, 또는 그 배치를 더한 operator | 되돌리기. 빠른 길(방금 추가 취소)이면 200 `{"ok", "fast": true, "table", "file", "rows"}`, 아니면 202 작업(다시 만들기) |
| `POST /tables/{t}/batches/{file}/restore` | admin, 또는 그 배치를 더한 operator | `_removed/`의 배치 되살리기 → 202 작업 |
| `POST /batches` | 운용자 | **여러 파일 한꺼번에**(아래 "여러 파일") → 202 작업 |
| `GET /tables/{t}/template` | 운용자 | 빈 양식: 열 이름 머리글 + 예시 한 줄(`default`, enum의 첫 값, 유형별 예시)의 CSV(UTF-8 BOM) |
| `POST /tables/{t}/aliases` | admin | 머리글 맞춤 저장: `{"aliases": {"<머리글>": "<열>"}, "revision"}` → 200 `{"revision", "changed"}`. `column.update` 연산으로 그 열의 `aliases`에 더한다(Studio와 같은 저장. [studio.md](studio.md) "변경 연산"). `revision`은 `GET /tables/{t}`가 준 값이고 다르면 409 `stale_config`. aliases는 DB와 상관없어 적용이 필요 없다. 감사 로그 `data.table.alter` |
| `GET /jobs`, `GET /jobs/{id}` | 운용자 | 작업 목록(최근 것부터, 로그 없이), 작업 하나(아래 "작업") |
| `GET /jobs/{id}/rejects` | 운용자 | 오류 행을 빼고 넣은 작업의 빠진 행: 올린 행(정규형 머리글) + `오류` 열의 CSV(UTF-8 BOM). 여러 파일이면 파일마다 하나씩 담은 zip. 없으면 404 `unknown_job` |

경로는 `dashkit/manage/routes/`의 모듈 하나씩이다. 경로를 더하는 일은 모듈 하나와 `routes/__init__.py`의 목록 한 줄이다.

### 데이터 더하기 (`POST /tables/{t}/batches`)

| 본문 | 응답 |
|---|---|
| JSON `{"rows": [{…}], "method"?: "form" \| "paste", "dry_run"?, "columns"?, "force"?, "skip_invalid"?}` | 200 결과([ingest.md](ingest.md) "결과"). 행은 `INGEST_INLINE_MAX_ROWS`까지(넘으면 413 `too_many_rows`: 파일로 안내) |
| multipart `file`(`<이름>.csv`) + 글 부분 `dry_run`·`force`·`skip_invalid`·`strict`(`1`/`true`)·`columns`(JSON 글) | 202 `{"job"}`. 파일은 디스크의 임시 파일로 받는다(파일 하나 `INGEST_MAX_UPLOAD_MB`, 요청 하나 `INGEST_MAX_REQUEST_MB`, 넘으면 413 `upload_too_large`, 행이 `INGEST_MAX_ROWS`를 넘으면 작업이 413 `too_many_rows`). 미리보기(`dry_run`)면 작업 결과에 `upload_id`가 있다 |
| JSON `{"upload_id", "columns"?, "force"?, "skip_invalid"?}` | 202 `{"job"}`. 미리보기한 파일을 다시 올리지 않고 넣는다. 같은 테이블·같은 사람의 1시간 안의 미리보기만(아니면 410 `upload_expired`). 넣기에 성공하면 그 업로드는 풀리고, 실패하면 1시간 안에는 같은 `upload_id`로 다시 넣을 수 있다. 실패한 미리보기의 파일은 잡아 두지 않는다 |

- 대상이 `replace` 테이블이면 409 `not_append`. 그 밖의 오류 코드는 [ingest.md](ingest.md)와 같다(422 `invalid_rows`·`unknown_columns`·`key_conflict`·`check_failed`, 409 `duplicate_file`·`rebuild_required`·`project_mismatch`·`stale_config`·`apply_running`).
- 감사 로그: 넣으면 `data.batch.create`(`detail`: `project`, `table`, `file`, `sha256`, `rows`, `method`(`form`·`paste`·`upload`), `source_name`(올린 파일 이름)), 실패하면 `data.batch.failed`(`detail_code`). 미리보기는 남기지 않는다. 데이터 값은 싣지 않는다.

- **오류 행 빼고 넣기**(`skip_invalid`): 행 단위 검증 오류, 참조 오류, `default`가 없는 필수 열이 빠진 새 행을 빼고 나머지를 넣는다. 배치 파일에는 들어간 행만 쓴다. 결과의 batch에 `skip_invalid`, `rejected`(뺀 행 수)가 있고 작업 결과에 `rejects: true`면 `GET /jobs/{id}/rejects`로 빠진 행을 내려받는다. 키 충돌(`error` 정책), `checks` 위반, 배치 안의 기본 키 중복은 빼고 넣는 대상이 아니라 전체 거절이다.
- **진행률:** 파일 적재 작업의 `progress`는 읽는 동안 `{"step": "read", "table", "rows", "bytes", "total_bytes"}`(1만 행마다), 잠금을 기다리고 반영하는 동안 `{"step": "lock"}`이다.

### 여러 파일 (`POST /batches`)

| 본문 | 응답 |
|---|---|
| multipart `files`(여러 개, `<이름>.csv`) + 글 부분 `map`(JSON `{"<파일 이름>": "<테이블>"}`), `dry_run`·`force`·`skip_invalid`·`strict`, `columns`(JSON `{"<파일 이름>": {"<머리글>": "<열>" \| null}}`) | 202 `{"job"}`. 결과의 `batches`는 파일마다 하나(보낸 순서) |
| JSON `{"upload_id", "map"?, "columns"?, "force"?, "skip_invalid"?}` | 202. 미리보기한 파일들을 다시 올리지 않고 넣는다 |

- **한 트랜잭션이다**(설계서 D12): 참조 순서(`config.table_order`)대로 넣고, 하나라도 실패하면 어느 파일도 들어가지 않는다. 확정 도중에 끊겨도 묶음 단위로 복구한다([ingest.md](ingest.md) "묶음 기록과 끊김 복구").
- `map`을 빼면 파일 이름(확장자 제외)이 테이블 이름이어야 한다. 짝지을 수 없으면 422 `unknown_table`(`errors`에 파일별 이유). 같은 테이블에 파일 여럿도 된다(번호를 이어 매긴다).
- 한 번에 50개까지(넘으면 400 `too_many_files`), 합계 `INGEST_MAX_REQUEST_MB`.

### 되돌리기와 되살리기

- 감사 로그: `data.batch.remove`(`fast`), `data.batch.restore`.
- operator가 남의 배치(또는 누가 더했는지 기록이 없는 배치)를 되돌리거나 되살리면 403 `forbidden`.
- 형식과 동작(빠른 길의 조건, 다시 만들기, 끊김 복구)은 [ingest.md](ingest.md) "되돌리기와 되살리기".

### 작업

`{"id", "kind": "ingest" | "remove" | "restore", "table", "status": "running" | "succeeded" | "failed", "started_at", "finished_at", "actor", "progress": {"step", …}, "log": [줄], "result": {…} | null, "error": {"status", "detail", "message", "errors"?} | null}`. 화면을 떠나도 작업은 끝까지 간다. 작업은 관리 서비스 프로세스의 메모리에 있어(최근 100개) 다시 뜨면 없어진다(404 `unknown_job`). 적재가 어디까지 갔는지는 묶음 기록의 복구가 정하고, 끝난 작업의 요약은 감사 로그에 있다.

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
