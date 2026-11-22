# 레거시 차트의 guest 임베드 지원 대안 설계

[이슈 #50](https://github.com/ohyounjo/dashboard/issues/50)의 설계 및 결정 기록이다. Superset 6.1.0에서 `POST /superset/explore_json/`을 쓰는 레거시 차트 3종(`chord`, `world_map`, `country_map`)을 guest 임베드에서 어떻게 다룰지 대안별 보안·유지보수 비용을 비교하고, 대체 매핑과 회귀 방지 기준을 확정한다. 근거 문서는 [Superset 계약 5·10-2·13·14절](../contracts/superset-notes.md)과 [차트 카탈로그 2·4절](chart-catalog.md)이다.

## 확정된 설계 결정

1. **지도 시각화(`world_map`, `country_map`)는 필요하지 않다.** 국가·지역 단위 수치 비교가 필요한 경우 축약 `bar`, `treemap`, `table`을 사용한다.
2. **쌍별 관계·흐름(`chord`)은 기존 `POST /api/v1/chart/data` 기반 차트로 대체한다.** 이미 guest 임베드 렌더링과 정적 중복 라벨 검사를 모두 통과하는 원본 `graph_chart`(`source`, `target`, `metric`, `layout: circular`)와 축약 `sankey`(`source`, `target`, `metric`), `heatmap`(`x`, `y`, `metric`), `pivot_table`을 사용한다.
3. **`EmbedGuest`의 최소 권한 4개를 그대로 유지한다.** `superset/init_guest_role.py`의 `REQUIRED`(`can_read on CurrentUserRestApi`, `can_read on Dashboard`, `can_read on Chart`, `can_time_range on Api`)를 바꾸지 않으며, `can_explore_json on Superset`은 부여하지 않는다.

## 대안별 보안·유지보수 비용 비교

| 대안 | 채택 여부 | 데이터·SQL 노출 및 보안 영향 | 업그레이드·유지보수 비용 |
|---|---|---|---|
| `POST /api/v1/chart/data` 기반 기존 차트 대체 + 최소 권한 4개 유지 | **채택** | `chart/data`가 guest 응답에서 생성 SQL을 제거한다. 서브쿼리·타 데이터셋·타 대시보드 차트·CSV 차단 경계가 일관되고 `explore_json`이 `403`으로 닫혀 SQL 노출 경로가 열리지 않는다. | 기존 축약·원본 계약(`dashkit/charts/`, `chart-shorthand.md`)과 카탈로그를 그대로 활용하며 Superset 내부 패치·CORS·CSP 완화가 없다. |
| `can_explore_json on Superset` 권한 확대 | 기각 | 데이터셋·서브쿼리·CSV 경계는 `chart/data`와 같지만, `chart/data`가 지우는 생성 SQL 문 전체(`dash.<뷰>`, 지표 SQL 식, 조회 조건)가 기본 JSON 응답 `query` 필드, `?query=true`, 오류 응답에 그대로 노출된다. viz 경로에 `query_context_modified` 변조 검사도 없다. | `@deprecated(eol_version="5.0.0")` 경로에 의존하므로 Superset 업그레이드 시 제거·동작 변경 위험이 크다. |
| Superset 내부 뷰(`views/core.py`, `viz.py`) 래핑으로 `query` 가리기 | 기각 | `query` 필드 마스킹 여부와 별개로 deprecated 경로 의존과 `query_context_modified` 변조 검사 부재가 그대로 남는다. | Superset 업스트림 내부 구현에 강하게 결합되어 버전 업그레이드마다 깨지기 쉽고 유지보수 비용이 크다. |
| 외부 타일 지도(`deck_*`, `mapbox`) 또는 별도 웹앱 지도 위젯 개발 | 기각 | 외부 타일 지도 도입 시 `ENABLE_CORS = False`와 좁은 CSP 완화가 필요하며 지도 시각화 요구 자체가 없다. | 불필요한 타일 출처 관리, 프론트엔드 위젯 개발·테스트 비용이 발생한다. |

## 유형별 대체 매핑

| 미지원 레거시 유형 | 대체 유형 | 방법 | 핵심 파라미터 및 확인 상태 |
|---|---|---|---|
| `chord` (쌍별 관계·원형 배치) | `graph_chart` | 원본 | `params: { source, target, metric, layout: circular }`. `POST /api/v1/chart/data` 사용, 정적 중복 라벨 검사 대상. |
| `chord` (출발→도착 흐름) | `sankey` | 축약 | `source`, `target`, `metric`. 정적 중복 라벨 검사 대상. |
| `chord` (쌍별 교차 행렬) | `heatmap`, `pivot_table` | 축약 | `heatmap`(`x`, `y`, `metric`), `pivot_table`(`rows`, `columns`, `metrics`). 정적 중복 라벨 검사 대상. |
| `world_map`, `country_map` (국가·지역 수치 비교) | `bar`, `treemap`, `table` | 축약 | 범주 열 기준 막대(`x`, `metrics`), 구성비(`groupby`, `metric`), 상세·집계 표(`columns`/`groupby`, `metrics`). |

## 변경 사항 및 회귀 방지

1. **권한 유지 (`superset/init_guest_role.py`):** `EmbedGuest` 필수 권한 4개를 그대로 유지한다.
2. **차단 회귀 테스트 (`tests/test_api.py`):** guest 세션으로 자기 대시보드 차트에 대해 `POST /superset/explore_json/`(기본 form_data 요청 및 `?query=true`)과 `GET /superset/explore_json/`을 호출했을 때 모두 `403`으로 거부되고 응답 본문에 `SELECT ` SQL 문이 포함되지 않음을 단언한다.
3. **카탈로그·계약 정리 (`reference/chart-catalog.md`, `contracts/superset-notes.md`, `contracts/chart-shorthand.md`):**
   - `reference/chart-catalog.md` 2절에서 `chord`, `world_map`, `country_map`을 대체 차트로 바꾸고, 4-1·4-2절의 실패하는 원본 YAML 조각을 제거했으며, 4-3절에 미지원 확정 사유와 대체 매핑 표를 정리했다.
   - `contracts/superset-notes.md` 5·10-2·13-5절에서 잠정 보류 표현을 이슈 #50 확정 결정으로 갱신했다.
   - `contracts/chart-shorthand.md`의 원본 유형 라벨 검사 표 안내를 카탈로그·노트와 맞췄다.

## 완료 조건

- [x] `EmbedGuest` 최소 권한 4개 유지 및 `POST /superset/explore_json/`(기본·`?query=true`)·`GET /superset/explore_json/` `403` 회귀 테스트 추가
- [x] `reference/chart-catalog.md` 2·4절 및 `contracts/superset-notes.md`·`contracts/chart-shorthand.md` 갱신
- [x] 격리된 Superset 6.1.0 테스트 스택에서 대체 차트 렌더링(1280px·390px, 라이트·다크), 필터·테마 전환, `python -m dashkit gen --check`·`make test-unit`·격리 `make test` 통과 확인

## 구현 검증 결과 (2026-11-22)

- **권한 확인:** 격리된 Superset 6.1.0 테스트 스택(`dashkit-test-3495560932`)에서 `superset/init_guest_role.py` 실행 결과 `EmbedGuest` 권한 4개(`can_read on Chart`, `can_read on CurrentUserRestApi`, `can_read on Dashboard`, `can_time_range on Api`) 유지 확인.
- **단위·생성 문서·diff 검사:** `make test-unit` 통과(pytest 단위 521 passed / 273 deselected, 웹앱 JS 43 passed, Studio 단위 433 passed / 13 deselected), `python -m dashkit gen --check` 통과(`✔ gen: 생성 구간이 최신임`), `git diff --check` 통과.
- **격리 전체 통합 테스트 (`AUTH_COOKIE_SECURE=0 make test KEEP=1`):** 개발 스택 `.state/` 미변경 상태로 전체 통과.
  - 1단계(`devtracker`): `739 passed, 55 skipped` (`tests/test_api.py::test_guest_cannot_call_explore_json` 포함)
  - Studio 단계: `13 passed`
  - 2단계(`sales-sample`): `15 passed`
  - 3단계(`chart-gallery`): `45 passed` (`test_gallery.py` 28개 차트 데이터 조회 + `test_bundle_golden.py` 3개 예시 번들 스냅샷 포함)
  - 4단계(`zz-newtest`): `3 passed`
  - 5단계(`zz-ingest`): `15 passed`
- **`chart-gallery` 추가 `explore_json` 회귀 확인:** 격리 스택에 `chart-gallery` 적용 후 `test_guest_cannot_call_explore_json` 실행 시 `1 passed` (`403`, SQL 미노출).
- **Playwright guest 임베드 실측 (`chart-gallery`, 1280px·390px × 라이트·다크 4조합):**
  - 대상 차트(`shelf_native`의 `관계망(graph_chart)`·`트리(tree_chart)`·`간트(gantt_chart)`, `shelf_links`의 `생키(sankey)`, `shelf_cross`의 `히트맵(heatmap)`·`피벗 테이블(pivot_table)`, `shelf_basic`의 `막대(bar)`·`표(table)`, `shelf_share`의 `트리맵(treemap)`) 모두 `"Data error"`·`"Access is Denied"`·`"Unexpected error"`·`"Something went wrong"`·`"No data"` 없이 실제 시각화 요소(`<canvas>`/`<svg>`/`<table>`)가 렌더링됨.
  - `POST /api/v1/chart/data` 114건 전부 HTTP 200, `POST /superset/explore_json/` 요청 0건.
  - `shelf_blocks` 필터 적용(1280px·390px): 숫자 범위 하한 `10` 입력 후 `Apply filters` 클릭 시 상자 수 `2,876` → `2,821` 갱신 확인.
  - 테마 전환(1280px·390px): `#themeBtn` 클릭 시 `data-theme` `light` → `dark` 전환과 iframe 내부 배경색(`#app > div` `rgb(245, 245, 245)` → `rgb(0, 0, 0)`, `.dashboard-component-chart-holder` `rgb(255, 255, 255)` → `rgb(20, 20, 20)`) 변경 확인.
  - 웹앱 콘솔 오류 0건, `pageerror` 0건, Superset iframe 비-서비스워커 콘솔 오류 0건(기존 알려진 Superset `service-worker.js` 404 제외).
- 검증 완료 후 `make test-down`으로 격리 테스트 스택·볼륨을 제거했고 개발 스택(`dashboard-*`)은 변경 없이 유지됨을 확인했다.
