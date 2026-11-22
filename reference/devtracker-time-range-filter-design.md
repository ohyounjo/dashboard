# 운영 `devtracker` 대시보드 기간 필터 추가 설계

[이슈 #67](https://github.com/ohyounjo/dashboard/issues/67)의 확정 설계안이다. [이슈 #49](https://github.com/ohyounjo/dashboard/issues/49)에서 `EmbedGuest` 역할에 `can_time_range on Api`를 반영하고 공개 경로의 guest 기간 해석 API와 차단 경계를 확인했지만, 운영 `devtracker` 대시보드에는 기간 필터가 없어 실제 운영 임베드 화면에서의 기간 선택과 차트 재조회를 확인하지 못했다. 이 문서는 확정된 기간 필터 사양, `devtracker` 차트별 시간 열 매핑, 변경·검증·운영 반영 절차를 정의한다.

참조: [차트·필터 계약](../contracts/chart-shorthand.md#필터filters) · [Superset 실측 12·13절](../contracts/superset-notes.md#12-배치-노드와-필터-확장-보강설계서-4-3) · [이전 권한 설계서](./time-range-filter-design.md) · [오케스트레이션 프롬프트](./devtracker-time-range-filter-prompt.md)

## 현재 상태와 데이터 분포

`projects/devtracker/dashboard.yaml`에는 Superset 대시보드 3개(`overview`, `issues`, `docs`)가 있으며, 현재 `time_range` 필터는 하나도 없다. `issues`에는 `select` 필터 3개(`심각도`, `상태`, `담당자`), `docs`에는 `select` 필터 1개(`유형`)가 있고 `overview`에는 필터가 없다. 기준일은 예시 데이터 기준 `app.as_of: "2026-11-19"`로 고정되어 있다.

| 데이터셋 | 시간 열(뷰 정의 순서) | 현재 CSV 건수 및 기간 | 2026-11월 건수 | 2026-10월 건수 |
|---|---|---|---|---|
| `issues` | `created`, `updated` | 24건 (`created`: 2026-08-25~11-18, `updated`: 2026-09-03~11-19) | `created` 14건 / `updated` 17건 | `created` 5건 / `updated` 2건 |
| `docs` | `updated` | 8건 (`2026-09-08`~`2026-11-18`) | 5건 | 2건 |
| `kpi_snapshots` | `week_start` | 6주 (`2026-10-12`~`2026-11-16`) | 3주 | 3주 |
| `milestones` | `start_date`, `end_date` | 5건 (`2026-08-03`~`2027-02-26`) | — | — |

## 확정 설계 사양

| 항목 | 확정 내용 | 결정 근거 |
|---|---|---|
| 적용 대시보드 | `overview`, `issues`, `docs` 3개 모두 적용 | 목록 대시보드(`issues`, `docs`)는 기간 필터 + 기존 `select` 필터 조합을 제공하고, 다중 차트 대시보드(`overview`)는 범위 안 차트 재조회와 범위 밖 차트 유지를 같은 운영 화면에서 직접 검증할 수 있다. |
| 필터 적용 범위(`charts`) | - `overview`: `charts: [trend_open, issues_recent, issues_by_severity]`<br>- `issues`: 생략(전체 = `issue_table`)<br>- `docs`: 생략(전체 = `doc_table`) | `overview`의 마일스톤 현황 4개(`kpi_progress`, `kpi_dday`, `ms_bar`, `ms_remaining`)와 직전 주 대비 KPI 2개(`kpi_open`, `kpi_critical`)는 기간으로 자르면 전체 진척률·비교 지표가 왜곡되므로 범위 밖으로 제외한다. |
| 날짜 기준 열 | - `issues` 데이터셋 차트(`issue_table`, `issues_recent`, `issues_by_severity`): **`updated`(수정일)** 통일<br>- `docs.doc_table`: **`updated`(수정일)**<br>- `overview.trend_open`: **`week_start`** | `issue_table`과 `issues_recent`가 이미 `updated` 내림차순 정렬과 표시 열을 사용하므로, 같은 `overview`의 `issues_by_severity`도 `updated`로 통일해 날짜 기준 혼선을 없앤다. |
| 기본 기간(`default`) | 3개 대시보드 모두 **`"2026-10-01 : 2026-12-01"`** (고정 절대 기간) | `devtracker`는 `as_of: "2026-11-19"` 고정 예시 데이터이므로 상대 기간(`Last quarter` 등)은 시점에 따라 `No data`가 된다. `"2026-10-01 : 2026-12-01"`은 `kpi_snapshots` 6주 전체와 열린 이슈 16건을 보존하면서도 `issues`(24→19건)·`docs`(8→7건)에서 9월 데이터를 걸러내어 초기 화면에서 기본 기간 적용을 바로 확인할 수 있다. |

## 차트별 시간 열 자동 선택과 계약 대응

`dashkit/sync/bundle.py`의 `time_filter_columns()`와 `chart_params()`는 `time_range` 필터가 있는 대시보드에서 필터 범위(`charts`, 생략 시 전체)에 포함된 축약 차트(`viz`)에 다음 조건을 자동 추가한다.

```yaml
adhoc_filters:
  - expressionType: SIMPLE
    clause: WHERE
    subject: <자동 선택된 시간 열>
    operator: TEMPORAL_RANGE
    comparator: "No filter"
```

자동 선택 우선순위는 (1) 차트가 참조한 열(`column_refs(chart)`) 중 첫 `date`/`timestamp` 열, (2) 없으면 데이터셋 뷰(`information_schema.columns`)의 첫 `date`/`timestamp` 열이다. 확정 범위 기준 `devtracker` 차트별 결과는 다음과 같다.

| 대시보드 | 차트 키 | 범위 포함 | 데이터셋 | 자동 선택 열 | 최종 적용 열 및 처리 |
|---|---|---|---|---|---|
| `overview` | `trend_open` | O | `kpi_snapshots` | `week_start` | `week_start` (자동 선택 일치) |
| `overview` | `issues_recent` | O | `issues` | `updated` | `updated` (자동 선택 일치, `columns`에 `updated` 참조) |
| `overview` | `issues_by_severity` | O | `issues` | **`created`** (불일치) | **`updated` 명시** (`params.adhoc_filters`로 덮어쓰기) |
| `overview` | `kpi_progress`, `kpi_dday`, `kpi_open`, `kpi_critical`, `ms_bar`, `ms_remaining` | X (`excluded`) | `milestones` / `kpi_snapshots` | 미적용 | `TEMPORAL_RANGE` 미추가, `scope.excluded`에 포함 |
| `issues` | `issue_table` | O | `issues` | `updated` | `updated` (자동 선택 일치) |
| `docs` | `doc_table` | O | `docs` | `updated` | `updated` (자동 선택 일치) |

### `overview.issues_by_severity` 시간 열 명시 이유

`issues_by_severity`는 `groupby: [severity]`, `metric: open_issues`만 선언해 시간 열을 참조하지 않는다. 따라서 `time_filter_columns()`가 `datasets.issues.sql`의 첫 날짜 열인 `created`를 자동 선택한다. 뷰 SQL의 열 순서를 바꾸면 `main_dttm_col`이 연쇄 변경되므로, [차트·필터 계약 생성 순서 3번](../contracts/chart-shorthand.md#생성-순서)에 따라 `issues_by_severity` 차트에 `params.adhoc_filters`를 명시해 `subject: updated`로 덮어쓴다(`issues_by_severity`에는 `where` 축약 필드가 없으므로 덮어쓰기로 누락되는 조건이 없다).

### `projects/devtracker/dashboard.yaml` 변경 내용

1. **`dashboards.overview.charts`의 `issues_by_severity`**에 명시적 시간 조건을 추가한다.
   ```yaml
   - key: issues_by_severity
     title: 심각도별 열린 이슈
     viz: pie
     dataset: issues
     groupby: [severity]
     metric: open_issues        # issues 데이터셋의 지표(is_open인 이슈 수)
     params:
       adhoc_filters:
         - expressionType: SIMPLE
           clause: WHERE
           subject: updated
           operator: TEMPORAL_RANGE
           comparator: "No filter"
   ```
2. **`dashboards.overview.filters`**를 추가한다.
   ```yaml
   filters:
     - key: f_overview_period
       label: 기간
       type: time_range
       default: "2026-10-01 : 2026-12-01"
       charts: [trend_open, issues_recent, issues_by_severity]
   ```
3. **`dashboards.issues.filters`** 맨 앞에 기간 필터를 추가한다(기존 `f_severity`, `f_issue_status`, `f_assignee` 유지).
   ```yaml
   - { key: f_issue_period, label: 기간, type: time_range, default: "2026-10-01 : 2026-12-01" }
   ```
4. **`dashboards.docs.filters`** 맨 앞에 기간 필터를 추가한다(기존 `f_doc_type` 유지).
   ```yaml
   - { key: f_doc_period, label: 기간, type: time_range, default: "2026-10-01 : 2026-12-01" }
   ```

> [!NOTE]
> `overview`에 `charts`를 쓴 필터가 들어가므로, 번들 생성기가 `overview`의 차트들에 1부터 시작하는 자리표시 `chartId`를 부여하고 제외 차트 6개를 `scope.excluded`에 기록한다. Superset import가 이를 실제 차트 ID로 치환한다([계약](../contracts/chart-shorthand.md#필터filters)).

## 변경 파일 범위

1. `projects/devtracker/dashboard.yaml`: 위 확정 사양의 `time_range` 필터 3개와 `issues_by_severity`의 `params.adhoc_filters` 추가. 기존 `select` 필터, 데이터셋 SQL, 레이아웃, `schedule` 탭은 변경하지 않는다.
2. `tests/golden/devtracker/bundle/`: `make golden`으로 갱신하고 `overview`/`issues`/`docs` 대시보드 YAML과 `trend_open`/`issues_recent`/`issues_by_severity`/`issue_table`/`doc_table` 차트 YAML의 diff를 확인한다.
3. `tests/test_layout_filters.py` (및 필요 시 `tests/test_api.py`):
   - `devtracker` 번들에서 대상 차트 5개의 `TEMPORAL_RANGE` `subject`(`week_start`, `updated`)와 `overview` 제외 차트 6개의 `scope.excluded` 및 `TEMPORAL_RANGE` 미포함을 단위 회귀 테스트로 고정한다.
   - `devtracker` 적용 상태에서 guest `GET /api/v1/time_range/` 200·잘못된 기간 400 및 `GET /api/v1/form_data/` 403 경계 회귀를 확인한다.
4. `contracts/chart-shorthand.md`: 173행의 오래된 문구("임베드(guest) 화면에서는 `time_range` 필터의 값을 바꿀 수 없고 `default`만 적용된다")를 #49 반영 이후의 실제 동작(`can_time_range on Api`로 기간 변경 가능, `superset-notes.md` 12-3절·13절 참조)으로 정정한다.
5. 필요 시 `contracts/superset-notes.md`·`README.md`의 안내 문구를 맞추고, 검증 결과를 이 문서, 이슈 #67, [이슈 #49](https://github.com/ohyounjo/dashboard/issues/49)에 기록한다.

## 검증 계획 및 기대 수치

기존 개발·운영 스택과 분리된 격리 테스트 스택에서 먼저 검증한다. 결과 기록에 토큰·비밀번호는 남기지 않는다.

### 기간별 기대 데이터 수치(`projects/devtracker/data/*.csv` 기준)

| 기간 문자열 | `overview.trend_open` (`week_start`) | `overview.issues_by_severity` (`updated`, 열린 이슈) | `overview` 범위 밖 6개 차트 | `issues.issue_table` (`updated`) | `docs.doc_table` (`updated`) |
|---|---|---|---|---|---|
| `2026-10-01 : 2026-12-01` (기본값) | 6주 | 16건 (`critical` 3, `major` 6, `minor` 5, `trivial` 2) | 고정 (`kpi_progress` 55 등) | 19건 (9월 5건 제외) | 7건 (9월 1건 제외) |
| `2026-11-15 : 2026-11-20` (`updated` vs `created` 판별) | 1주 (`2026-11-16`) | **12건** (`critical` 3, `major` 6, `minor` 3; `created`면 4건) | 변동 없음 | **12건** (`created`면 4건) | 2건 (`D3`, `D6`) |
| `2026-10-01 : 2026-11-01` | 3주 (`10-12`~`10-26`) | **0건 / No data** (`created`면 2건) | 변동 없음 | **2건** (`ISS-108`, `ISS-116`) | 2건 (`D4`, `D8`) |
| `2026-01-01 : 2026-02-01` (데이터 없음) | 0주 / No data | 0건 / No data | 변동 없음 | 0건 / No data | 0건 / No data |

### 검증 체크리스트

| 검증 항목 | 확인 내용 및 기대 결과 |
|---|---|
| 정적·번들·단위 테스트 | `make check`, `make plan`, `make test-unit`, `git diff --check` 통과. `issues_by_severity`의 `adhoc_filters` `subject`가 `updated`이고, 제외 차트 6개에 `TEMPORAL_RANGE`가 없으며 `scope.excluded`가 정확함. |
| 통합 테스트 | 빈 테스트 볼륨에서 `AUTH_COOKIE_SECURE=0 make test`(브라우저 검증을 이어갈 경우 `KEEP=1`: 종료 시 `devtracker`를 다시 적용해 남김, 종료 후 `make test-down`) 통과. |
| 임베드 화면 기본값·변경·재조회 | `KEEP=1`로 남긴 격리 웹앱 임베드(`overview`, `issues`, `docs`)에서 기본 기간 표시 및 위 표의 기대 수치 확인. 기간 변경 시 `GET /api/v1/time_range/` 200, `POST /api/v1/chart/data` 200과 함께 차트 수치가 위 표와 일치하게 변경됨. |
| 범위 밖 차트 유지 | `overview`에서 기간을 `2026-11-15 : 2026-11-20` 및 `2026-01-01 : 2026-02-01`로 변경해도 `kpi_progress`, `kpi_dday`, `kpi_open`, `kpi_critical`, `ms_bar`, `ms_remaining` 값이 그대로 유지됨. |
| 기존 필터 조합 | `issues`에서 기간(`2026-11-15 : 2026-11-20`) + `심각도=critical` 적용 시 3건(`ISS-101`, `ISS-102`, `ISS-123`)만 표시. `docs`에서 기간(`2026-11-15 : 2026-11-20`) + `유형=PRD` 적용 시 1건(`D3`)만 표시. |
| guest 권한 경계 유지 | 같은 guest token으로 `GET /api/v1/time_range/`(유효 200, 잘못된 식 400), `GET /api/v1/form_data/` 403, 다른 대시보드·다른 데이터셋·서브쿼리·CSV·쓰기 차단 유지. |

## 운영 반영 및 되돌리기 절차

운영 데이터(`raw.*`), 기존 대시보드 설정, 다른 작업의 변경 사항을 보존하면서 반영한다.

1. **사전 확인**: 운영 스택의 작업 상태(`git status`), 실행 중인 컨테이너 상태, 현재 적용 프로젝트(`.state/embed.json`이 `devtracker`인지)를 확인한다. 다른 작업의 미커밋 변경이나 운영 데이터는 건드리지 않는다.
2. **변경 미리보기**: `make check`와 `make plan`을 실행해 변경 대상이 의도한 대시보드 필터와 차트 `adhoc_filters`뿐이며 `raw.*` 데이터·뷰·기존 필터에 의도치 않은 변경이나 삭제가 없음을 확인한다.
3. **반영**: 이번 변경은 `projects/devtracker/dashboard.yaml`의 필터·차트 설정 변경뿐이고 스키마·CSV·뷰 SQL 변경이 없으므로, `raw.*`를 다시 적재하는 `make load` 없이 **`make sync`**로 Superset 객체만 반영한다.
4. **운영 화면 검증**: 실제 운영 웹앱 임베드 화면(`overview`, `issues`, `docs`)에서 기본 기간, 기간 변경, 차트 재조회, 범위 밖 차트 유지, 기존 `select` 필터 조합을 확인하고 공개 경로의 guest `time_range` 200 / `form_data` 403 경계를 재확인한다.
5. **되돌리기**: 반영 후 화면 오류나 의도치 않은 차트 필터링이 발견되면 `projects/devtracker/dashboard.yaml` 변경을 원복한 뒤 `make plan` → `make sync`로 Superset 대시보드를 이전 상태로 복구한다. 데이터 볼륨은 변경하지 않았으므로 별도 데이터 복구가 필요 없다.

## 완료 조건

- [ ] 확정 설계 사양을 이슈 #67에 기록했다.
- [ ] `projects/devtracker/dashboard.yaml`에 `overview`·`issues`·`docs` 기간 필터와 `issues_by_severity`의 명시적 `updated` 시간 조건을 반영했다.
- [ ] 격리 스택에서 위 기간별 기대 수치, 범위 밖 차트 유지, 기존 필터 조합, guest 접근 경계와 `make test-unit`·`make test`를 검증했다.
- [ ] 운영 반영 절차(`make check` → `make plan` → `make sync`)로 데이터·기존 설정을 보존하며 반영하고, 실제 운영 웹앱 임베드 화면에서 동작을 확인했다.
- [ ] 계약(`contracts/chart-shorthand.md` 등)·golden·회귀 테스트를 갱신하고, 운영 화면 검증 결과를 이 문서, 이슈 #67, [이슈 #49](https://github.com/ohyounjo/dashboard/issues/49)에 기록했다.
