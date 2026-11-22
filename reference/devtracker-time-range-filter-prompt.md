# 운영 `devtracker` 대시보드 기간 필터 추가 오케스트레이션 프롬프트

설계 PR이 병합된 뒤 `ohyounjo/dashboard` 저장소에서 새 세션을 열고 아래 블록을 붙여 넣는다. [설계서](devtracker-time-range-filter-design.md)

````text
# 목표

ohyounjo/dashboard의 이슈 #67을 구현·검증하고 운영 `devtracker` 대시보드에 기간 필터를 반영한다. 실제 확인하지 않은 결과를 성공으로 기록하지 않는다.

# 시작과 격리

- AGENTS.md, GitHub 이슈 #67의 최신 본문·댓글, 이슈 #49의 운영 반영 기록, reference/devtracker-time-range-filter-design.md를 읽는다.
- contracts/chart-shorthand.md(필터 절 및 173행), contracts/superset-notes.md 12·13절, dashkit/sync/bundle.py의 time_filter_columns()·chart_params(), projects/devtracker/dashboard.yaml을 확인한다. 이슈와 계약이 다르면 계약을 기준으로 해석하고 차이를 보고한다.
- 최신 origin/main을 가져와 기준 SHA를 기록하고 그 커밋에서 새 격리 worktree와 작업 브랜치(예: feat/67-devtracker-time-range)를 준비한다. 기존 작업·다른 세션의 변경·운영 스택 데이터는 건드리지 않는다. 비밀값·토큰은 환경 변수로만 취급하고 로그·문서·커밋에 남기지 않는다.

# 구현 및 격리 검증

1. 설계서의 확정 사양대로 `projects/devtracker/dashboard.yaml`을 수정한다.
   - `overview`: `f_overview_period`(`type: time_range`, `default: "2026-10-01 : 2026-12-01"`, `charts: [trend_open, issues_recent, issues_by_severity]`) 추가.
   - `overview.issues_by_severity`: 자동 선택(`created`) 대신 `updated`가 걸리도록 `params.adhoc_filters`에 `TEMPORAL_RANGE`(`subject: updated`, `comparator: "No filter"`)를 명시한다.
   - `issues`: `f_issue_period`(`type: time_range`, `default: "2026-10-01 : 2026-12-01"`) 추가(기존 `select` 필터 3개 유지).
   - `docs`: `f_doc_period`(`type: time_range`, `default: "2026-10-01 : 2026-12-01"`) 추가(기존 `f_doc_type` 유지).
2. `make check`와 `make golden`을 실행해 `tests/golden/devtracker/bundle/`을 갱신한다. 생성된 번들 diff에서 대상 차트 5개의 `TEMPORAL_RANGE` `subject`(`trend_open`은 `week_start`, 나머지 4개는 `updated`), `overview` 제외 차트 6개의 `scope.excluded` 및 `TEMPORAL_RANGE` 미포함, 기존 `select` 필터 보존을 확인한다.
3. 회귀 테스트를 추가·갱신한다.
   - `tests/test_layout_filters.py`에 `devtracker` 번들의 대상 차트 시간 열(`subject`)과 `overview` 제외 차트 6개의 `scope.excluded`·`TEMPORAL_RANGE` 미포함 검사를 추가한다.
   - 필요 시 `tests/test_api.py` 등에서 `devtracker` 적용 상태의 guest 기간 API(유효 200, 잘못된 식 400)와 `form_data` 403 경계를 확인한다.
4. `contracts/chart-shorthand.md` 173행의 오래된 제약 문구(guest가 `time_range` 값을 바꿀 수 없다는 설명)를 #49 반영 이후의 실제 동작으로 정정하고, 필요한 사용 안내 문구를 맞춘다.
5. `make test-unit`과 격리된 빈 테스트 볼륨의 `AUTH_COOKIE_SECURE=0 make test KEEP=1`을 실행한다. `_test_run`은 마지막 단계에서 `PROJECT=zz-ingest`를 `apply`한 뒤 폴더를 삭제해 `.test/state/embed.json`의 임베드 매핑이 비워지므로, 브라우저 검증 전에 반드시 `make info`로 확인한 동일한 테스트 스택 환경(`Makefile`의 `TEST_ENV`: `COMPOSE_PROJECT_NAME`, `WEB_PORT`, `SUPERSET_PORT`, `STUDIO_PORT`, `WEB_ORIGINS`, `SUPERSET_PUBLIC_URL`, `WEB_PUBLIC_URL`, `STATE_DIR=/app/.test/state`, `BUILD_DIR=/app/.test/build`, `HOST_STATE_DIR=./.test/state`, `HOST_BUILD_DIR=./.test/build`)에서 `PROJECT=devtracker make apply`를 다시 실행한다. 그다음 같은 격리 테스트 스택의 실제 웹앱 임베드 화면(`overview`, `issues`, `docs`)에서 설계서의 기대 수치 표를 대조한다.
   - 기본값(`2026-10-01 : 2026-12-01`), 기간 변경(`2026-11-15 : 2026-11-20`, `2026-10-01 : 2026-11-01`, `2026-01-01 : 2026-02-01`) 시 `GET /api/v1/time_range/` 200, `POST /api/v1/chart/data` 200과 차트 건수 변화를 확인한다. 특히 `2026-11-15 : 2026-11-20`에서 `issues_by_severity`가 12건(`created`면 4건)임을 확인해 `updated` 기준 적용을 증명한다.
   - `overview`의 범위 밖 차트 6개(`kpi_progress`, `kpi_dday`, `kpi_open`, `kpi_critical`, `ms_bar`, `ms_remaining`)가 기간 변경 전후 그대로 유지되는지 확인한다.
   - `issues`의 `심각도=critical` 조합(3건), `docs`의 `유형=PRD` 조합(1건) 등 기존 `select` 필터 조합과 guest 접근 경계를 확인한다.
   - 격리 검증이 끝나면 자신이 띄운 테스트 스택만 `make test-down`으로 정리한다.
6. `git diff --check`를 실행한다.

# PR 및 운영 반영

- 격리 검증이 끝나면 구현 브랜치에 커밋·푸시하고 `.github/pull_request_template.md`를 따라 한국어 PR을 연다(`Refs #67`). PR 본문에 확정 사양, 시간 열 매핑, golden diff 요약, 테스트·브라우저 검증 결과를 기록한다.
- 리뷰 지적이나 CI 실패가 있으면 수정하고 관련 검증을 다시 실행한다. 병합 여부는 저장소 규칙과 사용자 지시를 따른다.
- 운영 반영이 허용된 환경이라면 운영 스택의 `git status`와 다른 작업 변경·`.state/embed.json`(`devtracker`)을 먼저 확인해 보존한다. `make check` → `make plan`으로 `raw.*` 데이터·뷰·기존 필터에 의도치 않은 변경이 없음을 확인한 뒤 `make load` 없이 `make sync`로 Superset 객체만 반영한다.
- 실제 운영 웹앱 임베드 화면에서 `overview`·`issues`·`docs`의 기본 기간, 기간 변경, 차트 재조회, 범위 밖 차트 유지, 기존 필터 조합과 공개 경로의 guest `time_range` 200 / `form_data` 403 경계를 확인한다. 문제가 있으면 즉시 원복 후 `make plan` → `make sync`로 되돌린다.
- 운영 화면 검증 결과를 `reference/devtracker-time-range-filter-design.md`, 이슈 #67, 그리고 [이슈 #49](https://github.com/ohyounjo/dashboard/issues/49)에 기록·연결한다. 운영 환경에 접근할 수 없거나 운영 반영을 수행하지 못한 경우 미완료 항목으로 명시하고 PR을 draft 또는 후속 조치 필요 상태로 보고한다.

# 최종 보고

- PR 링크, 기준 SHA, 확정 사양, 격리 테스트(단위·통합·임베드 화면) 결과, 운영 반영 및 운영 화면 검증 결과(또는 미수행 사유), 이슈 #67·#49 갱신 내역을 간결하게 보고한다.
````
