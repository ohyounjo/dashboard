# 차트 카탈로그 (Superset 6.1.0)

`dashboard.yaml`에서 쓸 수 있는 차트 유형과 추가 방법을 모았다. **지금 구현 그대로** 쓸 수 있는 내용이다.

- **근거:** 유형 이름과 파라미터 이름은 이 저장소가 쓰는 `apache/superset:6.1.0` 이미지 안의 예제 차트(`/app/superset/examples/**/charts/*.yaml`)에서 읽었다.
- **확인 범위:** 축약 유형 전부와 4절의 원본 유형은 갤러리 프로젝트(`projects/chart-gallery/`)로 데이터 조회와 화면 렌더링을 확인했다. 유형별 최소 파라미터와 결과는 `contracts/superset-notes.md` 10절에 있다. `chord`, `world_map`, `country_map`은 관리자 화면에서는 그려지지만 웹앱(guest) 화면에서는 권한이 없어 그려지지 않는다(4-3절).
- **견본:** `PROJECT=chart-gallery make apply`로 갤러리를 띄우면 유형을 화면에서 고르고 `projects/chart-gallery/dashboard.yaml`에서 같은 차트의 설정을 베껴 올 수 있다.

## 1. 차트를 쓰는 두 가지 방법

| 방법 | 쓰는 필드 | 대상 | 설정 검사 |
|---|---|---|---|
| 축약 | `viz:` + 축약 필드 | 3절의 유형(`dashkit/charts/`에 파일이 있는 유형) | 열·지표 이름까지 검사 |
| 원본 | `viz_type:` + `params:` | Superset의 모든 유형(축약에 없는 유형의 탈출구) | 형식만 검사. `params` 안의 열 이름은 검사하지 않음 |

두 방법 모두 `key`, `title`, `dataset`이 필요하고, `layout`에 키를 배치해야 한다.

지표는 어느 쪽이든 두 가지로 쓴다.

```yaml
metric: revenue                                   # 데이터셋 metrics에 정의한 이름
metric: { expressionType: SQL, sqlExpression: "SUM(amount)", label: 매출 }   # 원본 params 안에서 그 차트 전용 식
```

축약 필드에서는 차트 전용 식을 `{ sql: "SUM(amount)", label: 매출 }`로 쓴다.

## 2. 무엇을 보여 주려는지로 고르기

| 보여 주려는 것 | 유형 | 방법 |
|---|---|---|
| 숫자 하나 | `big_number_total` | 축약 |
| 숫자 하나와 추이, 직전 대비 | `big_number` | 축약 |
| 목표 대비 달성 | `gauge` | 축약 |
| 시간에 따른 변화 | `line`, `bar`, `area`, `scatter` | 축약 |
| 단위가 다른 두 지표의 추이 | `mixed` | 축약 |
| 기간별·항목별 증감 누적 | `waterfall` | 축약 |
| 범주별 비교 | `bar`(x에 범주 열) | 축약 |
| 구성 비율 | `pie`, `treemap`, `sunburst` | 축약 |
| 단계별 전환 | `funnel` | 축약 |
| 두 범주의 교차 | `heatmap`, `pivot_table` | 축약 |
| 값의 분포 | `histogram`, `box_plot` | 축약 |
| 두 수치의 관계 | `bubble` | 축약 |
| 여러 지표를 항목별로 | `radar` | 축약 |
| 흐름 | `sankey` | 축약 |
| 관계 | `chord`, `graph_chart` | 원본 |
| 계층 구조 | `sunburst` / `tree_chart` | 축약 / 원본 |
| 일정 막대 | `gantt_chart` (또는 웹앱 `gantt` 위젯) | 원본 |
| 단어 빈도 | `word_cloud` | 축약 |
| 나라별·지역별 값 | `world_map`, `country_map` | 원본 |
| 행 목록, 집계 표 | `table` | 축약 |

## 3. 축약 유형

필드 대응(축약 필드 → Superset 파라미터, 기본값)의 기준은 `contracts/chart-shorthand.md`다.

<!-- 생성 시작: shorthand-types -->
축약 유형은 22종이다. 유형 하나가 `dashkit/charts/<viz>.yaml` 파일 하나이고, 아래 표와 예시는 그 파일들과 `projects/chart-gallery/dashboard.yaml`에서 생성한다(`make gen`). 손으로 고치지 않는다.

| 축약 `viz` | 이름 | Superset `viz_type` | 필수 필드 | 선택 필드 |
|---|---|---|---|---|
| `area` | 영역 | `echarts_area` | `x`, `metrics` | `time_grain`, `groupby`, `stacked`, `format` |
| `bar` | 막대 | `echarts_timeseries_bar` | `x`, `metrics` | `time_grain`, `groupby`, `horizontal`, `format` |
| `big_number` | 숫자와 추이 | `big_number` | `metric`, `time_column` | `time_grain`, `compare_lag`, `compare_suffix`, `format` |
| `big_number_total` | 숫자 | `big_number_total` | `metric` | `subtitle`, `format` |
| `box_plot` | 상자 수염 | `box_plot` | `columns`, `metrics` | `groupby`, `whiskers`, `format` |
| `bubble` | 버블 | `bubble_v2` | `entity`, `x`, `y`, `size` | `series` |
| `funnel` | 퍼널 | `funnel` | `groupby`, `metric` | `format` |
| `gauge` | 게이지 | `gauge_chart` | `metric` | `groupby`, `min_value`, `max_value`, `format` |
| `heatmap` | 히트맵 | `heatmap_v2` | `x`, `y`, `metric` | `format` |
| `histogram` | 히스토그램 | `histogram_v2` | `column` | `groupby`, `bins` |
| `line` | 선 | `echarts_timeseries_line` | `x`, `metrics` | `time_grain`, `groupby`, `format` |
| `mixed` | 혼합(두 묶음) | `mixed_timeseries` | `x`, `metrics`, `metrics_b` | `time_grain`, `groupby`, `series_type`, `groupby_b`, `series_type_b`, `secondary_axis`, `format`, `format_b` |
| `pie` | 원 | `pie` | `groupby`, `metric` | `donut`, `format` |
| `pivot_table` | 피벗 테이블 | `pivot_table_v2` | `metrics` | `rows`, `columns`, `aggregate`, `format` |
| `radar` | 레이더 | `radar` | `groupby`, `metrics` | `format` |
| `sankey` | 생키 | `sankey_v2` | `source`, `target`, `metric` | — |
| `scatter` | 시계열 산점 | `echarts_timeseries_scatter` | `x`, `metrics` | `time_grain`, `groupby`, `format` |
| `sunburst` | 선버스트 | `sunburst_v2` | `columns`, `metric` | `format` |
| `table` | 표 | `table` | — | `columns`, `groupby`, `metrics`, `order_by`, `row_limit`, `search`, `format` |
| `treemap` | 트리맵 | `treemap_v2` | `groupby`, `metric` | `format` |
| `waterfall` | 폭포 | `waterfall` | `x`, `metric` | `time_grain`, `groupby`, `show_value`, `format` |
| `word_cloud` | 워드 클라우드 | `word_cloud` | `series`, `metric` | `rotation` |

유형별 예시는 `projects/chart-gallery/dashboard.yaml`에서 그 유형을 쓴 첫 차트다. `PROJECT=chart-gallery make apply`로 띄우면 화면에서 볼 수 있다. `dataset`, 열, 지표 이름은 자기 설정의 것으로 바꿔 쓴다.

**`area`** 영역

```yaml
- key: g_area
  title: 영역(area)
  viz: area
  dataset: harvests
  x: picked_on
  time_grain: P1M
  metrics: [weight_total]
  groupby: [fruit]
  stacked: true
  format: ',d'
```

**`bar`** 막대

```yaml
- key: g_bar
  title: 막대(bar)
  viz: bar
  dataset: harvests
  x: orchard
  metrics: [weight_total]
  horizontal: true
  format: ',d'
```

**`big_number`** 숫자와 추이

```yaml
- {key: g_big_number, title: 숫자와 추이(big_number), viz: big_number, dataset: harvests, metric: crate_total, time_column: picked_on,
  time_grain: P1W, compare_suffix: 전주 대비}
```

**`big_number_total`** 숫자

```yaml
- {key: g_big_number_total, title: 숫자(big_number_total), viz: big_number_total, dataset: harvests, metric: crate_total,
  subtitle: 상자, format: ',d'}
```

**`box_plot`** 상자 수염

```yaml
- key: g_box_plot
  title: 상자 수염(box_plot)
  viz: box_plot
  dataset: harvests
  groupby: [fruit]
  columns: [harvest_no]
  metrics: [weight_total]
```

**`bubble`** 버블

```yaml
- {key: g_bubble, title: 버블(bubble), viz: bubble, dataset: harvests, entity: orchard, series: valley, x: avg_crate_won,
  y: weight_total, size: crate_total}
```

**`funnel`** 퍼널

```yaml
- key: g_funnel
  title: 퍼널(funnel)
  viz: funnel
  dataset: sorting_steps
  groupby: [step_title]
  metric: crates_remaining
```

**`gauge`** 게이지

```yaml
- {key: g_gauge, title: 게이지(gauge), viz: gauge, dataset: harvests, metric: top_grade_pct, min_value: 0, max_value: 100}
```

**`heatmap`** 히트맵

```yaml
- {key: g_heatmap, title: 히트맵(heatmap), viz: heatmap, dataset: harvests, x: orchard, y: fruit, metric: crate_total}
```

**`histogram`** 히스토그램

```yaml
- {key: g_histogram, title: 히스토그램(histogram), viz: histogram, dataset: harvests, column: weight_kg, bins: 12}
```

**`line`** 선

```yaml
- key: g_line
  title: 선(line)
  viz: line
  dataset: harvests
  x: picked_on
  time_grain: P1W
  metrics: [crate_total]
  groupby: [valley]
```

**`mixed`** 혼합(두 묶음)

```yaml
- key: g_mixed
  title: '혼합(mixed): 막대 + 선'
  viz: mixed
  dataset: harvests
  x: picked_on
  time_grain: P1M
  metrics: [crate_total]
  series_type: bar
  metrics_b: [avg_crate_won]
  series_type_b: line
  secondary_axis: true
  format: ',d'
  format_b: ',d'
```

**`pie`** 원

```yaml
- key: g_pie
  title: 원(pie)
  viz: pie
  dataset: harvests
  groupby: [fruit]
  metric: crate_total
  donut: true
```

**`pivot_table`** 피벗 테이블

```yaml
- key: g_pivot_table
  title: 피벗 테이블(pivot_table)
  viz: pivot_table
  dataset: harvests
  rows: [valley, orchard]
  columns: [grade]
  metrics: [crate_total]
  format: ',d'
```

**`radar`** 레이더

```yaml
- key: g_radar
  title: 레이더(radar)
  viz: radar
  dataset: harvests
  groupby: [valley]
  metrics:
  - {sql: SUM(crates) FILTER (WHERE grade = '특'), label: 특}
  - {sql: SUM(crates) FILTER (WHERE grade = '상'), label: 상}
  - {sql: SUM(crates) FILTER (WHERE grade = '보통'), label: 보통}
  - {sql: SUM(crates) FILTER (WHERE outlet = '온라인'), label: 온라인}
  - {sql: SUM(crates) FILTER (WHERE outlet = '대형마트'), label: 대형마트}
```

**`sankey`** 생키

```yaml
- {key: g_sankey, title: 생키(sankey), viz: sankey, dataset: harvests, source: valley, target: outlet, metric: crate_total}
```

**`scatter`** 시계열 산점

```yaml
- key: g_scatter
  title: 시계열 산점(scatter)
  viz: scatter
  dataset: harvests
  x: picked_on
  time_grain: P1D
  metrics: [avg_crate_won]
  format: ',d'
```

**`sunburst`** 선버스트

```yaml
- key: g_sunburst
  title: 선버스트(sunburst)
  viz: sunburst
  dataset: harvests
  columns: [valley, orchard, fruit]
  metric: crate_total
```

**`table`** 표

```yaml
- key: g_table
  title: 표(table)
  viz: table
  dataset: harvests
  columns: [harvest_no, picked_on, orchard, fruit, grade, crates, weight_kg]
  order_by:
  - [weight_kg, desc]
  row_limit: 10
```

**`treemap`** 트리맵

```yaml
- key: g_treemap
  title: 트리맵(treemap)
  viz: treemap
  dataset: harvests
  groupby: [valley, orchard]
  metric: weight_total
```

**`waterfall`** 폭포

```yaml
- key: g_waterfall
  title: 폭포(waterfall)
  viz: waterfall
  dataset: crate_ledger
  x: booked_on
  time_grain: P1M
  metric: net_crates
  show_value: true
  params: {x_axis_time_format: '%Y-%m'}
```

**`word_cloud`** 워드 클라우드

```yaml
- {key: g_word_cloud, title: 워드 클라우드(word_cloud), viz: word_cloud, dataset: harvests, series: buyer, metric: harvest_count}
```
<!-- 생성 끝: shorthand-types -->

축약 차트에 옵션을 더하려면 `params:`에 Superset 파라미터를 쓴다. 생성된 값 위에 마지막으로 덮어쓴다.

```yaml
- { key: by_region, title: 지역별 매출, viz: bar, dataset: orders, x: region, metrics: [revenue], params: { show_value: true } }
```

축약 필드에서 차트 전용 지표는 `{ sql: "SUM(amount)", label: 매출 }`로 쓴다(위 `radar` 예시).

## 4. 원본 유형 조각

축약에 없는 유형이다. 아래 조각은 `charts:` 목록에 그대로 넣고 `dataset`, 열, 지표 이름을 자기 것으로 바꾼다. 갤러리(`projects/chart-gallery/dashboard.yaml`의 `shelf_native`)에 같은 차트가 있다.

### 4-1. 관계, 계층, 일정

```yaml
- key: chord_flow                    # 코드. groupby, columns는 목록이 아니라 열 하나
  title: 창고 사이 이동
  viz_type: chord
  dataset: hauls
  params: { groupby: from_site, columns: to_site, metric: pallet_sum }

- key: graph_flow                    # 관계망
  title: 창고 관계망
  viz_type: graph_chart
  dataset: hauls
  params: { source: from_site, target: to_site, metric: pallet_sum, layout: circular }

- key: org_tree                      # 트리. 행마다 id와 부모 id가 있어야 한다
  title: 조직도
  viz_type: tree_chart
  dataset: org
  params: { id: id, parent: parent_id, name: name, root_node_id: "1", metric: headcount }

- key: plan_gantt                    # Superset 간트. 시작·끝은 date/timestamp 열
  title: 일정
  viz_type: gantt_chart
  dataset: phases
  params: { start_time: begins_at, end_time: ends_at, y_axis: phase_name, series: state, y_axis_title_margin: 80 }
```

간트는 두 가지가 있다. Superset `gantt_chart`는 대시보드 안에서 필터와 함께 움직인다. 웹앱 `gantt` 위젯(`tabs`의 섹션)은 진척률 채움, 상태 이모지, 오늘 선을 그린다. `gantt_chart`의 `y_axis_title_margin`은 왼쪽 항목 이름이 잘릴 때 넓힌다.

### 4-2. 지도

```yaml
- key: world                         # 세계 지도. country_fieldtype: name | cca2 | cca3 | cioc
  title: 나라별 물량
  viz_type: world_map
  dataset: exports
  params: { entity: country_code, country_fieldtype: cca3, metric: tonnes_sum, show_bubbles: false }

- key: country                       # 한 나라의 지역 지도. entity는 ISO 3166-2 지역 코드(예: KR-11)
  title: 시도별 판매액
  viz_type: country_map
  dataset: sales_by_area
  params: { select_country: korea, entity: area_code, metric: sold_sum }
```

`select_country`에 쓸 수 있는 나라 이름은 Superset 화면의 Country Map 차트 설정에서 확인한다.

### 4-3. 웹앱 화면에서 그려지지 않는 원본 유형

`chord`, `world_map`, `country_map`은 Superset의 예전 조회 경로(`POST /superset/explore_json/`)를 쓴다. 웹앱은 guest token으로 대시보드를 임베드하고 guest 역할(`EmbedGuest`)에는 이 경로의 권한(`can_explore_json on Superset`)이 없어서, 차트 자리에 "Data error: Access is Denied"가 보인다. Superset 관리자 화면(http://localhost:8088)에서는 그려진다. 권한을 더하면 웹앱에서도 그려지는 것은 확인했다(`contracts/superset-notes.md` 10절). 기본 구성은 최소 권한을 지키려고 이 권한을 주지 않으며, 그래서 이 세 유형은 갤러리 프로젝트에도 넣지 않았다. 꼭 써야 하면 `superset/init_guest_role.py`의 권한 목록에 더하되, guest가 그 경로로 조회할 수 있는 범위를 먼저 확인한다.

### 4-4. 기본 구성에서 권하지 않는 유형

| 유형 | 이유 |
|---|---|
| `deck_arc`, `deck_grid`, `deck_hex`, `deck_path`, `deck_polygon`, `deck_scatter`, `deck_screengrid` | 지도 타일을 외부 출처에서 받아야 한다. 이 템플릿은 CORS를 끄고 CSP를 좁혀 두었으므로 `superset/superset_config.py`에서 타일 출처를 허용해야 쓸 수 있다. |
| `bubble`, `para`, `horizon` | 예전 방식 차트다. `bubble`은 `bubble_v2`를 쓴다. |

## 5. 차트 말고 대시보드에 쓸 수 있는 것

| 항목 | 지금 | 비고 |
|---|---|---|
| 필터 | `select`(값 선택) | Superset에는 `filter_range`(숫자 범위), `filter_time`(기간), `filter_timegrain`(시간 단위)도 있다. 보강설계서 4-3절에서 추가한다. |
| 배치 | 행과 차트(너비, 높이) | Superset에는 `HEADER`, `MARKDOWN`, `COLUMN`, `TABS`도 있다. 보강설계서 4-3절에서 추가한다. |
| 차트 사이 연동 | 꺼짐 | `cross_filters_enabled`를 설정으로 여는 것을 보강설계서 4-3절에서 다룬다. |

## 6. 새 유형이나 옵션을 추가하는 절차

카탈로그에 없는 옵션은 Superset 화면에서 만들어 키 이름을 확인한 뒤 옮긴다. README 5절과 같은 절차다.

1. http://localhost:8088 에 관리자로 로그인한다.
2. Charts → + Chart에서 `make apply`가 만든 데이터셋을 고르고 차트를 만들어 저장한다. 대시보드에는 넣지 않는다.
3. 차트 목록에서 그 차트를 Export한다.
4. zip 안 `charts/<이름>.yaml`의 `viz_type`과 `params`를 `dashboard.yaml`로 옮긴다. `params`에서 `datasource`, `slice_id`, `dashboards`, `extra_form_data`는 지운다. 기본값과 같은 키는 지워도 된다.
5. `layout`에 키를 배치하고 `make apply`를 실행한다.
6. 화면에서 임시로 만든 차트는 Superset에서 직접 지운다.

주의할 점:

- `params` 안의 열 이름은 `make check`가 검사하지 않는다. 틀리면 차트 자리에 Superset 오류가 보인다.
- 같은 유형이라도 Superset 버전이 바뀌면 키 이름이 바뀔 수 있다. 예: Big Number 부제는 6.1.0에서 `subtitle`이고 이전 버전은 `subheader`였다.
- 자주 쓰는 원본 유형은 축약으로 올리는 편이 낫다. `dashkit/charts/<viz>.yaml` 파일 하나를 만들면 된다(`contracts/chart-shorthand.md`의 "유형 추가"). 갤러리에 그 유형의 차트를 하나 넣어 데이터 조회와 화면을 확인한 뒤에 올린다.
