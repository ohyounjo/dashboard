# 설정 형식 변경 기록

`projects/<이름>/schema.yaml`과 `dashboard.yaml`의 **형식**이 바뀐 내역이다. 코드 변경 내역은 git 이력을 본다.

- 형식 버전은 두 파일 최상위의 `version`이다. 없으면 1로 본다. 이 dashkit이 읽는 버전은 `dashkit/config.py`의 `CONFIG_VERSION`이다.
- **버전을 올리는 변경**: 고치지 않은 기존 파일이 통과하지 못하게 되는 변경(키 이름·뜻이 바뀌거나 없어짐). 이때 `python -m dashkit migrate`(`make migrate`)가 파일을 고칠 수 있도록 변환을 함께 넣는다.
- **버전을 올리지 않는 변경**: 선택 키나 새 유형을 더하는 변경. 아래 "추가"에 적는다.
- 규칙의 기준은 [contracts/README.md](contracts/README.md)의 "설정 버전" 절이다.

## 버전 1 (현재)

### 정의

형식의 기준은 [contracts/schema.schema.json](contracts/schema.schema.json), [contracts/dashboard.schema.json](contracts/dashboard.schema.json)과 [contracts/README.md](contracts/README.md)의 "참조 검사" 표다. 처음 구현(커밋 `b525bce`, 2026-11-19)의 형식이 버전 1의 출발점이다.

- `schema.yaml`: 최상위 `tables`. 테이블마다 `primary_key`(열 하나 또는 복합 키), `columns`, 선택 `description`·`checks`. 열 규칙은 `type`(`text`, `integer`, `number`, `boolean`, `date`, `timestamp`, `enum`), `required`, `values`, `ref`, `min`, `max`, `pattern`.
- `dashboard.yaml`: 최상위 `app`, `datasets`, `dashboards`, `tabs`.
  - `app`: `title`, `as_of`(날짜 또는 `today`), 선택 `logo_text`·`theme`.
  - `datasets.<키>`: `table` 또는 `sql`, 선택 `metrics`·`labels`·`description`. 키마다 `dash.<키>` 뷰가 된다.
  - `dashboards.<키>`: `title`, `charts`, `layout`(행의 목록. 항목은 차트 키 또는 `{ chart, width, height }`), 선택 `filters`(값 선택 필터 `type: select` 하나뿐: `key`, `label`, `dataset`, `column`, `multi`).
  - 차트: 축약(`viz` + 유형별 필드) 또는 원본(`viz_type` + `params`). 처음의 축약 유형은 6종(`big_number_total`, `big_number`, `line`, `bar`, `pie`, `table`).
  - `tabs[]`: `key`, `label`, `icon`, `sections`. 섹션 유형은 `superset`(대시보드 임베드), `gantt`, `kanban`.

### 추가 (버전 1 안에서 호환. 기존 파일은 고치지 않아도 통과한다)

| 시점 | 추가된 것 | 기준 문서 |
|---|---|---|
| 1단계(`26b69c9`) | 형식 변화 없음. 축약 6종의 정의가 차트 유형 레지스트리(`dashkit/charts/<viz>.yaml`)로 옮겨졌다 | [contracts/chart-shorthand.md](contracts/chart-shorthand.md) |
| 2단계(`cb55abf`) | 축약 유형 16종 추가, 모두 **22종**: `area`, `mixed`, `scatter`, `bubble`, `histogram`, `box_plot`, `heatmap`, `pivot_table`, `funnel`, `gauge`, `radar`, `treemap`, `sunburst`, `sankey`, `waterfall`, `word_cloud`. 유형 목록과 필드는 레지스트리에서 생성한다 | [contracts/chart-shorthand.md](contracts/chart-shorthand.md), [reference/chart-catalog.md](reference/chart-catalog.md) |
| 3단계(`8570523`) | `layout`에 제목 `{ header }`, 글 `{ markdown }`, 대시보드 안 탭 `{ tabs: [{ label, rows }] }` | [contracts/dashboard.schema.json](contracts/dashboard.schema.json), [contracts/README.md](contracts/README.md) "참조 검사" |
| 데이터 추가 적재 1단계 | `schema.yaml` 테이블의 `mode`(`replace` 기본 · `append`)와 `on_conflict`(`error` 기본 · `skip` · `update`), 열의 `default`·`aliases`. `append` 테이블은 `data/<테이블>/`의 배치 파일도 원본이다. 지정하지 않으면 지금과 똑같다 | [contracts/schema.schema.json](contracts/schema.schema.json), [contracts/ingest.md](contracts/ingest.md) |
| 3단계 | 필터 유형 `type`에 `time_range`, `number_range`, `time_grain` 추가(`select`는 그대로다. 처음에는 `type`을 꼭 써야 했고 이때부터 생략하면 `select`다). 필터의 `default`(초기값), `parent`(상위 필터), `charts`(적용할 차트) | 위와 같음 |
| 3단계 | 대시보드의 `cross_filters`(차트를 눌러 다른 차트를 거르기. 기본 `false`) | 위와 같음 |
| 3단계 | 위젯 매니페스트: 탭 섹션 유형과 그 옵션을 `web/widgets/<유형>/widget.json`이 정한다. 기존 `gantt`·`kanban` 섹션의 형식은 그대로이고, 폴더 하나를 더하면 새 섹션 유형이 생긴다 | [contracts/widgets.md](contracts/widgets.md) |
| 4단계 | 두 파일 최상위의 `version`(선택, 없으면 1). 모르는 버전은 `<파일>:version: …` 오류. `python -m dashkit migrate`는 `version` 키가 없는 파일 맨 위에 `version: 1`을 넣는다(넣지 않아도 통과한다) | [contracts/README.md](contracts/README.md) "설정 버전" |

### 변환

없음. 버전 1 → 2가 생기면 여기에 "무엇이 왜 바뀌었고 `migrate`가 무엇을 고치는지"를 적는다.
