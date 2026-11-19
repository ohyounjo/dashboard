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
| `format: "<d3>"` | 유형마다 다르다(아래 표의 `format` 행). table은 쓰지 않음(params로) | |
| 지표 참조 문자열 `"m"` | `"m"` (데이터셋 지표 이름 그대로) | 데이터셋 `metrics`에 있어야 한다 |
| 지표 참조 `{sql, label}` | `{"expressionType":"SQL","sqlExpression":sql,"label":label}` | |

## 유형별

유형 하나가 `dashkit/charts/<viz>.yaml` 파일 하나다. 아래 표는 그 파일들에서 생성한다(`make gen`). 손으로 고치지 않는다.

- 괄호 안은 필드의 `kind`(값의 종류와 검사), 필수 여부, 생략했을 때 쓰는 값이다. 기본값이 없는 선택 필드는 생략하면 파라미터를 쓰지 않는다.
- params에 쓰는 순서: `viz_type` → 필드(선언 순서) → "함께 쓰는 파라미터" → `format` → `where`.

<!-- 생성 시작: chart-types -->
| 축약 `viz` | 이름 | `viz_type` | 축약 필드 → 파라미터 | 함께 쓰는 파라미터 |
|---|---|---|---|---|
| `area` | 영역 | `echarts_area` | `x` → `x_axis` (column, 필수)<br>`time_grain` → `time_grain_sqla` (time_grain)<br>`metrics` → `metrics` (metrics, 필수)<br>`groupby` → `groupby` (columns, 생략 시 `[]`)<br>`stacked` → `stack` (true → `"Stack"`, false → `null`) (bool)<br>`format` → `y_axis_format` (d3format) | — |
| `bar` | 막대 | `echarts_timeseries_bar` | `x` → `x_axis` (column, 필수)<br>`time_grain` → `time_grain_sqla` (time_grain)<br>`metrics` → `metrics` (metrics, 필수)<br>`groupby` → `groupby` (columns, 생략 시 `[]`)<br>`horizontal` → `orientation` (true → `"horizontal"`, false → `"vertical"`) (bool, 생략 시 `false`)<br>`format` → `y_axis_format` (d3format) | — |
| `big_number` | 숫자와 추이 | `big_number` | `metric` → `metric` (metric, 필수)<br>`time_column` → `x_axis` (column, 필수)<br>`time_grain` → `time_grain_sqla` (time_grain, 생략 시 `"P1D"`)<br>`compare_lag` → `compare_lag` (int, 생략 시 `1`)<br>`compare_suffix` → `compare_suffix` (string)<br>`format` → `y_axis_format` (d3format) | `show_trend_line: true`, `aggregation: "LAST_VALUE"` |
| `big_number_total` | 숫자 | `big_number_total` | `metric` → `metric` (metric, 필수)<br>`subtitle` → `subtitle` (string)<br>`format` → `y_axis_format` (d3format) | — |
| `box_plot` | 상자 수염 | `box_plot` | `groupby` → `groupby` (columns, 생략 시 `[]`)<br>`columns` → `columns` (columns, 필수)<br>`metrics` → `metrics` (metrics, 필수)<br>`whiskers` → `whiskerOptions` (tukey → `"Tukey"`, min_max → `"Min/max (no outliers)"`, p2_98 → `"2/98 percentiles"`, p9_91 → `"9/91 percentiles"`) (enum, 생략 시 `"tukey"`)<br>`format` → `number_format` (d3format) | — |
| `bubble` | 버블 | `bubble_v2` | `entity` → `entity` (column, 필수)<br>`series` → `series` (column)<br>`x` → `x` (metric, 필수)<br>`y` → `y` (metric, 필수)<br>`size` → `size` (metric, 필수) | — |
| `funnel` | 퍼널 | `funnel` | `groupby` → `groupby` (columns, 필수)<br>`metric` → `metric` (metric, 필수)<br>`format` → `number_format` (d3format) | `sort_by_metric: true` |
| `gauge` | 게이지 | `gauge_chart` | `metric` → `metric` (metric, 필수)<br>`groupby` → `groupby` (columns, 생략 시 `[]`)<br>`min_value` → `min_val` (int)<br>`max_value` → `max_val` (int)<br>`format` → `number_format` (d3format) | — |
| `heatmap` | 히트맵 | `heatmap_v2` | `x` → `x_axis` (column, 필수)<br>`y` → `groupby` (column, 필수)<br>`metric` → `metric` (metric, 필수)<br>`format` → `y_axis_format` (d3format) | — |
| `histogram` | 히스토그램 | `histogram_v2` | `column` → `column` (column, 필수)<br>`groupby` → `groupby` (columns, 생략 시 `[]`)<br>`bins` → `bins` (int) | — |
| `line` | 선 | `echarts_timeseries_line` | `x` → `x_axis` (column, 필수)<br>`time_grain` → `time_grain_sqla` (time_grain)<br>`metrics` → `metrics` (metrics, 필수)<br>`groupby` → `groupby` (columns, 생략 시 `[]`)<br>`format` → `y_axis_format` (d3format) | — |
| `mixed` | 혼합(두 묶음) | `mixed_timeseries` | `x` → `x_axis` (column, 필수)<br>`time_grain` → `time_grain_sqla` (time_grain)<br>`metrics` → `metrics` (metrics, 필수)<br>`groupby` → `groupby` (columns, 생략 시 `[]`)<br>`series_type` → `seriesType` (`line`, `bar`, `scatter`, `smooth`) (enum, 생략 시 `"line"`)<br>`metrics_b` → `metrics_b` (metrics, 필수)<br>`groupby_b` → `groupby_b` (columns, 생략 시 `[]`)<br>`series_type_b` → `seriesTypeB` (`line`, `bar`, `scatter`, `smooth`) (enum, 생략 시 `"bar"`)<br>`secondary_axis` → `yAxisIndexB` (true → `1`, false → `0`) (bool)<br>`format` → `y_axis_format` (d3format)<br>`format_b` → `y_axis_format_secondary` (d3format) | — |
| `pie` | 원 | `pie` | `groupby` → `groupby` (columns, 필수)<br>`metric` → `metric` (metric, 필수)<br>`donut` → `donut` (bool)<br>`format` → `number_format` (d3format) | — |
| `pivot_table` | 피벗 테이블 | `pivot_table_v2` | `rows` → `groupbyRows` (columns, 생략 시 `[]`)<br>`columns` → `groupbyColumns` (columns, 생략 시 `[]`)<br>`metrics` → `metrics` (metrics, 필수)<br>`aggregate` → `aggregateFunction` (`Sum`, `Average`, `Median`, `Minimum`, `Maximum`, `Count`) (enum, 생략 시 `"Sum"`)<br>`format` → `valueFormat` (d3format) | `metricsLayout: "COLUMNS"` |
| `radar` | 레이더 | `radar` | `groupby` → `groupby` (columns, 필수)<br>`metrics` → `metrics` (metrics, 필수)<br>`format` → `number_format` (d3format) | — |
| `sankey` | 생키 | `sankey_v2` | `source` → `source` (column, 필수)<br>`target` → `target` (column, 필수)<br>`metric` → `metric` (metric, 필수) | — |
| `scatter` | 시계열 산점 | `echarts_timeseries_scatter` | `x` → `x_axis` (column, 필수)<br>`time_grain` → `time_grain_sqla` (time_grain)<br>`metrics` → `metrics` (metrics, 필수)<br>`groupby` → `groupby` (columns, 생략 시 `[]`)<br>`format` → `y_axis_format` (d3format) | — |
| `sunburst` | 선버스트 | `sunburst_v2` | `columns` → `columns` (columns, 필수)<br>`metric` → `metric` (metric, 필수)<br>`format` → `number_format` (d3format) | — |
| `table` | 표 | `table` | `columns` → 설명 참고 (columns)<br>`groupby` → 설명 참고 (columns)<br>`metrics` → 설명 참고 (metrics)<br>`order_by` → 설명 참고 (order_by)<br>`row_limit` → 설명 참고 (int)<br>`search` → 설명 참고 (bool)<br>`format` → 쓰지 않음 (d3format)<br>원본 행 모드(`columns`): `columns` → `all_columns`, `order_by: [[c, asc\|desc]]` → `order_by_cols: ["[\"c\", true\|false]"]`(JSON 문자열, asc=true), `query_mode: "raw"`. 집계 모드(`metrics`): `groupby` → `groupby`(생략 시 `[]`), `metrics` → `metrics`, `order_by`는 지표면 `timeseries_limit_metric` + `order_desc`(첫 지표 항목만), 열이면 `order_by_cols`, `query_mode: "aggregate"`. 공통: `row_limit` → `row_limit`, `search` → `include_search`, `table_timestamp_format: "%Y-%m-%d"`. | — |
| `treemap` | 트리맵 | `treemap_v2` | `groupby` → `groupby` (columns, 필수)<br>`metric` → `metric` (metric, 필수)<br>`format` → `number_format` (d3format) | — |
| `waterfall` | 폭포 | `waterfall` | `x` → `x_axis` (column, 필수)<br>`time_grain` → `time_grain_sqla` (time_grain)<br>`metric` → `metric` (metric, 필수)<br>`groupby` → `groupby` (columns, 생략 시 `[]`)<br>`show_value` → `show_value` (bool)<br>`format` → `y_axis_format` (d3format) | — |
| `word_cloud` | 워드 클라우드 | `word_cloud` | `series` → `series` (column, 필수)<br>`metric` → `metric` (metric, 필수)<br>`rotation` → `rotation` (`square`, `flat`, `random`) (enum) | — |
<!-- 생성 끝: chart-types -->

### 유형 추가

1. `dashkit/charts/<viz>.yaml`을 만든다. 형식은 `dashkit/charts/__init__.py` 머리말에 있다.

   ```yaml
   viz: funnel                      # dashboard.yaml에서 쓰는 이름(= 파일 이름)
   viz_type: funnel                 # Superset 이름
   label: 퍼널
   fields:                          # 축약 필드 → Superset 파라미터(선언 순서대로 쓴다)
     groupby: { kind: columns, required: true }
     metric:  { kind: metric, required: true }
     format:  { kind: d3format, param: number_format }
   defaults: { sort_by_metric: true }
   ```

2. `make gen`으로 위 표를 다시 만들고 `make golden`, `make test-unit`을 돌린다.

| `kind` | 값 | 검사 |
|---|---|---|
| `column`, `columns` | 열 이름(들) | 뷰에 열이 있는지 |
| `metric`, `metrics` | 지표 이름 또는 `{ sql, label }` | 이름이면 데이터셋에 지표가 있는지 |
| `order_by` | `[[열 또는 지표 라벨, asc\|desc], …]` | 지표 라벨이 아니면 뷰에 열이 있는지 |
| `time_grain` | `P1D` `P1W` `P1M` `P3M` `P1Y` | enum |
| `bool`, `int`, `string`, `enum`, `d3format` | 값 | 형식(`min`, `max`, `values`) |

- `param`을 생략하면 필드 이름과 같은 파라미터가 된다. `default`가 있으면 생략했을 때 그 값을 쓴다.
- `bool` 필드에 `values: { true: a, false: b }`를 주면 참·거짓을 그 값으로 바꿔 쓴다.
- 필드 하나가 파라미터 하나로 대응되지 않으면 `hook: <모듈>:<함수>`로 함수를 지정한다(`dashkit/charts/hooks.py`의 `table`).
- 스키마(`contracts/dashboard.schema.json`)의 `viz` 허용 값과 유형별 정의 `v_<viz>`는 파일에 없고 로딩 때 합성한다(`dashkit.charts.extend_schema`).

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
| `metrics.<이름>: <식>` | `{metric_name: <이름>, expression: <식>, verbose_name: labels[<이름>] 또는 null}` |
| `labels.<열>: <표시 이름>` | 열의 `verbose_name`(표 머리글, 필터·범례 라벨). 없으면 null(열 이름 그대로 표시) |

## 데이터베이스

| 항목 | 값 |
|---|---|
| 파일 | `databases/dashkit.yaml` |
| `database_name` | `dashkit-<프로젝트>` (Superset에서 이름이 유일해야 하므로 프로젝트를 넣는다) |
| `sqlalchemy_uri` | `postgresql+psycopg2://dash_reader:XXXXXXXXXX@data-db:5432/<DATA_DB_NAME>` |
| 비밀번호 | import 요청 `passwords={"databases/dashkit.yaml": DATA_DB_READER_PASSWORD}` |
| 그 밖 | `expose_in_sqllab: false`, `allow_dml: false`, `allow_file_upload: false`, `uuid` = uuid5(`<프로젝트>/database/main`) |
