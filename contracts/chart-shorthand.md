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
| 축약 `viz` | 이름 | `viz_type` | 축약 필드 → 파라미터 | 함께 쓰는 파라미터 | 한 쿼리의 열·지표(중복 라벨 검사) |
|---|---|---|---|---|---|
| `area` | 영역 | `echarts_area` | `x` → `x_axis` (column, 필수)<br>`time_grain` → `time_grain_sqla` (time_grain)<br>`metrics` → `metrics` (metrics, 필수)<br>`groupby` → `groupby` (columns, 생략 시 `[]`)<br>`stacked` → `stack` (true → `"Stack"`, false → `null`) (bool)<br>`format` → `y_axis_format` (d3format) | — | `x`·`metrics`·`groupby` |
| `bar` | 막대 | `echarts_timeseries_bar` | `x` → `x_axis` (column, 필수)<br>`time_grain` → `time_grain_sqla` (time_grain)<br>`metrics` → `metrics` (metrics, 필수)<br>`groupby` → `groupby` (columns, 생략 시 `[]`)<br>`horizontal` → `orientation` (true → `"horizontal"`, false → `"vertical"`) (bool, 생략 시 `false`)<br>`format` → `y_axis_format` (d3format) | — | `x`·`metrics`·`groupby` |
| `big_number` | 숫자와 추이 | `big_number` | `metric` → `metric` (metric, 필수)<br>`time_column` → `x_axis` (column, 필수)<br>`time_grain` → `time_grain_sqla` (time_grain, 생략 시 `"P1D"`)<br>`compare_lag` → `compare_lag` (int, 생략 시 `1`)<br>`compare_suffix` → `compare_suffix` (string)<br>`format` → `y_axis_format` (d3format) | `show_trend_line: true`, `aggregation: "LAST_VALUE"` | `metric`·`time_column` |
| `big_number_total` | 숫자 | `big_number_total` | `metric` → `metric` (metric, 필수)<br>`subtitle` → `subtitle` (string)<br>`format` → `y_axis_format` (d3format) | — | `metric` |
| `box_plot` | 상자 수염 | `box_plot` | `groupby` → `groupby` (columns, 생략 시 `[]`)<br>`columns` → `columns` (columns, 필수)<br>`metrics` → `metrics` (metrics, 필수)<br>`whiskers` → `whiskerOptions` (tukey → `"Tukey"`, min_max → `"Min/max (no outliers)"`, p2_98 → `"2/98 percentiles"`, p9_91 → `"9/91 percentiles"`) (enum, 생략 시 `"tukey"`)<br>`format` → `number_format` (d3format) | — | `groupby`·`columns`·`metrics` |
| `bubble` | 버블 | `bubble_v2` | `entity` → `entity` (column, 필수)<br>`series` → `series` (column)<br>`x` → `x` (metric, 필수)<br>`y` → `y` (metric, 필수)<br>`size` → `size` (metric, 필수) | — | `entity`·`series`·`x`·`y`·`size` |
| `funnel` | 퍼널 | `funnel` | `groupby` → `groupby` (columns, 필수)<br>`metric` → `metric` (metric, 필수)<br>`format` → `number_format` (d3format) | `sort_by_metric: true` | `groupby`·`metric`<br>같은 열은 합침 |
| `gauge` | 게이지 | `gauge_chart` | `metric` → `metric` (metric, 필수)<br>`groupby` → `groupby` (columns, 생략 시 `[]`)<br>`min_value` → `min_val` (int)<br>`max_value` → `max_val` (int)<br>`format` → `number_format` (d3format) | — | `metric`·`groupby`<br>같은 열은 합침 |
| `heatmap` | 히트맵 | `heatmap_v2` | `x` → `x_axis` (column, 필수)<br>`y` → `groupby` (column, 필수)<br>`metric` → `metric` (metric, 필수)<br>`format` → `y_axis_format` (d3format) | — | `x`·`y`·`metric` |
| `histogram` | 히스토그램 | `histogram_v2` | `column` → `column` (column, 필수)<br>`groupby` → `groupby` (columns, 생략 시 `[]`)<br>`bins` → `bins` (int) | — | `column`·`groupby` |
| `line` | 선 | `echarts_timeseries_line` | `x` → `x_axis` (column, 필수)<br>`time_grain` → `time_grain_sqla` (time_grain)<br>`metrics` → `metrics` (metrics, 필수)<br>`groupby` → `groupby` (columns, 생략 시 `[]`)<br>`format` → `y_axis_format` (d3format) | — | `x`·`metrics`·`groupby` |
| `mixed` | 혼합(두 묶음) | `mixed_timeseries` | `x` → `x_axis` (column, 필수)<br>`time_grain` → `time_grain_sqla` (time_grain)<br>`metrics` → `metrics` (metrics, 필수)<br>`groupby` → `groupby` (columns, 생략 시 `[]`)<br>`series_type` → `seriesType` (`line`, `bar`, `scatter`, `smooth`) (enum, 생략 시 `"line"`)<br>`metrics_b` → `metrics_b` (metrics, 필수)<br>`groupby_b` → `groupby_b` (columns, 생략 시 `[]`)<br>`series_type_b` → `seriesTypeB` (`line`, `bar`, `scatter`, `smooth`) (enum, 생략 시 `"bar"`)<br>`secondary_axis` → `yAxisIndexB` (true → `1`, false → `0`) (bool)<br>`format` → `y_axis_format` (d3format)<br>`format_b` → `y_axis_format_secondary` (d3format) | — | `x`·`groupby`·`metrics`<br>`x`·`groupby_b`·`metrics_b` |
| `pie` | 원 | `pie` | `groupby` → `groupby` (columns, 필수)<br>`metric` → `metric` (metric, 필수)<br>`donut` → `donut` (bool)<br>`format` → `number_format` (d3format) | — | `groupby`·`metric`<br>같은 열은 합침 |
| `pivot_table` | 피벗 테이블 | `pivot_table_v2` | `rows` → `groupbyRows` (columns, 생략 시 `[]`)<br>`columns` → `groupbyColumns` (columns, 생략 시 `[]`)<br>`metrics` → `metrics` (metrics, 필수)<br>`aggregate` → `aggregateFunction` (`Sum`, `Average`, `Median`, `Minimum`, `Maximum`, `Count`) (enum, 생략 시 `"Sum"`)<br>`format` → `valueFormat` (d3format) | `metricsLayout: "COLUMNS"` | `rows`·`columns`·`metrics`<br>같은 열은 합침 |
| `radar` | 레이더 | `radar` | `groupby` → `groupby` (columns, 필수)<br>`metrics` → `metrics` (metrics, 필수)<br>`format` → `number_format` (d3format) | — | `groupby`·`metrics`<br>같은 열은 합침 |
| `sankey` | 생키 | `sankey_v2` | `source` → `source` (column, 필수)<br>`target` → `target` (column, 필수)<br>`metric` → `metric` (metric, 필수) | — | `source`·`target`·`metric` |
| `scatter` | 시계열 산점 | `echarts_timeseries_scatter` | `x` → `x_axis` (column, 필수)<br>`time_grain` → `time_grain_sqla` (time_grain)<br>`metrics` → `metrics` (metrics, 필수)<br>`groupby` → `groupby` (columns, 생략 시 `[]`)<br>`format` → `y_axis_format` (d3format) | — | `x`·`metrics`·`groupby` |
| `sunburst` | 선버스트 | `sunburst_v2` | `columns` → `columns` (columns, 필수)<br>`metric` → `metric` (metric, 필수)<br>`format` → `number_format` (d3format) | — | `columns`·`metric`<br>같은 열은 합침 |
| `table` | 표 | `table` | `columns` → 설명 참고 (columns)<br>`groupby` → 설명 참고 (columns)<br>`metrics` → 설명 참고 (metrics)<br>`order_by` → 설명 참고 (order_by)<br>`row_limit` → 설명 참고 (int)<br>`search` → 설명 참고 (bool)<br>`format` → 쓰지 않음 (d3format)<br>원본 행 모드(`columns`): `columns` → `all_columns`, `order_by: [[c, asc\|desc]]` → `order_by_cols: ["[\"c\", true\|false]"]`(JSON 문자열, asc=true), `query_mode: "raw"`. 집계 모드(`metrics`): `groupby` → `groupby`(생략 시 `[]`), `metrics` → `metrics`, `order_by`는 지표면 `timeseries_limit_metric` + `order_desc`(첫 지표 항목만), 열이면 `order_by_cols`, `query_mode: "aggregate"`. 공통: `row_limit` → `row_limit`, `search` → `include_search`, `table_timestamp_format: "%Y-%m-%d"`. | — | `columns`·`groupby`·`metrics`<br>같은 열은 합침 |
| `treemap` | 트리맵 | `treemap_v2` | `groupby` → `groupby` (columns, 필수)<br>`metric` → `metric` (metric, 필수)<br>`format` → `number_format` (d3format) | — | `groupby`·`metric`<br>같은 열은 합침 |
| `waterfall` | 폭포 | `waterfall` | `x` → `x_axis` (column, 필수)<br>`time_grain` → `time_grain_sqla` (time_grain)<br>`metric` → `metric` (metric, 필수)<br>`groupby` → `groupby` (columns, 생략 시 `[]`)<br>`show_value` → `show_value` (bool)<br>`format` → `y_axis_format` (d3format) | — | `x`·`metric`·`groupby` |
| `word_cloud` | 워드 클라우드 | `word_cloud` | `series` → `series` (column, 필수)<br>`metric` → `metric` (metric, 필수)<br>`rotation` → `rotation` (`square`, `flat`, `random`) (enum) | — | `series`·`metric` |
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
- `query`는 Superset 화면이 그 유형으로 보내는 쿼리의 모양이다. 위 표의 마지막 열이 이것이고, `make check`의 **중복 라벨 검사**가 쓴다(규칙과 실측은 `superset-notes.md` 14절).
  - `groups: [[필드, …], …]`: 한 쿼리에 함께 들어가는 필드. 생략하면 `kind`가 `column`·`columns`·`metric`·`metrics`인 필드 전부가 쿼리 하나다. 쿼리를 둘 보내는 유형(`mixed`)만 쓴다.
  - `merge_columns: true`: 화면이 같은 열을 하나로 합쳐 보내는 유형(같은 열을 두 번 넣어도 오류가 나지 않는다). 생략하면 `false`.
  - 새 유형을 더할 때는 `x`와 `groupby`에 같은 열을 넣은 차트, `groupby`에 같은 열을 두 번 넣은 차트를 테스트 스택에서 그려 보고 정한다(14절의 방법).

#### 중복 라벨 검사

한 쿼리 안에서 열·지표의 **Superset 라벨**이 겹치면 Superset이 차트 자리에 `Duplicate column/metric labels` 오류를 그린다. `make check`(그리고 같은 검사를 쓰는 Studio·관리 서비스의 저장)는 이것을 `dashboard.yaml:<차트 경로>.<필드>: 열·지표 라벨 '<라벨>'가 한 쿼리에서 겹침(<겹친 자리들>). Superset이 차트 자리에 "Duplicate column/metric labels" 오류를 냄`으로 보고한다. 경로는 겹친 자리 가운데 마지막이다.

| 항목 | Superset 라벨 |
|---|---|
| 열(`column`·`columns`) | 열 이름 그대로. 데이터셋 `labels`의 표시 이름은 쓰지 않는다 |
| 데이터셋 지표 이름 | 데이터셋 `labels`에 표시 이름이 있으면 **표시 이름**, 없으면 이름 |
| `{ sql, label }` 지표 | `label` |

- 같은 저장 지표 반복과 같은 라벨·동일 SQL 정의의 adhoc 지표 반복은 허용한다. 같은 라벨에 서로 다른 SQL 정의 또는 저장 지표와 adhoc 지표가 있으면 화면이 뒤의 지표를 보내지 않으므로, 제거될 항목의 YAML 경로와 두 자리를 `ConfigError`로 보고한다. SQL 정의 비교는 전달되는 식 문자열을 기준으로 하며 SQL의 의미상 동등성은 추론하지 않는다. 확인하지 않은 SIMPLE 등 원본 객체의 서로 다른 정의는 제거 오류로 판정하지 않는다.
- 제거 후에도 서버 라벨이 겹치면 별도로 위의 `Duplicate column/metric labels` 오류를 보고한다. `mixed`의 두 묶음은 각각 검사하며 묶음 사이의 같은 라벨은 허용한다.
- `params`는 번들과 같이 마지막에 얕게 덮어쓰고, 최종 `viz_type`을 기준으로 검사한다. 값이 `params`에서 왔으면 오류 경로도 `params`를 가리킨다. 지원되지 않는 구성은 검사를 생략하며, `make check` 성공은 모든 원본 차트의 정상 동작을 보장하지 않는다.

| 원본 유형 | 검사 범위 |
|---|---|
| 레지스트리에서 유일하며 hook이 없는 `viz_type` | 해당 유형의 쿼리 선언과 파라미터. 예: `echarts_timeseries_bar`, `pie`, `mixed_timeseries` |
| `table` | `query_mode: raw`는 `all_columns`, `aggregate`는 `groupby`, `metrics`, `percent_metrics`; 생략은 `groupby`, `all_columns`, `metrics`, `percent_metrics`. 문자열 열과 저장 지표 또는 라벨이 있는 SQL 지표만 확인한다. 같은 열은 합치고 정렬은 라벨에 넣지 않는다. 알 수 없는 모드와 그 밖의 객체는 검사하지 않는다 |
| `graph_chart`, `tree_chart`, `gantt_chart` | 갤러리와 같은 문자열 열과 저장 지표 또는 라벨이 있는 SQL 단일 지표 구성. 같은 열은 합친다. 다른 형태는 검사하지 않는다 |
| `chord`, `world_map`, `country_map` 및 그 밖의 유형 | 검사하지 않는다. 앞의 세 유형은 기본 guest 역할에서 렌더링되지 않는다([카탈로그](../reference/chart-catalog.md#4-3-웹앱-화면에서-그려지지-않는-원본-유형)) |

`time_grain`은 x 열이 date/timestamp일 때만 의미가 있다. 범주 열(text)이면 쓰지 않는다(쓰면 `make check`가 경고하지 않고 Superset이 무시한다).

## 대시보드와 필터

근거: `contracts/superset-notes.md` 2절(번들 형식 함정), 12절(배치 노드, 필터 유형·기본값·연쇄·적용 범위). 견본은 `projects/chart-gallery`의 `shelf_blocks` 대시보드다.

| 설정 | 번들 |
|---|---|
| `dashboards.<키>.title` | `dashboard_title`, `slug: "<프로젝트>-<키>"`(`_`는 `-`로), `uuid` = uuid5(`<프로젝트>/dashboard/<키>`), `published: true` |
| `cross_filters: true` | `metadata.cross_filters_enabled: true`(생략 시 `false`). 차트를 누르면 같은 대시보드의 다른 차트가 걸러진다 |
| 대시보드 메타 기본 | `metadata: {native_filter_configuration, color_scheme: "", cross_filters_enabled, refresh_frequency: 0}` |

### 배치(`layout`)

`layout`은 위에서 아래로 놓는 항목의 목록이다. 항목은 **행**(목록)이거나 **대시보드 안 탭 묶음**(`tabs`)이다.

```yaml
layout:
  - [{ header: 핵심 지표 }]                                   # 제목. 한 행에 혼자 둔다
  - [kpi_a, kpi_b, kpi_c]                                     # 차트 키. 12칸을 균등 분할
  - [{ markdown: "지표 정의는 [문서](https://example.com) 참고", width: 4 }, { chart: trend, width: 8 }]
  - tabs:                                                     # 대시보드 안 탭
      - { label: 지역별, rows: [[by_region]] }
      - { label: 유형별, rows: [[{ header: 유형 }], [by_category]] }
```

| 설정 | 번들(`position`) |
|---|---|
| 행 | `ROW-<i>` 노드(`i`는 `layout` 안 순번, 1부터), `meta.background: BACKGROUND_TRANSPARENT` |
| 행의 차트: `<키>` 또는 `{ chart, width, height }` | `CHART-<키>` 노드, `meta: {chartId, uuid, sliceName, width, height}`. `chartId`는 자리표시(필수)이고 import가 실제 id로 바꾼다. 값은 0, 단 `charts`를 쓴 필터가 있는 대시보드에서는 차트마다 다른 번호(`charts` 순서, 1부터)다. 높이 기본 50 |
| 행의 글: `{ markdown: "글", width, height }` | `MARKDOWN-<i>-<칸>` 노드, `meta: {code, width, height}`. 마크다운 문법. 높이 기본은 같은 행 차트의 가장 큰 높이, 차트가 없는 행이면 20 |
| 너비 | 너비가 없는 항목(차트·글)은 남은 칸(12 − 지정 너비 합)을 균등 분할하고 나머지는 앞 항목부터 1칸씩 더 준다 |
| 제목: `[{ header: 글 }]` | `HEADER-<i>` 노드(행 없이 격자나 탭의 자식), `meta: {text, headerSize: MEDIUM_HEADER, background: BACKGROUND_TRANSPARENT}` |
| `tabs: [{ label, rows }]` | `TABS-<i>` 노드와 탭마다 `TAB-<i>-<t>` 노드(`meta.text: label`). `rows`의 행은 `ROW-<i>-<t>-<k>`, 제목은 `HEADER-<i>-<t>-<k>`. 탭 안에 탭은 넣지 않는다 |
| `parents` | 모든 노드에 뿌리부터 부모까지의 id 목록(`[ROOT_ID, GRID_ID, …]`) |

세로 묶음(`COLUMN`)과 구분선(`DIVIDER`)은 만들지 않는다.

### 필터(`filters`)

```yaml
filters:
  - { key: f_period, label: 기간, type: time_range, default: "Last quarter" }
  - { key: f_amount, label: 금액, type: number_range, dataset: orders, column: amount, default: [0, 100000] }
  - { key: f_region, label: 지역, dataset: orders, column: region, default: [서울] }      # type 생략 = select
  - { key: f_city, label: 도시, type: select, dataset: orders, column: city, parent: f_region }
  - { key: f_grain, label: 단위, type: time_grain, dataset: orders, default: P1M, charts: [trend] }
```

| `type` | `filterType` | 쓰는 필드(공통: `key`, `label`, `default`, `charts`) | `targets` | `default` → `defaultDataMask` |
|---|---|---|---|---|
| `select`(기본, 생략 가능) | `filter_select` | `dataset`·`column`(필수), `multi`(기본 true), `parent` | `[{column: {name}, datasetUuid}]` | 값 또는 값 목록 → `extraFormData.filters: [{col, op: "IN", val: [값…]}]`, `filterState: {label: "값, 값", value: [값…]}`. `multi: false`면 값 하나 |
| `time_range` | `filter_time` | 없음(`dataset`·`column`을 쓰지 않는다) | `[{}]` | Superset 시간 범위 문자열(`"Last quarter"`, `"2026-01-01 : 2026-07-01"`) → `extraFormData.time_range`, `filterState.value` |
| `number_range` | `filter_range` | `dataset`·`column`(필수) | `[{column: {name}, datasetUuid}]` | `[최소, 최대]`(한쪽은 `null` 가능) → `extraFormData.filters: [{col, op: ">=", val}, {col, op: "<=", val}]`, `filterState: {label: "최소 ≤ x ≤ 최대", value: [최소, 최대]}` |
| `time_grain` | `filter_timegrain` | `dataset`(필수. 고를 수 있는 단위를 이 데이터셋에서 읽는다) | `[{datasetUuid}]` | `P1D`·`P1W`·`P1M`·`P3M`·`P1Y` → `extraFormData.time_grain_sqla`, `filterState: {label: "Month", value: ["P1M"]}` |

| 설정 | 번들(`metadata.native_filter_configuration[]`) |
|---|---|
| 공통 | `id: "NATIVE_FILTER-<키>"`, `name: label`, `type: NATIVE_FILTER`, `description: ""` |
| `controlValues` | `select`: `{multiSelect: multi, enableEmptyFilter: false, defaultToFirstItem: false, inverseSelection: false, searchAllOptions: false}`. 나머지 유형: `{enableEmptyFilter: false}` |
| `default` 없음 | `defaultDataMask: {extraFormData: {}, filterState: {}, ownState: {}}`. 있으면 위 표의 값에 `ownState: {}` |
| `parent: <필터 키>` | `cascadeParentIds: ["NATIVE_FILTER-<필터 키>"]`(없으면 `[]`). 상위 필터에서 고른 값에 맞는 선택지만 남는다. `select` 필터에만 쓴다 |
| `charts: [키…]` | `scope: {rootPath: [ROOT_ID], excluded: [나열하지 않은 차트의 자리표시 chartId…]}`. import가 실제 차트 id로 바꾼다. 생략하면 `excluded: []`(대시보드의 모든 차트). `chartsInScope`는 쓰지 않는다 |

- 필터는 대상 열과 **이름이 같은 열**을 가진 차트(데이터셋)에 적용된다.
- `time_range` 필터가 있는 대시보드에서는, 그 필터의 적용 범위에 든 **축약 차트**의 `adhoc_filters`에 시간 조건 `{expressionType: SIMPLE, clause: WHERE, subject: <시간 열>, operator: TEMPORAL_RANGE, comparator: "No filter"}`를 더한다(`where` 조건이 있으면 그 뒤). Superset의 시간 범위 필터는 이 조건이 있는 차트에만 적용되기 때문이다. `<시간 열>`은 차트가 참조한 열 중 첫 date/timestamp 열, 없으면 데이터셋 뷰의 첫 date/timestamp 열이다(둘 다 없으면 붙이지 않는다). 원본(`viz_type`) 차트에는 붙이지 않으므로 필요하면 `params.adhoc_filters`에 직접 쓴다. 차트의 `params`에 `adhoc_filters`를 쓰면 생성한 값을 통째로 덮어쓴다(생성 순서 3번).
- 임베드(guest) 화면에서는 `time_range` 필터의 값을 바꿀 수 없고 `default`만 적용된다(`superset-notes.md` 12-3절).

### 표식

데이터베이스·데이터셋·차트·대시보드 YAML의 끝에 `is_managed_externally`, `external_url: "dashkit://<프로젝트>/<종류>/<키>"`를 쓴다. 규칙과 `SYNC_LOCK_UI`는 `state-and-uuid.md`의 "표식"에 있다.

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
