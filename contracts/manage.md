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
- 경로 값(`{table}`·`{dataset}`·`{file}`·이력 `{id}`)은 현재 프로젝트의 설정과 폴더 목록에 대조한 뒤에만 쓴다. 사용자 입력이 들어가는 SQL은 전부 매개변수 바인딩이고 식별자는 목록과 대조한 뒤 `psycopg.sql.Identifier`로만 넣는다.
- 로그·오류 응답·감사 로그에 비밀값과 데이터 값을 싣지 않는다.

## 경로

| 메서드·경로 | 로그인 | 내용 |
|---|---|---|
| `GET /health` | 불필요 | `{"status": "ok"}` |
| `GET /config` | 운용자 | `{"project", "role", "limits"}`. 운용 화면이 데이터 메뉴를 그리는 데 쓴다 |
| `GET /tables` | 운용자 | `{"items", "total", "project"}`. 항목 `{name, mode, on_conflict, description, rows, batches, compact_hint, used_by, last_batch: {file, added_at, actor_email} \| null}`. `rows`는 배치 기록의 `inserted` 합(적용한 적이 없으면 `null`), `batches`는 배치 폴더의 파일 수, `compact_hint`는 그 수가 `INGEST_COMPACT_HINT`를 넘는지, `used_by`는 그 테이블을 읽는 데이터셋의 수(계보) |
| `GET /tables/{t}` | 운용자 | 입력 폼을 만드는 정의: `{name, mode, on_conflict, description, primary_key, revision, columns: [{name, type, required, primary_key, label, values?, ref?, min?, max?, pattern?, description?, default?, aliases?}], checks: [이름], datasets: [그 테이블을 그대로 보는 데이터셋]}`. `label`은 그 테이블을 보는 `table:` 데이터셋의 `labels` |
| `POST /tables/{t}/batches` | 운용자 | **데이터 더하기**([ingest.md](ingest.md) "더하기"). 아래 "데이터 더하기" |
| `GET /tables/{t}/batches?removed=1` | 운용자 | 배치 이력, 최근 것부터: `{"items", "total"}`. 항목 `{file, removed, first, added_at, recorded, rows, inserted, updated, skipped, unchanged, actor_email, method, source_name, mine}`. 누가·방법·원래 파일 이름은 감사 로그(`data.batch.create`)에서 온다. `mine`은 요청한 사람이 더한 배치인지. `removed=1`이면 `_removed/`의 배치도 싣는다 |
| `GET /tables/{t}/batches/{file}` | 운용자 | 배치 파일 내려받기(정규형 그대로, `.csv.gz`는 `application/gzip`) |
| `DELETE /tables/{t}/batches/{file}` | admin, 또는 그 배치를 더한 operator | 되돌리기. 빠른 길(방금 추가 취소)이면 200 `{"ok", "fast": true, "table", "file", "rows"}`, 아니면 202 작업(다시 만들기) |
| `POST /tables/{t}/batches/{file}/restore` | admin, 또는 그 배치를 더한 operator | `_removed/`의 배치 되살리기 → 202 작업 |
| `POST /batches` | 운용자 | **여러 파일 한꺼번에**(아래 "여러 파일") → 202 작업 |
| `GET /tables/{t}/template` | 운용자 | 빈 양식: 열 이름 머리글 + 예시 한 줄(`default`, enum의 첫 값, 유형별 예시)의 CSV(UTF-8 BOM) |
| `POST /tables/{t}/aliases` | admin | 머리글 맞춤 저장: `{"aliases": {"<머리글>": "<열>"}, "revision"}` → 200 `{"revision", "changed"}`. `column.update` 연산으로 그 열의 `aliases`에 더한다(Studio와 같은 저장. [studio.md](studio.md) "변경 연산"). `revision`은 `GET /tables/{t}`가 준 값이고 다르면 409 `stale_config`. aliases는 DB와 상관없어 적용이 필요 없다. 감사 로그 `data.table.alter` |
| `POST /tables/{t}/rows/query` | 운용자 | 테이블 원본(`raw.<t>`)의 행 조회(아래 "행 조회") |
| `POST /datasets/{d}/rows/query` | 운용자 | 그래프가 실제로 보는 뷰(`dash.<d>`)의 행 조회 |
| `GET /datasets` | 운용자 | 데이터셋 목록 `{"items", "total"}`: `{key, kind: "table" \| "sql", table, sources, description, used_by, labels}`. `sources`는 그 뷰가 읽는 테이블과 열(계보. `sql:`이고 적용 전이면 `null`), `used_by`는 그 데이터셋을 쓰는 차트·위젯 섹션의 수(설정에서 센다) |
| `GET /lineage` | 운용자 | 계보(아래 "구조 변경") |
| `GET /tables/{t}/used_by` | 운용자 | 그 테이블의 쓰이는 곳: `{table, datasets, charts: [{key, title, dashboard, dataset, tabs}], sections: [{tab, index, type, dataset}], tabs, unknown_datasets}` |
| `PATCH /config` | admin | 구조 변경: `{"ops": [연산…], "revision"}` → 202 작업(아래 "구조 변경") |
| `POST /tables/infer` | admin | 새 테이블 추론: multipart `file`(CSV 하나) → 200 `{upload_id, source_name, table, mode, on_conflict, dataset, suggest}`. 파일은 쓰지 않고 1시간 잡아 둔다 |
| `POST /tables` | admin | 새 테이블 만들기: `{upload_id, table, mode?, on_conflict?, dataset?, suggest?, revision}` → 202 작업 |
| `POST /tables/{t}/compact` | admin | 합치기 → 202 작업. `append` 테이블만(아니면 409 `not_append`) |
| `GET /history` | admin | 설정 변경 이력, 최근 것부터: `{"items": [{id, action, at, files: [{path, before, after}]}], "total", "revision"}` |
| `POST /history/{id}/restore` | admin | 그 판으로 되돌리기: `{"revision"}` → 202 작업. 없는 이력은 404 `unknown_history` |
| `GET /export` | admin | 설정 파일 내려받기(zip: `<프로젝트>/schema.yaml`, `dashboard.yaml`, `REVISION`. 데이터는 넣지 않는다). 사람이 저장소에 커밋한다 |
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

### 행 조회 (`POST /tables/{t}/rows/query`, `POST /datasets/{d}/rows/query`)

```json
{ "filters": [ { "column": "<열>", "op": "in", "value": ["a", "b"] }, { "column": "<열>", "op": "ge", "value": "2026-11-01" } ],
  "sort":    [ { "column": "<열>", "dir": "desc" } ],
  "columns": ["<열>", "…"],
  "limit": 50, "cursor": null, "count": false }
```

→ `{"columns": [{"name", "type"}], "rows": [{…}], "next_cursor": "…" | null, "total": n | "100000+" | null, "changed": false, "truncated_cells": [[행 번호, "열"]]}`. 값의 형식은 `/api/data`와 같다([api.md](api.md)).

- **읽기는 `dash_inspect`만** 쓴다(읽기 전용 트랜잭션, `MANAGE_STATEMENT_TIMEOUT_MS` 시간 제한 → 넘으면 504 `query_timeout`). owner 접속을 열지 않는다.
- 연산: `eq` `ne` `lt` `le` `gt` `ge` `in`(1~1000개) `contains` `starts_with` `is_null` `not_null`. `lt`~`ge`는 글·숫자·날짜·시각 열, `contains`·`starts_with`는 글 열만(글자 그대로 찾는다. `%`·`_`는 와일드카드가 아니다). 열 이름·연산·유형이 맞지 않으면 422 `invalid_filter`. 열 이름은 목록과 대조한 뒤 `Identifier`로, 값은 모두 매개변수로 넣는다.
- `limit`은 1~200(기본 50, 넘으면 422 `invalid_query`). 구현은 `dashkit/paging.py` 하나다(backend의 `/api/data`와 함께 쓴다).
- **커서**: 테이블과, 원본 테이블의 기본 키 열을 모두 내보내는 `table:` 데이터셋은 **키 방식**(정렬 열 + 기본 키의 다음 값부터. 깊은 페이지도 속도가 같다. 기본 키로 정렬하면(기본) 첫 페이지 때 있던 행은 겹치거나 빠지지 않는다. 다른 열로 정렬하면 그 사이 `update` 배치로 값이 바뀐 행은 겹치거나 빠질 수 있다). 그 밖(`sql:` 뷰, 기본 키 열이 빠진 뷰)은 **건너뛰기 방식**: 요청한 정렬 열 뒤에 나머지 모든 열로 순서를 고정하고 100,000행까지(넘으면 422). 정렬은 NULL을 맨 뒤에 둔다. 커서는 서버만 읽는 값(서명)이고 그 조회(필터·열)의 것만 받는다(다르면 422 `invalid_query`). 관리 서비스가 다시 뜨면 이전 커서는 받지 않는다.
- **데이터 판**: 커서에 첫 페이지 때의 판(마지막으로 다시 만든 시각 + 그 테이블의 배치 기록 이름·해시)을 넣고, 다음 페이지에서 다르면 `changed: true`다(커서는 첫 페이지의 판을 이어 가므로 그 뒤의 페이지에서도 계속. 화면은 "조회하는 사이 데이터가 바뀌었습니다. 처음부터 다시 보기"). 데이터셋은 `table:`이면 그 테이블, `sql:`이면 계보가 말하는 읽는 테이블들의 판을 합친다(계보가 없는 옛 적재 스냅샷이면 모든 테이블).
- **전체 행 수**: 테이블과 `table:` 데이터셋은 필터가 없으면 배치 기록의 `inserted` 합(세지 않는다). `sql:` 데이터셋이나 필터가 있으면 `null`. `"count": true`면 결과 행을 100,000까지 센다(넘으면 `"100000+"`).
- **값 표현**: 정수가 ±(2^53−1)을 넘거나 숫자가 JSON 수(배정밀도)로 정확히 나타나지 않으면 문자열로 보낸다(고치기 폼이 그 값을 키와 배치로 그대로 다시 보낸다. 키·필터의 정수·숫자는 문자열도 받는다).
- **긴 값**: 목록에서는 칸 하나를 2,000자에서 자르고 `truncated_cells`에 적는다. 잘린 값은 보여 주기만 하고 저장에 쓰지 않는다.
- **한 행 읽기**: `"key": {"<기본 키 열>": 값, …}`(기본 키 열 전부)를 주면 그 한 행을 자르지 않고 돌려준다: `{"columns", "row", "truncated_cells": ["열"]}`(응답이 2MB를 넘을 때만 큰 칸부터 자른다). 없으면 404 `unknown_row`. 고치기 폼이 쓴다. 키가 없는 데이터셋은 422.
- DB에 그 테이블·뷰가 없으면(적용 전) 409 `rebuild_required`.

### 되돌리기와 되살리기

- 감사 로그: `data.batch.remove`(`fast`), `data.batch.restore`.
- operator가 남의 배치(또는 누가 더했는지 기록이 없는 배치)를 되돌리거나 되살리면 403 `forbidden`.
- 형식과 동작(빠른 길의 조건, 다시 만들기, 끊김 복구)은 [ingest.md](ingest.md) "되돌리기와 되살리기".

### 구조 변경

형식과 동작(바뀌는 파일, 순서, 설정 변경 이력, 끊김 복구, 합치기, 계보)은 [ingest.md](ingest.md) "구조 변경"·"합치기"·"계보", 연산은 [studio.md](studio.md) "변경 연산"이다.

- `PATCH /config`가 받는 연산은 `table.add`, `table.update`, `table.remove`, `column.add`, `column.update`, `dataset.add`, `dataset.update`, `dataset.remove`뿐이다(그 밖은 422 `invalid_op`. 탭·섹션·차트 연산은 Studio에서 한다). `revision`이 없으면 400 `invalid_request`.
- 요청을 받을 때 연산 형식(400 `invalid_op`)·새 설정 검사(422 `invalid_config`)·쓰는 곳(409 `in_use`: 데이터셋이 읽는 테이블이나 다른 테이블이 `ref`로 가리키는 테이블의 삭제, 그래프가 쓰는 데이터셋의 삭제. `errors`에 `데이터셋 <키>`·`참조 <테이블.열>`·`차트 <키>`·`섹션 <탭>#<번호>`)을 먼저 보고, 통과하면 202 작업이다. 배치 파일이 있는 `append` 테이블을 `replace`로 바꾸는 것은 422 `invalid_op`(배치를 읽지 않게 된다).
- 작업이 잠금 안에서 다시 검사한다. 작업의 실패는 `error`에 담긴다: 409 `stale_config`(그 사이 설정이 바뀜), 422 `load_failed`(+`errors`, 모든 파일을 되돌렸다), 409 `apply_running`·`project_mismatch`.
- 작업의 결과: `{changed: [경로], history: <이력 id>, revision, counts: {테이블: 행 수}, sync: {ok, errors}, affected?, datasets_without_column?}`. `affected`는 유형을 바꾸는 열 연산이 닿는 데이터셋·차트(`[{table, column, datasets, charts}]`), `datasets_without_column`은 더한 열이 나오지 않는 `sql:` 데이터셋(`[{table, column, datasets}]`)이다. 새 테이블은 `table`, `dataset`, `dashboard`, `tab`도 싣는다. 되돌리기는 `restored`(이력 id)를 싣는다.
- 새 테이블: `POST /tables/infer`의 `table`을 사람이 고쳐 그대로 보낸다(`name`, `primary_key`, `columns: [{name, type, source?, required?, values?, ref?, description?, min?, max?, pattern?}]`). 추론 규칙과 원래 머리글의 `aliases`는 [scaffold.md](scaffold.md) "운용 화면의 새 테이블". 이름이 이미 있으면 409 `table_exists`, 이름·열 형식이 틀리면 422 `invalid_request`, 첫 파일이 규칙을 통과하지 못하면 422 `invalid_rows`(+`errors`). `upload_id`가 없거나 지났으면 410 `upload_expired`.
- 감사 로그: `data.table.create`·`data.table.alter`·`data.table.drop`, `data.dataset.create`·`data.dataset.alter`·`data.dataset.drop`, `data.config.restore`, `data.batch.compact`([auth.md](auth.md) "감사 로그"). 데이터 값은 싣지 않는다.
- 설정 파일은 서버에서 바뀐다. `GET /export`로 내려받아 사람이 저장소에 커밋한다(설계서 D4). 관리 서비스와 Studio가 같은 프로젝트를 동시에 고치면 `revision`이 어긋난 쪽이 409 `stale_config`다.

### 작업

`{"id", "kind": "ingest" | "remove" | "restore" | "config" | "create" | "compact" | "restore_config", "table", "status": "running" | "succeeded" | "failed", "started_at", "finished_at", "actor", "progress": {"step", …}, "log": [줄], "result": {…} | null, "error": {"status", "detail", "message", "errors"?} | null}`. 화면을 떠나도 작업은 끝까지 간다. 작업은 관리 서비스 프로세스의 메모리에 있어(최근 100개) 다시 뜨면 없어진다(404 `unknown_job`). 적재가 어디까지 갔는지는 묶음 기록의 복구가 정하고, 끝난 작업의 요약은 감사 로그에 있다.

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
