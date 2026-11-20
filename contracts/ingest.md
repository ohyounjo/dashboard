# 데이터 추가 적재

설계: `reference/data-design.md` 4·5·10-3절. 구현: `dashkit/ingest/`(로더·관리 서비스·CLI·Studio가 함께 쓰는 한 벌), 로더 `dashkit/loader/`.
경로·권한·요청 보호는 [manage.md](manage.md)에 적는다. 이 문서는 형식과 동작의 기준이다.

## 테이블 방식

`schema.yaml`의 테이블에 선택 키 둘이 있다([schema.schema.json](schema.schema.json)).

| 키 | 값 | 뜻 |
|---|---|---|
| `mode` | `replace`(기본) · `append` | 원본 파일의 방식 |
| `on_conflict` | `error`(기본) · `skip` · `update` | `append` 테이블에서 같은 기본 키가 이미 있을 때. `mode: append`일 때만 쓴다(아니면 `dashkit check` 오류) |

| | `replace` | `append` |
|---|---|---|
| 원본 파일 | `data/<테이블>.csv` 하나 | `data/<테이블>.csv`(있으면 첫 배치) + `data/<테이블>/`의 배치 파일들 |
| 테이블 내용 | 그 파일 | 배치를 번호순으로 차례로 넣은 결과(같은 키는 `on_conflict`대로) |
| CSV 머리글 | 스키마의 열과 정확히 같은 집합(지금과 같다) | 스키마 열의 부분집합. 기본 키 열은 반드시 있다 |
| 값 검증 | 지금의 엄격한 검증 그대로 | 같은 검증(배치 파일은 정규형이다) |

- `replace` 테이블에 `mode: append`만 더하면 기존 CSV가 첫 배치가 된다.
- 열 규칙에 선택 키 둘이 있다. `default`: **새로 들어가는 행**에서 배치에 그 열이 없을 때 쓰는 값(없으면 NULL). 이미 있는 행을 고칠 때는 쓰지 않는다. 열의 type·규칙에 맞아야 하고 기본 키 열에는 쓸 수 없다. `aliases`: 올린 파일의 머리글이 이 이름이면 이 열로 본다. 같은 테이블의 다른 열 이름이나 다른 열의 `aliases`와 겹칠 수 없다.
- 이 키들은 추가만 하는 변경이라 설정 버전은 1 그대로다. 지정하지 않으면 지금과 똑같이 동작한다.

## 배치 폴더

```
projects/<프로젝트>/data/
├─ <replace 테이블>.csv
├─ <append 테이블>.csv                  # 첫 배치(선택)
├─ <append 테이블>/
│  ├─ 000001-20261120T0910Z.csv.gz     # 배치(번호순으로 읽는다)
│  ├─ _removed/                         # 되돌린 배치. 읽지 않는다
│  └─ _merged/                          # 합치기 전의 배치. 읽지 않는다
└─ _pending/                            # 끝나지 않은 폴더 작업의 묶음 기록. 평소에는 비어 있다
   └─ incoming/                         # 확정 전의 정규형 파일
```

- 배치 파일의 이름은 `<번호>[-<덧붙임>].csv` 또는 `….csv.gz`다. 로더는 폴더 바로 아래의 배치를 **번호순**(숫자로 비교)으로 읽고 `data/<테이블>.csv`를 언제나 맨 앞에 읽는다. `_`나 `.`으로 시작하는 이름은 읽지 않는다. 그 밖의 이름, 폴더, 같은 번호가 둘이면 `dashkit check`와 load가 `data/<테이블>/<이름>:1:-: …` 오류로 알린다.
- 관리 서비스가 쓰는 이름은 `<6자리 번호>-<UTC 시각 YYYYMMDDTHHMMZ>.csv.gz`다. 번호는 지금까지 쓴 가장 큰 번호 + 1(`_removed/`·`_merged/` 아래 포함)이라 새 배치는 언제나 모든 배치의 뒤에 온다. 시각은 더한 때이고 배치 기록의 `added_at`이 된다(이름에 없으면 NULL). 올린 파일의 이름은 경로로 쓰지 않는다.
- 손으로 정규형 파일을 넣어도 같다. 이름 규칙만 지키고 지금 가장 큰 번호보다 큰 번호를 붙인다.
- **테이블 내용은 (`schema.yaml`, 번호순 배치 파일들)만으로 정해진다.** DB는 그 결과물이고 `make apply`로 언제든 같은 DB를 다시 만든다.

### 정규형

배치 파일은 정규형이다: UTF-8(BOM 없음), 쉼표 구분, 줄 끝 `\n`, 필요할 때만 큰따옴표(Python `csv.QUOTE_MINIMAL`). 머리글은 **그 배치가 실제로 받은 열만** 스키마 순서로 적는다(빠진 열을 채우지 않는다). 값은 지금 검증기가 받는 표기이고 빈 칸은 NULL이다. 관리 서비스는 gzip(머리말의 시각 0, 파일 이름 없음, 압축 수준 6)으로 쓰므로 같은 내용이면 같은 바이트다.

- **내용 해시**는 압축을 푼 내용의 SHA-256이다(`.csv`와 `.csv.gz`가 같은 내용이면 같다). 배치 기록, 중복 확인, 대조가 이 값을 쓴다.
- 같은 행들을 직접 입력·붙여넣기·파일로 넣으면 배치 파일이 바이트 단위로 같다(`tests/test_ingest_db.py`).

## 접수

입력이 무엇이든 같은 길을 지난다: **파서 → 머리글 맞추기 → 값 다듬기 → 한 줄씩 검증 → 정규형 쓰기·임시 테이블 채우기.** `strict`면 아무것도 바꾸지 않고 정규형만 받는다(로더가 배치 파일을 읽는 길이다).

| 파서(`PARSERS`) | 받는 것 | 오류 위치 |
|---|---|---|
| `csv` | 디스크의 파일. 인코딩은 UTF-8(BOM 허용), 안 되면 CP949. 구분자는 머리글 줄에서 쉼표·탭·세미콜론 가운데 가장 많은 것 | `<파일 이름>:<줄>:<열>`(레코드가 시작한 줄, 머리글 = 1행) |
| `rows` | JSON 행 목록. 모든 행이 같은 열 집합이어야 한다(아니면 422 `invalid_rows`). 값은 문자열로 바꾼다: `null`·`""`는 NULL("일부러 비움"), 키가 없으면 "보내지 않음", 참거짓은 `true`/`false` | `rows[<번호>].<열>` |

머리글 → 열은 차례로 맞춘다: ① 열 이름 ② 그 열의 `aliases` ③ 그 테이블을 그대로 보는(`table:`) 데이터셋의 `labels` ④ 대소문자·공백·`_`·`-`를 무시한 비교. 한 단계에서 두 열에 맞으면 맞추지 않는다. 요청의 `columns`(`{"<머리글>": "<열>" 또는 null}`)가 있으면 그 머리글은 그대로 따른다(`null`은 버린다). 이름이 빈 머리글(엑셀이 끝에 남기는 빈 열)은 버리고 `columns.dropped`에 `""`로 알린다(`strict`면 오류). 머리글 두 개가 같은 열에 맞거나 기본 키 열이 없으면 오류다. 맞지 않은 머리글이 남으면 422 `unknown_columns`와 `columns: [{"header", "candidates"}]`.

값 다듬기(`NORMALIZERS`, 유형마다의 규칙 목록). **뜻이 하나인 표기만** 바꾸고 바꾼 수를 결과의 `normalized`에 센다.

| 유형 | 바꾸는 표기 → 정규형 | `normalized`의 항목 |
|---|---|---|
| 모든 유형 | 앞뒤 공백 제거 | `trimmed` |
| `date` | `2026/11/20`, `2026.11.20`, `20261120` → `2026-11-20`. 연도가 앞에 오는 형식만(`11/20/2026`은 그대로 두어 오류) | `dates` |
| `integer`, `number` | 천 단위 쉼표 제거 | `numbers` |
| `boolean` | `Y`/`N`, `yes`/`no`, `1`/`0`, 대문자 → `true`/`false` | `booleans` |
| `timestamp` | 날짜와 시각 사이의 공백·`T`를 `T` 하나로, 날짜 부분은 `date`와 같다 | `timestamps` |

검증 규칙과 오류 글은 지금의 CSV 검증과 같다(유형, `required`, `enum`, `min`·`max`, `pattern`, 배치 안의 기본 키 중복). 파일 하나의 오류는 200개까지 글로 싣고 `error_count`에 모두 센다. 원래 올린 파일은 보관하지 않는다.

## 반영

운영 중의 추가 적재와 load의 다시 만들기는 **같은 함수**를 쓴다(`dashkit/ingest/apply.py`). load는 빈 테이블에 배치를 번호순으로 하나씩 넣는다.

1. `stage_batch`(올린 내용만 보고 정해지는 일): 한 줄씩 검증하며 임시 테이블에 COPY하고 정규형 파일을 쓴다. 배치 안의 기본 키 중복을 본다.
2. `apply_batch`(DB 상태를 보는 일): 참조 열의 값이 대상 테이블(DB)에 있는지 → 이미 있는 키의 수 → 충돌 정책 → 새 키의 행 넣기. 새 행의 머리글에 없는 열은 `default` 또는 NULL이고, `default`가 없는 `required` 열이 빠진 새 행이 있으면 422 `invalid_rows`.
3. `run_checks`: `schema.yaml`의 `checks` 전부(load와 같은 함수, 같은 오류 글). 위반하면 422 `check_failed`.

### 충돌 정책(`POLICIES`)

| 정책 | 이미 있는 키의 행 | 세는 항목 |
|---|---|---|
| `error` | 하나라도 있으면 전체 거절. 422 `key_conflict`, `keys`(앞 20개) | |
| `skip` | 그대로 둔다 | `skipped` |
| `update` | **배치의 머리글에 있는 열만** 바꾼다. 머리글에 없는 열은 그대로, 칸이 비어 있으면 NULL. 값이 모두 같으면 바꾸지 않는다 | `updated`, `unchanged` |

정책은 테이블의 속성이라 더할 때마다 바꿀 수 없다.

## 배치 기록과 데이터 설정 지문

`dashkit_meta.batch`(로더가 load 때 통째로 다시 만들고, 추가 적재가 줄을 더한다. 형식은 [plan.md](plan.md) "적재 기록"):

| 열 | 뜻 |
|---|---|
| `table_name`, `file` | 테이블, 원본 파일 이름(replace·첫 배치는 `<테이블>.csv`, 배치는 폴더 안의 이름). 기본 키 |
| `sha256` | 내용 해시 |
| `added_at` | 파일 이름의 시각(없으면 NULL) |
| `rows`, `inserted`, `updated`, `skipped`, `unchanged` | 그 배치의 행 수와 반영 결과 |

**프로젝트의 모든 원본 데이터 파일**이 한 줄씩 들어간다(`replace` 테이블의 CSV 포함). 원본 값은 없다. `dash_reader`·`dash_inspect`가 읽을 수 있다. 누적 테이블은 행 지문(`row_fingerprint`)을 남기지 않는다.

적재 스냅샷(`load_snapshot.snapshot`)의 **`data_revision`**: DB의 내용을 정하는 설정만으로 구한 SHA-256. `schema.yaml`의 `tables` 전체(정규화한 값: 열, 규칙, 방식, 충돌 정책, `checks`)와 `dashboard.yaml`의 데이터셋 뷰 정의(`table`·`sql`)·`app.as_of`. 탭·그래프 배치·데이터셋의 지표와 표시 이름은 넣지 않는다(DB와 상관없다). 스냅샷의 `tables.<이름>.mode`에 방식을 적는다.

## 쓰기 잠금

쓰기는 한 번에 하나다. 잠금은 **데이터 DB에 하나**뿐인 세션 단위 advisory lock이다(`dashkit/project/locks.py`, 고정 키). 배치 폴더나 `raw`를 바꾸는 모든 작업(더하기, 되돌리기, 합치기, load·apply, 구조 변경)이 잡는다. 잠금은 작업의 DB 접속에 묶여 접속이 끊기면 DB가 푼다. 관리 서비스는 `MANAGE_LOCK_WAIT_MS`(기본 5000)까지 기다리고 못 잡으면 409 `apply_running`, CLI·Studio의 load는 10초까지 기다리고 못 잡으면 load 오류다.

## 공통 순서

폴더나 `raw`를 바꾸는 작업은 모두 이 순서다. `dashkit/ingest/operation.py`의 `locked`·`finish`가 감싸고 작업마다 따로 구현하지 않는다.

| 순서 | 내용 |
|---|---|
| 잠금 밖 | 올린 내용만 보고 정해지는 일: 읽기, 머리글 맞추기, 다듬기·검증, 정규형 쓰기(`_pending/incoming/`), 해시, 임시 테이블 채우기 |
| 1. 잠금 | 쓰기 잠금. 잡을 때마다 ① DB에 적재된 프로젝트(`load_snapshot.project`)가 이 프로젝트인지(아니면 409 `project_mismatch`, 적재한 적이 없으면 409 `rebuild_required`) ② 접수에 쓴 설정 파일의 `revision`이 지금 것과 같은지(다르면 잠금을 풀고 지금 설정으로 처음부터 다시 접수한다. 두 번 어긋나면 409 `stale_config`) ③ 지금 설정의 `data_revision`이 스냅샷의 것과 같은지(다르면 설정을 고치고 적용하지 않은 것이므로 409 `rebuild_required`. load·apply와 구조 변경은 이 확인을 하지 않는다) |
| 2. 복구 | 이 프로젝트 폴더의 남은 묶음 기록(아래) |
| 3. 대조 | **프로젝트의 모든 원본 데이터 파일**의 이름과 내용 해시를 배치 기록과 견준다(크기·수정 시각이 지난번과 같으면 해시를 다시 구하지 않는다). 차이의 처리는 아래 표 |
| 4. 작업 | DB 반영 → (5) → 묶음 기록 쓰기 → 파일 옮기기 → 배치 기록 |
| 5. 규칙 검사 | 같은 트랜잭션에서 `checks`. 위반하면 모두 되돌린다 |
| 6. 마무리 | 커밋 → 묶음 기록 지우기 → 잠금 풀기 → `after_ingest(project, tables)` 훅(지금은 하는 일이 없다) |

대조에서 파일과 기록이 다를 때(더하기):

| 차이 | 처리 |
|---|---|
| 바꾸려는 테이블에 기록에 없는 새 파일만 있고 번호가 기록의 모든 배치보다 뒤다 | 그 파일들을 번호순으로 먼저 반영한 뒤 진행한다(결과의 `applied_first`) |
| 바꾸려는 테이블에서 기록에 있는 파일이 바뀌었거나 없어졌다, 새 파일의 번호가 기록의 배치보다 앞이다, 이름 규칙 오류 | 409 `rebuild_required`(`errors`에 파일) |
| 다른 테이블의 파일이 기록과 다르다(`replace` 테이블의 CSV를 고치고 적용하지 않은 경우 포함) | 409 `rebuild_required` |

## 더하기

`dashkit.ingest.ingest(project, items, *, dry_run, force, strict, skip_invalid)`. 한 행도 배치이고 여러 항목은 한 트랜잭션이다(참조 순서대로 넣고, 하나라도 실패하면 아무것도 들어가지 않는다).

- 대상이 `replace` 테이블이면 409 `not_append`, 없는 테이블이면 404 `unknown_table`.
- **같은 내용 다시 올리기:** 그 테이블에 같은 내용 해시의 배치가 있으면(같은 요청 안의 두 항목도) 409 `duplicate_file`, `duplicates: [{"table", "file"}]`. `force`면 진행한다. 견주는 일은 잠금 안에서 한다(동시에 두 번 보내도 뒤의 것이 거절된다).
- **전부 들어가거나 하나도 들어가지 않는다.** 검증 오류(422 `invalid_rows`, `errors`·`error_count`·`batches`), 충돌(422 `key_conflict`), `checks` 위반(422 `check_failed`), DB 오류 어느 것이든 테이블도 폴더도 그대로다.
- **미리보기**(`dry_run`): 반영과 규칙 검사까지 하고 되돌린다. 결과 형식은 같고 `file`은 `null`이다. 검증 오류가 있으면 `ok: false`로 그 오류를 싣는다.

결과:

```json
{ "ok": true, "dry_run": false,
  "batches": [ { "table": "<테이블>", "file": "000007-20261121T0102Z.csv.gz", "sha256": "<내용 해시>",
                 "rows": { "total": 100000, "invalid": 0, "inserted": 99813, "updated": 150, "skipped": 0, "unchanged": 37 },
                 "normalized": { "encoding": "cp949", "delimiter": ",", "dates": 1204, "trimmed": 31 },
                 "columns": { "matched": { "<머리글>": "<열>" }, "missing": ["<열>"], "unknown": [], "dropped": [] },
                 "errors": [], "error_count": 0,
                 "changes": [ { "key": "<기본 키>", "before": { "<열>": "<값>" }, "after": { "<열>": "<값>" } } ] } ],
  "applied_first": [ { "table": "<테이블>", "file": "<손으로 넣은 파일>", "rows": 1 } ],
  "recovered": [ { "id": "<묶음 id>", "kind": "add", "result": "applied" } ] }
```

`changes`는 `update` 정책에서 바뀐 행의 전후 값(바뀐 열만, 앞 20개)이다. `applied_first`·`recovered`는 해당할 때만 있다.

## 묶음 기록과 끊김 복구

폴더를 바꾸는 작업은 파일을 바꾸기 전에 `data/_pending/<묶음 id>.json`을 쓰고(`{"id", "kind", "project", "files": [{"table", "name", "incoming"}], "first": [{"table", "name"}]}`. `first`는 같은 트랜잭션에서 먼저 반영하는, 원래부터 폴더에 있던 손 파일), 커밋한 뒤 지운다. 도중에 죽으면 기록이 남는다. **파일을 지워도 되는지를 판정하는 곳은 복구 절차(`dashkit/ingest/recover.py`) 하나뿐이다.**

- 복구는 쓰기 잠금을 잡은 뒤에 한다. 살아 있는 작업은 모두 같은 잠금을 잡고 있으므로 잠금을 잡은 쪽이 보는 기록은 전부 끊긴 작업의 것이다.
- 관리 서비스·추가 적재는 작업 전에(공통 순서 2), 로더는 load를 시작할 때 처리한다. **지금 DB에 적재된 프로젝트의 기록만** 처리하고 다른 프로젝트 폴더는 건드리지 않는다(그 프로젝트를 다시 load할 때 처리된다).
- 종류 `add`(위에서부터 차례로 본다):

  | 남은 기록의 상태 | 처리 |
  |---|---|
  | ① 적힌 배치가 배치 기록에 있다(커밋은 됐다) | 파일은 그대로 두고 기록만 지운다 |
  | ② 배치 기록에 없고 적힌 파일이 모두 제자리에 있다 | 묶음 전체(`first`의 손 파일을 먼저, 각각 참조 순서·번호순)를 한 트랜잭션으로 반영하고 기록을 지운다(load는 어차피 전부 다시 넣으므로 기록만 지운다). 반영이 실패하면 ③ |
  | ③ 일부만 제자리에 있다 | 옮겨진 파일(`files`)을 모두 지우고 기록을 지운다. 묶음 전체가 없던 일이 된다(`first`의 손 파일은 원래 있던 것이라 지우지 않는다) |

- **커밋에 실패했거나 결과를 받지 못한 작업은 직접 치우지 않는다.** 그 접속을 닫고 새 접속으로 잠금을 다시 잡아 자기 기록에 대해 복구 절차를 돌린 결과로 답한다: 반영돼 있으면 성공(①·②), 치워졌으면 500 `commit_failed`. 잠금을 다시 잡았을 때 DB에 다른 프로젝트가 적재돼 있으면 복구하지 않고 파일과 기록을 남긴 채 409 `project_mismatch`, 잠금을 못 잡으면 500 `commit_unknown`(다음 작업이 복구한다).
- `_pending/incoming/`의 파일 가운데 기록이 가리키지 않고 하루가 지난 것은 복구가 지운다(끊긴 접수가 남긴 것).
- 읽기만 하는 `dashkit plan`은 기록을 고치지 않고 그 테이블에 "끝나지 않은 폴더 작업이 있음"(`status: pending`)을 알린다.

## DB 계정

| 계정 | 권한 | 쓰는 곳 |
|---|---|---|
| `dash_owner` | `raw`·`dash`·`dashkit_meta` 소유 | 로더, 추가 적재(관리 서비스·CLI) |
| `dash_reader` | `dash`·`dashkit_meta` SELECT | backend, Superset, plan. `raw`를 읽지 못한다 |
| `dash_inspect` | `raw`·`dash`·`dashkit_meta`의 SELECT만, 기본 읽기 전용 트랜잭션 | 관리 서비스의 읽기 경로(조회·목록) |

- `dash_inspect`는 `data-db/init/03-inspect.sh`가 만든다(새 볼륨은 initdb 때, 이미 있는 볼륨은 `make data-init`). 쓰기·역할·DB 생성 권한이 없고 `dashkit_auth`에 권한이 없다. `raw`·`dash`·`dashkit_meta`의 SELECT와 `dash.as_of()` 실행 권한은 로더가 적재할 때마다 준다(역할이 없으면 건너뛴다).
- 테스트가 고정한다: `dash_reader`가 `raw`를 읽지 못하고, `dash_inspect`가 쓰지 못하고 `dashkit_auth`를 읽지 못한다(`tests/test_ingest_db.py`).
