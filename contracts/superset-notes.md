# Superset 확인 기록 (Explore 단계)

- 확인일: 2026-11-19
- 방법: 공식 배포물(PyPI, npm, Docker Hub)과 **해당 태그의 소스**(`apache/superset` 태그 `6.1.0`, 커밋 `c83fb2bb1dcf`)를 읽었다. 그다음 같은 버전의 실제 인스턴스를 띄워 동작을 확인했다.
- 검증 환경: `apache/superset:6.1.0`에 `psycopg2-binary==2.9.9`를 추가한 이미지, `postgres:16.10` 2개(메타데이터, 데이터), `nginx:1.29.3-alpine`(호스트 페이지 역할), 헤드리스 Chromium(`mcr.microsoft.com/playwright/python:v1.55.0-noble`).
- 각 항목의 근거는 `[소스]`(파일:줄, 태그 6.1.0 기준), `[실측]`(검증 인스턴스 결과), `[배포물]`(레지스트리 메타데이터)로 표시한다.

---

## 1. 버전과 PostgreSQL 드라이버

| 항목 | 결정 | 근거 |
|---|---|---|
| Superset | **6.1.0** (최신 안정판, 2026-05-13) | [배포물] PyPI `apache-superset` 최신 6.1.0. 이후에는 rc가 없다 |
| 이미지 | `apache/superset:6.1.0` digest `sha256:16b50bbef6648912a79e3293d418fefa743ce555d36c5af6b85d859119ed7f88` | [배포물] Docker Hub. 이미지 안 Python은 3.10(`/app/.venv`) |
| Embedded SDK | **`@superset-ui/embedded-sdk@0.4.0`** (2026-06-18, npm latest). 웹앱에는 UMD 번들 `bundle/index.js`(전역 `supersetEmbeddedSdk`)를 `web/vendor/`에 복사해 쓴다 | [배포물] npm. [실측] 6.1.0과 함께 임베드, 필터, 테마 전환이 동작함 |
| PostgreSQL 드라이버 | **공식 이미지에 없다.** `psycopg2`와 `psycopg2-binary` 모두 MISSING. `superset/Dockerfile`에서 `uv pip install --python /app/.venv/bin/python psycopg2-binary==2.9.9`로 추가한다(버전은 Superset의 `postgres` extra와 같게) | [실측] 이미지 안에서 `importlib.metadata`로 확인. [소스] `Dockerfile:222-234`(`lean` 스테이지는 postgres extra 없이 설치), `pyproject.toml:177` `postgres = ["psycopg2-binary==2.9.9"]` |
| 기타 고정값 | flask-talisman 1.1.0, PyJWT 2.10.1, flask-appbuilder 5.0.2 (이미지에 포함) | [실측] |

## 2. asset 번들 가져오기

- **엔드포인트:** `POST /api/v1/assets/import/` (multipart). 필드는 `bundle`(zip), `passwords`(JSON), `sparse`(선택)이다. [소스] `superset/importexport/api.py:94-226`
- **메타데이터:** zip 최상위 폴더 아래 `metadata.yaml`에 `type: assets`를 쓴다. 폴더는 `databases/`, `datasets/`, `charts/`, `dashboards/`다. [소스] `commands/importers/v1/assets.py:194-200`
- **덮어쓰기:** 모든 객체를 `overwrite=True`로 가져온다(DB → 데이터셋 → 차트 → 대시보드 순). 데이터셋은 `sync=["columns","metrics"]`라서 번들에 없는 열과 지표가 **삭제된다**. 대시보드-차트 연결(`dashboard_slices`)은 지우고 `position`의 차트로 다시 만든다. [소스] `assets.py:95-163`, `dataset/importers/v1/utils.py`의 `import_dataset`
- **UUID 기준 멱등성:** 기존 객체는 `uuid`로 찾아 같은 id를 유지한 채 갱신한다. [실측] 같은 번들을 두 번 가져와도 객체 수와 (id, uuid) 목록이 같았다.
- **삭제는 하지 않는다:** 번들에서 뺀 차트는 대시보드에서만 빠지고 객체는 남는다. 따라서 **prune을 직접 구현해야 한다.** [실측] 차트 하나를 빼고 import한 뒤에도 `GET /api/v1/chart/<uuid>`가 200이었다.
- **prune 방법:** 종류별로 `GET /api/v1/{chart|dataset|dashboard|database}/?q=(filters:!((col:uuid,opr:eq,value:'<uuid>')),columns:!(id,uuid))`로 id를 찾고 `DELETE /api/v1/<kind>/<id>`를 호출한다. 순서는 대시보드 → 차트 → 데이터셋 → DB다. 대시보드를 지우면 `embedded_dashboards` 행도 함께 지워진다. [실측]
- **원자성은 완전하지 않다:** `run()`은 `@transaction`으로 감싸져 있지만, 대시보드 단계에서 실패한 import에서도 **database 객체는 남았다**. 다시 실행하면 같은 UUID로 덮어쓰므로 결과는 수렴한다. "실패 시 Superset은 이전 상태"라고 보장할 수는 없으니 plan 6-2절의 "멱등, 다시 실행" 전제로 다룬다. [실측]
- **오류 응답:** 스키마 검증 실패도 HTTP **500 HTML**(`Import failed for an unknown reason`)로 온다. 원인은 Superset 로그에만 남는다. sync는 실패하면 `make logs` 확인을 안내해야 한다. [실측]
- **DB 비밀번호:** 번들의 `sqlalchemy_uri`에는 `XXXXXXXXXX`로 가리고 `passwords={"databases/<파일>.yaml": "<pw>"}`로 넘긴다. 키에는 zip 최상위 폴더명을 **넣지 않는다**(`bundle/databases/...`이면 실패). 기존 DB가 같은 UUID로 있으면 저장된 비밀번호를 재사용한다. [소스] `importers/v1/utils.py`의 `load_configs`. [실측] 키 형식이 틀리면 `Must provide a password for the database`
- **데이터셋 형식:** `table_name`, `schema: dash`, `sql: null`(뷰를 물리 테이블처럼), `main_dttm_col`, `uuid`, `database_uuid`, `metrics[]`(`metric_name`, `verbose_name`, `expression`), `columns[]`(`column_name`, `type`, `is_dttm`, `groupby`, `filterable`, `is_active`). export 원본은 `contracts/superset-6.1.0-export/datasets/`에 있다.
- **대시보드 `position` 함정:**
  1. CHART 노드 `meta`에 **`chartId`가 반드시 있어야 한다**(자리표시 0도 된다. import 때 실제 id로 바뀐다). 없으면 `KeyError: 'chartId'`.
  2. CHART 노드 `meta.uuid`(차트 UUID)가 차트를 연결하는 열쇠다. 없으면 export 때 Superset이 배치 밖 차트로 보고 행을 하나 더 붙인다.
  [소스] `dashboard/importers/v1/utils.py`의 `update_id_refs`, `find_chart_uuids`. [실측]
- **네이티브 필터 형식:** `metadata.native_filter_configuration[]`. 대상 데이터셋은 `targets[].datasetUuid`로 쓰면 import 때 `datasetId`로 바뀐다. `chartsInScope`는 import 때 **변환되지 않으므로 넣지 않는다**(`scope.rootPath: [ROOT_ID]`, `excluded: []`만 쓰고 열 이름이 같은 차트에 적용되게 둔다). [소스] `update_id_refs`, 공식 예제 `superset/examples/sales_dashboard/dashboard.yaml`. [실측] 아래 형식으로 임베드 화면에서 필터 표시·적용을 확인했다. 유형 4종·기본값·연쇄·적용 범위는 12절.

```yaml
metadata:
  native_filter_configuration:
  - id: NATIVE_FILTER-<키>          # 고정 문자열
    name: <라벨>
    filterType: filter_select
    type: NATIVE_FILTER
    targets: [{ column: { name: <열> }, datasetUuid: <데이터셋 uuid> }]
    controlValues: { multiSelect: true, enableEmptyFilter: false, defaultToFirstItem: false, inverseSelection: false, searchAllOptions: false }
    defaultDataMask: { extraFormData: {}, filterState: {}, ownState: {} }
    cascadeParentIds: []
    scope: { rootPath: [ROOT_ID], excluded: [] }
    description: ""
```

## 3. 임베드 설정

- **API:** `POST`(또는 `PUT`) `/api/v1/dashboard/<id_or_slug>/embedded` 본문은 `{"allowed_domains": [...]}`, 조회는 `GET`, 해제는 `DELETE`다. [소스] `dashboards/api.py:1955-2120`
- **UUID 유지:** `EmbeddedDashboardDAO.upsert`가 기존 행을 재사용한다(주석: "Upsert is used to preserve the embedded_dashboard uuid across updates"). 임베드 UUID는 Superset이 uuid4로 만들기 때문에 **우리가 정할 수 없다** → `.state/embed.json` 필요. [소스] `daos/dashboard.py`의 `EmbeddedDashboardDAO.upsert`. [실측] 두 번 호출해도, 그 뒤 번들을 다시 import해도 같은 UUID였다(대시보드 import는 `recursive=False`).
- **허용 도메인:** `/embedded/<uuid>`가 `Referer`를 `allowed_domains`와 same-origin으로 비교하고, 맞지 않으면 403을 준다. [소스] `embedded/view.py`. [실측] `http://evil.test/`는 403이었다. → 웹앱을 `localhost:8080`과 `127.0.0.1:8080` 중 어느 쪽으로 열지에 따라 둘 다 등록해야 한다(frame-ancestors도 같다).

## 4. guest token 직접 서명

- **claim:** `user`(username/first_name/last_name), `resources: [{type: "dashboard", id: <임베드 UUID>}]`, `rls_rules: []`, `iat`, `exp`, `aud`, `type: "guest"`. `user`, `resources`, `rls_rules`가 없거나 `type`이 `guest`가 아니면 거부한다. [소스] `security/manager.py:3008-3070`(`create_guest_access_token`, `get_guest_user_from_request`)
- **알고리즘:** `GUEST_TOKEN_JWT_ALGO = "HS256"`(기본), 비밀값은 `GUEST_TOKEN_JWT_SECRET`, 헤더는 `X-GuestToken`이다. [소스] `config.py:2306-2315`
- **aud:** `GUEST_TOKEN_JWT_AUDIENCE`가 없으면 요청 호스트가 기본값이다. 고정 문자열을 설정하고 backend도 같은 값으로 서명한다. [소스] `_get_guest_token_jwt_audience`
- **resources id:** 임베드 UUID를 넣는다. 대시보드 숫자 id도 받지만 우리는 UUID만 쓴다. [소스] `has_guest_access`
- **수명:** 서명자(backend)가 `exp = iat + 300`으로 정한다. Superset 기본 `GUEST_TOKEN_JWT_EXP_SECONDS = 300`과 같다. SDK는 토큰 `exp`를 읽어 만료 전에 `fetchGuestToken`을 다시 부른다(`jwt-decode` 의존). [소스] SDK `lib/guestTokenRefresh.js`
- **실측 결과**(EmbedGuest 역할, 토큰은 대시보드 A의 임베드 UUID):

| 요청 | 결과 |
|---|---|
| `GET /api/v1/dashboard/<A id>`, `/charts`, `/datasets` | 200 |
| `POST /api/v1/chart/data` (저장된 차트 그대로) | 200 |
| `POST /api/v1/chart/data` (지표를 바꿔서) | ~~403~~ **정정(Verify): 200.** Explore 때의 403은 다른 데이터셋 id를 함께 보내서 생긴 데이터셋 불일치 거부였다. guest는 차트 목록 권한이 없어 `ChartDAO.find_by_id`가 None → `query_context_modified` 검사가 건너뛰어진다(`common/query_context_factory.py:64`, `security/manager.py:201`). 실제 경계: 자기 대시보드 차트가 쓰는 데이터셋 안에서는 임의 열·집계 가능, 다른 데이터셋·대시보드에 없는 차트는 403, 서브쿼리는 400 |
| `GET /api/v1/dashboard/<B id>`, `/charts` | **404** |
| `GET /api/v1/database/` | **403** |
| `GET /api/v1/chart/` | 200, `count: 0` |
| `GET /api/v1/security/csrf_token/` | **403** → guest는 CSRF 토큰을 받을 수 없다 |
| `PUT/DELETE chart`, `PUT dashboard`, `POST dashboard`, `POST sqllab/execute` | 400(CSRF 없음) 또는 302 → **변경되지 않음** 확인 |
| aud 불일치, 만료, 비밀값 불일치 토큰 | **401** |

## 5. guest 역할 최소 권한

- 기본 `GUEST_ROLE_NAME = "Public"`. 우리는 전용 역할 `EmbedGuest`를 만든다. [소스] `config.py:2306`
- **기준선 3개(실측):** `can_read on CurrentUserRestApi`, `can_read on Dashboard`, `can_read on Chart` **3개만으로** 6개 유형 차트 렌더링, 네이티브 필터 값 조회와 적용, 테마 전환이 동작했다. 데이터셋 `datasource_access`는 필요 없다. guest의 데이터 접근 경계와 payload 변조에 관한 정정은 4절을 따른다. [소스] `security/manager.py:2564-2680`(`raise_for_access`의 guest 분기), `embedded/index.tsx:177-204`(시작 시 `GET /api/v1/me/roles/`, 권한이 없으면 "Something went wrong with embedded authentication")
- **만드는 방법:** `superset-init`에서 앱 컨텍스트 파이썬으로 `security_manager.add_role("EmbedGuest")` 후 `find_permission_view_menu(perm, view)`로 찾아 `role.permissions`에 넣는다. 반드시 `superset init` **다음에** 실행한다(권한-뷰 행이 그때 생긴다). [실측] `superset init`을 다시 실행해도 EmbedGuest 권한은 유지된다.
- **현재 역할:** 위 세 권한에 `can_time_range on Api`를 더한 네 권한이다(이슈 #49에서 사용자 승인). `can_explore_json on Superset`은 부여하지 않는다. 권한별 실측과 기존 데이터 접근 한계는 13절을 따른다. 변경된 초기화를 실행해야 기존 역할에 반영된다.

## 6. iframe 허용 (Talisman, frame-ancestors)

- 6.1.0 기본 `TALISMAN_ENABLED=True`이고 `TALISMAN_CONFIG`에 `frame-ancestors`가 없다. flask-talisman 1.1.0은 기본으로 `X-Frame-Options: SAMEORIGIN`을 붙인다. [소스] `config.py:2113-2158`, `initialization/__init__.py:892-902`
- 설정: 기본 `TALISMAN_CONFIG`를 deepcopy하고 `content_security_policy["frame-ancestors"] = [웹앱 출처...]`, `frame_options = None`으로 둔다(`TALISMAN_DEV_CONFIG`는 쓰지 않는다). [실측] 응답 헤더에 `frame-ancestors http://localhost:8080`이 있고 `X-Frame-Options`는 없다. 다른 출처에서는 Referer 검사로 403.
- CORS: SDK는 iframe과 `postMessage`만 쓴다. **정정(Implement):** 6.1.0 기본값은 `ENABLE_CORS = True`(지도 타일 출처 허용)라서 `superset_config.py`에 `ENABLE_CORS = False`를 명시했다. [실측] 끈 뒤에도 임베드 동작, 응답에 `Access-Control-*` 헤더 없음.
- 캐시: 6.1.0 기본이 이미 `DATA_CACHE_CONFIG = {"CACHE_TYPE": "NullCache"}`, `CACHE_CONFIG`도 NullCache다. 명시적으로 다시 써 둔다. `FILTER_STATE_CACHE_CONFIG`와 `EXPLORE_FORM_DATA_CACHE_CONFIG`는 메타데이터 DB(`SupersetMetastoreCache`)를 쓰므로 Redis가 필요 없다. [소스] `config.py:1106-1140`

## 7. 축약 문법 6개 유형의 차트 파라미터

방법: REST로 최소 params 차트를 만든 뒤, 헤드리스 브라우저로 Explore 화면을 열어 **화면의 Save로 다시 저장**하고 `/api/v1/dashboard/export/`로 내보냈다. 그다음 결정적 UUID와 **최소 params만 담은 번들**(아래 열)을 import해 guest 임베드 화면에서 6개 모두 렌더링되는 것을 확인했다.

| 축약 `viz` | Superset `viz_type` | 최소 params(번들에 쓰는 것, 실측 통과) | 화면 저장 시 붙는 주요 기본값 |
|---|---|---|---|
| `big_number_total` | `big_number_total` | `metric` | `header_font_size 0.4`, `y_axis_format SMART_NUMBER`, `adhoc_filters`(시간열 `No filter`) |
| `big_number` | `big_number` | `metric`, `x_axis`, `time_grain_sqla` | `aggregation: LAST_VALUE`, `show_trend_line: true`, `start_y_axis_at_zero: true`, `compare_lag`/`compare_suffix`(직전 대비, controlPanel 소스로 이름 확인) |
| `line` | `echarts_timeseries_line` | `x_axis`, `time_grain_sqla`(시간축일 때), `metrics[]` | `groupby: []`, `row_limit 10000`, `order_desc`, `y_axis_format`, `show_legend` 등 |
| `bar` | `echarts_timeseries_bar` | `x_axis`(범주 열도 됨), `metrics[]`, `groupby: []` | `orientation: vertical`, `x_axis_sort_asc`, `y_axis_format` |
| `pie` | `pie` | `groupby[]`, `metric` | `row_limit 100`, `sort_by_metric`, `label_type: key`, `donut: false` |
| `table` | `table` | 원본 행: `query_mode: raw`, `all_columns[]`, `order_by_cols: ['["<열>", false]']`(JSON 문자열 배열), `row_limit` / 집계: `query_mode: aggregate`, `groupby[]`, `metrics[]` | `server_pagination: false`, `include_search`, `show_cell_bars` |

- **지표 표현:** 데이터셋 지표는 **이름 문자열**(`"open_cnt"`), 차트 전용 식은 `{"expressionType":"SQL","sqlExpression":"…","label":"…"}`. 두 형식 모두 guest 렌더링에서 통과했다.
- **`query_context`:** 화면 저장 시에는 생기지만 **번들에 없어도 대시보드와 guest 렌더링은 동작한다**(프론트엔드가 params로 쿼리를 만든다). 다만 `GET /api/v1/chart/<id>/data/`는 `query_context`가 없으면 400("Chart has no query context saved")이다. `POST /api/v1/chart/data`에 쿼리를 직접 넣으면 200이다(데이터셋 지표 이름도 쓸 수 있다). → Plan에서 결정할 항목.
- export 원본: 화면이 저장한 6개 차트의 전체 params와 query_context는 `contracts/superset-6.1.0-export/`에 그대로 두었다(DB 비밀번호는 Superset이 가린 상태).

## 8. iframe 다크모드 (themeMode / setThemeMode)

- SDK 0.4.0에는 `embedDashboard`의 **`themeMode` 인자가 없다.** 반환 객체의 `setThemeMode(mode: 'default' | 'dark' | 'system')`와 `setThemeConfig(config)`만 있다. [배포물] `dist/index.d.ts`
- iframe 쪽 6.1.0은 `Switchboard.defineMethod('setThemeMode', …)`를 구현한다. 테마가 잠겨 있으면 조용히 무시한다. 기본 설정에는 `THEME_DARK`가 정의되어 있어 전환된다. [소스] `superset-frontend/src/embedded/index.tsx:272-318`, `config.py:921-980`
- [실측] `embedDashboard` 완료 후 `setThemeMode('dark')` → iframe 전체가 다크로, `setThemeMode('default')` → 라이트로 바뀌었다.
- 웹앱: 처음 임베드할 때 `embedDashboard` 결과를 받은 뒤 현재 모드로 `setThemeMode`를 한 번 부르고, 사용자가 바꿀 때마다 다시 부른다. iframe이 처음 그려질 때 잠깐 라이트로 보일 수 있다(한계).

## 9. Plan에 반영할 함정 요약

1. `superset/Dockerfile`에 psycopg2-binary 추가(공식 이미지에 없음).
2. 번들 `position`의 CHART `meta`에 `chartId`(자리표시)와 `uuid`가 반드시 있어야 한다.
3. `passwords` 키는 `databases/<파일>.yaml`(zip 최상위 폴더 제외).
4. import는 삭제를 하지 않는다 → `.state/managed.json` 기반 prune.
5. 임베드 UUID는 Superset이 만든다 → `.state/embed.json`. 재import·재호출에도 유지된다.
6. allowed_domains와 frame-ancestors에 `localhost`와 `127.0.0.1` 출처를 둘 다 넣을지 결정.
7. import 실패는 500 HTML이고 부분 적용(DB 객체)이 남을 수 있다 → 재실행으로 수렴.
8. guest 역할 권한 네 개는 `superset init` 다음에 부여한다.

## 10. 축약 16종 추가와 갤러리의 원본 유형 (보강설계서 2단계)

- 확인일: 2026-11-20. 방법: `projects/chart-gallery`를 테스트 스택(`dashkit-test`, Superset 6.1.0)에 적용하고
  1. 차트마다 `POST /api/v1/chart/data`(관리자)가 200이고 행이 있는지(`tests/test_gallery.py`, 28개 통과),
  2. 웹앱(guest 임베드) 화면을 헤드리스 Chromium으로 탭마다 캡처(1280px 라이트·다크, 390px)해 차트 자리의 오류 알림("Data error", "Unexpected error" 등)이 없는지와 그림이 그려졌는지
  를 봤다. 통과 기준은 보강설계서 7절 D4(둘 다 통과해야 축약으로 올린다)다.
- 파라미터 이름의 출발점은 이미지 안 예제(`/app/superset/examples/<묶음>/charts/*.yaml`)다. 예제에는 예전 버전에서 옮겨 온 키가 섞여 있어(예: `box_plot`의 `whisker_options`, `mixed_timeseries`의 `seriesType: echarts_timeseries_line`) 그대로 믿지 않고 렌더링으로 확인했다.
- "최소 params"는 그 값만 담은 번들로 guest 화면에서 그려진 조합이다. 선택 필드를 뺀 최소 조합도 임시 대시보드로 따로 확인했다(`area`, `scatter`, `mixed`, `waterfall`(시간 x, 범주 x), `gauge`, `histogram`, `pivot_table`, `bubble`).

### 10-1. 축약으로 올린 16종 (모두 통과)

| 축약 `viz` | `viz_type` | 최소 params(실측 통과) | 근거 예제 | 메모 |
|---|---|---|---|---|
| `area` | `echarts_area` | `x_axis`, `metrics[]`, `groupby: []` | `world_health/World_s_Pop_Growth` | `stack: "Stack"`은 선택(`stacked: true`). 시간 x면 `time_grain_sqla` |
| `scatter` | `echarts_timeseries_scatter` | `x_axis`, `metrics[]`, `groupby: []` | `featured_charts/Scatter_Plot` | |
| `mixed` | `mixed_timeseries` | `x_axis`, `metrics[]`, `metrics_b[]`, `groupby: []`, `groupby_b: []`, `seriesType`, `seriesTypeB` | `featured_charts/Mixed` | `seriesType` 값은 `line`·`bar`·`scatter`·`smooth`(예제의 `echarts_timeseries_line`은 예전 값). 둘째 묶음을 오른쪽 축에 두려면 `yAxisIndexB: 1`, 그 축 형식은 `y_axis_format_secondary` |
| `waterfall` | `waterfall` | `x_axis`, `metric`, `groupby: []` | `featured_charts/Waterfall` | x는 시간 열·범주 열 모두 됨. 시간 x의 눈금 기본 형식은 시각까지 나와(`03 PM`) 갤러리는 `params: { x_axis_time_format: "%Y-%m" }`를 더했다 |
| `funnel` | `funnel` | `groupby[]`, `metric`, `sort_by_metric: true` | `featured_charts/Funnel` | |
| `gauge` | `gauge_chart` | `metric`, `groupby: []` | `featured_charts/Gauge` | `min_val`/`max_val`을 생략하면 최댓값은 값의 2배로 잡힌다 |
| `treemap` | `treemap_v2` | `groupby[]`, `metric` | `world_health/Treemap` | `groupby` 순서가 바깥 → 안 |
| `sunburst` | `sunburst_v2` | `columns[]`, `metric` | `world_health/Rural_Breakdown` | `columns` 순서가 안쪽 → 바깥쪽 |
| `heatmap` | `heatmap_v2` | `x_axis`, `groupby`(열 **하나**, 목록 아님), `metric` | `sales_dashboard/Number_of_Deals_for_each_Combination` | 축약 `y` → `groupby` |
| `pivot_table` | `pivot_table_v2` | `groupbyRows[]`, `groupbyColumns[]`(둘 다 `[]` 가능), `metrics[]`, `aggregateFunction: "Sum"`, `metricsLayout: "COLUMNS"` | `usa_births_names/Pivot_Table_v2` | 값 형식은 `valueFormat` |
| `histogram` | `histogram_v2` | `column`, `groupby: []` | `fcc_new_coder_survey/Last_Year_Income_Distribution` | `bins` 생략 시 5 |
| `box_plot` | `box_plot` | `columns[]`(분포 단위), `groupby[]`(상자 묶음), `metrics[]`, `whiskerOptions: "Tukey"` | `world_health/Box_plot` | 키는 `whiskerOptions`다(예제의 `whisker_options`는 예전 이름). 이 값이 없으면 프론트엔드가 boxplot 후처리를 붙이지 않으므로 축약이 항상 쓴다 |
| `bubble` | `bubble_v2` | `entity`, `x`, `y`, `size`(셋 다 지표) | `featured_charts/Bubble` | `series`는 선택(색). `max_bubble_size` 없이도 그려진다 |
| `radar` | `radar` | `groupby[]`, `metrics[]` | `featured_charts/Radar` | [실측] 크기가 크게 다른 지표(천 단위와 십 단위)를 섞으면 작은 쪽이 중심에 뭉쳐 읽을 수 없었다. 크기가 비슷한 지표끼리 쓴다 |
| `sankey` | `sankey_v2` | `source`, `target`, `metric` | `fcc_new_coder_survey/First_Time_Developer_Commute_Time` | 갤러리는 양쪽 열의 값이 겹치지 않는 데이터로만 확인했다(순환이 있는 데이터는 확인하지 않음) |
| `word_cloud` | `word_cloud` | `series`, `metric` | `usa_births_names/Boy_Name_Cloud` | 차트가 좁으면 큰 단어가 잘린다. 갤러리는 한 행 전체 너비로 배치 |

### 10-2. 원본 방식으로 남긴 유형

| `viz_type` | 최소 params(실측) | 근거 예제 | 데이터 조회 | guest 화면 | 메모 |
|---|---|---|---|---|---|
| `graph_chart` | `source`, `target`, `metric`, `layout: circular` | `featured_charts/Graph` | 200 | 통과 | |
| `tree_chart` | `id`, `parent`, `name`, `root_node_id`, `metric` | `featured_charts/Tree` | 200 | 통과 | 뿌리 행의 부모 열은 비워 둔다 |
| `gantt_chart` | `start_time`, `end_time`, `y_axis`, `series` | `featured_charts/Gantt` | 200 | 통과 | 왼쪽 항목 이름이 잘리면 `y_axis_title_margin`을 넓힌다(갤러리 80) |
| `chord` | `groupby`(열 하나), `columns`(열 하나), `metric` | `slack_dashboard/Cross_Channel_Relationship` | 200 | **실패: "Data error / Access is Denied"** | 아래 설명 |
| `world_map` | `entity`, `country_fieldtype`, `metric`, `show_bubbles` | `world_health/Rural` | 200 | **실패: 같은 오류** | `show_bubbles: true`는 `secondary_metric`이 있어야 의미가 있다 |
| `country_map` | `select_country`, `entity`(ISO 3166-2), `metric` | `misc_charts/Birth_in_France_by_department_in_2016` | 200 | **실패: 같은 오류** | `select_country: korea`의 지역 코드는 `KR-11` 꼴(이미지의 geojson에서 확인) |

- **`chord`, `world_map`, `country_map`이 guest 화면에서 안 그려지는 이유:** 이 세 유형은 예전 조회 경로 `POST /superset/explore_json/`를 쓴다. guest 역할(`EmbedGuest`, 5절의 권한 네 개)에는 `can_explore_json on Superset`이 없어 403이 난다. [실측] Superset 로그의 `POST /superset/explore_json/?form_data={"slice_id":…}&dashboard_id=… 403`.
- [실측] 테스트 스택에서만 `EmbedGuest`에 `can_explore_json on Superset`을 임시로 더하니 세 유형 모두 guest 화면에서 그려졌다(확인 뒤 `init_guest_role.py`를 다시 돌려 권한 3개로 되돌렸다). **결정: 이 권한을 주지 않는다.** 이후 실측한 접근 경계와 SQL 노출을 포함한 결정은 13절을 따른다.
- 이 세 유형은 그래서 축약으로 올리지 않고 갤러리에도 넣지 않았다(임시로 넣어 확인한 뒤 뺐다). 원본 방식 조각은 `reference/chart-catalog.md` 4절에 남겼다. Superset 관리자 화면에서는 그려지고, 데이터 조회(`POST /api/v1/chart/data`)는 200이었다.

## 11. "외부에서 관리됨" 표식과 상태 파일 없는 prune (보강설계서 3-5, 7절 D3)

- 확인일: 2026-11-20. 방법: 테스트 스택(`dashkit-test`, Superset 6.1.0)에 갤러리 사본(임시 프로젝트)의 번들을 넣었다. 번들의 데이터베이스·데이터셋·차트·대시보드 YAML 최상위에 `is_managed_externally: true`, `external_url: "dashkit://<프로젝트>/<종류>/<키>"`를 더했다. 화면은 헤드리스 Chromium으로 관리자 로그인해 봤다. 소스는 이미지 안 `/app/superset`(파이썬)과 `/app/superset/static/assets`(프론트엔드 번들)다.

| 질문 | 결과 | 근거 |
|---|---|---|
| import가 값을 저장하는가 | **저장한다. 네 종류 모두.** `GET /api/v1/<종류>/<id>`의 `is_managed_externally`가 true, 메타데이터 DB의 `external_url` 열에 문자열이 들어갔다 | [실측]. [소스] 모델의 `extra_import_fields = ["is_managed_externally", "external_url", …]`(`models/slice.py`, `models/dashboard.py`, `connectors/sqla/models.py`, `models/core.py`)를 `import_from_dict`가 `export_fields`와 합쳐 받는다(`models/helpers.py`). import 스키마 `ImportV1*Schema`에도 두 필드가 있다 |
| 키를 빼고 다시 import하면 | **이전 값이 그대로 남는다.** `false`를 명시해야 풀린다 | [실측] true로 넣은 뒤 키 없는 번들을 다시 넣어도 true. `is_managed_externally: false`를 넣으면 false |
| export에 나오는가 | **나오지 않는다**(네 종류 모두). import 전용 필드다 | [실측] `GET /api/v1/dashboard/export/` 결과의 키 목록. 그래서 `tests/test_superset_keys.py`의 대조 기준에 `superset-6.1.0-export/managed/import-only-keys.yaml`을 더했고, 저장 여부는 `tests/test_sync.py`의 통합 테스트가 본다 |
| 목록 API 필터로 찾을 수 있는가 | **없다.** `filters:!((col:is_managed_externally,…))`, `(col:external_url,…)` 모두 400 `Filter column: … not allowed to filter` | [실측]. [소스] 각 `api.py`의 `search_columns`에 두 열이 없다 |
| 목록 API 열로 읽을 수 있는가 | `is_managed_externally`: **차트·대시보드 목록에는 나온다.** 데이터셋·데이터베이스는 목록에는 없고 한 건 조회(`GET /api/v1/<종류>/<id>`)에만 나온다. `external_url`: **REST로는 어디서도 읽을 수 없다**(쓰기 전용) | [실측] `columns:!(id,uuid,is_managed_externally,external_url)`. [소스] `charts/api.py`의 `list_columns`, `dashboards/api.py`의 `BASE_LIST_COLUMNS`, `datasets/api.py`·`databases/api.py`의 `show_columns` |
| 화면이 편집을 막는가 | **막는다**(아래 표). REST(`PUT`, import)는 막지 않으므로 sync는 그대로 덮어쓴다 | [실측] 표식 있는 객체와 없는 객체를 같은 화면에서 비교 |

| 화면 | 표식이 있을 때 | 근거 |
|---|---|---|
| 대시보드 | **Edit dashboard 버튼이 없다.** 속성 창의 저장도 막힌다 | [실측] 버튼 유무. [소스] 프론트엔드 `dash_edit_perm && !is_managed_externally` |
| 차트(Explore) | 저장 창에서 **Save (Overwrite)가 비활성**, Save as…만 된다. 차트 속성 창의 저장도 막힌다("This chart is managed externally, and can't be edited in Superset") | [실측] 라디오 버튼 상태. [소스] `canOverwriteSlice()`가 `!slice.is_managed_externally`를 본다 |
| 데이터셋 편집 창 | **Save 비활성**, 안내 문구 "This dataset is managed externally, and can't be edited in Superset" | [실측] |
| 데이터베이스 편집 창 | **Finish 비활성**, 같은 문구 | [실측] |
| 목록의 삭제 | 막지 않는다(휴지통 아이콘이 그대로 있다) | [실측] |

- **결론(설계서와 달라진 점):** 표식은 저장되고 화면 편집도 막힌다(D3 통과). 다만 "목록 API 필터로 표식 있는 객체를 찾는다"는 되지 않고, `external_url`은 읽을 수 없어 **표식만으로는 어느 프로젝트의 객체인지 알 수 없다.** 그래서 설계서의 두 방법을 합쳤다. 프로젝트는 대안이던 **데이터베이스 이름**(`dashkit-<프로젝트>`)에서 따라가고(DB → 데이터셋 → 차트 → 대시보드), 그 안에서 **표식이 있는 것만** 지움 대상으로 삼는다. 규칙은 `state-and-uuid.md`의 "표식"에 있다.
- 따라가는 데 쓰는 조회([실측] 모두 200): 데이터베이스 `filters:!((col:database_name,opr:eq,value:'dashkit-<프로젝트>'))`, 데이터셋 `filters:!((col:database,opr:rel_o_m,value:<DB id>))`(열 `database.id`), 차트 목록의 `datasource_id`·`datasource_type`·`dashboards.id`·`is_managed_externally`, 대시보드 목록의 `is_managed_externally`. 목록은 `page_size:100`으로 읽고(20으로도 페이지 넘김 확인) `order_column:id`는 400(`Invalid order by column`)이라 기본 순서로 페이지를 넘기며 id로 한 번씩만 센다.
- **`SYNC_LOCK_UI=0`:** `is_managed_externally: false`를 **명시해** 쓴다(키를 빼면 풀리지 않는다). 이때는 표식이 없으므로 "sync가 만든 객체"의 증거로 **UUID 버전**을 쓴다. sync의 UUID는 uuid5(버전 5)이고 Superset 화면·REST로 만든 객체는 uuid4다([소스] `models/helpers.py`의 `UUIDMixin`(`default=uuid.uuid4`). [실측] REST로 만든 차트의 UUID 버전 4, 통합 테스트).
- 확인하지 않은 것: Superset의 다른 화면(SQL Lab에서 데이터셋 덮어쓰기, 태그·소유자 일괄 편집)이 표식을 보는지.

## 12. 배치 노드와 필터 확장 (보강설계서 4-3)

- 확인일: 2026-11-20. 방법: `projects/chart-gallery`의 `shelf_blocks` 대시보드를 테스트 스택에 적용하고 (1) 관리자 화면의 필터 설정 창에서 유형별 기본값을 **화면으로 넣어 저장**한 뒤 `GET /api/v1/dashboard/export/`로 형태를 확인하고, (2) 웹앱(guest 임베드) 화면에서 그려지는지와 필터가 적용되는지를 헤드리스 Chromium으로 봤다.

### 12-1. 배치 노드

| 노드 | 번들에 쓰는 형태 | 근거 |
|---|---|---|
| `HEADER` | `GRID`(또는 `TAB`)의 자식. `meta: {text, headerSize: MEDIUM_HEADER, background: BACKGROUND_TRANSPARENT}`. `ROW` 안에는 넣지 않는다 | [소스] 프론트엔드의 새 노드 기본값(`New header`, `headerSize`, `background`). [실측] import → export에 그대로 남고 guest 화면에 그려짐 |
| `MARKDOWN` | `ROW`의 자식. `meta: {code, width, height}` | [소스] 예제 `examples/fcc_new_coder_survey/dashboard.yaml`, 프론트엔드 기본값(`width`, `height: 50`). [실측] |
| `TABS` | `GRID`의 자식. `meta: {}`, `children`은 `TAB` | [소스] 같은 예제(`parents: [ROOT_ID, GRID_ID]`). [실측] |
| `TAB` | `meta: {text}`. 자식은 `ROW`·`HEADER` | [소스] 같은 예제. 화면이 만드는 `defaultText`·`placeholder`는 없어도 된다. [실측] 탭 전환, 탭 안 차트 조회 |
| `parents` | 뿌리부터 부모까지의 id 목록. 탭 안 차트는 `[ROOT_ID, GRID_ID, TABS-…, TAB-…, ROW-…]` | [소스] 같은 예제 |

- 노드 id는 우리가 정한다(화면은 임의 문자열을 쓴다). 배치 안 위치에서 만든다: `ROW-<i>`, `HEADER-<i>`, `TABS-<i>`, `TAB-<i>-<t>`, 탭 안은 `ROW-<i>-<t>-<k>`, 글은 `MARKDOWN-<행 번호>-<칸>`.

### 12-2. 필터 유형과 기본값

화면에서 기본값을 넣고 저장한 뒤 export한 `native_filter_configuration` 항목이다(관계없는 키 생략).

| 설정 `type` | `filterType` | `targets` | `controlValues`(화면 저장) | 화면이 저장한 `defaultDataMask` |
|---|---|---|---|---|
| `select` | `filter_select` | `[{column: {name}, datasetUuid}]` | `multiSelect`, `enableEmptyFilter`, `defaultToFirstItem`, `inverseSelection`, `searchAllOptions`, `creatable: true` | `{extraFormData: {filters: [{col, op: "IN", val: ["상"]}]}, filterState: {value: ["상"], label: "상", validateMessage: false, validateStatus: false, excludeFilterValues: true}}` |
| `time_range` | `filter_time` | 없음(예제는 `[{}]`) | `enableEmptyFilter` | `{extraFormData: {time_range: "Last quarter"}, filterState: {value: "Last quarter"}}` |
| `number_range` | `filter_range` | `[{column: {name}, datasetUuid}]` | `enableEmptyFilter`, `rangeDisplayMode: "slider-and-input"` | `{extraFormData: {filters: [{col, op: ">=", val: 5}, {col, op: "<=", val: 30}]}, filterState: {value: [5, 30], label: "5 ≤ x ≤ 30", validateMessage: ""}}` |
| `time_grain` | `filter_timegrain` | `[{datasetUuid}]` | `enableEmptyFilter` | `{extraFormData: {time_grain_sqla: "P1M"}, filterState: {value: ["P1M"], label: "Month"}}` |

- **번들에 쓰는 것:** `extraFormData`는 위와 같게, `filterState`는 `value`와 `label`만(`time_range`는 `value`만), `ownState: {}`. 화면 상태값(`validateMessage`, `validateStatus`, `excludeFilterValues`)과 `creatable`, `rangeDisplayMode`는 쓰지 않는다. [실측] 이 형태로 guest 화면의 필터 막대에 기본값이 보이고(기간 문자열, "상", 5–40, Month) 차트에 적용됐다(등급 기본값으로 출하 건수 240 → 123).
- **기본값이 없는 필터**는 지금까지와 같이 `{extraFormData: {}, filterState: {}, ownState: {}}`다.
- **`time_grain`은 데이터셋이 있어야 한다.** [실측] `targets: [{}]`로 바꾸면 선택지를 못 읽어 값이 `P1M` 그대로 보인다. 데이터셋이 있으면 "Month"와 선택지 14개가 나온다. 설계서 예시(`{ key, label, type: time_grain }`)와 달리 `dataset`을 필수로 했다.
- **`time_range` 필터는 차트에 시간 조건이 있어야 적용된다.** [실측] 최소 params 차트에는 기본값 "Last quarter"(데이터가 없는 기간)를 줘도 숫자가 그대로였다. 필터 설정 창의 안내문도 같다("Dashboard time range filters apply to temporal columns defined in the filter section of each chart"). 화면에서 만든 차트에는 `adhoc_filters: [{clause: WHERE, subject: <시간 열>, operator: TEMPORAL_RANGE, comparator: "No filter", expressionType: SIMPLE}]`가 기본으로 붙는다(`superset-6.1.0-export/charts/`). 그래서 번들이 **`time_range` 필터가 있는 대시보드의 축약 차트**에 이 조건을 붙인다. [실측] 붙인 뒤 기본값 `2026-03-01 : 2026-04-01`로 상자 수 2,876 → 366.
- **연쇄:** 자식 필터의 `cascadeParentIds: ["NATIVE_FILTER-<부모 키>"]`. [실측] guest 화면에서 부모(골짜기)를 하나 고르고 적용하면 자식(농원) 선택지가 6개 → 2개.
- **적용 범위:** `scope: {rootPath: ["ROOT_ID"], excluded: [<차트 id>, …]}`. import가 `excluded`의 값을 "position의 `meta.chartId` → 실제 차트 id"로 바꾼다. [소스] `commands/dashboard/importers/v1/utils.py`의 `update_id_refs`(`id_map`). 자리표시 `chartId`가 모두 0이면 구분할 수 없으므로, `charts`를 쓴 필터가 있는 대시보드에서는 차트마다 다른 번호(1부터)를 쓴다. [실측] `excluded: [1, 2, 3, 5, 6, 7]`이 import 뒤 실제 id 6개로 바뀌었고, guest 화면에서 범위 안 차트만 필터 수가 하나 더 많았다(4 대 3). `chartsInScope`·`tabsInScope`는 여전히 쓰지 않는다(없어도 프론트엔드가 `scope`에서 계산한다).
- **교차 필터:** `metadata.cross_filters_enabled: true`만 쓴다(`chart_configuration` 없이). [실측] guest 화면에서 막대를 누르면 다른 차트의 숫자가 바뀌었다.

### 12-3. guest 임베드 화면의 한계

- **기간(`time_range`) 필터:** `can_time_range on Api`를 부여해 선택 창에서 기간을 바꿀 수 있다. [기존 실측] 기본 기간 적용, 선택 후 차트 재조회는 13절을 따른다. 차트에 시간 조건이 필요하며 원본 차트에 대해서는 12-2절의 범위를 따른다. 기존 역할에는 변경된 초기화를 실행해야 권한이 반영된다.
- [실측, 이슈 #49] 격리된 실제 웹앱 임베드에서 기간을 `2026-01-01 : 2026-07-01`에서 `2026-03-01 : 2026-04-01`로 바꾸고 적용했을 때, 범위 안 상자 수는 2,876 → 366으로 바뀌고 범위 밖 무게는 34,214로 유지됐다. 기간 해석과 차트 재조회는 200이었다. 테스트 설정과 범위는 [검증 기록](../reference/time-range-filter-design.md#구현-검증-결과-2026-11-21)을 따른다.
- 필터 막대의 문구("Filters and controls", "Apply filters")는 영어다(언어 설정은 보강설계서 4-4, 6단계).

## 13. guest 권한 추가 실측(TimeRange, explore_json)

당시 기간 필터를 바꿀 수 없던 문제와 10-2절(`chord`·`world_map`·`country_map`이 그려지지 않음)의 원인인 권한 두 개를 guest 역할에 하나씩 더해 보고, 4절 표의 경계가 그대로인지와 그 권한이 새로 여는 엔드포인트를 확인했다. **이 실측 당시** 저장소 권한은 바꾸지 않았다(3개). 이후 이슈 #49에서 기간 해석 권한을 승인해 현재 역할에는 네 권한을 설정한다. 결론은 13-5절이다.

### 13-1. 검증 환경과 방법

- 확인일: 2026-11-21. 테스트 스택(`dashkit-test-<식별자>`, main `ab2fc92`, Superset 6.1.0). 이미지 안 `/app/superset`의 소스는 `apache/superset:6.1.0`과 같다(`views/core.py`, `security/manager.py` 등 md5 일치). 아래 [소스]는 이 경로 기준이다.
- 임시 프로젝트: `chart-gallery` 사본에 데이터셋 `region_sales`(나라 코드, 지역 코드)와 대시보드 `shelf_legacy`(`chord`(depot_hauls), `world_map`·`country_map`(region_sales), 10-2절의 최소 params)를 더해 적용했다. 이하 "내 대시보드"는 `shelf_legacy`, "다른 대시보드"는 `shelf_blocks`(데이터셋 harvests)다. 끝난 뒤 지웠다.
- guest token: tools 컨테이너에서 4절 claim 형식으로 직접 서명했다(`resources`는 `shelf_legacy`의 임베드 UUID). 요청에는 `X-GuestToken`과 웹앱 출처의 `Referer`를 붙였다.
- 권한: superset 컨테이너에서 앱 컨텍스트 파이썬으로 `find_permission_view_menu(perm, view)`를 찾아 `EmbedGuest`의 `role.permissions`에 넣고 뺐다(5절과 같은 방식). 세 상태를 차례로 쟀다. **기준선**(3개), **(a)** 기준선 + `can_time_range on Api`, **(b)** 기준선 + `can_explore_json on Superset`. 끝에 3개로 되돌린 것을 확인했다.
- **권한 이름 정정:** 12-3절의 `can_read on TimeRange`라는 권한-뷰는 **없다**(`find_permission_view_menu("can_read", "TimeRange")` → None). `/api/v1/time_range/`는 `Api` 뷰의 `time_range` 메서드이고 FAB 권한은 메서드 단위라 **`can_time_range on Api`**다. [소스] `views/api.py:55`(`class Api`), `:98-103`(`@has_access_api`, `@expose("/v1/time_range/")`). [실측] 권한-뷰 목록에 `can_time_range on Api`, `can_explore_json on Superset`이 있다.
- **반영 시점:** 권한을 넣거나 빼면 Superset을 다시 시작하지 않아도 다음 요청부터 바로 반영됐다(캐시 없음). guest 사용자의 역할은 요청마다 `find_role(GUEST_ROLE_NAME)`으로 만든다. [소스] `security/manager.py:3033-3070`(`get_guest_user_from_request`). [실측] 뺀 직후 같은 토큰으로 403.
- 화면: 헤드리스 Chromium(`mcr.microsoft.com/playwright/python:v1.55.0-noble`)에서 웹앱 출처(`http://localhost:<웹앱 포트>`)의 빈 페이지에 SDK 0.4.0으로 대시보드를 임베드했다(웹앱 로그인 없이 같은 토큰). 이 이미지에는 Python 패키지 `playwright`가 없어 `playwright==1.55.0`을 임시 폴더에 따로 설치해 썼다.

### 13-2. 실측 표

상태 열은 기준선 / (a) TimeRange / (b) explore_json 순서다. 요청은 모두 "내 대시보드" 토큰으로 보냈다.

| 요청 | 기준선 | (a) | (b) | 비고 |
|---|---|---|---|---|
| `GET /embedded/<내 UUID>`, `GET /api/v1/me/roles/` | 200 | 200 | 200 | |
| `GET /api/v1/dashboard/<내 id>`, `/charts`, `/datasets` | 200 | 200 | 200 | |
| `GET /api/v1/dashboard/<다른 id>`, `/charts` | 404 | 404 | 404 | |
| `GET /api/v1/database/`, `GET /api/v1/dataset/` | 403 | 403 | 403 | |
| `GET /api/v1/chart/` | 200, 0건 | 같음 | 같음 | |
| `GET /api/v1/security/csrf_token/` | 403 | 403 | 403 | |
| `POST /api/v1/chart/data` 내 차트 데이터셋(저장된 지표, 임의 SQL 지표·다른 열) | 200 | 200 | 200 | 4절 정정과 같은 한계 |
| 〃 서브쿼리 지표 | 400 | 400 | 400 | "Custom SQL fields cannot contain sub-queries." |
| 〃 다른 데이터셋(harvests) + 내 차트 `slice_id` | 403 | 403 | 403 | |
| 〃 다른 대시보드의 차트 `slice_id`(내 대시보드 id / 다른 대시보드 id) | 403 / 403 | 같음 | 같음 | |
| 〃 `result_type: samples` | 200(데이터셋 모든 열 10행) | 같음 | 같음 | 기존 한계 |
| 〃 `result_type: query` | 200, **SQL 문 없음**(`{"language": "sql"}`만) | 같음 | 같음 | guest에게는 SQL을 지운다 |
| 〃 `result_format: csv` | 403 | 403 | 403 | |
| `PUT`/`DELETE chart`, `PUT dashboard`, `POST dashboard`, `POST sqllab/execute/` | 400 또는 302, 변경 없음 | 같음 | 같음 | |
| `GET /api/v1/form_data/?slice_id=<다른 대시보드 차트>` | 403 | 403 | 403 | 같은 `Api` 뷰의 다른 메서드 |
| `POST /api/v1/query/` | 302(CSRF) | 302 | 302 | 같은 `Api` 뷰의 다른 메서드 |
| `GET /superset/slice/<내 차트>/`, `GET /superset/warm_up_cache/` | 302 / 403 | 같음 | 같음 | 같은 `Superset` 뷰의 다른 메서드 |
| aud 불일치·만료·비밀값 불일치 토큰 | 401 | 401 | 401 | |
| **`GET /api/v1/time_range/?q='Last week'`**(목록·`shift`, `DATEADD(...) : now` 식 포함) | 403 | **200** | 403 | 아래 13-3 |
| 〃 `q='2026-01-01 : 2026-02-01'' OR 1=1'` | 403 | 400 "Unexpected time range" | 403 | 파서 오류 |
| **`POST /superset/explore_json/`** 저장된 `chord`·`world_map`·`country_map`(`slice_id`, `dashboardId`) | 403 | 403 | **200** | GET도 200 |
| 〃 같은 데이터셋에서 임의 SQL 지표·다른 열(`haul_no`)·다른 `viz_type` | 403 | 403 | **200** | `chart/data`와 같은 한계 |
| 〃 서브쿼리 지표, `WHERE` 서브쿼리 | 403 | 403 | 400 | 같은 서브쿼리 검사 |
| 〃 `WHERE` 괄호 탈출(`1=1) UNION SELECT … FROM pg_user --`) | 403 | 403 | 400 | "Invalid SQL clause" |
| 〃 Jinja 식(`{{ 1 + 1 }}`) | 403 | 403 | 400 | 템플릿 처리 꺼짐(`ENABLE_TEMPLATE_PROCESSING: False`), 문자로 남아 DB 오류 |
| 〃 `datasource`를 harvests로(내 차트 `slice_id`) | 403 | 403 | 403 | |
| 〃 다른 대시보드의 차트 `slice_id`(내 대시보드 id / 다른 대시보드 id) | 403 | 403 | 403 / 403 | |
| 〃 `slice_id` 없음 + 내 대시보드 데이터셋 / 다른 데이터셋 | 403 | 403 | 200 / 403 | drill 경로. `chart/data`도 200 / 403(같은 판정 함수) |
| 〃 `dashboardId` 없음 | 403 | 403 | 403 | |
| 〃 `?csv=true`, `?xlsx=true` | 403 | 403 | 403 | `can_csv` 없음 |
| 〃 `?results=true`, `?samples=true` | 403 | 403 | 200 | `chart/data`의 full·samples와 같은 데이터 |
| 〃 `?query=true` | 403 | 403 | **200, SQL 문 전체** | `SELECT … FROM dash.depot_hauls GROUP BY …` |
| 〃 기본 응답(JSON) | 403 | 403 | **200, `query` 필드에 SQL 문 전체** | 오류일 때도 `query`와 DB 오류 문구가 온다 |
| `POST /superset/explore_json/table/<harvests id>/` + form_data의 `datasource`가 내 데이터셋 / 없음 | 403 | 403 | 200 / 403 | 경로보다 form_data가 우선, 판정은 실제로 조회할 데이터셋으로 한다 |
| `GET /superset/explore_json/data/<임의 키>` | 403 | 403 | 404 "Cached data not found" | 비동기 쿼리 꺼짐 |

화면([실측], 같은 토큰으로 임베드):

| 화면 | 기준선 | (a) | (b) |
|---|---|---|---|
| `shelf_blocks`의 기간 필터 | 기본값 적용. 선택 창에서 범위를 바꾸면 "Actual time range"가 "Access is Denied", APPLY 비활성(`time_range` 403) | **바꿀 수 있다.** "Last quarter"로 바꾸고 APPLY → Apply filters → 차트가 다시 조회되어 숫자가 2,876 → "No data"(데이터 없는 기간) | 기준선과 같음 |
| `shelf_legacy`의 `chord`·`world_map`·`country_map` | 세 자리 모두 "Data error / Access is Denied"(`explore_json` 403) | 기준선과 같음 | **세 개 모두 그려진다**(`explore_json` 200 3건) |

### 13-3. TimeRange가 여는 것

- `time_range`는 문자열을 날짜 두 개로 바꿔 돌려줄 뿐이다. `get_since_until`(`utils/date_parser.py`)로 해석하고 DB나 메타데이터를 읽지 않는다. 응답은 `{since, until, timeRange, shift}`이고 `timeRange`는 보낸 문자열을 그대로 돌려준다. 해석할 수 없는 문자열은 400 "Unexpected time range"다. [소스] `views/api.py:98-127`. [실측] `'select 1 from raw.harvests'`도 날짜로만 해석됐다(`since: ""`, `until: <오늘>`).
- FAB 권한은 메서드 단위라 같은 `Api` 뷰의 `query_form_data`(`/api/v1/form_data/`, 아무 차트의 저장된 form_data를 접근 검사 없이 돌려준다)와 `query`(`/api/v1/query/`)는 열리지 않는다. [소스] `views/api.py:58-95`. [실측] (a)에서도 403·302.
- Superset의 Public 역할 권장 목록에도 들어 있다(`("can_time_range", "Api")`). [소스] `security/manager.py:410-440`(`PUBLIC_ROLE_PERMISSIONS`)

### 13-4. explore_json이 여는 것

- **접근 판정은 `chart/data`와 같은 함수다.** `explore_json`은 `@check_resource_permissions(check_datasource_perms)`로 감싸져 있고, 이 함수가 form_data로 viz 객체를 만든 뒤 `viz_obj.raise_for_access()` → `security_manager.raise_for_access(viz=…)`를 부른다. 그 안의 데이터셋 분기는 `chart/data`(`query_context`)와 같다: guest는 form_data의 `dashboardId` 대시보드에 접근할 수 있고, `slice_id` 차트가 그 대시보드에 있으며 그 차트의 데이터셋이 조회할 데이터셋과 같을 때(또는 `slice_id` 없이 그 대시보드의 데이터셋일 때, drill 경로)만 통과한다. [소스] `views/core.py:272-293`(데코레이터), `views/utils.py:483-540`(`check_datasource_perms`), `viz.py:701-708`, `security/manager.py:2578-2680`(데이터셋 분기), `:721-761`(`has_drill_access`). [실측] 13-2 표의 403 행들.
- **"payload를 바꾸지 말라" 검사는 viz 경로에 없다.** `query_context_modified` 검사는 `query_context`가 있을 때만 돈다. [소스] `security/manager.py:2563-2576`. 지금은 `chart/data`에서도 이 검사가 건너뛰어지므로(4절 정정) 데이터 범위는 늘지 않는다. 다만 나중에 이 검사가 `chart/data`에서 동작하게 되더라도(예: guest에게 차트 목록 권한을 줌) `explore_json`은 계속 우회로로 남는다.
- **서브쿼리·SQL 절 검사도 같다.** 임의 SQL 지표와 `WHERE` 식은 같은 `_process_sql_expression` → `validate_adhoc_subquery`(서브쿼리 거부, `ALLOW_ADHOC_SUBQUERY: False`)와 `sanitize_clause`를 지난다. [소스] `models/helpers.py:187-214`, `:917-936`
- **CSV·XLSX는 막힌다.** `?csv=true`, `?xlsx=true`는 `can_csv on Superset`(또는 `GRANULAR_EXPORT_CONTROLS`이면 `can_export_data`)이 없으면 403이다. [소스] `views/core.py:311-327`
- **새로 새는 것: 생성된 SQL 문.** `chart/data`는 guest 응답에서 `query`를 지우지만(`charts/data/api.py:447-450`), `explore_json`은 기본 응답의 `query` 필드(`viz.py:626-640`)와 `?query=true`(`views/core.py:152-167`, `get_query_string_response`)로 SQL 문 전체를 돌려준다. 오류 응답에도 `query`가 붙는다. 드러나는 것은 스키마·뷰 이름(`dash.<뷰>`), 데이터셋 지표의 SQL 식, 조회 조건이다. 열 이름은 `samples`로 이미 보인다. `stacktrace`는 `SHOW_STACKTRACE = False`(기본)라 비어 있다.
- 그 밖: CSRF 예외 목록에 있다(`config.py:307-313`, 읽기 전용이라 CSRF는 상관없다). 6.1.0에서 `@deprecated(eol_version="5.0.0")`가 붙은 예전 경로다(`views/core.py:239`, `:292`). 비동기 쿼리(`GLOBAL_ASYNC_QUERIES`)는 꺼져 있어 `explore_json/data/<키>`는 404다. 같은 `Superset` 뷰의 다른 메서드(`slice`, `warm_up_cache`)는 열리지 않는다.

### 13-5. 결론

| 권한 | 판단 | 이유 |
|---|---|---|
| `can_time_range on Api` | **넓혀도 된다** | 날짜 문자열 해석만 하고 데이터·메타데이터를 읽지 않는다. 4절 경계는 그대로다(13-2 표). 같은 뷰의 다른 메서드는 열리지 않는다. Superset의 Public 권장 목록에도 있다 |
| `can_explore_json on Superset` | **지금은 넓히지 않는다**(넓혀도 데이터 범위는 늘지 않지만 조건이 있다) | 조회할 수 있는 데이터의 범위는 `chart/data`와 같다(같은 판정 함수, 같은 서브쿼리 검사, CSV 막힘). 대신 `chart/data`가 guest에게 일부러 숨기는 **SQL 문을 그대로 돌려준다.** 또 6.1.0에서 이미 없어질 예정으로 표시된 예전 경로이고, payload 변조 검사가 없는 경로를 하나 더 여는 일이다. 얻는 것은 세 유형(`chord`, `world_map`, `country_map`)뿐이다 |

- **TimeRange 확대에 따른 작업(이슈 #49):** `superset/init_guest_role.py`에 `("can_time_range", "Api")`를 추가하고 관련 설명과 `tests/test_api.py`의 기간 해석·`form_data` 경계를 갱신했다. 권한 이름은 `can_read on TimeRange`가 아니다.
- **explore_json을 넓힌다면 함께 할 것:** (1) "guest가 생성 SQL(스키마·뷰 이름, 지표식)을 볼 수 있다"를 받아들이는 결정을 이 문서 5절에 남긴다. dash 뷰 이름과 지표식에 숨길 정보가 없어야 한다. (2) `init_guest_role.py`에 권한을 더하고 `tests/test_api.py`에 `explore_json` 경계(다른 데이터셋·다른 대시보드 차트 403, 서브쿼리 400, `?csv=true` 403)를 `chart/data`와 같은 꼴로 더한다. (3) Superset을 올릴 때 이 경로가 사라지거나 판정이 바뀌는지 다시 잰다. 넓히지 않는다면 지금처럼 세 유형을 갤러리에서 빼 두고, 관계도는 `graph_chart`(10-2절)를 쓴다.
- 확인하지 않은 것: `DASHBOARD_RBAC`·`GLOBAL_ASYNC_QUERIES`·`ENABLE_TEMPLATE_PROCESSING`을 켠 구성, `rls_rules`가 있는 토큰, `deck_*`·`mapbox` 등 다른 예전 viz 유형의 화면.

## 14. 중복 열·지표 라벨("Duplicate column/metric labels")

- 확인일: 2026-11-21. 방법: `projects/chart-gallery`의 스키마·데이터를 복사한 임시 프로젝트(차트 85개: 축약 81, 원본 4)를 테스트 스택에 적용하고, 헤드리스 Chromium으로 **관리자 대시보드 화면**을 열어 차트마다 화면이 보낸 `POST /api/v1/chart/data`의 요청(쿼리의 `columns`·`metrics`)과 응답(상태, 오류 문구)을 모았다. 그래서 아래 결과에는 화면(프론트엔드)이 쿼리를 만들 때 하는 정리가 들어 있다. 데이터셋은 `harvests`, 지표 `crate_total`·`crate_again`의 표시 이름(`labels`)은 둘 다 "상자 수", 지표 `fruit_cnt`의 표시 이름은 "fruit", 열 `orchard`의 표시 이름도 "상자 수", 지표 이름 `grade`는 열 이름과 같게 두었다.
- 오류는 응답 400 `{"message": "Duplicate column/metric labels: \"valley\". Please make sure all columns and metrics have a unique label."}`이고, 화면은 차트 자리에 이 문구를 그린다. 쿼리 하나라도 오류면 차트 전체가 안 그려진다(`mixed`).

### 14-1. 소스 근거 [소스]

- `superset/common/query_object.py:279-305` `QueryObject.validate()`가 `_validate_no_have_duplicate_labels()`를 부른다: `all_labels = self.metric_names + self.column_names`에 같은 값이 있으면 `QueryObjectValidationError("Duplicate column/metric labels: …")`. 중복 찾기는 `utils/core.py:1884` `find_duplicates`(Counter).
- `metric_names`(`query_object.py:262-271`)는 `get_metric_names(self.metrics, self.datasource.verbose_map)`이다. `utils/core.py:1297` `get_metric_name`: adhoc 지표는 `label`(없으면 SQL 식, SIMPLE은 `AGG(열)`), **문자열(데이터셋 지표 이름)은 `verbose_map.get(이름, 이름)`**, 곧 표시 이름(`verbose_name`)으로 바뀐다. `connectors/sqla/models.py:428-439` `verbose_map`은 지표, 그다음 열의 `verbose_name or 이름`이다.
- `column_names`(`query_object.py:273-277`)는 `get_column_names(self.columns)`로 **`verbose_map` 없이** 부른다(`utils/core.py:1269` `get_column_name`: 문자열은 이름 그대로, adhoc 열은 `label` 또는 식). 그래서 열은 표시 이름이 아니라 열 이름으로 비교된다.
- 이 검증은 조회 경로에서 항상 돈다: `common/query_context_processor.py:89`(`get_df_payload`, 캐시 키 만들기 전), `:532`(`raise_for_access`). 사용자 종류와 무관하므로 guest 임베드 화면도 같다(관리자 화면으로 실측했다).
- x축 유형에서 화면은 `x_axis`를 쿼리 `columns`의 맨 앞에 `{columnType: "BASE_AXIS", label: <열 이름>, sqlExpression: <열 이름>}`으로 넣는다(시간 열이면 `timeGrain`도). 라벨이 열 이름이라 `groupby`의 같은 열과 겹친다. [실측] 요청 본문. 시간 단위(`time_grain`) 유무는 상관없다.
- 참고: `superset/mcp_service/chart/schemas.py:1049,1152`에도 같은 문구의 사전 검사가 있다(MCP 서비스용, dashkit은 쓰지 않음).

### 14-2. 조합별 결과 [실측]

| 조합 | 유형 | 결과 | 화면이 보낸 쿼리 |
|---|---|---|---|
| `x`와 `groupby`가 같은 열 | `bar`·`line`·`area`·`scatter`·`waterfall`, `mixed`(첫째·둘째 묶음 각각) | **오류** | `columns: [x(BASE_AXIS), groupby…]` |
| `groupby` 여럿 가운데 하나가 `x`와 같음(`x: valley, groupby: [fruit, valley]`) | `bar` | **오류** | |
| 시간 `x`와 `groupby`에 같은 시간 열(`time_grain` 있음·없음) | `line`·`scatter` | **오류** | |
| `groupby`에 같은 열 두 번 | `bar`·`line`·`area`·`scatter`·`waterfall`·`mixed`·`histogram`·`box_plot` | **오류** | 두 번 그대로 보낸다 |
| `groupby`(·`columns`)에 같은 열 두 번 | `pie`·`funnel`·`treemap`·`sunburst`·`radar`·`gauge`·`pivot_table`(행 둘, 행과 열에 같은 열)·`table`(원본 행 `columns`, 집계 `groupby`) | 통과 | 화면이 하나로 합쳐 보낸다 |
| 열 필드 둘에 같은 열 | `heatmap`(`x`=`y`), `sankey`(`source`=`target`), `bubble`(`entity`=`series`), `histogram`(`column`이 `groupby`에도), `box_plot`(`columns`와 `groupby`) | **오류** | |
| 지표 라벨이 같은 쿼리의 열 이름과 같음(`{sql, label: <열>}`) | 열이 있는 축약 유형 전부(`bar`·`line`·`waterfall`·`mixed`의 둘째 묶음·`big_number`의 시간 열·`pie`·`funnel`·`treemap`·`sunburst`·`radar`·`gauge`·`pivot_table`·`table`·`heatmap`·`sankey`·`bubble`·`box_plot`·`word_cloud`) | **오류** | 합치는 유형도 열과 지표 사이는 합치지 않는다 |
| 데이터셋 지표의 **표시 이름**이 열 이름과 같음(`fruit_cnt` → "fruit", `groupby: [fruit]`) | `bar` | **오류** | 지표는 이름(`fruit_cnt`)으로 가지만 서버가 표시 이름으로 바꿔 비교 |
| 지표 이름이 열 이름과 같음(지표 `grade`, `x: grade`) | `bar` | **오류** | |
| 열의 표시 이름이 지표 표시 이름과 같음(`orchard` → "상자 수", 지표 "상자 수") | `bar` | 통과 | 열은 표시 이름으로 비교하지 않는다 |
| 이름이 다른 두 지표의 표시 이름이 같음(`crate_total`, `crate_again`) | `bar`·`radar` | **오류** | |
| adhoc 지표 `label`이 다른 지표의 표시 이름과 같음(`{label: 상자 수}`, `crate_total`) | `bar` | **오류** | |
| 같은 지표 두 번(`[crate_total, crate_total]`, `bubble`의 `x`=`y`=`size`) | `bar`·`mixed`·`radar`·`pivot_table`·`table`·`box_plot`·`bubble` | 통과 | 화면이 하나로 합쳐 보낸다 |
| adhoc 지표 둘의 `label`이 같음(식은 다름) | `bar` | 통과 | 화면이 **라벨로** 합쳐 첫째만 보낸다(둘째 지표는 말없이 빠진다) |
| adhoc 지표 `label`이 데이터셋 지표 이름과 같음(`{label: crate_total}`, `crate_total`) | `bar` | 통과 | 위와 같이 하나만 보낸다 |
| `mixed`의 두 묶음에 같은 지표·같은 `groupby` | `mixed` | 통과 | 묶음마다 쿼리가 따로 간다 |
| `table`의 `order_by`에 같은 열 두 번 | `table` | 통과 | 정렬은 라벨이 아니다 |
| 열이 없는 쿼리의 adhoc 지표 라벨(`big_number_total`의 `{label: fruit}`) | `big_number_total` | 통과 | |
| 원본 `viz_type: echarts_timeseries_bar`의 `x_axis`=`groupby`, `viz_type: pie`의 지표 라벨=열 | 원본 | **오류** | 축약과 같다 |
| 원본 `viz_type: pie`의 `groupby`에 같은 열 두 번 | 원본 | 통과 | 축약과 같다 |
| 축약 `bar`에 `params: { groupby: [<x와 같은 열>] }` | 축약 + params | **오류** | params가 덮어쓴 값으로 간다 |

### 14-3. 추가 실측: 자동 제거와 원본 모드

2026-11-21, Superset 6.1.0의 독립 테스트 스택에 `chart-gallery`의 스키마와 데이터를 복사해 적용했다. 관리자 대시보드를 헤드리스 Chromium으로 열고 화면이 보낸 `POST /api/v1/chart/data`의 쿼리와 응답을 캡처했다. 충돌 사례는 테스트 스택의 차트 메타데이터만 바꾸어 확인했다. 아래 `a`는 `SUM(crates) AS dup`, `b`는 `COUNT(*) AS dup`다.

| 사례 | 화면이 보낸 쿼리와 응답 |
|---|---|
| `bar`에 `[a, a]` 또는 `[a, b]` | 둘 다 `columns: [valley(BASE_AXIS)]`, `metrics: [a]`, 200. 다른 SQL인 b도 전송 전 제거 |
| 저장 지표 `crate_total`과 같은 라벨의 adhoc 지표, 두 순서 | 각 순서에서 첫 항목만 `metrics`에 남고 200 |
| `mixed`의 A `[a,b]`, B `[b,a]` | 두 쿼리 각각 `[a]`, `[b]`; 200. 묶음 사이에 제거 없음 |
| `table` raw: 중복 `all_columns`, 반대 모드의 `groupby`·`metrics` 포함 | `columns: [valley]`, `metrics: null`, 200 |
| `table` aggregate: 중복 `groupby`, `all_columns`, `[a,b]` | `columns: [valley]`, `metrics: [a]`, 200; `all_columns` 무시 |
| `table` 모드 생략: `groupby: [valley]`, `all_columns: [fruit]`, `[a,b]` | `columns: [valley, fruit]`, `metrics: [a]`, 200. 명시적 aggregate와 다름 |
| `table` aggregate의 `metrics: [a]`, `percent_metrics: [b]`, 열 정렬 | `columns: [valley]`, `metrics: [a]`, 200. 뒤의 b 제거; 정렬 열은 라벨 열에 추가되지 않음 |
| 갤러리 `graph_chart`의 `source=target`, `tree_chart`의 `id=parent`, `gantt_chart`의 `y_axis=series` | 각 쿼리에서 같은 열을 하나만 보내고 200 |
| `graph_chart`의 지표 라벨=`source`, `tree_chart`의 지표 라벨=`id` | 열과 지표를 함께 보내고 400 `Duplicate column/metric labels` |
| `chord`, `world_map`, `country_map` | 별도 스택의 갤러리 차트 메타데이터를 세 유형으로 바꾸어 관리자 화면을 열었다. 셋 모두 `POST /superset/explore_json/?form_data={slice_id}&dashboard_id=…`, multipart의 `form_data` JSON으로 요청하고 200. 각각 `groupby`·`columns`·`metric`, `entity`·`country_fieldtype`·`metric`, `entity`·`select_country`·`metric`이 들어가며 `queries` 배열은 없다. 지도에 유효한 지리 데이터는 쓰지 않았으므로 렌더링 검증이 아니다. 기본 guest는 403(10절·13절); 라벨 검사는 미지원 |

### 14-4. 검사 규칙

[차트 계약](chart-shorthand.md#중복-라벨-검사)의 지원 범위와 오류 정책을 따른다. 화면이 같은 키의 서로 다른 SQL 지표 또는 저장 지표와 SQL 지표를 제거하는 경우 설정 오류로 보고, 제거 후 서버 라벨 충돌은 기존 오류로 따로 보고한다. 같은 정의의 반복과 `mixed` 쿼리 간 같은 라벨은 허용한다. `params`의 최종 값과 YAML 출처를 반영한다. SIMPLE 등 확인하지 않은 객체의 정의 차이, 다른 원본 차트와 확인 범위 밖 설정에 대한 제거 검사는 보장하지 않는다.
