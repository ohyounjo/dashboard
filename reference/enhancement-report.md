# 보강 구현 보고

> 지시문: `reference/enhancement-prompt.md` · 설계: `reference/enhancement-plan.md` · 실행: 2026-11-19 ~ 2026-11-20
> 리뷰어: Codex(`chatgpt-codex-connector[bot]`) · 저장소: `ohyounjo/dashboard`

## 1. 단계별 상태

| 단계 | 내용 | 상태 | PR |
|---|---|---|---|
| 준비 | 설계 문서 3종 | 병합됨 | [#5](https://github.com/ohyounjo/dashboard/pull/5) |
| 0 | 안전망: 번들 스냅샷, 테스트 분리, CI | 병합됨 | [#6](https://github.com/ohyounjo/dashboard/pull/6) |
| 1 | 차트 유형 레지스트리(기존 6종 이전) | 병합됨 | [#7](https://github.com/ohyounjo/dashboard/pull/7) |
| 2 | 축약 유형 16종, 갤러리 프로젝트, Superset 버전 관리 | 병합됨 | [#8](https://github.com/ohyounjo/dashboard/pull/8) |
| 3 | 위젯 매니페스트, 배치·필터 확장, prune 표식 | 병합됨 | [#9](https://github.com/ohyounjo/dashboard/pull/9) |
| 4 | `make new`, `make plan`, 설정 버전 | 병합됨 | [#10](https://github.com/ohyounjo/dashboard/pull/10) |
| 5a | Studio: 프로젝트 마법사, 데이터 교체, 탭 구성 | 병합됨 | [#11](https://github.com/ohyounjo/dashboard/pull/11) |
| 5b | Studio: 대시보드 배치, 필터 | 병합됨 | [#14](https://github.com/ohyounjo/dashboard/pull/14) |
| 후속 | 작업 폴더 분리(이미지 태그, 테스트 스택 이름·포트, 다른 폴더의 스택 보호) | 이 보고서 갱신을 포함한 PR | 9절 |
| 6 | 테마·언어, 위젯 후보, `pull`, 5-4~5-6 | 범위 밖(하지 않음) | |

## 2. 단계별 완료 조건의 확인 결과

| 단계 | 완료 조건(설계서 8절) | 확인 |
|---|---|---|
| 0 | `make test-unit`이 스택 없이 통과. PR에서 자동 실행 | `32 passed, 27 deselected`(compose·`.env` 없이 `docker run`). `.github/workflows/tests.yml`이 PR마다 실행. `make test`는 별도 스택 `dashkit-test`에서 `54 passed, 5 skipped` + `12 passed`, 전후로 개발 스택 `.state/` 체크섬 동일 |
| 1 | 스냅샷 변화 없음. `chart_params`에 유형별 분기 없음 | `tests/golden/` diff 0바이트. `test_chart_params_has_no_per_type_branch` 통과. 합성 스키마가 변경 전 스키마와 dict 수준에서 동일(1회 확인). `45 passed` / 통합 `67 passed, 5 skipped` + `12 passed` |
| 2 | 갤러리의 모든 차트가 데이터 조회 200. 카탈로그 문서가 레지스트리에서 생성됨 | 갤러리 차트 25개 조회 200(`make test` 3단계 `38 passed`). `reference/chart-catalog.md` 축약 절과 `contracts/chart-shorthand.md` 표가 `make gen`으로 생성되고 `test_generated_docs_are_current`가 최신 여부 확인. 헤드리스 캡처 75건(탭 6 × 3모드)에서 차트 오류 없음 |
| 3 | 위젯 추가가 폴더 하나. 기존 두 예시 설정 무수정 통과 | `tests/test_widgets.py`가 임시 폴더 하나로 스키마·기본값·열 참조·허용 목록·`/api/app` 반영을 확인. devtracker·sales-sample 설정 diff 0줄(스냅샷은 표식 2줄씩만 추가). 통합 `162 passed, 37 skipped` + `15 passed` + `45 passed` |
| 4 | 예시 CSV 폴더에서 만든 초안이 `make check`와 `make apply` 통과 | `make test` 4단계: `make new NAME=zz-newtest FROM=projects/sales-sample/data` → apply → `3 passed`(탭·임베드, 위젯 데이터, 차트 20개 조회). 최종 `318 passed`(단위) / 통합 4단계 모두 통과 |

| 5a | Studio만으로 CSV 폴더에서 대시보드까지 만들 수 있음. Studio가 쓴 파일이 `make check` 통과하고 주석이 보존됨 | 헤드리스로 화면을 실제 조작: 마법사에 CSV 4개 → 생성 → 적용(약 7초) → 미리보기에 새 프로젝트의 대시보드. 같은 흐름의 API 통합 테스트 11건을 `make test`의 Studio 단계로 추가. 저장은 임시 사본 → `load_project` 검증 → 원자적 교체. 탭 이동은 줄의 자리만 바뀌고 되돌리면 바이트 동일, 초안의 `# 추론: 확인 필요` 주석 24개 유지. 중첩 iframe(Studio → 웹앱 → Superset) 렌더링 확인, `make studio-down` 뒤 `frame-ancestors 'none'`. 단위 `331 passed`(app) + `322 passed`(studio 이미지) |
| 5b | 차트 추가·이동·너비 조절이 `layout`에 반영됨 | 헤드리스로 화면을 실제 조작: 팔레트로 차트 추가 → 다른 행으로 끌기 → 가장자리를 끌어 6칸 → `dashboard.yaml`의 diff는 3줄 추가·2줄 삭제뿐(다른 줄과 주석 24개 그대로) → 적용 → 미리보기의 Superset 대시보드에 같은 행·순서·너비(6:3:3)로 배치. API 통합 테스트(`chart.add` → `layout.set` → `filter.add` → apply → Superset position의 너비와 순서, 필터 scope 확인 → 삭제 → apply) 2건을 Studio 단계에 추가 |

모든 PR에서 GitHub Actions(단위 테스트, 화면 캡처 + 통합 테스트)가 통과한 뒤 병합했다.

## 3. 리뷰에서 받은 지적과 처리

| PR | 등급 | 지적 | 처리 |
|---|---|---|---|
| #5 | P2 | 테스트 스택의 상태·빌드 디렉터리도 분리해야 한다 | 고침. 설계서 3-2에 `.test/state`·`.test/build`와 완료 조건을 추가하고 0단계에서 구현 |
| #5 | P2 | Studio 미리보기에 필요한 프레임 허용 정책이 설계에 없다 | 고침. 설계서 6-2에 `STUDIO_ORIGIN` 정책과 중첩 iframe 완료 조건 추가, 5a에서 구현 |
| #6~#9 | — | 지적 없음(👍) | — |
| #10 | P1 | `make plan`이 raw 테이블을 owner 계정으로 읽는다(AGENTS.md: 읽기 경로는 reader만) | 고침. 로더가 적재 트랜잭션에서 `dashkit_meta`에 행 지문(키·행 모두 SHA-256)과 적재 스냅샷을 남기고, plan은 `dash_reader`로 그것만 읽는다. owner 자격 증명 없이 동작(테스트). 대가: `sql:` 데이터셋의 새 열 예측을 없앰 |
| #10 | P2 | 공통 열이 없으면 기존 행 수가 0으로 계산된다 | 고침. 행 수는 적재 스냅샷에서, 열 구성이 다르면 비교 불가(전부 추가·전부 삭제) |
| #10 | P2 | 새 기본 키가 기존 행에서 중복이면 삭제를 놓친다 | 고침. 기본 키 정의가 바뀌면 비교 불가, `diff_rows`는 키 중복을 덮어쓰지 않음 |
| #10 | P2 | 숫자 지문이 Decimal 컨텍스트 정밀도(28자리)로 반올림된다 | 고침. 전체 자릿수를 펼쳐 끝의 0만 제거 |
| #10 | P2 | 초안 YAML의 키(`off`, `yes` 등)가 불리언으로 읽힌다 | 고침. 모든 동적 키를 값과 같이 인코딩 |
| #10 | P2 | 실패한 sync의 번들이 plan의 비교 기준이 된다 | 고침. import 성공 뒤에만 `.build/<프로젝트>/applied/`를 쓰고 그것과 비교 |
| #10 | P2 | 초안 YAML에서 U+0085가 줄바꿈으로 읽힌다 | 고침. U+0085·U+2028·U+2029 이스케이프 |
| #10 | P2 | prune 실패 뒤 남은 삭제를 plan이 놓친다 | 고침. `superset.pending_prune`으로 표시. 리뷰 반복 3회를 채워 이 커밋은 재리뷰 없이 CI 통과 뒤 병합 |

| #11 | P2 | 마법사를 떠난 뒤에도 드롭 리스너가 남아 데이터 화면의 드롭을 가로챈다 | 고침. `fileDrop`이 해제 함수를 돌려주고 셸이 화면 전환 전에 `destroy()`를 부른다. 수정 전·후를 헤드리스로 재현 |
| #11 | P2 | 떠난 화면의 저장 응답이 현재 화면을 다시 그린다 | 고침. 모든 편집 화면에 `destroy()`와 생존 검사. PATCH 응답을 늦춘 재현으로 확인 |
| #11 | P2 | 앱 화면에서 저장 중 입력한 내용이 사라진다 | 고침. 저장 중 폼을 `inert`로 잠금(탭 구성의 옵션 폼도) |
| #11 | P2 | 떠난 프로젝트의 설정 조회 실패가 현재 화면에 표시된다 | 고침. 실패 경로에서도 요청 당시의 프로젝트를 확인. 리뷰 반복 3회를 채워 재리뷰 없이 CI 통과 뒤 병합 |

Codex는 푸시만으로는 다시 리뷰하지 않아서, 고친 뒤에는 PR에 `@codex review` 댓글로 재리뷰를 요청했다.

## 4. 실측 결과와 그에 따라 설계서와 달라진 점

기록은 `contracts/superset-notes.md` 10~12절에 있다.

| 항목 | 실측 | 달라진 점 |
|---|---|---|
| D4 축약 유형 통과 기준 | 16종 모두 갤러리에서 데이터 조회 200, guest 화면 오류 없음 | 내려간 유형 없음(합계 22종) |
| 원본 유형 `chord`, `world_map`, `country_map` | 데이터 조회는 200이지만 guest 화면에서 "Access is Denied". 예전 조회 경로 `/superset/explore_json/`에 `can_explore_json` 권한이 필요 | 권한을 넓히지 않고 갤러리에서 뺌. 카탈로그에 원본 조각과 이유만 남김 |
| 3-5 표식 저장 | import가 `is_managed_externally`, `external_url`을 저장함(4종 객체 모두) | — |
| 3-5 목록 API 필터 | 두 열 모두 필터 불가(400), `external_url`은 REST로 읽을 수도 없음. export에도 안 나옴 | prune 식별을 "프로젝트 DB 계보 + (표식 또는 uuid5)"로 합침. 프로젝트 전환 때 이전 프로젝트 객체 삭제는 여전히 `managed.json` 필요. 키 대조 테스트에 import 전용 키 2개를 근거와 함께 허용 |
| D3 화면 편집 차단 | 표식이 있으면 대시보드 Edit 버튼 없음, Explore 덮어쓰기 저장·데이터셋·DB 저장 비활성 | 통과. 기본 차단, `SYNC_LOCK_UI=0`으로 해제(`is_managed_externally: false` 명시) |
| 4-3 `defaultDataMask` | 화면에서 필터 4종 기본값을 저장해 export로 형태 확인 | 번들이 같은 `extraFormData`를 씀 |
| 4-3 `time_range` 필터 | 차트에 시간 조건이 없으면 적용되지 않음 | `time_range` 필터가 있는 대시보드의 범위 안 축약 차트에 "No filter" 시간 조건을 자동으로 붙임 |
| 4-3 `time_grain` 필터 | 데이터셋이 없으면 선택지를 읽지 못함 | `dataset`을 필수로(설계서 예시에는 없음) |
| 4-3 기간 필터(guest) | 임베드 화면에서는 값을 바꿀 수 없음(`/api/v1/time_range/`에 권한 없음) | 권한을 넓히지 않음. 기본값만 적용되는 한계로 기록 |
| 4-3 `HEADER` 노드 | ROW 안에 들어갈 수 없음 | header 항목은 한 행에 혼자 둠 |
| 4-3 필터 `charts`(scope) | import가 position의 자리표시 chartId를 실제 id로 바꿈 | `charts`를 쓴 필터가 있는 대시보드만 자리표시 chartId를 서로 다르게 줌 |

## 5. 스스로 정한 판단

각 PR 본문의 "스스로 정한 판단"에 이유와 함께 적었다. 주요한 것만 모은다.

**공통·0단계**
- `make test-unit`은 compose 대신 `docker run`으로 실행한다(compose는 `.env`의 비밀값을 요구함).
- 테스트 정리 경로는 리터럴 `./.test`로 고정했다(변수가 비면 `rm -rf ./`가 되는 것을 피함). 테스트 스택 삭제는 프로젝트 이름을 직접 지정한다.
- 스냅샷용 뷰 열 정보는 파일로 고정하고 통합 테스트가 실제 DB와 대조한다.

**1~2단계**
- params에 쓰는 순서를 규칙으로 고정(`viz_type` → 필드 → `defaults` → `d3format` → `where`)해 스냅샷을 바이트 단위로 유지했다.
- 레지스트리에 `kind: order_by`, 유형별 `schema:` 조각, `emit: false`를 추가했다(설계서 표에 없음).
- 생성 명령 이름은 `gen`이다(`docs`는 예시 테이블 이름이라 도메인 단어 테스트에 걸림).
- 유형 파일에 `example:`을 넣지 않고 갤러리 설정에서 예시를 읽는다(예시 열 이름이 템플릿 코드에 들어가면 금지어 검사와 부딪침).
- Superset 버전은 `.env.example` 한 곳에 두고 Makefile이 `.env.example` → `.env` 순으로 읽는다(사용자의 기존 `.env`를 고치지 않기 위해).
- 키 대조 범위는 종류별 최상위 키, 데이터셋 `metrics[]`·`columns[]`, DB `extra`로 한정했다. 기존 probe export에 대시보드 `metadata`가 없어 sync가 만든 대시보드의 export를 `managed/`로 추가했다.
- **guest 권한을 넓히지 않았다**(`can_explore_json`).

**3단계**
- 위젯 매니페스트 표시 `x-unique`, `x-date-range`를 새로 정의했다(기존 검사와 오류 문구 유지). `dataset` 없는 위젯을 허용했다.
- prune 식별 규칙과 시간 조건 자동 추가(4절 표).
- **당시 guest 권한을 넓히지 않았다.** 이후 필요한 권한이 `can_time_range on Api`로 확인되어 이슈 #49에서 확대를 승인했다.

**4단계**
- plan은 차이가 있어도 종료 코드 0이다(검증·설정·접속 실패만 1).
- 추론 기준: enum은 고유값 12개 이하이면서 값 있는 행의 절반 이하, ref는 `<테이블>_id` 꼴에 더해 "다른 테이블의 단일 열 기본 키와 이름이 같은 열"도 후보(설계서보다 넓힘), 테이블당 차트 최대 6개. 한글 열 이름은 `column_<자리>`로 바꾸고 원래 머리글을 `labels`에 남긴다. CP949 CSV도 읽는다.
- `migrate`는 `version: 1`을 첫 줄에 텍스트로 넣는다. `make migrate CHECK=1`은 키가 없는 예시 프로젝트에서 종료 코드 1이다(예시 설정은 고치지 않는다는 조건 때문에 그대로 둠).
- 행 지문의 기본 키도 해시로 저장했다(raw의 키 원본이 reader에게 보이지 않게).

**5a단계**
- YAML 쓰기의 기본 경로는 "고친 곳만 다시 쓰기"이고 `ruamel.yaml`은 예비 경로다. ruamel round-trip만으로는 예시 파일마다 72~164줄이 달라져(flow 스타일 공백, 줄 맞춤) 주석·글자 보존 조건을 채우지 못했다. D2의 의존성과 빌드 단계 분리(studio 이미지에만 설치)는 그대로다.
- backend가 `.state/embed.json`의 `project`(마지막으로 sync에 성공한 프로젝트)를 현재 프로젝트로 따르게 했다. Studio 컨테이너에는 Docker 권한이 없어 backend의 `PROJECT` 환경변수를 바꿀 수 없기 때문이다. `.env`의 `PROJECT`와 마지막 적용 프로젝트가 다를 때의 동작이 바뀐다(예전: 미적용 표시, 지금: 마지막 적용 프로젝트 표시).
- 인증이 없는 로컬 서비스라 Host 검사(DNS rebinding), 쓰기 요청의 Origin·`Sec-Fetch-Site`·Content-Type 검사(CSRF)를 넣었다.
- 바뀐 파일 목록은 Studio 프로세스가 쓴 파일로 한정했다(컨테이너에 git을 넣지 않음).
- Studio 화면 캡처 5장을 `reference/studio/`에 넣어 PR 본문에서 참조했다.

**5b단계**
- 너비 조절은 "끄는 칸만 바꾸고 한계에서 막는" 방식이다. 이웃을 자동으로 줄이면 사용자가 건드리지 않은 칸의 YAML 글자가 바뀐다.
- 배치 편집기는 차트를 그리지 않고 제목·유형·데이터셋 카드만 보여 준다(설계서 6-3). 차트 폼은 레지스트리의 필드에서 만들고, 원본 방식 차트는 params를 JSON 글상자로만 고친다.
- 대시보드 안 탭 묶음은 전체 편집(탭 추가·이름·순서·삭제, 카드의 탭 안팎 이동)까지 넣었다.
- `chart.remove`는 비게 된 행뿐 아니라 빈 탭·빈 묶음까지 지운다(스키마가 빈 탭을 허용하지 않아, 그러지 않으면 탭의 마지막 차트를 지울 수 없다).
- `layout.set`은 layout 전체를 받되 바뀌지 않은 행의 글자는 그대로 둔다(행 단위로 짝지어 원문 조각 재사용).
- 팔레트의 유형 묶음과 아이콘은 화면이 필드 kind와 유형 이름으로 정한다(레지스트리에 묶음·아이콘 항목이 없음).

## 6. 진행 중에 낸 결함과 수습

- **1단계에서 `dashkit/config.py`에 `referenced_columns`가 두 번 정의된 채 병합됐다.** 내가 스크립트로 코드를 바꾸다 잘못 이어 붙였고, 뒤쪽의 옛 정의가 쓰여서 기존 6종에서는 결과가 같아 테스트로 드러나지 않았다. 2단계에서 `bubble`(x가 지표)을 추가하다 발견해 고치고 회귀 테스트를 넣었다(#8).
- 0단계에서 PR 대기 스크립트가 CI 진행 상태를 잘못 읽어(`gh pr checks`에 없는 옵션 사용) "대기 중인 검사 없음"으로 판단한 적이 있다. 병합 전에 다시 확인해 실제로는 모든 검사가 끝난 뒤 병합했다.
- **5a 리뷰 대응 중 작업 트리가 겹쳤다.** 내가 쓰던 체크아웃(`/home/ohyounjo/source/dashboard`)에서 다른 작업(PR #12 `feat/severity-pie`, #13)이 시작돼 브랜치가 바뀌었다. 그 순간 내 미커밋 변경 4개 파일이 그 브랜치의 작업 트리에 실려 있었다. 발견 즉시 내 작업을 별도 git worktree로 옮기고, 원래 체크아웃에서는 내 미커밋 변경만 되돌렸다(상대의 파일과 브랜치는 건드리지 않음). 이후 작업과 `make test`는 worktree에서 했다.
- 그 사이 main에 #12·#13이 병합되면서 PR #11의 CI가 병합 결과 기준으로 실패했다(예시 파일의 줄 번호에 기대던 Studio 테스트 1건, 워크플로와 `e2e/report.mjs`의 버전 불일치). main을 브랜치에 병합하고 테스트가 줄 번호 대신 내용으로 찾게 고쳤다.

## 7. 남은 한계

- **guest 권한 제한:** 이후 이슈 #49에서 기간 변경을 위한 `can_time_range on Api`를 승인했다. `chord`, `world_map`, `country_map`은 `can_explore_json on Superset`을 부여하지 않아 여전히 임베드에서 그려지지 않는다. 권한별 실측과 SQL 노출 등은 [Superset 계약 13절](../contracts/superset-notes.md)을 따른다.
- **prune:** 프로젝트를 전환했을 때 이전 프로젝트의 Superset 객체를 지우는 일은 여전히 `.state/managed.json`이 필요하다(같은 프로젝트 안의 이전 객체는 상태 파일 없이 지워진다).
- **plan:** `sql:` 데이터셋의 새 열은 적용 전에 예측하지 못한다("적용해야 알 수 있음"). 이 변경 전에 적재된 DB는 한 번 `make apply`해야 데이터 차이가 나온다. 열 구성이나 기본 키를 바꾼 테이블은 전부 추가·전부 삭제로 센다. 행 지문은 솔트 없는 SHA-256이라 값의 범위가 좁은 키는 추측해 맞춰 볼 수 있다(원본은 저장하지 않음).
- **설정 검사:** 5b 통합 확인 당시에는 막대·선 차트의 `x`와 `groupby`에 같은 열을 넣어도 검사를 통과했지만, 이후 [PR #31](https://github.com/ohyounjo/dashboard/pull/31)에서 적용 전에 YAML 경로와 함께 충돌을 보고하도록 고쳤다. [PR #60](https://github.com/ohyounjo/dashboard/pull/60)은 자동 제거되는 서로 다른 지표와 일부 원본 유형 검사도 보완했다. 확인 범위 밖 원본 설정과 일부 지표 객체는 검사를 보장하지 않는다. 현재 지원 범위는 [차트 계약](../contracts/chart-shorthand.md#중복-라벨-검사)을 따른다.
- **`make apply`:** backend 이미지를 다시 만들지 않는다. 템플릿 코드(`dashkit/`, `web/widgets/*/widget.json`)를 바꿨으면 `make up`이 필요하다.
- **위젯:** 새 위젯의 고정 문구(`web/strings.js`)와 전용 CSS(`web/styles.css`)는 아직 위젯 폴더 밖이다.
- **Studio:** Chromium 헤드리스로만 확인했다. Firefox·Safari, 터치 끌기(좁은 화면은 버튼으로 조작), 실제 OS 파일 드래그, 스크린 리더는 확인하지 않았다. 적용 중에 열려 있던 미리보기가 prune된 차트를 조회해 Superset 404·400이 콘솔에 한 번 찍힌다(적용 뒤 새로 고쳐짐). `ruamel` 예비 경로(앵커·별칭 등이 있는 파일)에서는 주석과 값은 남지만 파일 모양이 표준 꼴로 바뀐다.
- 당시 **`make migrate CHECK=1`**은 `version` 키가 없는 예시 프로젝트에서 종료 코드 1이었다. 이슈 #57에서 계약을 변경하여 생략된 현재 버전은 검사에 통과한다(일반 migrate는 여전히 키를 삽입한다).
- **Superset 격자**는 390px에서 행을 접지 않아 차트가 좁게 나온다(기존 동작).
- 6단계(테마·언어, 위젯 후보, `make pull`, 설정 파일 분할, 원본 어댑터, 여러 프로젝트 동시 운영)는 하지 않았다.

## 8. 직접 확인해 주세요

1. **개발 스택:** PR #11 병합 뒤 main에서 `make up && make apply`가 되는 것까지 확인했다(devtracker, `frame-ancestors 'none'`). 5b 병합 뒤에는 이 체크아웃이 사용 중일 수 있어 같은 확인을 하지 못했을 수 있다(마지막 메시지 참고). `make up && make apply` 한 번이면 된다.
2. **Studio 화면:** `make studio` → http://localhost:8090. 마법사(CSV 놓기) → 적용 → 미리보기, 탭 구성, 데이터 교체, 대시보드 배치(끌기·너비), 필터. 끝나면 `make studio-down`(웹앱·Superset의 프레임 허용을 되돌림). 캡처는 `reference/studio/`에 있다.
3. **갤러리 화면:** `PROJECT=chart-gallery make apply` 후 탭 7개(축약 22종, 원본 유형, 배치·필터 견본). 확인 뒤 `PROJECT=devtracker make apply`. (Studio에서 프로젝트를 골라 "적용"해도 된다.)
4. **Superset 화면 편집 차단:** http://localhost:8088 에서 sync가 만든 차트·대시보드를 직접 고칠 수 없다(D3). 필요하면 `.env`에 `SYNC_LOCK_UI=0`.
5. **backend의 현재 프로젝트 규칙:** `.env`의 `PROJECT`가 아니라 마지막으로 적용한 프로젝트를 보여 준다. 의도와 맞는지.
6. **guest 권한:** 기간 해석 권한은 이슈 #49에서 승인했다. 레거시 차트 권한은 별도 판단이다.
7. **작업 트리:** 내 worktree(`git worktree list`로 보이는 스크래치 경로)는 작업이 끝나면 지워도 된다(`git worktree remove <경로>`). 원래 체크아웃에서 내가 되돌린 것은 내 미커밋 변경 4개 파일뿐이다.

## 9. 후속: 여러 작업 폴더에서 동시에 작업하기

5a·5b를 진행하는 동안 같은 컴퓨터의 다른 세션(로그인 기능 구현)과 여러 번 부딪쳤다. git 브랜치는 소스 파일만 나눠 주고, 아래 자원은 컴퓨터 전체가 함께 쓰기 때문이다.

| 겹친 자원 | 생긴 일 |
|---|---|
| Docker 이미지 태그 `dashkit-app:local`, `dashkit-studio:local` | 다른 세션이 빌드한 이미지로 내 테스트 스택의 backend가 떠서 시작에 실패 |
| 테스트 스택 이름 `dashkit-test`와 포트 18080·18088·18090 | 한쪽이 내리는 중에 다른 쪽이 올려 "marked for removal", 포트 "already allocated" |
| 작업 폴더 하나 | 다른 세션이 브랜치를 바꿀 때 내 미커밋 파일이 그 브랜치에 실려 감(6절) |

0단계에서 "이미지는 개발 스택과 공유한다"(설계서 D5)와 고정된 테스트 스택 이름으로 구현한 것이 원인이었다. 한 번에 한 작업만 한다는 전제에서는 맞았지만 동시 작업에는 맞지 않았다. 사용자 승인을 받아 다음과 같이 고쳤다(README "여러 작업 폴더에서 동시에").

- Makefile이 작업 폴더 경로의 체크섬에서 식별자를 만들고(`make info`), 직접 빌드하는 이미지 태그(`…:local-<식별자>`), 테스트 스택 이름(`dashkit-test-<식별자>`), 테스트 포트(20000~39999의 폴더별 구간)에 쓴다.
- `make test`는 시작할 때 쓸 포트를 다른 컨테이너가 쓰고 있는지 확인하고, 그렇다면 이유와 함께 멈춘다.
- 스택의 주인은 그 스택을 띄운 compose 파일의 경로(정확히 같은지)로 판정하고, 프로젝트 이름은 compose가 실제로 해석한 값을 쓴다. 같은 확인을 테스트 스택에도 걸어, 식별자가 겹치더라도 다른 폴더의 테스트 스택을 올리거나 지우지 않는다(리뷰 지적 3건을 반영).
- 개발 스택의 compose 프로젝트 이름은 바꾸지 않았다(바꾸면 지금 쓰는 스택·볼륨과 끊어진다). 대신 폴더 이름이 같은 다른 폴더가 이미 그 이름으로 스택을 띄웠으면 `make up`·`apply`·`down`·`reset`·`studio` 등 그 스택에 닿는 목표가 실행 전에 멈춘다.
- `make test-down`이 `studio`·`tools` 프로필의 컨테이너까지 지운다(중간에 끊긴 실행이 남긴 Studio 컨테이너가 다음 실행에서 지워진 `.test/` 폴더를 붙잡은 채 재사용되던 문제. #14에서 먼저 고침).

확인: 두 작업 폴더에서 한쪽은 `make test`(5단계 통과), 다른 쪽은 `make test-unit` 2회를 **동시에** 실행해 모두 통과했고, 이미지가 폴더별 태그로 따로 만들어졌다. 그 사이 다른 세션의 스택(`dashkit-test`, `dashkit-ui`)과 개발 스택도 떠 있었다. worktree에서 `make _guard_project`가 원래 체크아웃의 개발 스택을 감지해 멈추는 것도 확인했다.

남은 것: 개발 스택은 폴더마다 자동으로 나뉘지 않는다(이름과 포트를 직접 줘야 한다). 테스트 포트 구간이 우연히 겹칠 확률은 폴더 두 개 기준 약 1/2000이고, 겹치면 `TEST_PORT_BASE`로 바꾼다.

