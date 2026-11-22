# Superset 표 차트 서버 페이지 나눔 설계

[이슈 #47](https://github.com/ohyounjo/dashboard/issues/47)의 설계서다. 기준 커밋은 `main`(`463ab70`)이며, 상위 설계 기준은 [데이터 설계서 8-5절](data-design.md#8-5-대시보드-쪽의-페이지-나눔)이다.

참조: [차트 축약 계약](../contracts/chart-shorthand.md) · [Superset 확인 기록](../contracts/superset-notes.md) · [Superset 6.1.0 표 차트 export](../contracts/superset-6.1.0-export/charts/p_table_6.yaml) · [대시보드 스키마](../contracts/dashboard.schema.json)

## 1. 배경과 범위

현재 원본 행 모드(`columns`)의 Superset 표 차트(`viz: table`)는 `row_limit`(최대 50,000)까지 한 번에 조회해 브라우저로 내려받는다. 누적 테이블 도입으로 원본 행 수가 커질 수 있으므로, 필요할 때 차트 단위로 Superset의 서버 페이지 나눔을 선택적으로 켤 수 있게 한다.

### 범위와 기존 동작 보존

| 대상 | 이번 변경 | 기존 동작 보존 |
|---|---|---|
| `viz: table` 원본 행 모드(`columns`) + `page_size` 지정 | Superset 서버 페이지 나눔 활성화(`server_pagination: true`, `server_page_length: <page_size>`) | 선택 필드이므로 지정한 차트에만 적용 |
| `viz: table` 원본 행 모드(`columns`) + `page_size` 생략 | 변경 없음 | `server_pagination`·`server_page_length`를 `params`에 쓰지 않아 기존 번들·golden·동작을 바이트 단위로 유지 |
| `viz: table` 집계 모드(`metrics`) 및 다른 집계 차트 | `page_size` 사용 금지(JSON Schema 거부) | 기존 집계 조회(`row_limit`)와 번들을 그대로 유지 |
| 웹앱 위젯(`/api/data/{dataset}`) | 변경 없음 | 선택 `limit`·`cursor` 경로는 이미 구현됨(`dashkit/api/data.py`, `tests/test_api_paging.py`). 간트·칸반 등 전체 데이터가 필요한 위젯은 매개변수 없이 기존 전체 조회를 그대로 유지 |
| DB 계정·권한 | 변경 없음 | Superset 조회는 기존 `dash_reader` 읽기 전용 계정만 사용하며 `EmbedGuest` 권한(4개)과 비밀값 환경 변수 관리 규칙을 유지 |

이슈 #47의 요구사항과 기존 `contracts/` 문서 사이에 충돌은 없다.

## 2. Superset 6.1.0 소스 분석 및 구현 전 실측 계획

설계 8-5절과 이슈 #47 완료 조건에 따라 **구현보다 먼저 Superset 6.1.0 실제 동작을 측정하고 `contracts/superset-notes.md`(16절)에 기록**한다.

### 2-1. 사전 소스 확인 결과 (`apache/superset:6.1.0`)

로컬 Superset 6.1.0 이미지(`/app/superset/`)에서 확인한 구현 근거(`[소스]`)는 다음과 같다.

- **백엔드 행 상한** (`superset/common/query_context_factory.py:72-88`, `superset/common/query_object_factory.py:60-125`, `superset/utils/core.py:2048-2078`, `superset/config.py:1304-1308`):
  - `form_data.server_pagination`을 읽어 `apply_max_row_limit(limit, server_pagination=...)`에 넘긴다.
  - `server_pagination`이 거짓이면 `SQL_MAX_ROW`(100,000), 참이면 `TABLE_VIZ_MAX_ROW_SERVER`(500,000)를 상한으로 적용한다. `limit == 0`이면 해당 상한값을 쓴다.
- **프론트엔드 컨트롤** (`plugin-chart-table` 번들):
  - `server_pagination`: 체크박스, 기본 `false`.
  - `server_page_length`: 숫자 선택(`freeForm: true`), 기본 `10`, 선택지 `[10, 20, 50, 100, 200]`, 설명 `"Rows per page, 0 means no pagination"`. `server_pagination`이 참일 때만 표시된다.
  - `page_length`(클라이언트 페이지 나눔): `server_pagination`이 거짓일 때만 표시된다.
  - 기존 export 원본(`contracts/superset-6.1.0-export/charts/p_table_6.yaml`)에도 이미 `server_pagination: false`, `server_page_length: 10`이 포함돼 있다.
- **프론트엔드 쿼리 생성** (`plugin-chart-table` `buildQuery`):
  - `server_pagination: true`(비내보내기 조회)일 때 `POST /api/v1/chart/data` 한 요청에 `queries` 배열 2개를 보낸다.
    1. **페이지 데이터 쿼리**: `row_limit = ownState.pageSize ?? server_page_length`, `row_offset = (ownState.currentPage ?? 0) * pageSize`.
    2. **전체 행 수 쿼리**: `is_rowcount: true`, `row_limit = Number(formData.row_limit ?? 0)`, `row_offset = 0`, `post_processing: []`, `time_offsets: []`.
  - 열 머리글 정렬 클릭 시 `ownState.sortBy`가 서버 쿼리의 `orderby`에 반영된다.
  - `include_search: true` 상태에서 검색 시 `ownState.searchText`와 `ownState.searchColumn`이 `ILIKE` 필터로 추가된다.
  - 대시보드 네이티브 필터 등으로 `filters`가 바뀌면 `row_offset: 0`, `currentPage: 0`으로 초기화한다.

### 2-2. 구현 전 실측 항목 (`contracts/superset-notes.md` 16절 기록 대상)

격리 테스트 스택(`dashkit-test-<WT_ID>`, Superset 6.1.0)에서 임시 프로젝트 사본(25행 이상 데이터 포함)을 사용해 아래 항목을 실측한다.

1. **화면 저장 export 키 대조**: Explore에서 `server_pagination: true`, `server_page_length: 10`으로 저장한 뒤 `GET /api/v1/dashboard/export/` 결과의 차트 YAML 키가 기존 `contracts/superset-6.1.0-export/charts/p_table_6.yaml`에 없는 추가 필수 키를 요구하는지 확인한다.
2. **최소 params 번들 + guest 임베드 렌더링·페이지 넘김**: `query_mode: raw`, `all_columns`, `server_pagination: true`, `server_page_length: 10`만 담은 번들로 import한 뒤 `EmbedGuest`(4권한) 웹앱 임베드 화면에서 다음을 확인한다.
   - 초기 로드 시 `POST /api/v1/chart/data`가 200이고, `queries[0]`의 `row_limit=10, row_offset=0` 및 `queries[1]`의 `is_rowcount=true`가 기대대로 전송·응답되는지.
   - 다음 페이지(2페이지, 마지막 페이지) 이동 시 `row_offset=10`, `row_offset=20`으로 서버 재조회가 일어나며 중복·누락 없이 해당 구간의 행만 표시되는지.
   - 페이지 크기 변경 UI가 노출되면 크기 변경 시 재조회가 동작하는지.
3. **`row_limit`과 `page_size`의 상호작용**:
   - `row_limit` 생략 시, `row_limit > page_size`일 때, `row_limit < page_size`일 때 `is_rowcount` 결과와 표시 행 수·총 페이지 수가 어떻게 동작하는지 확인한다.
   - `row_limit < page_size`에서 총 행 수와 1페이지 표시 행 수가 어긋나거나 오류가 나면 `dashkit/config.py`에 `page_size <= row_limit` 교차 검증을 둘지 실측 결과를 근거로 확정한다.
4. **정렬·검색·필터 연계**:
   - 설정 `order_by` 초기 정렬 유지 및 열 머리글 클릭 시 서버 `orderby` 재조회 확인.
   - `search: true`(`include_search: true`)일 때 guest 임베드에서 검색 입력이 정상 동작하는지(서버 `ILIKE`인지, 열 선택 UI가 있는지, 제약이 있는지) 확인.
   - 대시보드 `select`·`time_range` 필터 적용 시 2페이지 이상에서 1페이지(`row_offset: 0`)로 복귀하고 총 행 수가 갱신되는지 확인.
5. **임베드 레이아웃 안정성**: 1280px 및 390px(900px 최소 너비) 임베드에서 페이지 나눔 막대 표시와 자동 높이(`getScrollSize()`)가 안정적으로 유지되는지 확인한다.

실측에서 `EmbedGuest` 권한 제약이나 Superset 6.1.0의 치명적 결함이 발견되면 구현을 멈추고 이슈 #47에 실측 결과와 대안을 먼저 기록한다.

## 3. 설정 스키마 및 파라미터 매핑 설계

### 3-1. 차트 유형 레지스트리 (`dashkit/charts/table.yaml`)

`fields`에 선택 필드 `page_size`를 추가하고, `schema.oneOf`에서 집계 모드(`metrics`)에 `page_size`가 들어오는 것을 차단한다.

```yaml
fields:
  columns: { kind: columns, min_items: 1 }
  groupby: { kind: columns, min_items: 1 }
  metrics: { kind: metrics }
  order_by: { kind: order_by }
  row_limit: { kind: int, min: 1, max: 50000 }
  page_size: { kind: int, min: 1, max: 1000, description: 서버 페이지 나눔 행 수(원본 행 모드 전용) }
  search: { kind: bool, description: 표 위 검색 상자 }
  format: { kind: d3format, emit: false }
schema:
  description: columns가 있으면 원본 행 모드, groupby/metrics가 있으면 집계 모드
  oneOf:
    - required: [columns]
      not: { anyOf: [ { required: [groupby] }, { required: [metrics] } ] }
    - required: [metrics]
      not: { anyOf: [ { required: [columns] }, { required: [page_size] } ] }
```

- **`min: 1` 이유**: Superset에서 `server_page_length: 0`은 "페이지 나눔 없음"을 뜻한다. `page_size`를 명시하는 의도는 서버 페이지 나눔을 켜는 것이므로 양의 정수만 허용한다.
- **`max: 1000` 이유**: Superset UI 기본 선택지(`[10, 20, 50, 100, 200]`)를 포함하면서 한 페이지당 과도한 행 전송을 막는다(6.1.0 실측에서 더 낮은 상한이 필요한 제약이 발견되면 조정한다).
- **타입 검사**: `kind: int`는 JSON Schema `{"type": "integer", "minimum": 1, "maximum": 1000}`으로 합성되므로 `0`, 음수, 소수, 문자열, `bool`은 모두 `make check` 및 Studio·manager 저장 시 거부된다.
- **자동 스키마 합성**: `contracts/dashboard.schema.json`의 `$defs.v_table`은 로딩 시 `dashkit.charts.extend_schema()`가 `table.yaml`에서 합성하므로 `dashboard.schema.json` 자체는 수정하지 않는다.
- **`notes` 갱신**: 원본 행 모드 설명에 `page_size` 지정 시 `server_pagination: true`, `server_page_length: <page_size>`를 쓴다는 내용과 생략 시 두 파라미터를 쓰지 않는다는 내용을 추가한다.

### 3-2. 파라미터 생성 (`dashkit/charts/hooks.py`)

`table(chart)` 함수의 원본 행 모드(`"columns" in chart`) 분기에서만 `page_size`를 처리한다.

```python
if "columns" in chart:
    p["query_mode"] = "raw"
    p["all_columns"] = list(chart["columns"])
    if order_by:
        p["order_by_cols"] = [_order_by_col(c, d) for c, d in order_by]
    if chart.get("page_size") is not None:
        p["server_pagination"] = True
        p["server_page_length"] = chart["page_size"]
```

- **기존 설정 불변**: `page_size`가 없는 원본 행 모드 표 차트와 모든 집계 모드 표 차트는 `server_pagination`·`server_page_length` 키를 출력하지 않는다. 따라서 기존 예시 프로젝트(`devtracker`, `sales-sample`, `chart-gallery`)의 golden 번들이 전혀 바뀌지 않는다.
- **`params` 덮어쓰기 규칙 유지**: `dashkit/sync/bundle.py`의 `chart_params()`가 마지막에 `{**params, **(chart.get("params") or {})}`로 얕게 덮어쓰는 계약(생성 순서 3번)을 그대로 따른다.
- **레지스트리 원칙 준수**: `bundle.chart_params()`와 `charts.build_params()`에 차트 유형별 분기를 추가하지 않는다(`tests/test_charts_registry.py::test_chart_params_has_no_per_type_branch`).
- **중복 라벨 검사 영향 없음**: `page_size`는 `kind: int`이므로 `LABEL_KINDS`(`column`, `columns`, `metric`, `metrics`)에 포함되지 않는다.

### 3-3. Studio 연동

`web/studio/chart-form.js`는 `GET /api/registry`의 `charts[].fields`를 읽어 `kind: int` 필드를 숫자 입력 칸으로 자동 생성한다. 따라서 `table.yaml`에 `page_size`를 추가하는 것만으로 Studio에서 편집할 수 있으며, 집계 모드에 `page_size`를 넣어 저장하려 하면 서버의 JSON Schema 검증이 422로 거부한다.

## 4. 변경 파일 범위

1. `contracts/superset-notes.md`: **16절 신설** — Superset 6.1.0 표 차트 서버 페이지 나눔의 소스 근거, 실제 guest 임베드 측정 결과(`queries[0]`/`is_rowcount`, 페이지 이동, 정렬, 검색, 필터, `row_limit` 상호작용, 레이아웃), 확인된 제약 기록.
2. `dashkit/charts/table.yaml`: `page_size` 필드, `schema.oneOf` 집계 모드 차단, `notes` 갱신.
3. `dashkit/charts/hooks.py`: `table()` 원본 행 모드 분기에 `page_size` → `server_pagination: True`, `server_page_length: <page_size>` 매핑 추가.
4. `contracts/chart-shorthand.md`, `reference/chart-catalog.md`: `make gen`으로 자동 생성 구간 갱신(손으로 편집하지 않는다).
5. `contracts/superset-6.1.0-export/`: 6.1.0 실측 export에서 `p_table_6.yaml`에 없는 신규 차트 키가 발견될 때만 갱신한다(현재 `p_table_6.yaml`에 `server_pagination`, `server_page_length`가 이미 존재하므로 추가 키가 없으면 변경하지 않는다).
6. `dashkit/config.py` 및 `contracts/README.md` (조건부): 실측 결과 `row_limit < page_size` 조합을 금지해야 한다면 참조 검사 규칙과 오류 메시지를 추가한다.
7. `tests/test_charts_registry.py`, `tests/test_config.py`, `tests/test_bundle_golden.py` 및 통합 테스트: 아래 5절의 단위·통합 회귀 테스트 추가.
8. 기존 예시 프로젝트(`projects/devtracker`, `projects/sales-sample`, `projects/chart-gallery`)와 `tests/golden/`: 기본 동작 보존 검증을 위해 기존 예시 표 차트에는 `page_size`를 추가하지 않으며, golden 번들의 불변을 확인한다.

## 5. 검증 계획

기존 개발·운영 스택과 `.state/`·`.build/`를 건드리지 않고 단위 테스트와 격리 테스트 스택(`dashkit-test-<WT_ID>`)에서 검증한다.

### 5-1. 단위 테스트 (`make gen` → `make test-unit`)

| 검증 항목 | 확인 내용 |
|---|---|
| 파라미터 생성 | 원본 행 모드 `page_size: 25` → `server_pagination: True`, `server_page_length: 25` 생성. `params`로 덮어쓰기 가능. |
| 기존 설정 보존 | `page_size` 미지정 원본 행 모드·집계 모드 표 차트에 `server_pagination`, `server_page_length` 미포함. `tests/test_bundle_golden.py` 기존 golden 스냅샷 전부 일치. |
| 스키마 검증 | 집계 모드(`metrics`)에 `page_size` 지정 시 `ConfigError` 거부. 경계값 `1`, `1000` 허용; `0`, `-1`, `1001`, `1.5`, `"10"`, `True` 거부. |
| 레지스트리·문서·키 대조 | `test_charts_registry.py`(`test_generated_docs_are_current`, `test_chart_params_has_no_per_type_branch` 포함), `test_superset_keys.py`, `test_domain_words.py` 통과. |

### 5-2. 통합 및 브라우저 실측 검증 (격리 테스트 스택)

1. `AUTH_COOKIE_SECURE=0 make test`(브라우저 실측을 이어갈 경우 `KEEP=1` 사용; 종료 시 `devtracker`가 자동 복원된다)로 전체 통합 테스트(`tests/test_api_paging.py` 등 기존 위젯·API·동기화 회귀 포함) 통과를 확인한다.
2. 임시 프로젝트 사본(`page_size` 설정 표 차트 1개 + `page_size` 미설정 원본 행 표 차트 1개 + 집계 표 차트 1개, 25행 이상 데이터)을 테스트 스택 환경(`TEST_ENV`)에서 적용해 헤드리스 Chromium으로 guest 임베드 화면을 검증한다.
   - `page_size` 설정 차트: `POST /api/v1/chart/data` 200, 페이지별 `row_limit`·`row_offset` 및 `is_rowcount: true` 확인, 1→2→마지막 페이지 행 일치, 정렬·검색·필터·높이 안정성 확인.
   - `page_size` 미설정 원본 행 차트와 집계 차트: `server_pagination` 없이 기존과 동일하게 동작 확인.
3. 검증 완료 후 임시 프로젝트 폴더를 삭제하고 `make test-down`으로 테스트 스택·볼륨·`.test/`를 정리한다.

## 6. 완료 조건

- [ ] Superset 6.1.0 격리 테스트 스택에서 표 차트 서버 페이지 나눔의 실제 동작(guest 임베드, `is_rowcount`, 페이지 이동, 정렬·검색·필터, `row_limit` 상호작용, export 키)을 측정하고 `contracts/superset-notes.md` 16절에 기록했다.
- [ ] `dashkit/charts/table.yaml`에 `page_size`를 추가(원본 행 모드 전용)하고 `dashkit/charts/hooks.py`에 매핑을 구현했으며, `make gen`으로 `contracts/chart-shorthand.md`와 `reference/chart-catalog.md`를 갱신했다(필요 시 export 계약도 갱신).
- [ ] `page_size` 미지정 시 기존 번들·동작·golden이 불변이고, 지정 시 서버에서 페이지가 나뉘는 것을 단위·통합·브라우저 검증으로 확인했다.
- [ ] 집계 차트(`viz: table` 집계 모드 포함)와 전체 데이터가 필요한 웹앱 위젯(`/api/data/{dataset}`)의 기존 동작이 보존됨을 회귀 테스트(`make test-unit`, `make test`)로 확인했다.
