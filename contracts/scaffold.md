# 프로젝트 초안 만들기: 추론 결과와 추론 규칙

`make new`(`python -m dashkit new <이름> --from <CSV 폴더>`)와 Studio의 프로젝트 마법사가 함께 쓰는 계약이다. 구현은 `dashkit/scaffold.py`.

## 함수

```python
class ScaffoldError(Exception):
    errors: list[str]                      # 한 줄에 하나씩 보여 줄 이유

def read_folder(folder: Path) -> dict[str, bytes]             # 폴더 바로 아래의 *.csv(이름순)
def infer(sources: Mapping[str, bytes]) -> dict               # 파일 이름 → 내용. 추론 결과를 돌려준다. 파일을 쓰지 않는다
def infer_tables(sources) -> list[dict]                       # 추론 결과의 tables만
def suggest_dashboard(tables: list[dict]) -> dict             # tables → 추론 결과의 dashboard. tables를 고친 뒤 다시 부를 수 있다
def problems(tables: list[dict]) -> list[str]                 # 이대로는 만들 수 없는 이유
def render(name, inference, sources) -> dict[str, bytes]      # → {"schema.yaml", "dashboard.yaml", "data/<테이블>.csv"}. 파일을 쓰지 않는다
def verify(name, files) -> None                               # 임시 폴더에서 load_project + validate_csv. 실패하면 ScaffoldError
def write_project(name, files, projects_dir=None) -> Path     # verify 후 projects/<name>/에 쓴다. 이미 있으면 ScaffoldError
def create_project(name, sources, projects_dir=None) -> tuple[Path, dict]   # infer → render → write_project
```

- 프로젝트 이름은 `load_project`와 같은 형식(`[a-z0-9][a-z0-9_-]*`)만 받는다.
- `sources`의 키는 경로 문자(`/`, `\`)가 없는 파일 이름이다. `render`에도 `infer`에 준 것과 같은 `sources`를 준다(추론 결과에는 CSV 내용이 들어 있지 않다).
- 읽을 수 없는 CSV(머리글 없음, 행의 값 개수가 머리글과 다름, CSV 문법 오류, UTF-8도 CP949도 아님)는 `infer`가 모든 파일의 오류를 모아 `ScaffoldError`로 알린다.
- 기본 키를 정할 수 없는 테이블은 오류가 아니라 `problems`에 담아 돌려준다(사람이 고칠 수 있게). `problems`가 남아 있으면 `render`가 거부한다.
- `write_project`는 검증을 통과하지 못하거나 프로젝트가 이미 있으면 아무것도 쓰지 않는다. 검증은 시스템 임시 폴더에서 하고 `projects/` 아래에 임시 파일을 만들지 않는다.

Studio API(보강설계서 6-4) 대응: `POST /studio/api/projects/infer` = `infer`, `POST /studio/api/projects` = (고친 추론 결과로) `render` → `write_project`.

## 추론 결과(JSON)

JSON으로 직렬화했다가 되돌려도 `render` 결과가 같다. 아래에서 "근거"는 사람에게 보여 줄 글이고, "확신"은 `high`·`medium`·`low` 중 하나다.

```jsonc
{
  "format": 1,
  "tables": [
    {
      "name": "orders",                 // 테이블 키(= data/orders.csv = raw.orders). 고칠 수 있음
      "source": "Orders 2024.csv",      // sources의 키. 고치지 않는다
      "label": "Orders 2024",           // 파일 이름에서 확장자를 뺀 것. 대시보드·탭 제목에 쓴다
      "rows": 120,
      "primary_key": ["order_id"],      // 열 하나 또는 복합 키. 비어 있으면 만들 수 없음
      "primary_key_basis": "값이 유일하고 비어 있지 않은 첫 열",
      "primary_key_confidence": "high",
      "notes": ["테이블 이름을 'Orders 2024'에서 바꿈: …"],
      "columns": [                      // CSV 머리글과 같은 순서, 같은 개수
        {
          "name": "ship_status",        // 스키마의 열 이름. 고칠 수 있음
          "source": "Ship Status",      // CSV의 원래 머리글(앞뒤 공백 제거). 고치지 않는다
          "type": "enum",               // text | integer | number | boolean | date | timestamp | enum
          "required": true,             // 빈 값이 하나도 없으면 true
          "values": ["sent", "packing"],        // enum일 때만. 처음 나온 순서
          "ref": "customers.id",                // 있을 때만
          "ref_basis": "…", "ref_confidence": "high",
          "basis": "text 열의 고유값이 2개(값 있는 120행의 절반 이하)",   // 유형의 근거
          "confidence": "high",
          "notes": ["열 이름을 'Ship Status'에서 바꿈: …", "ref 후보였으나 만들지 않음. …"],
          "stats": { "rows": 120, "empty": 0, "distinct": 2 }      // 숫자 열은 min, max도
        }
      ]
    }
  ],
  "dashboard": {                        // dashboard.yaml과 같은 모양(app은 선택)
    "app": { "title": "…", "as_of": "today" },      // 생략하면 title = 프로젝트 이름, as_of = today
    "datasets": { "orders": { "table": "orders", "labels": { … }, "metrics": { "row_count": "COUNT(*)" } } },
    "dashboards": { "orders": { "title": "…", "charts": [ { "key": "…", "viz": "…", …, "note": "근거" } ], "layout": [ … ] } },
    "tabs": [ { "key": "orders", "label": "…", "icon": "table", "sections": [ { "type": "superset", "dashboard": "orders" }, { "type": "kanban", …, "note": "근거" } ] } ]
  },
  "problems": ["dupes.csv: 기본 키를 정할 수 없음(…)"]     // 비어 있어야 만들 수 있다. 정보용이고, render는 tables에서 다시 계산한다
}
```

- `render`가 읽는 열 항목: `name`, `source`, `type`, `required`, `values`, `ref`, `ref_basis`, `basis`, `notes`, 그리고 사람이 더할 수 있는 `min`, `max`, `pattern`, `description`. `stats`, `confidence`는 표시용이고 `suggest_dashboard`만 `stats`의 `min`·`max`를 본다.
- 차트와 섹션의 `note`는 dashboard.yaml에 쓰지 않고 그 항목 위의 주석이 된다. 나머지 키는 그대로 쓴다.
- `tables`를 고쳤으면(이름, 유형, 열 추가·삭제) `suggest_dashboard(tables)`로 `dashboard`를 다시 만든다. 그러지 않으면 옛 열 이름을 가리키는 차트가 남아 `verify`에서 걸린다.
- 열을 빼거나 순서를 바꾸는 것은 지원하지 않는다(CSV의 열과 개수·순서가 같아야 한다).

## 만드는 파일

- `schema.yaml`, `dashboard.yaml`: 최상위에 `version: 1`. 추론한 곳마다 `# 추론: 확인 필요 - <근거>` 주석이 붙는다(열, `primary_key`, 테이블·열의 `notes`, 차트, 위젯 섹션).
- `data/<테이블>.csv`: 열 이름이 머리글과 모두 같고 UTF-8이면 원본 바이트 그대로, 아니면 머리글을 고친 UTF-8 사본(값은 그대로, 빈 줄 제거, 줄 끝 `\n`).

## 추론 규칙

CSV를 읽는 방식은 `dashkit/loader/validate.py`와 같다: UTF-8(BOM 허용), 머리글은 앞뒤 공백 제거, 빈 줄은 건너뜀, 값은 다듬지 않음, 빈 문자열만 NULL.

### 이름

테이블 이름(파일 이름에서 확장자를 뺀 것)과 열 이름(머리글)을 `^[a-z][a-z0-9_]{0,62}$`이고 예약어가 아닌 이름으로 바꾼다.

1. 유니코드 호환 정규화(NFKC), camelCase를 `_`로 나눔, 소문자로
2. 영문·숫자가 아닌 글자(공백, 기호, 한글 등)는 `_`. 앞뒤와 겹친 `_`는 지움
3. 남는 글자가 없으면 `column_<자리>`·`table_<자리>`(자리는 1부터)
4. 숫자로 시작하면 `c_`·`t_`를 앞에 붙임
5. SQL 예약어(`dashkit/reserved_words.py`)면 `_col`·`_tbl`을 뒤에 붙임
6. 63자로 자르고, 같은 이름이 있으면 `_2`, `_3`…

원래 머리글은 데이터셋의 `labels`에 넣어 화면에는 원래 이름이 보이게 한다. 원래 파일 이름은 대시보드 제목과 탭 라벨이 된다.

### 유형

비어 있지 않은 값이 모두 맞을 때만 그 유형으로 본다. 표기는 검증기가 받는 것보다 넓지 않다.

| 유형 | 조건 |
|---|---|
| `integer` | `[+-]?\d+`이고 bigint 범위 안. 0으로 시작하는 값(`007`)이 하나라도 있으면 코드로 보고 `text`. 범위를 벗어나면 `number` |
| `number` | `[+-]?(\d+\.?\d*\|\.\d+)([eE][+-]?\d+)?` (공백, `_`, `inf`, `nan`, 천 단위 쉼표는 안 됨) |
| `boolean` | 대소문자 무시하고 `true`/`false` |
| `date` | `YYYY-MM-DD`이고 실제로 있는 날짜 |
| `timestamp` | `YYYY-MM-DD[T ]HH:MM[:SS[.ffffff]][Z\|±HH:MM]`이고 `datetime.fromisoformat`이 받는 값. 확신 `medium` |
| `text` | 그 밖. 값이 모두 비어 있는 열도 `text`(확신 `low`) |

`required`는 빈 값이 하나도 없을 때 `true`다. `min`·`max`·`pattern`은 추론하지 않는다.

### enum

`text` 열(기본 키와 ref 열 제외) 중에서

- 고유값이 12개 이하이고
- 고유값 수가 값 있는 행의 절반 이하이고(값이 평균 두 번 이상 나옴)
- 가장 긴 값이 40자 이하

이면 `enum`이고 `values`는 처음 나온 순서다. 이름의 마지막 토막이 `status`·`state`·`step`인 열("상태 꼴")은 절반 조건 대신 "같은 값이 한 번이라도 반복됨"이면 된다(행이 적은 표의 상태 열. 확신 `low`).

### primary_key

1. 앞에서부터, 빈 값이 없고 값이 모두 다른 첫 열. `number`·`boolean` 열은 키 후보에서 뺀다. 중복은 검증기처럼 파싱한 값으로 본다(`1`과 `+1`은 같다).
2. 없으면 앞의 8개 열 안에서, 함께 쓰면 유일한 첫 두 열 조합(확신 `low`).
3. 그래도 없거나 데이터 행이 없으면 `primary_key`를 비우고 `problems`에 적는다. `make new`는 안내를 출력하고 아무것도 만들지 않는다(종료 코드 1).

### ref

기본 키가 아닌 열 중에서

- 이름이 `<테이블>_id` 꼴: `_id`를 뺀 이름과 테이블 이름을 단수형으로 맞춰 본다(`s`, `es`, `ies` 처리. `category_id` → `categories`). 확신 `high`
- 또는 다른 테이블의 단일 열 기본 키와 이름이 같음(예: `shop_code`). 확신 `medium`

인 열이 후보다. 대상이 단일 열 기본 키를 갖고, 두 열의 type이 같고, 값이 모두 대상에 있고, 참조가 순환하지 않을 때만 `ref`로 만든다. 아니면 만들지 않고 이유를 열의 `notes`(→ schema.yaml의 주석)에 남긴다. 자기 테이블을 가리키는 ref는 만들지 않는다.

### dashboard.yaml 초안

테이블마다 데이터셋(`table:` 축약, 지표 `row_count`), Superset 대시보드 하나, 웹앱 탭 하나를 만든다.

| 열 구성 | 초안 | 고르는 열 |
|---|---|---|
| 테이블마다 | 건수 `big_number_total`, 원본 행 `table` | 표는 앞의 12개 열, 기본 키 오름차순 |
| date 열 + 숫자 열 | 주별 합계 `line`(`time_grain: P1W`) | 첫 date 열(없으면 첫 timestamp 열), 첫 숫자 열. 지표 `sum_<열>` |
| enum 열 | 값별 건수 `bar` | 앞의 2개 enum 열 |
| enum 열 + 숫자 열 | 값별 합계 `pie` | 첫 enum 열, 첫 숫자 열 |
| date 열 2개(시작·종료 꼴 이름) | `gantt` 섹션 | 아래 |
| 상태 꼴 enum 열 | `kanban` 섹션 | 첫 상태 꼴 enum 열. 500행 이하인 테이블만 |

- "숫자 열"은 `integer`·`number` 열 중 기본 키·ref가 아니고 이름의 마지막 토막이 `id`·`no`·`code`·`key`·`seq`·`num`·`idx`가 아닌 열이다.
- 차트는 테이블마다 최대 6개다(건수 1, 선 1, 막대 2, 원 1, 표 1).
- 배치(12칸): 첫 행 `[건수 3칸, 선(없으면 첫 막대) 9칸]`, 둘째 행에 남은 막대·원을 고르게, 마지막 행에 표(높이 60). 선도 막대도 없으면 `[건수 3칸, 표 9칸]` 한 행.
- gantt: 이름 토막에 시작 표식(`start`, `begin`, `from`, `opened`, `launch` 등)이 있는 date 열과 종료 표식(`end`, `finish`, `until`, `due`, `to`, `close`, `closed`, `deadline` 등)이 있는 date 열의 쌍. 표식을 뺀 나머지 이름이 같은 쌍(`plan_start`·`plan_end`)을 먼저 고른다. `fields.progress`는 이름에 `progress`·`pct`·`percent`가 있고 값이 0~100인 숫자 열, `fields.status`는 첫 상태 꼴 enum 열. timestamp 열로는 만들지 않는다.
- kanban: `columns`는 enum 값 순서 그대로(`label` = 값), `card.chips`는 다른 enum 열 2개까지.
- gantt `fields.label`과 kanban `card.title`: 이름 토막에 `name`·`title`·`label`·`subject`가 있는 text 열 → 첫 text 열 → 기본 키.
- 탭 아이콘: gantt가 있으면 `calendar`, kanban만 있으면 `list`, 아니면 `table`.

## 운용 화면의 새 테이블

관리 서비스의 `POST /api/manage/tables/infer`·`POST /tables`([manage.md](manage.md) "구조 변경")도 이 추론을 쓴다(`dashkit/ingest/newtable.py`). 다른 점만 적는다.

- CSV 하나를 `infer_tables`로 추론한다. 이름이 이미 있는 테이블이나 남아 있는 데이터 파일(지운 테이블의 `data/<이름>.csv`·`data/<이름>/`)과 겹치면 `_2`, `_3`…을 붙인다.
- **이미 있는 테이블의 단일 열 기본 키도 `ref` 후보**로 본다: 이름이 그 기본 키와 같거나(`id`는 빼고) `<테이블>_id`·`<단수형>_id`이고 유형이 같으면 `ref`와 `ref_basis`를 채운다. 값 대조는 apply가 한다(없는 값이면 load가 실패하고 모든 파일이 되돌아간다).
- 열의 `source`(원래 머리글)가 열 이름과 다르면 그 열의 **`aliases`에 남긴다**. 같은 모양의 파일을 다음에 올리면 머리글이 저절로 맞는다.
- 방식은 기본 `append`(충돌 정책 `error`)이고 `replace`도 고를 수 있다. 첫 파일은 사람이 고친 정의로 접수(유형·필수·기본 키 검증)한 정규형이다. 규칙을 통과하지 못한 행이 있으면 422 `invalid_rows`(+`errors`)이고 아무것도 쓰지 않는다.
- 데이터셋(`table:`, 기본 켬)을 함께 만든다. 초안 그래프를 고르면 위 "dashboard.yaml 초안"의 Superset 대시보드 하나와 그것을 담은 웹앱 탭 하나를 더한다(위젯 섹션은 만들지 않는다. 새 탭은 그룹이 허용하기 전에는 `user` 등급에게 보이지 않는다).
