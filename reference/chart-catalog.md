# 차트 카탈로그 (Superset 6.1.0)

`dashboard.yaml`에서 쓸 수 있는 차트 유형과 추가 방법을 모았다. **지금 구현 그대로** 쓸 수 있는 내용이다.

- **근거:** 유형 이름과 파라미터 이름은 이 저장소가 쓰는 `apache/superset:6.1.0` 이미지 안의 예제 차트(`/app/superset/examples/**/charts/*.yaml`)에서 읽었다.
- **확인 범위:** 축약 6종은 이 저장소 테스트로 렌더링까지 확인돼 있다. 원본 유형(`viz_type`)의 조각은 이 문서의 조각 전부를 `make check`와 같은 설정 검증에 통과시켰지만, **화면 렌더링은 확인하지 않았다.** 유형별 렌더링 확인은 보강설계서(`enhancement-plan.md`) 4절의 갤러리 프로젝트가 맡는다.

## 1. 차트를 쓰는 두 가지 방법

| 방법 | 쓰는 필드 | 대상 | 설정 검사 |
|---|---|---|---|
| 축약 | `viz:` + 축약 필드 | 6종: `big_number_total`, `big_number`, `line`, `bar`, `pie`, `table` | 열·지표 이름까지 검사 |
| 원본 | `viz_type:` + `params:` | Superset의 모든 유형 | 형식만 검사. `params` 안의 열 이름은 검사하지 않음 |

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
| 목표 대비 달성 | `gauge_chart` | 원본 |
| 시간에 따른 변화 | `line`, `bar` / `echarts_area`, `echarts_timeseries_scatter` | 축약 / 원본 |
| 단위가 다른 두 지표의 추이 | `mixed_timeseries` | 원본 |
| 기간별 증감 누적 | `waterfall` | 원본 |
| 범주별 비교 | `bar`(x에 범주 열) | 축약 |
| 구성 비율 | `pie` / `treemap_v2`, `sunburst_v2` | 축약 / 원본 |
| 단계별 전환 | `funnel` | 원본 |
| 두 범주의 교차 | `heatmap_v2`, `pivot_table_v2` | 원본 |
| 값의 분포 | `histogram_v2`, `box_plot` | 원본 |
| 두 수치의 관계 | `bubble_v2` | 원본 |
| 여러 지표를 항목별로 | `radar` | 원본 |
| 흐름, 관계 | `sankey_v2`, `chord`, `graph_chart` | 원본 |
| 계층 구조 | `tree_chart`, `sunburst_v2` | 원본 |
| 일정 막대 | `gantt_chart` (또는 웹앱 `gantt` 위젯) | 원본 |
| 단어 빈도 | `word_cloud` | 원본 |
| 나라별 값 | `world_map`, `country_map` | 원본 |
| 행 목록, 집계 표 | `table` | 축약 |

## 3. 축약 6종

필드 대응의 기준은 `contracts/chart-shorthand.md`다.

```yaml
- { key: kpi_rev, title: 매출, viz: big_number_total, dataset: orders, metric: revenue, format: ",d" }

- key: kpi_rev_trend
  title: 주간 매출
  viz: big_number
  dataset: orders
  metric: revenue
  time_column: ordered_on
  time_grain: P1W            # P1D P1W P1M P3M P1Y

- { key: trend, title: 주별 매출, viz: line, dataset: orders, x: ordered_on, time_grain: P1W, metrics: [revenue], groupby: [region] }

- { key: by_region, title: 지역별 매출, viz: bar, dataset: orders, x: region, metrics: [revenue], horizontal: true }

- { key: share, title: 유형별 비중, viz: pie, dataset: orders, groupby: [category], metric: revenue, donut: true }

- key: recent
  title: 최근 주문
  viz: table
  dataset: orders
  columns: [id, ordered_on, customer, amount]
  order_by: [[ordered_on, desc]]
  row_limit: 10
```

축약 차트에 옵션을 더하려면 `params:`에 Superset 파라미터를 쓴다. 생성된 값 위에 마지막으로 덮어쓴다.

```yaml
- { key: by_region, title: 지역별 매출, viz: bar, dataset: orders, x: region, metrics: [revenue], params: { show_value: true } }
```

## 4. 원본 유형 조각

아래 조각은 `charts:` 목록에 그대로 넣는다. 예시 데이터셋 `orders`의 열은 `ordered_on`(date), `region`, `category`, `status`, `customer`, `amount`, `quantity`, `country_code`, `region_code`이고 지표는 `revenue`, `order_count`다. 자기 데이터셋의 열과 지표 이름으로 바꿔 쓴다.

### 4-1. 시간 추이

```yaml
- key: area_rev                      # 영역(누적 추이)
  title: 지역별 매출 추이
  viz_type: echarts_area
  dataset: orders
  params: { x_axis: ordered_on, time_grain_sqla: P1W, metrics: [revenue], groupby: [region] }

- key: scatter_rev                   # 시계열 산점
  title: 일별 주문 수
  viz_type: echarts_timeseries_scatter
  dataset: orders
  params: { x_axis: ordered_on, time_grain_sqla: P1D, metrics: [order_count], groupby: [] }

- key: mixed_rev                     # 지표 두 묶음을 한 차트에(예: 막대 + 선)
  title: 매출과 주문 수
  viz_type: mixed_timeseries
  dataset: orders
  params: { x_axis: ordered_on, time_grain_sqla: P1M, metrics: [revenue], metrics_b: [order_count] }

- key: waterfall_rev                 # 기간별 증감
  title: 분기별 매출 증감
  viz_type: waterfall
  dataset: orders
  params: { x_axis: ordered_on, time_grain_sqla: P3M, metric: revenue, show_value: true }
```

`mixed_timeseries`에서 두 번째 묶음의 그래프 종류와 보조 축은 화면에서 정한 뒤 6절 방법으로 키를 옮긴다.

### 4-2. 비율, 단계, 달성

```yaml
- key: funnel_status                 # 단계별 전환
  title: 단계별 건수
  viz_type: funnel
  dataset: orders
  params: { groupby: [status], metric: order_count, sort_by_metric: true }

- key: gauge_rev                     # 게이지
  title: 매출 달성
  viz_type: gauge_chart
  dataset: orders
  params: { metric: revenue, groupby: [], show_pointer: true, show_progress: true }

- key: tree_cat                      # 트리맵
  title: 유형별 매출
  viz_type: treemap_v2
  dataset: orders
  params: { groupby: [category], metric: revenue }

- key: sun_cat                       # 선버스트(계층 비율). columns 순서가 안쪽 → 바깥쪽
  title: 지역·유형 구성
  viz_type: sunburst_v2
  dataset: orders
  params: { columns: [region, category], metric: revenue }
```

### 4-3. 교차, 분포, 관계

```yaml
- key: heat_region_cat               # 히트맵. groupby는 목록이 아니라 열 하나
  title: 지역 × 유형
  viz_type: heatmap_v2
  dataset: orders
  params: { x_axis: region, groupby: category, metric: revenue, linear_color_scheme: blue_white_yellow }

- key: pivot_region_cat              # 피벗 테이블
  title: 지역 × 유형 표
  viz_type: pivot_table_v2
  dataset: orders
  params: { groupbyRows: [region], groupbyColumns: [category], metrics: [revenue], aggregateFunction: Sum, metricsLayout: COLUMNS }

- key: hist_amount                   # 히스토그램. 숫자 열 하나
  title: 주문 금액 분포
  viz_type: histogram_v2
  dataset: orders
  params: { column: amount }

- key: box_amount                    # 상자 수염
  title: 지역별 금액 분포
  viz_type: box_plot
  dataset: orders
  params: { groupby: [region], metrics: [revenue], whiskerOptions: Tukey }

- key: bubble_cust                   # 버블. x, y, size는 지표
  title: 고객별 주문 수와 매출
  viz_type: bubble_v2
  dataset: orders
  params: { entity: customer, series: region, x: order_count, y: revenue, size: order_count, max_bubble_size: "25" }

- key: radar_region                  # 레이더
  title: 지역별 지표
  viz_type: radar
  dataset: orders
  params: { groupby: [region], metrics: [revenue, order_count] }
```

### 4-4. 흐름, 계층, 일정, 텍스트

```yaml
- key: sankey_flow                   # 생키. source → target
  title: 지역에서 유형으로
  viz_type: sankey_v2
  dataset: orders
  params: { source: region, target: category, metric: revenue }

- key: chord_flow                    # 코드
  title: 지역과 유형의 관계
  viz_type: chord
  dataset: orders
  params: { groupby: region, columns: category, metric: revenue }

- key: graph_flow                    # 네트워크 그래프
  title: 관계망
  viz_type: graph_chart
  dataset: orders
  params: { source: region, target: category, metric: order_count, layout: circular }

- key: org_tree                      # 트리. 행마다 id와 부모 id가 있어야 한다
  title: 조직도
  viz_type: tree_chart
  dataset: org
  params: { id: id, parent: parent_id, name: name, root_node_id: "1" }

- key: plan_gantt                    # Superset 간트
  title: 일정
  viz_type: gantt_chart
  dataset: phases
  params: { start_time: begins_on, end_time: ends_on, y_axis: phase_name, series: state }

- key: words                         # 워드 클라우드
  title: 고객 빈도
  viz_type: word_cloud
  dataset: orders
  params: { series: customer, metric: order_count }
```

간트는 두 가지가 있다. Superset `gantt_chart`는 대시보드 안에서 필터와 함께 움직인다. 웹앱 `gantt` 위젯(`tabs`의 섹션)은 진척률 채움, 상태 이모지, 오늘 선을 그린다.

### 4-5. 지도

```yaml
- key: world                         # 세계 지도. country_fieldtype: name | cca2 | cca3 | cioc
  title: 나라별 매출
  viz_type: world_map
  dataset: orders
  params: { entity: country_code, country_fieldtype: cca2, metric: revenue, show_bubbles: false }

- key: country                       # 한 나라의 지역 지도. entity는 ISO 3166-2 지역 코드
  title: 지역별 매출
  viz_type: country_map
  dataset: orders
  params: { select_country: france, entity: region_code, metric: revenue }
```

`select_country`에 쓸 수 있는 나라 이름은 Superset 화면의 Country Map 차트 설정에서 확인한다.

### 4-6. 기본 구성에서 권하지 않는 유형

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
- 자주 쓰는 원본 유형은 축약으로 올리는 편이 낫다. 지금은 5곳을 고쳐야 해서 번거롭다. 보강설계서 4-1절이 이를 파일 하나로 줄인다.
