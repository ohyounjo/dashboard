# 보강설계서: 확장성·유지보수성 보강과 구성 UI(Studio)

> 작성일: 2026-11-19 · 대상: `main`(커밋 `8252817`) · 상태: 결정 완료(7절). 구현 전
> 관련 문서: `reference/plan.md`(원 설계), `reference/chart-catalog.md`(차트 카탈로그), `done.md`(완료 보고와 남은 한계)

## 1. 요약

구현은 원 설계를 충실히 따랐고 기반이 탄탄하다. 설정 검증이 한 곳(`dashkit/config.py`)에 있고, 번들 생성이 순수 함수이며, 권한 경계가 테스트로 고정돼 있다. 보강할 곳은 "새로운 것을 추가할 때 고쳐야 하는 곳의 수"다.

| 추가하려는 것 | 지금 고쳐야 하는 곳 | 보강 후 |
|---|---|---|
| 축약 차트 유형 하나 | 5곳 | 파일 1개 |
| 위젯 유형 하나 | 5곳 | 폴더 1개 |
| 필터 유형, 배치 요소 | 추가할 수 없음(`select`, 행·차트만) | 설정으로 추가 |
| 새 프로젝트 | 예시 폴더를 복사해 손으로 고침 | CSV 폴더에서 초안 자동 생성 |
| 데이터 교체 | CSV 편집 후 `make apply` | 그대로 + 선택적으로 화면에 끌어다 놓기 |

제안은 네 묶음이다.

1. **안전망(3절):** 번들 스냅샷 테스트, 스택 없이 도는 단위 테스트, CI. 뒤 단계의 리팩터링이 이것에 기댄다.
2. **확장 구조(4절):** 차트 유형 레지스트리, 위젯 매니페스트, 배치·필터 확장, 차트 갤러리 프로젝트.
3. **템플릿 사용성(5절):** `make new`(CSV에서 초안 생성), `make plan`(적용 전 차이 보기), 설정 버전, 설정 파일 분할.
4. **Studio(6절, 선택 기능):** 데이터 끌어다 놓기, 탭·섹션·차트 배치 편집 화면.

## 2. 리뷰 결과

### 2-1. 잘 된 점(유지할 것)

- 설정 형식의 기준이 JSON Schema 하나이고 loader, sync, api가 같은 `config.py`를 쓴다.
- `dashkit/sync/bundle.py`는 DB와 네트워크가 없는 순수 함수다. 출력이 결정적이다.
- 권한 분리: 로더만 owner 계정, Superset과 backend는 reader 계정, backend에는 Superset 계정이 없다.
- 오류를 모두 모아 `파일:위치: 이유`로 보고한다.
- 도메인 단어 grep 테스트가 범용성을 지킨다.
- Superset 동작을 소스와 실측으로 확인한 기록(`contracts/superset-notes.md`)이 있다.

### 2-2. 확장성

| # | 발견 | 근거 | 제안 |
|---|---|---|---|
| E1 | 축약 차트 유형을 하나 더하려면 5곳을 고친다. | `bundle.py`의 `VIZ_TYPES`·`FORMAT_PARAM`·`chart_params`의 if 사슬, `config.py`의 `referenced_columns`·`_metric_names`(필드 이름 고정), `dashboard.schema.json`의 `viz` enum과 `v_*` 정의, `chart-shorthand.md` | 4-1 차트 유형 레지스트리 |
| E2 | 위젯 유형을 하나 더하려면 5곳을 고친다. | `config.py`의 `WIDGET_TYPES`·`_SECTION_DEFAULTS`·`referenced_columns`의 유형별 분기, `dashboard.schema.json`, `web/app.js`의 `WIDGET_TYPES`, `contracts/widgets.md` "새 위젯 유형 추가" 4단계 | 4-2 위젯 매니페스트 |
| E3 | 필터가 `select` 하나뿐이다. 기간, 숫자 범위, 기본값, 연쇄 필터를 쓸 수 없다. | `bundle.py` `native_filters`가 `filter_select` 고정 | 4-3 |
| E4 | 배치가 행과 차트뿐이다. 제목, 설명 글, 세로 묶음, 대시보드 안 탭을 쓸 수 없다. | `bundle.py` `dashboard_position`이 `ROW`와 `CHART`만 생성 | 4-3 |
| E5 | 데이터 원본이 CSV뿐이고 매번 전체를 다시 만든다. | `loader/__init__.py` `_rebuild`가 `DROP SCHEMA`부터 시작 | 5-5 원본 어댑터(후순위) |
| E6 | 한 번에 한 프로젝트만 뜬다. 전환하면 이전 프로젝트의 데이터와 Superset 객체가 지워진다. | `done.md` 남은 한계 7 | 5-6 프로젝트별 스키마(후순위) |
| E7 | 브랜드 색이 웹앱 셸에만 적용된다. Superset 차트 색과 글꼴은 기본값이다. | `bundle.py` `color_scheme: ""`, `web/app.js` `applyBrand` | 4-4 |
| E8 | 문구가 한국어 고정이다. | `web/strings.js` 한 파일, Superset 기본 로캘 `en` | 4-4 |

### 2-3. 유지보수성

| # | 발견 | 근거 | 제안 |
|---|---|---|---|
| M1 | 번들 생성 함수에 단위 테스트가 없다. `bundle.py`를 고치면 전체 스택을 띄운 통합 테스트로만 확인된다. | `tests/`에 `build_bundle`, `chart_params`를 부르는 테스트가 없다. | 3-1 스냅샷 테스트 |
| M2 | 테스트가 실행 중인 환경을 바꾼다. `make test`는 다른 프로젝트로 전환했다가 되돌린다. 도중에 멈추면 다른 프로젝트 상태로 남는다. | `Makefile` `test` 목표 | 3-2 테스트 분리 |
| M3 | 테스트를 돌리는 CI가 없다. 워크플로는 화면 캡처 하나뿐이다. | `.github/workflows/` | 3-3 |
| M4 | Superset 버전이 여러 곳에 흩어져 있고, 번들이 6.1.0 export의 키를 전부 직접 쓴다. 업그레이드 때 어디가 깨지는지 알 수단이 없다. | `superset/Dockerfile`, `docker-compose.yml`의 이미지 태그 2곳, `contracts/superset-6.1.0-export/`, `bundle.py`의 `dataset_yaml` 등 | 3-4 버전 한 곳, 키 대조 테스트, 업그레이드 절차 |
| M5 | `.state/managed.json`이 없어지면 이전 객체를 지울 수 없다. | `done.md` 남은 한계 9 | 3-5 Superset 쪽 표식으로 찾기 |
| M6 | `dashboard.schema.json`이 1,112줄 한 파일이다. 유형별 정의가 섞여 있다. | `contracts/dashboard.schema.json` | 4-1, 4-2에서 유형별 조각으로 나뉜다 |
| M7 | 설정에 버전 표시가 없다. 형식이 바뀌면 기존 프로젝트가 왜 깨지는지 알 수 없다. | 최상위 키가 `app, datasets, dashboards, tabs`뿐 | 5-3 |
| M8 | 프로젝트가 커지면 `dashboard.yaml` 한 파일이 길어진다. SQL이 YAML 문자열 안에 있어 편집기 지원을 못 받는다. | devtracker 193줄, sales-sample 161줄 | 5-4 |
| M9 | `make apply`가 backend 이미지를 다시 빌드하고 재시작한다. 데이터만 바꿔도 코드 배포 경로를 탄다. | `Makefile` `apply` 목표 | 5-2에서 설정 다시 읽기로 대체 |
| M10 | 적용 전에 무엇이 바뀌는지 볼 수 없다. | `check`는 형식만 본다 | 5-2 `make plan` |

보안과 정확성 관점에서 새로 발견한 결함은 없다. `done.md` 남은 한계 1(guest token으로 같은 데이터셋의 다른 열 조회 가능)은 그대로 유효하다. Studio(6절)는 쓰기 권한을 가지므로 별도 컨테이너로 분리한다.

## 3. 안전망

뒤 절의 리팩터링보다 먼저 한다.

### 3-1. 번들 스냅샷 테스트
- 두 예시 프로젝트와 갤러리 프로젝트(4-5)의 번들을 고정된 뷰 열 정보로 생성해 `tests/golden/<프로젝트>/`와 비교한다. DB와 Superset이 필요 없다.
- `metadata.yaml`의 타임스탬프는 `build_bundle(timestamp=...)`로 고정한다.
- 의도한 변경이면 `make golden`으로 갱신하고 diff를 PR에서 검토한다.
- 4-1의 레지스트리 전환은 "스냅샷이 한 바이트도 바뀌지 않는다"를 완료 조건으로 삼는다.

### 3-2. 테스트 분리
| 목표 | 내용 | 필요한 것 |
|---|---|---|
| `make test-unit` | 설정 검증, 번들 스냅샷, CSV 검증, 도메인 단어 | tools 이미지만 |
| `make test` | 지금의 통합 테스트 | 전체 스택 |

통합 테스트의 프로젝트 전환은 실행 중인 스택 대신 **별도 compose 프로젝트 이름**(`COMPOSE_PROJECT_NAME=dashkit-test`, 다른 포트, 별도 볼륨)에서 돌린다. 개발 중인 화면이 테스트 때문에 바뀌지 않는다.

파일 상태도 나눈다. sync는 `STATE_DIR`(`embed.json`, `managed.json`)과 `BUILD_DIR`에 쓰고 backend는 `STATE_DIR`을 읽으므로, 테스트 스택은 전용 디렉터리(`.test/state`, `.test/build`)를 쓰고 테스트 backend도 그 디렉터리를 마운트한다. 완료 조건: `make test` 전후로 개발 스택의 `.state/`와 `.build/` 파일이 바뀌지 않는다.

### 3-3. CI
- PR마다 `make test-unit`을 돌린다.
- `dashkit/`, `superset/`, `projects/`, `contracts/`가 바뀐 PR에는 `make test`도 돌린다. 기존 화면 캡처 워크플로가 이미 전체 스택을 띄우므로 같은 작업에 단계로 붙인다.

### 3-4. Superset 버전 관리
- 버전을 `.env.example`의 `SUPERSET_VERSION` 한 곳에 두고 Dockerfile `ARG`와 compose 이미지 태그가 이를 읽는다.
- **키 대조 테스트:** 생성한 번들 YAML의 키 집합이 `contracts/superset-<버전>-export/`의 같은 종류 파일 키 집합에 포함되는지 확인한다. 업그레이드 후 export를 다시 받으면 사라진 키가 테스트로 드러난다.
- **업그레이드 절차**를 `contracts/superset-upgrade.md`로 남긴다: 버전 변경 → export 다시 받기 → 키 대조 → 갤러리 프로젝트 렌더링 확인 → `superset-notes.md` 갱신.

### 3-5. prune을 상태 파일 없이도 가능하게
- 번들의 차트·대시보드·데이터셋에 `is_managed_externally: true`와 `external_url: "dashkit://<프로젝트>/<종류>/<키>"`를 쓴다. 6.1.0의 차트·대시보드 스키마와 목록 API에 이 필드가 있는 것은 확인했다. import가 이 값을 저장하는지와 목록 API 필터로 찾을 수 있는지는 구현 전에 실측한다.
- 실측이 통과하면 prune 대상을 "표식이 있고 이번 목록에 없는 객체"로 구한다. `managed.json`은 보조 수단으로 남긴다.
- 통과하지 못하면 대안으로 `dashkit-<프로젝트>` 데이터베이스에 속한 데이터셋과 그 차트를 따라가 찾는다.
- 부수 효과: 표식이 있는 객체는 Superset 화면에서 "외부에서 관리됨"으로 표시돼 실수로 고치는 일이 줄어든다.

## 4. 확장 구조

### 4-1. 차트 유형 레지스트리

유형 하나를 파일 하나로 선언한다. 스키마, 파라미터 생성, 열 참조 검사, 문서가 모두 이 파일에서 나온다.

```yaml
# dashkit/charts/funnel.yaml
viz: funnel                      # dashboard.yaml에서 쓰는 이름
viz_type: funnel                 # Superset 이름
label: 퍼널
fields:                          # 축약 필드 → Superset 파라미터
  groupby: { kind: columns, required: true }
  metric:  { kind: metric, required: true }
  format:  { kind: d3format, param: number_format }
defaults: { sort_by_metric: true }
example:                         # 갤러리와 문서에 쓰는 예
  groupby: [stage]
  metric: item_count
```

| `kind` | 값 | 검사 |
|---|---|---|
| `column`, `columns` | 열 이름(들) | 뷰에 열이 있는지 |
| `metric`, `metrics` | 지표 이름 또는 `{ sql, label }` | 데이터셋에 지표가 있는지 |
| `time_grain` | `P1D` 등 | enum |
| `bool`, `int`, `string`, `enum`, `d3format` | 값 | 형식 |

- `param`을 생략하면 필드 이름과 같은 파라미터가 된다.
- 표의 정렬처럼 단순 대응이 안 되는 유형은 `hook: dashkit.charts.hooks:table`로 함수를 지정한다. 지금 `chart_params`의 `table` 분기가 그 함수가 된다.
- 레지스트리에서 나오는 것:
  1. `dashboard.schema.json`의 `viz` enum과 유형별 정의(로딩 때 합성)
  2. `chart_params`(if 사슬 제거)
  3. `referenced_columns`, `_metric_names`(`kind`를 보고 수집)
  4. `contracts/chart-shorthand.md` 대응표와 `reference/chart-catalog.md`의 축약 절(생성, 테스트가 최신인지 확인)
  5. 갤러리 프로젝트의 차트(4-5)
- `viz_type` + `params` 원본 방식은 그대로 둔다. 레지스트리에 없는 유형의 탈출구다.

**축약으로 올릴 유형(1차 16종 추가, 합계 22종):**

| 묶음 | 유형 |
|---|---|
| 지금(6) | `big_number_total`, `big_number`, `line`, `bar`, `pie`, `table` |
| 추이(4) | `area`, `scatter`, `mixed`, `waterfall` |
| 비율·단계(4) | `funnel`, `gauge`, `treemap`, `sunburst` |
| 교차·분포(5) | `heatmap`, `pivot_table`, `histogram`, `box_plot`, `bubble` |
| 관계·기타(3) | `radar`, `sankey`, `word_cloud` |

각 유형의 Superset 이름과 필수 파라미터는 `reference/chart-catalog.md` 4절에 있다. 나머지 유형(`chord`, `graph_chart`, `tree_chart`, `gantt_chart`, 지도)은 원본 방식으로 쓴다.

### 4-2. 위젯 매니페스트

위젯 하나를 폴더 하나로 만든다.

```
web/widgets/kanban/
├─ widget.js          # 지금의 kanban.js (계약은 contracts/widgets.md 그대로)
└─ widget.json        # 옵션 JSON Schema + 열 역할 표시
```

```json
{
  "type": "kanban",
  "label": "칸반",
  "options": {
    "type": "object",
    "required": ["dataset", "group_by", "columns", "card"],
    "properties": {
      "group_by": { "type": "string", "x-column": true },
      "card": { "properties": { "title": { "type": "string", "x-column": true },
                                "chips": { "type": "array", "items": { "type": "string", "x-column": true } } } }
    }
  }
}
```

- `config.py`는 `web/widgets/*/widget.json`을 읽어 섹션 스키마를 합성하고, `x-column` 표시를 따라 열 참조를 수집한다. `WIDGET_TYPES`, `_SECTION_DEFAULTS`, 유형별 분기가 사라진다.
- `/api/app`이 위젯 유형 목록을 함께 내려 주고 셸은 그 목록으로 `import()`를 허용한다. `app.js`의 고정 목록이 사라진다.
- Studio(6절)는 같은 `widget.json`으로 설정 폼을 만든다.

**추가할 위젯 후보:**

| 위젯 | 용도 | 비고 |
|---|---|---|
| `markdown` | 안내 글, 링크 모음 | 데이터셋 없음. HTML은 허용하지 않고 제한된 마크다운만 |
| `stat_list` | 항목과 값의 짧은 목록(순위, 최근 N건) | Superset 표보다 가볍게 |
| `calendar` | 날짜별 일정 | 간트와 같은 데이터셋을 재사용 |
| `timeline` | 사건을 시간순으로 | 변경 이력, 릴리스 기록 |

### 4-3. 배치와 필터 확장

```yaml
dashboards:
  overview:
    cross_filters: true                    # 차트를 누르면 다른 차트가 걸러짐
    layout:
      - [{ header: 핵심 지표 }]
      - [kpi_a, kpi_b, kpi_c]
      - [{ markdown: "지표 정의는 [문서](https://example.com)를 참고." , width: 4 }, { chart: trend, width: 8 }]
      - tabs:                               # 대시보드 안 탭
          - { label: 지역별, rows: [[by_region]] }
          - { label: 유형별, rows: [[by_category]] }
    filters:
      - { key: f_period, label: 기간, type: time_range, default: "Last quarter" }
      - { key: f_amount, label: 금액, type: number_range, dataset: orders, column: amount }
      - { key: f_region, label: 지역, type: select, dataset: orders, column: region, default: [서울] }
      - { key: f_city, label: 도시, type: select, dataset: orders, column: city, parent: f_region }
      - { key: f_grain, label: 단위, type: time_grain }
```

| 설정 | Superset |
|---|---|
| `header`, `markdown` 항목 | `HEADER`, `MARKDOWN` 노드 |
| `tabs` | `TABS`, `TAB` 노드 |
| `type: time_range` / `number_range` / `time_grain` | `filter_time` / `filter_range` / `filter_timegrain` |
| `default` | `defaultDataMask` |
| `parent` | `cascadeParentIds` |
| `charts: [키…]` (선택) | `scope.excluded`(나열하지 않은 차트를 제외) |

필터 유형 4종과 노드 유형은 6.1.0 예제 대시보드에서 쓰이는 것을 확인했다. `defaultDataMask`의 정확한 형태는 화면에서 저장한 대시보드를 export해 확인한 뒤 구현한다.

### 4-4. 테마와 언어

- **차트 색:** `app.theme.palette`(색 목록)를 받아 Superset의 `EXTRA_CATEGORICAL_COLOR_SCHEMES`에 등록하고 대시보드 `color_scheme`으로 지정한다. `superset_config.py`가 프로젝트 설정을 읽기 전용으로 읽는다.
- **iframe 글꼴과 메인 컬러:** SDK 0.4.0의 `setThemeConfig`로 웹앱 토큰을 iframe에 넘긴다(메서드가 있는 것은 `superset-notes.md` 8절에서 확인됨. 받는 토큰 범위는 실측 필요).
- **언어:** `app.locale`(기본 `ko`)을 두고 `web/strings/<언어>.js`로 나눈다. Superset에는 `BABEL_DEFAULT_LOCALE`을 같은 값으로 준다.

### 4-5. 차트 갤러리 프로젝트

- `projects/chart-gallery/`: 레지스트리의 모든 축약 유형과 카탈로그의 원본 유형을 하나씩 담은 세 번째 예시 프로젝트. 유형 묶음별 탭으로 나눈다.
- 역할:
  1. 템플릿 사용자가 화면에서 유형을 고르고 설정을 베껴 가는 견본
  2. 유형별 렌더링 확인: 통합 테스트가 차트마다 `POST /api/v1/chart/data`가 200이고 행이 있는지 본다
  3. Superset 업그레이드 때의 회귀 확인(3-4)
- 도메인 단어 grep 테스트의 금지어 수집 대상에 넣는다.

## 5. 템플릿 사용성

### 5-1. `make new`: CSV에서 프로젝트 초안 만들기

```bash
make new NAME=myproject FROM=~/exports/
```

- `FROM` 폴더의 CSV를 읽어 `projects/myproject/`를 만든다.
- **`schema.yaml` 추론:**
  - 유형: 모든 값이 정수면 `integer`, 숫자면 `number`, `YYYY-MM-DD`면 `date`, `true/false`면 `boolean`
  - 고유값이 적은 text 열은 `enum` 후보(값 목록 포함)
  - 값이 유일하고 비어 있지 않은 첫 열을 `primary_key` 후보
  - `<테이블>_id` 꼴 열은 `ref` 후보
  - 예약어나 대문자·공백이 있는 열 이름은 snake_case로 바꾸고 CSV 머리글도 고친 사본을 쓴다
- **`dashboard.yaml` 초안:** 테이블마다 데이터셋 하나와 `row_count` 지표. 규칙으로 차트를 고른다.

  | 열 구성 | 초안 차트 |
  |---|---|
  | 테이블마다 | 건수 `big_number_total`, 원본 행 `table` |
  | date 열 + 숫자 열 | 주별 합계 `line` |
  | enum 열 | 값별 건수 `bar` |
  | enum 열 + 숫자 열 | 값별 합계 `pie` |
  | date 열 2개(시작·종료 꼴 이름) | `gantt` 섹션 |
  | 상태 꼴 enum 열 | `kanban` 섹션 |

- 추론한 곳마다 `# 추론: 확인 필요` 주석을 남긴다. 결과는 `make check`를 통과하는 상태여야 한다.
- 이 추론 함수는 Studio의 프로젝트 마법사(6-3)가 그대로 쓴다.

### 5-2. `make plan`: 적용 전에 차이 보기

```
$ make plan
데이터   orders  +12행 −0행 ~3행      customers  변경 없음
뷰       dash.orders  열 추가: age_days
Superset 차트 +1 (funnel_status)  ~2 (trend, by_region)  −1 (old_pie)
         대시보드 ~1 (main: 배치 변경)
```

- 데이터: CSV와 현재 `raw` 테이블을 기본 키로 비교한다.
- Superset: 새 번들과 `.build/`의 지난 번들을 파일 단위로 비교한다.
- `make apply`는 지금처럼 바로 적용한다. `plan`은 선택이다.
- 함께 고칠 것: backend가 `projects/`의 변경을 `embed.json`처럼 파일 시각으로 감지해 다시 읽는다. `apply`의 이미지 재빌드와 재시작이 필요 없어진다.

### 5-3. 설정 버전

- `schema.yaml`과 `dashboard.yaml` 최상위에 `version: 1`을 둔다. 없으면 1로 본다.
- 형식을 깨는 변경이 생기면 버전을 올리고 `dashkit migrate`가 파일을 고친다. `CHANGELOG.md`에 형식 변경을 적는다.
- 4절의 변경은 모두 **추가**라서 버전 1 안에서 호환된다. 기존 두 예시 프로젝트는 고치지 않아도 통과해야 한다(완료 조건).

### 5-4. 설정 파일 분할(선택)

한 파일이 길어지면 폴더로 나눌 수 있게 한다. 한 파일 방식도 계속 지원한다.

```
projects/myproject/
├─ dashboard.yaml            # app, tabs
├─ datasets/orders.yaml      # 또는 orders.sql + 지표만 YAML
└─ dashboards/main.yaml
```

- 병합 규칙은 단순하게: 폴더 안 파일 이름이 키가 된다. 같은 키가 두 곳에 있으면 오류.
- `sql_file: orders.sql`로 SQL을 별도 파일에 둘 수 있게 한다.

### 5-5. 데이터 원본 어댑터(후순위)

`schema.yaml`의 테이블에 `source`를 둔다. 기본은 지금의 CSV다.

```yaml
tables:
  orders:   { source: { type: csv } }                      # 기본값, 생략 가능
  budget:   { source: { type: xlsx, file: budget.xlsx, sheet: 2026 } }
  tickets:  { source: { type: sql, connection: CRM, query: "SELECT ... FROM tickets" } }
```

- 어댑터는 "행을 내놓는 함수" 하나다. 검증과 적재는 지금 코드를 그대로 쓴다.
- `connection`의 접속 정보는 `.env`에서만 읽는다.
- 기존 DB를 직접 보는 방식(적재 없이 데이터셋 SQL이 그 DB를 읽음)은 권한 모델이 달라지므로 따로 설계한다.

### 5-6. 여러 프로젝트 동시 운영(후순위)

- 스키마를 `raw_<프로젝트>`, `dash_<프로젝트>`로 나누고 API를 `/api/p/<프로젝트>/…`로 바꾼다. Superset 객체 UUID는 이미 프로젝트 이름을 포함하므로 충돌하지 않는다.
- prune 범위를 "현재 프로젝트"로 좁혀야 한다. 3-5의 표식이 전제다.
- 변경 범위가 넓으므로 실제 요구가 생길 때 한다.

## 6. Studio: 끌어다 놓는 구성 화면(선택 기능)

### 6-1. 제안 요지

요청하신 "UI로 드래그 앤 드롭 구성"에 대한 제안은 이렇다.

- **만든다. 다만 YAML의 편집기로 만든다.** Studio가 하는 일은 `projects/<이름>/`의 파일을 고치고 `make apply`와 같은 일을 실행하는 것뿐이다. Studio로 만든 것과 손으로 쓴 것이 같은 파일이므로 git 이력, 리뷰, 재현성이 그대로 유지된다. Studio 전용 저장소나 DB는 두지 않는다.
- **차트 편집기는 새로 만들지 않는다.** 차트 유형 선택과 세부 옵션은 레지스트리(4-1)에서 생성한 폼으로 받고, 그 밖의 세밀한 조정은 Superset 화면에서 한 뒤 가져온다(6-5). Superset의 Explore 화면을 다시 만드는 일은 비용 대비 얻는 것이 적다.
- **보기 전용 운영 환경과 분리한다.** Studio는 owner DB 계정과 Superset 관리자 계정, `projects/` 쓰기 권한이 필요하다. 지금의 backend(읽기 전용, Superset 계정 없음)에 넣으면 권한 경계가 무너진다. 그래서 compose `studio` 프로필의 별도 컨테이너로 두고 `127.0.0.1`에만 연다. 운영 배포에는 포함하지 않는다.
- **순서는 5절이 먼저다.** `make new`의 추론, `make plan`의 차이 계산, 레지스트리와 매니페스트가 Studio의 부품이다. 이것들이 있으면 Studio는 얇은 화면이 된다.

### 6-2. 구성

```
브라우저 ── http://localhost:8090 ──▶ studio 컨테이너(프로필: studio)
                                        ├─ web/studio/         화면(바닐라 JS, 기존 디자인 토큰)
                                        ├─ dashkit/studio/     API
                                        ├─ projects/ (읽기·쓰기)
                                        └─ dashkit check / load / sync 호출
오른쪽 미리보기 ── iframe ──▶ http://localhost:8080 (기존 웹앱, 적용 결과 그대로)
```

| 항목 | 결정 |
|---|---|
| 실행 | `make studio`. `docker compose --profile studio up` |
| 권한 | tools 컨테이너와 같은 환경변수. `projects/` 쓰기 마운트 |
| 인증 | 없음. `127.0.0.1` 전용. README에 "외부에 열지 말 것" 명시 |
| 끌어다 놓기 | HTML5 Drag and Drop API. 라이브러리 없음. 키보드로도 순서를 바꿀 수 있게 함 |
| YAML 쓰기 | 주석과 순서를 보존해야 하므로 `ruamel.yaml`을 studio에만 추가(새 의존성) |
| 저장 방식 | 임시 사본에 써서 `config.load_project`로 검증 → 통과하면 원자적 교체. 실패하면 파일을 건드리지 않고 오류를 폼에 표시 |
| 미리보기 | 별도 렌더러를 만들지 않는다. "적용"을 누르면 실제 apply를 실행하고 오른쪽 iframe을 새로 고친다 |
| 되돌리기 | git에 맡긴다. 화면에 "바뀐 파일" 목록을 보여 준다 |
| 프레임 허용 | 웹앱은 `frame-ancestors 'none'`이고 Superset은 `WEB_ORIGINS`만 허용하므로 그대로는 미리보기가 차단된다. `STUDIO_ORIGIN`(기본 비어 있음)을 두고, `make studio`로 띄울 때만 웹앱 CSP의 `frame-ancestors`와 Superset의 `frame-ancestors`에 그 출처를 정확히 더한다. 값이 비어 있으면 지금과 같다(`'none'`). 완료 조건(5a): Studio 안에서 웹앱과 그 안의 Superset 대시보드가 중첩 iframe으로 렌더링된다 |

### 6-3. 화면과 기능

| 화면 | 끌어다 놓는 것 | 결과 |
|---|---|---|
| **프로젝트 마법사** | CSV 파일 여러 개를 창에 놓음 | 5-1의 추론 결과(유형, 키, 참조, 초안 차트)를 표로 보여 주고 고친 뒤 프로젝트 생성 |
| **데이터** | CSV 파일을 테이블 카드에 놓음 | 검증만 먼저 실행 → 오류를 행·열로 표시, 차이(5-2)를 요약 → "적용"을 누르면 파일 교체와 load |
| **탭 구성** | 탭 순서, 섹션 순서. 팔레트의 "Superset 대시보드 / 간트 / 칸반 / …"를 탭에 놓음 | `tabs` 수정. 위젯 옵션은 `widget.json`에서 만든 폼으로, 열은 뷰의 열 목록에서 고름 |
| **대시보드 배치** | 차트 카드를 행 사이로 옮김, 가장자리를 끌어 너비(12칸) 조절, 팔레트에서 차트 유형을 놓아 새 차트 추가 | `dashboards.<키>.charts`와 `layout` 수정. 차트 필드는 레지스트리에서 만든 폼으로 |
| **필터** | 필터 순서 | `filters` 수정 |
| **앱** | (없음) | 제목, 로고, 기준일, 색 |

배치 편집기는 실제 차트를 그리지 않고 **제목과 유형 아이콘이 있는 카드**로 격자를 보여 준다. 실제 모습은 적용 후 오른쪽 미리보기에서 본다. 이렇게 하면 편집기가 Superset 렌더링에 의존하지 않는다.

### 6-4. Studio API(초안)

| 메서드 경로 | 내용 |
|---|---|
| `GET /studio/api/projects` | 프로젝트 목록, 현재 프로젝트 |
| `POST /studio/api/projects/infer` | CSV 업로드 → 추론 결과(파일은 쓰지 않음) |
| `POST /studio/api/projects` | 추론 결과로 프로젝트 생성 |
| `GET /studio/api/projects/{p}/config` | 두 YAML의 내용과 뷰 열 목록 |
| `PATCH /studio/api/projects/{p}/config` | 변경 연산 목록(예: 탭 이동, 차트 추가). 검증 후 저장 |
| `POST /studio/api/projects/{p}/data/{table}/check` | CSV 업로드 → 검증 결과와 차이 |
| `PUT /studio/api/projects/{p}/data/{table}` | CSV 교체 |
| `POST /studio/api/projects/{p}/apply` | check → load → sync. 진행 로그를 스트림으로 |
| `GET /studio/api/registry` | 차트 유형 레지스트리와 위젯 매니페스트 |

- 업로드 크기 상한과 파일 이름 검사(테이블 이름과 일치, 경로 문자 금지)를 둔다.
- 경로의 `{p}`, `{table}`은 설정에서 온 목록과 대조한 뒤에만 파일 경로로 쓴다.

### 6-5. Superset에서 고친 것을 가져오기: `make pull`(선택)

- Superset 화면에서 대시보드 배치를 끌어 고치거나 차트 옵션을 조정한 뒤, `make pull`이 export를 읽어 `dashboard.yaml`에 되돌려 쓴다.
- 대응 규칙:
  - 관리 중인 차트의 `params` 차이 → 그 차트의 `params:`에 덮어쓸 키만 기록
  - 화면에서 새로 만든 차트 → `viz_type` + `params` 원본 차트로 추가. 키는 제목에서 만들고, Superset의 임시 객체는 다음 sync에서 지움
  - `position` → `layout`. 4-3의 요소(행, 차트, 제목, 글, 탭)로 표현할 수 있는 것만 받고, 표현할 수 없는 노드가 있으면 목록을 보여 주고 중단
- 3-5의 "외부에서 관리됨" 표식을 쓰면 Superset이 관리 객체의 화면 편집을 막을 수 있다. `pull`을 쓰려면 이 표식을 끄는 설정(`SYNC_LOCK_UI=0`)이 필요하다. 기본은 편집 차단이다(7절 D3).
- 변환에 손실이 있을 수 있어 Studio의 배치 편집기보다 후순위로 둔다.

## 7. 결정

다시 검토해 아래와 같이 정했다. D3과 D4는 처음 권장안에 조건을 붙였다.

| # | 결정 | 이유 | 조건·범위 |
|---|---|---|---|
| D1 | **Studio를 만든다.** 5단계에서, 두 번에 나눠 낸다. | 끌어다 놓는 구성 화면이 템플릿의 진입 장벽을 가장 크게 낮춘다. 부품(5-1 추론, 5-2 차이, 4-1 레지스트리, 4-2 매니페스트)이 먼저 있어야 얇게 만들 수 있다. | 5a: 프로젝트 마법사, 데이터 교체, 탭 구성. 5b: 대시보드 배치, 필터. 5a만으로도 "CSV를 놓으면 대시보드가 나온다"가 된다. |
| D2 | **`ruamel.yaml`을 Studio에만 추가한다.** | 주석과 순서를 보존하지 못하면 Studio가 저장할 때마다 손으로 쓴 주석이 사라져, 손 편집과 Studio를 섞어 쓸 수 없다. 직접 구현하는 것보다 검증된 라이브러리가 안전하다. | 정확한 버전으로 고정한다. `dashkit/Dockerfile`에 `studio` 빌드 단계를 따로 두어 backend와 tools 이미지에는 들어가지 않게 한다. |
| D3 | **Superset 화면에서 관리 객체 편집을 막는다(기본).** | 화면에서 고친 내용이 다음 `make apply`에서 말없이 사라지는 것이 지금 가장 헷갈리기 쉬운 동작이다. 설정 파일이 유일한 출처라는 원칙과도 맞다. 새 차트를 만들어 params를 옮기는 절차(README 5절)는 관리 객체를 고치지 않으므로 그대로 쓸 수 있다. | 3-5의 실측(import가 표식을 저장하는지, 화면이 편집을 막는지)이 통과해야 한다. 통과하지 못하면 막지 않고, 대신 `make plan`이 "Superset에서 직접 바뀐 객체"를 경고한다. `make pull`(6-5)은 `SYNC_LOCK_UI=0`일 때만 동작한다. |
| D4 | **축약 유형을 22종까지 늘린다. 다만 유형마다 통과 기준을 둔다.** | 축약 유형은 열·지표 이름을 `make check`에서 잡아 준다. 원본 방식은 틀린 열 이름이 화면 오류로만 드러난다. 레지스트리가 있으면 유형 하나의 유지 비용은 파일 하나다. | 갤러리 프로젝트에서 그 유형의 데이터 조회가 200이고 화면 캡처에 오류가 없을 때만 축약으로 올린다. 통과하지 못한 유형은 원본 방식 조각으로 카탈로그에 남긴다. 2단계에서 추이·비율 8종, 그다음 교차·분포·관계 8종 순으로 넣는다. |
| D5 | **통합 테스트는 별도 스택에서 돌린다.** | 지금은 테스트가 개발 중인 화면의 프로젝트를 바꿨다가 되돌린다. 도중에 멈추면 다른 프로젝트 상태로 남고, 테스트 중에는 화면을 쓸 수 없다. | `COMPOSE_PROJECT_NAME=dashkit-test`, 다른 포트(웹앱 18080, Superset 18088), 별도 볼륨. `make test`가 띄우고 끝나면 내린다(`KEEP=1`이면 남김). 이미지는 개발 스택과 공유하므로 추가 디스크는 볼륨만큼이다. 메모리가 부족한 환경을 위해 `make test-unit`만으로도 PR의 기본 검사가 되게 한다. |

## 8. 단계와 완료 조건

| 단계 | 내용 | 완료 조건 | 크기 |
|---|---|---|---|
| 0 | 안전망: 3-1 스냅샷, 3-2 분리, 3-3 CI | `make test-unit`이 스택 없이 통과. PR에서 자동 실행 | 작음 |
| 1 | 4-1 레지스트리(기존 6종 이전) | 스냅샷 변화 없음. `chart_params`에 유형별 분기 없음 | 중간 |
| 2 | 4-1 유형 16종 추가, 4-5 갤러리, 3-4 버전 관리 | 갤러리의 모든 차트가 데이터 조회 200. 카탈로그 문서가 레지스트리에서 생성됨 | 중간 |
| 3 | 4-2 위젯 매니페스트, 4-3 배치·필터, 3-5 prune 표식 | 위젯 추가가 폴더 하나. 기존 두 예시 설정 무수정 통과 | 중간 |
| 4 | 5-1 `make new`, 5-2 `make plan`, 5-3 버전 | 예시 CSV 폴더에서 만든 초안이 `make check`와 `make apply` 통과 | 중간 |
| 5a | 6절 Studio: 프로젝트 마법사, 데이터 교체, 탭 구성 | Studio만으로 CSV 폴더에서 대시보드까지 만들 수 있음. Studio가 쓴 파일이 `make check` 통과하고 주석이 보존됨 | 큼 |
| 5b | 6절 Studio: 대시보드 배치, 필터 | 차트 추가·이동·너비 조절이 `layout`에 반영됨 | 중간 |
| 6 | 4-4 테마·언어, 위젯 후보, 6-5 `pull`, 5-4~5-6 | 요구가 생길 때 | 각각 |

각 단계는 독립된 PR로 나눈다. 0~2단계만 해도 "차트를 쉽게 늘릴 수 있는 템플릿"이라는 목표의 대부분이 채워진다.

## 9. 하지 않는 것

- Superset Explore 수준의 차트 편집기를 직접 만들지 않는다.
- Studio를 운영 환경에 배포하거나 인증을 붙여 다중 사용자 편집 도구로 만들지 않는다.
- 웹앱에 UI 프레임워크나 빌드 단계를 들이지 않는다.
- 대시보드 화면 안에서 데이터를 고치는 기능은 넣지 않는다(보기 전용 유지).
