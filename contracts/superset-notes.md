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
- **네이티브 필터 형식:** `metadata.native_filter_configuration[]`. 대상 데이터셋은 `targets[].datasetUuid`로 쓰면 import 때 `datasetId`로 바뀐다. `chartsInScope`는 import 때 **변환되지 않으므로 넣지 않는다**(`scope.rootPath: [ROOT_ID]`, `excluded: []`만 쓰고 열 이름이 같은 차트에 적용되게 둔다). [소스] `update_id_refs`, 공식 예제 `superset/examples/sales_dashboard/dashboard.yaml`. [실측] 아래 형식으로 임베드 화면에서 필터 표시·적용을 확인했다.

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
| `POST /api/v1/chart/data` (지표를 바꿔서) | **403** "Guest user cannot modify chart payload" |
| `GET /api/v1/dashboard/<B id>`, `/charts` | **404** |
| `GET /api/v1/database/` | **403** |
| `GET /api/v1/chart/` | 200, `count: 0` |
| `GET /api/v1/security/csrf_token/` | **403** → guest는 CSRF 토큰을 받을 수 없다 |
| `PUT/DELETE chart`, `PUT dashboard`, `POST dashboard`, `POST sqllab/execute` | 400(CSRF 없음) 또는 302 → **변경되지 않음** 확인 |
| aud 불일치, 만료, 비밀값 불일치 토큰 | **401** |

## 5. guest 역할 최소 권한

- 기본 `GUEST_ROLE_NAME = "Public"`. 우리는 전용 역할 `EmbedGuest`를 만든다. [소스] `config.py:2306`
- **최소 권한(실측):** `can_read on CurrentUserRestApi`, `can_read on Dashboard`, `can_read on Chart` **3개만으로** 6개 유형 차트 렌더링, 네이티브 필터 값 조회와 적용, 테마 전환이 동작했다. 데이터셋 `datasource_access`는 필요 없다. guest는 "대시보드에 속한 차트 + 저장된 payload"일 때만 데이터 접근이 허용된다. [소스] `security/manager.py:2564-2680`(`raise_for_access`의 guest 분기), `embedded/index.tsx:177-204`(시작 시 `GET /api/v1/me/roles/`, 권한이 없으면 "Something went wrong with embedded authentication")
- **만드는 방법:** `superset-init`에서 앱 컨텍스트 파이썬으로 `security_manager.add_role("EmbedGuest")` 후 `find_permission_view_menu(perm, view)`로 찾아 `role.permissions`에 넣는다. 반드시 `superset init` **다음에** 실행한다(권한-뷰 행이 그때 생긴다). [실측] `superset init`을 다시 실행해도 EmbedGuest 권한은 유지된다.

## 6. iframe 허용 (Talisman, frame-ancestors)

- 6.1.0 기본 `TALISMAN_ENABLED=True`이고 `TALISMAN_CONFIG`에 `frame-ancestors`가 없다. flask-talisman 1.1.0은 기본으로 `X-Frame-Options: SAMEORIGIN`을 붙인다. [소스] `config.py:2113-2158`, `initialization/__init__.py:892-902`
- 설정: 기본 `TALISMAN_CONFIG`를 deepcopy하고 `content_security_policy["frame-ancestors"] = [웹앱 출처...]`, `frame_options = None`으로 둔다(`TALISMAN_DEV_CONFIG`는 쓰지 않는다). [실측] 응답 헤더에 `frame-ancestors http://localhost:8080`이 있고 `X-Frame-Options`는 없다. 다른 출처에서는 Referer 검사로 403.
- CORS는 켜지 않았다. SDK는 iframe과 `postMessage`만 쓴다. [실측] CORS 없이 동작.
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
8. guest 역할 권한 3개는 `superset init` 다음에 부여한다.
