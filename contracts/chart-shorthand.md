# 차트 축약 문법 → Superset 6.1.0 파라미터 대응표

근거: `contracts/superset-notes.md` 7절(화면 저장 결과를 export하고, 최소 params 번들로 guest 렌더링 확인). 화면에서 저장한 원본은 `contracts/superset-6.1.0-export/charts/`에 있다.

## 생성 순서

1. 공통 파라미터를 만든다: `viz_type`, `datasource`(`"<dataset id>__table"`는 import가 채우므로 쓰지 않는다).
2. 아래 표의 축약 필드를 대응 파라미터로 바꾼다. 생략한 선택 필드는 **쓰지 않는다**(Superset 기본값을 따른다). 단, 표의 "기본값" 열에 값이 있으면 그 값을 쓴다.
3. `params`를 **마지막에 얕게 덮어쓴다**: `final = {**generated, **chart.params}`.
4. `query_context`는 만들지 않는다(대시보드와 guest 렌더링에 필요 없음. 지표 정합성 테스트는 `POST /api/v1/chart/data`로 조회한다. Plan 결정).

`viz_type`(원본) 차트는 1~2를 건너뛰고 `{"viz_type": <viz_type>, **params}`만 쓴다.

## 공통 필드

| 축약 필드 | Superset 파라미터 | 비고 |
|---|---|---|
| `title` | 차트 YAML `slice_name` | params 아님 |
| `dataset` | 차트 YAML `dataset_uuid` = uuid5(`<프로젝트>/dataset/<키>`) | |
| `where: "<SQL>"` | `adhoc_filters: [{"expressionType":"SQL","clause":"WHERE","sqlExpression":"<SQL>"}]` | |
| `format: "<d3>"` | big_number_total, big_number → `y_axis_format` / line, bar → `y_axis_format` / pie → `number_format` / table → 쓰지 않음(params로) | |
| 지표 참조 문자열 `"m"` | `"m"` (데이터셋 지표 이름 그대로) | 데이터셋 `metrics`에 있어야 한다 |
| 지표 참조 `{sql, label}` | `{"expressionType":"SQL","sqlExpression":sql,"label":label}` | |

## 유형별

| 축약 `viz` | `viz_type` | 축약 필드 → 파라미터 | 기본값으로 함께 쓰는 파라미터 |
|---|---|---|---|
| `big_number_total` | `big_number_total` | `metric` → `metric`<br>`subtitle` → `subheader` | — |
| `big_number` | `big_number` | `metric` → `metric`<br>`time_column` → `x_axis`<br>`time_grain` → `time_grain_sqla`<br>`compare_lag` → `compare_lag`<br>`compare_suffix` → `compare_suffix` | `time_grain_sqla: "P1D"`(생략 시), `compare_lag: 1`(생략 시), `show_trend_line: true`, `aggregation: "LAST_VALUE"` |
| `line` | `echarts_timeseries_line` | `x` → `x_axis`<br>`time_grain` → `time_grain_sqla`<br>`metrics` → `metrics`<br>`groupby` → `groupby` | `groupby: []`(생략 시) |
| `bar` | `echarts_timeseries_bar` | `x` → `x_axis`<br>`time_grain` → `time_grain_sqla`<br>`metrics` → `metrics`<br>`groupby` → `groupby`<br>`horizontal: true` → `orientation: "horizontal"` | `groupby: []`, `orientation: "vertical"` |
| `pie` | `pie` | `groupby` → `groupby`<br>`metric` → `metric`<br>`donut` → `donut` | — |
| `table` (원본 행) | `table` | `columns` → `all_columns`<br>`order_by: [[c, asc\|desc]]` → `order_by_cols: ["[\"c\", true\|false]"]`(JSON 문자열, asc=true)<br>`row_limit` → `row_limit`<br>`search` → `include_search` | `query_mode: "raw"` |
| `table` (집계) | `table` | `groupby` → `groupby`<br>`metrics` → `metrics`<br>`order_by` → 지표면 `timeseries_limit_metric` + `order_desc`, 열이면 `order_by_cols`<br>`row_limit` → `row_limit`<br>`search` → `include_search` | `query_mode: "aggregate"`, `groupby: []`(생략 시) |

`time_grain`은 x 열이 date/timestamp일 때만 의미가 있다. 범주 열(text)이면 쓰지 않는다(쓰면 `make check`가 경고하지 않고 Superset이 무시한다).

## 대시보드와 필터

| 설정 | 번들 |
|---|---|
| `dashboards.<키>.title` | `dashboard_title`, `slug: "<프로젝트>-<키>"`(`_`는 `-`로), `uuid` = uuid5(`<프로젝트>/dashboard/<키>`), `published: true` |
| `layout` 행 | `ROW-<i>` 노드, `meta.background: BACKGROUND_TRANSPARENT` |
| 행의 차트 | `CHART-<키>` 노드, `meta: {chartId: 0, uuid, sliceName, width, height}`. `chartId: 0`은 자리표시(필수). 너비가 없는 항목은 남은 칸(12 - 지정 너비 합)을 균등 분할하고 나머지는 앞 항목부터 1칸씩 더 준다. 높이 기본 50 |
| `filters[]` | `metadata.native_filter_configuration[]`: `id: "NATIVE_FILTER-<키>"`, `name: label`, `filterType: "filter_select"`, `targets: [{column:{name}, datasetUuid}]`, `controlValues.multiSelect: multi`, `scope: {rootPath:[ROOT_ID], excluded:[]}`. `chartsInScope`는 쓰지 않는다 |
| 대시보드 메타 기본 | `metadata: {native_filter_configuration, color_scheme: "", cross_filters_enabled: false, refresh_frequency: 0}` |

## 데이터셋

| 설정 | 번들 |
|---|---|
| `datasets.<키>` | `table_name: <키>`, `schema: dash`, `sql: null`, `uuid` = uuid5(`<프로젝트>/dataset/<키>`), `database_uuid` |
| 열 | `information_schema.columns`에서 읽은 뷰 열. `type`은 Postgres 타입 대문자, date/timestamp면 `is_dttm: true`, 모두 `groupby/filterable/is_active: true` |
| `main_dttm_col` | 뷰의 첫 date/timestamp 열(없으면 null) |
| `metrics.<이름>: <식>` | `{metric_name: <이름>, expression: <식>, verbose_name: null}` |

## 데이터베이스

| 항목 | 값 |
|---|---|
| 파일 | `databases/dashkit.yaml` |
| `database_name` | `dashkit-<프로젝트>` (Superset에서 이름이 유일해야 하므로 프로젝트를 넣는다) |
| `sqlalchemy_uri` | `postgresql+psycopg2://dash_reader:XXXXXXXXXX@data-db:5432/<DATA_DB_NAME>` |
| 비밀번호 | import 요청 `passwords={"databases/dashkit.yaml": DATA_DB_READER_PASSWORD}` |
| 그 밖 | `expose_in_sqllab: false`, `allow_dml: false`, `allow_file_upload: false`, `uuid` = uuid5(`<프로젝트>/database/main`) |
