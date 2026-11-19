# Superset 임베드형 범용 대시보드 템플릿 계획

> 작성일: 2026-11-19 (설계 리뷰 반영 2판) · 방식: 내 웹앱 + Superset Embedded SDK · 실행 환경: 사용자의 Linux 머신
> 1~8절은 설계, 9절은 Claude Code에 그대로 붙여 넣을 오케스트레이션 프롬프트다.

## 1. 목표

**"범용"의 정의:** `projects/<이름>/` 폴더 하나(데이터 CSV, `schema.yaml`, `dashboard.yaml`)만 바꾸면 **도메인이 전혀 다른** 대시보드가 된다. 템플릿 코드(`dashkit/`, `web/`, `superset/`)에는 특정 도메인의 테이블명, 열 이름, 상태값, 탭 이름이 한 글자도 없다.

- **역할 분담**
  - 차트(KPI, 추이, 표, 필터)는 Superset이 그리고 웹앱에 iframe으로 임베드한다.
  - 화면 틀(Rail, 헤더, 다크모드)과 Superset이 못 그리는 위젯(칸반, 간트)은 웹앱이 그린다. 이 위젯들도 **설정으로 열을 매핑**해 쓰는 범용 부품이다.
- **재현성:** Superset 객체는 클릭으로 만들지 않고 설정 파일에서 생성한다(dashboard-as-code). 새 머신에서 `make up && make apply`면 같은 화면이 나온다.
- **예시 프로젝트 2개**로 범용성을 증명한다: `devtracker`(기존 스마트홈 개발 현황), `sales-sample`(도메인이 다른 두 번째 예시이자 테스트 픽스처).

## 2. 1판 설계 리뷰와 결정

1판은 "CSV와 설정만 바꾸면 재사용"을 목표로 했지만, 실제로는 개발 현황 도메인에 묶여 있었다. 아래가 고친 점이다.

### 2-1. 범용성을 막던 문제

| # | 1판의 문제 | 2판의 결정 |
|---|---|---|
| 1 | 백엔드 API가 `/api/milestones`, `/api/tasks`, `/api/members`로 도메인 고정. 다른 프로젝트에는 이 테이블이 없다. | 범용 `GET /api/data/{dataset}` 하나로 바꾼다. 위젯이 참조한 데이터셋만 열린다. |
| 2 | "일정 탭 = 간트 + 칸반"이 프롬프트에 고정. "탭은 하드코딩하지 않는다"는 요구와 모순. | `dashboard.yaml`의 `tabs`가 탭과 섹션을 정의한다. 섹션 유형은 `superset`, `gantt`, `kanban`이고, 간트·칸반은 열 매핑·상태 라벨·이모지를 설정으로 받는다. |
| 3 | 프로젝트 이름, 로고, 기준일, 메인 컬러를 둘 곳이 없다. | `dashboard.yaml`의 `app` 블록에 둔다. |
| 4 | `CURRENT_DATE` 기준 계산(🆕 표시, D-day)은 예시 데이터가 낡으면 틀어지고 테스트가 날짜에 따라 깨진다. | 기준일 `app.as_of`(`today` 또는 고정 날짜)를 두고 SQL에서 `dash.as_of()` 함수로 읽는다. |
| 5 | 검증 규칙이 `type/required/enum/ref/gte`뿐이라 "완료면 진척률 100", "스냅샷 마지막 값 = 현재 값" 같은 규칙을 못 쓴다. 코드에 넣으면 다시 도메인 고정. | 테이블 단위 `checks`(위반 행을 돌려주는 SQL)를 `schema.yaml`에 둔다. 열 단위 규칙은 `type, required, values, ref, min, max, pattern`. |
| 6 | 범용성 테스트가 "같은 스키마의 CSV만 교체". 범용성이 아니라 데이터 교체만 확인한다. | 도메인이 다른 `sales-sample`로 `PROJECT`만 바꿔 적용하고, 템플릿 코드에 도메인 단어가 없는지 grep으로 검사한다. |
| 7 | 바꿀 곳이 `data/`, `config/` 두 폴더에 흩어져 있고 한 번에 한 프로젝트만 담는다. | `projects/<이름>/`으로 모으고 `.env`의 `PROJECT`로 고른다. |

### 2-2. 정합성·보안 문제

| # | 1판의 문제 | 2판의 결정 |
|---|---|---|
| 8 | "계산 규칙은 한 곳에만 정의"라면서, Superset 가상 데이터셋은 Superset 안에만 있어 백엔드(칸반·간트)가 쓸 수 없다. 정의가 둘로 갈린다. | 데이터셋을 **Postgres 뷰**로 만든다. 원본 테이블은 `raw` 스키마, 뷰는 `dash` 스키마. Superset과 백엔드가 같은 뷰를 읽는다. |
| 9 | Superset과 백엔드가 데이터 DB에 쓰기 권한으로 접속할 수 있다. | 로더만 소유자 계정을 쓴다. Superset과 백엔드는 `dash` 스키마 SELECT만 있는 `dash_reader` 계정을 쓴다. `raw`는 직접 읽지 못한다. |
| 10 | 백엔드가 Superset **관리자 계정**으로 로그인해 guest token을 받는다. 로그인·CSRF·갱신 로직이 필요하고 관리자 비밀번호가 상시 서비스에 들어간다. | 백엔드가 `GUEST_TOKEN_JWT_SECRET`으로 guest token(JWT)을 **직접 서명**한다(Embedded SDK 문서가 허용하는 방식). 백엔드는 Superset 계정을 갖지 않는다. 관리자 계정은 `make apply` 때만 쓴다. |
| 11 | `embed.rls: "assignee = '{{user}}'"` 예시가 있지만 로그인이 없어 `user`가 누구인지 정의되지 않는다. 문자열 치환은 SQL 주입 위험이 있고, 백엔드 API 경로에는 RLS가 적용되지 않는다. | 1차 범위에서 사용자별 RLS를 뺀다. 고정된 범위 제한은 뷰 SQL에 쓴다. 사용자별 범위는 8절 확장 지점으로 남긴다. |
| 12 | Superset에 CORS 설정을 요구한다. | 뺀다. SDK는 iframe과 postMessage로 통신하므로 CORS가 필요 없다. 필요한 것은 `frame-ancestors` 허용과 임베드 허용 도메인뿐이다. |
| 13 | 인증이 없다는 전제가 문서에 없다. | 모든 포트를 `127.0.0.1`에만 연다. 외부 공개는 앞단 인증 프록시가 전제임을 README에 쓴다. |
| 14 | 예시 설정의 오류: 정의되지 않은 데이터셋(`kpi_snapshots`) 참조, 개요 필터가 개요 차트의 데이터셋에 없는 열을 가리킴, INNER JOIN이라 담당자 없는 이슈가 집계에서 빠짐, `end`·`column` 같은 SQL 예약어 열 이름. | 5절 예시를 고쳤다. `make check`가 키 참조와 필터 열 존재를 검사한다. 조인은 LEFT JOIN, 열 이름은 snake_case에 예약어 금지. |
| 15 | 기존 화면의 "다음 마일스톤 D-day" KPI와 목록 2개가 말없이 사라졌다. | D-day는 KPI로 넣는다. 목록 2개는 표 차트로 넣는다(6-2절). |

### 2-3. 구현 위험을 줄이는 결정

| # | 1판의 문제 | 2판의 결정 |
|---|---|---|
| 16 | sync가 REST API로 객체를 하나씩 만들고 이름으로 찾아 갱신한다. 호출이 많고 중간 실패 시 반쯤 적용된 상태가 남는다. 설정에서 지운 차트는 Superset에 계속 남는다. | 설정에서 **Superset asset 번들**(공식 import/export YAML 형식)을 만들어 한 번에 가져온다. 객체 UUID는 `프로젝트/키`에서 결정적으로 만든다(uuid5). 지난번에 만들었지만 이번 설정에 없는 객체는 지운다(prune). 번들은 `.build/`에 남아 diff로 확인할 수 있다. |
| 17 | 자체 YAML을 Superset 차트 파라미터로 옮기는 범위가 정해져 있지 않다. viz 유형마다 파라미터가 다르고 버전에 따라 바뀐다. | 축약 문법은 6개 유형(`big_number_total`, `big_number`, `line`, `bar`, `pie`, `table`)만 지원한다. 그 밖의 유형과 세부 옵션은 차트의 `params`에 Superset 원본 파라미터를 그대로 써서 넘긴다. |
| 18 | 적재 후 Superset 캐시 무효화 API를 호출한다. 로더가 Superset 계정과 가동 상태에 의존한다. Redis는 그 캐시 때문에만 있다. | 데이터가 작은 템플릿이므로 차트 데이터 캐시를 끄고 Redis를 뺀다. 컨테이너가 7개에서 6개로 줄고 로더는 DB만 안다. 캐시가 필요해지면 8절대로 붙인다. |
| 19 | 스키마가 바뀌면(열 추가) truncate 후 insert로는 반영되지 않는다. | 로더는 매번 트랜잭션 하나에서 `raw`, `dash` 스키마를 통째로 다시 만든다. 실패하면 이전 상태가 그대로 남는다. |
| 20 | 호스트에 Python을 설치해 loader/sync를 돌린다. 머신마다 환경이 달라진다. | 호스트에는 Docker와 make만 둔다. 로더·sync·테스트는 `tools` 컨테이너에서 돈다. |
| 21 | loader, sync, backend가 각자 YAML을 해석하면 해석이 어긋난다. | 파이썬 패키지 `dashkit` 하나에 설정 로딩·검증을 두고 셋이 같이 쓴다. 설정 형식은 `contracts/`의 JSON Schema가 유일한 기준이다. |
| 22 | 값은 `make load`, 차트는 `make sync`, 열 추가는 둘 다. 무엇을 바꿨는지에 따라 명령이 다르다. | 무엇을 바꾸든 `make apply` 하나다(검사 → 적재 → Superset 반영). |

Superset 자체는 유지한다. 컨테이너가 많다는 부담은 있지만, 차트 유형·필터·SQL 지표를 설정만으로 늘릴 수 있다는 점이 범용 템플릿의 목적과 맞는다.

## 3. 아키텍처

```
projects/<PROJECT>/
  data/*.csv ─────┐
  schema.yaml ────┼─(make apply ①적재)─▶ data-db: raw.* 테이블 → dash.* 뷰(데이터셋)
  dashboard.yaml ─┤                                    ▲ 읽기 전용(dash_reader) ▲
                  │                                    │                        │
                  ├─(make apply ②반영)─▶ Superset ─────┘                        │
                  │      asset 번들 가져오기              │ iframe + guest token  │
                  │                                      ▼                        │
                  └─(읽기)──▶ backend ── /api/app, /api/guest-token ──▶ 웹앱(Rail 셸)
                                 └────── /api/data/{dataset} ─────────▶ 간트 · 칸반
```

| 컨테이너 | 역할 |
|---|---|
| `data-db` (PostgreSQL) | 대시보드 데이터. `raw`(CSV 원본)와 `dash`(뷰 = 데이터셋) |
| `superset-db` (PostgreSQL) | Superset 메타데이터. 데이터 DB와 분리 |
| `superset-init` | 마이그레이션, 관리자 생성, 역할 초기화 후 종료 |
| `superset` | 데이터셋, 차트, 대시보드, 필터, 임베드 |
| `backend` (FastAPI) | 앱 설정 전달, guest token 서명, 위젯용 데이터 API |
| `web` (nginx) | 정적 웹앱 제공, `/api`를 backend로 프록시(웹앱과 API가 같은 출처) |
| `tools` (일회성) | `dashkit check / load / sync`, 테스트 실행 |

## 4. 저장소 구조

```
dashboard/
├─ docker-compose.yml
├─ .env.example                 # 포트, 비밀값, PROJECT. `make env`가 임의 비밀값을 채워 .env 생성
├─ Makefile                     # env, up, check, apply, test, reset, down, logs
├─ projects/                    # ★ 사용자가 바꾸는 유일한 곳
│  ├─ devtracker/               #   예시 1: 스마트홈 앱 개발 현황(reference/index.html의 DATA)
│  │  ├─ data/*.csv
│  │  ├─ schema.yaml            #   CSV 테이블·열·검증 규칙
│  │  └─ dashboard.yaml         #   app, datasets, dashboards, tabs
│  └─ sales-sample/             #   예시 2: 다른 도메인. 범용성 테스트 픽스처 겸용
├─ contracts/                   # 설정 형식 JSON Schema, API 계약, 차트 축약 문법 대응표
├─ dashkit/                     # 파이썬 패키지 하나(이미지 하나)
│  ├─ config.py                 #   YAML 로딩·검증. loader/sync/api가 공유
│  ├─ loader/                   #   CSV → raw 테이블 → dash 뷰
│  ├─ sync/                     #   dashboard.yaml → asset 번들 → Superset
│  └─ api/                      #   FastAPI
├─ superset/
│  ├─ Dockerfile                # 고정 버전 이미지(드라이버가 빠진 버전이면 여기서 추가)
│  └─ superset_config.py
├─ web/                         # Rail 셸, 임베드, widgets/gantt.js, widgets/kanban.js
├─ tests/
├─ .build/                      # 생성된 asset 번들(커밋 제외)
└─ .state/                      # 임베드 UUID, 관리 중인 객체 목록(커밋 제외)
```

## 5. 설정 파일

### 5-1. `schema.yaml`: CSV 테이블과 검증

```yaml
tables:
  milestones:                       # data/milestones.csv → raw.milestones
    primary_key: id
    columns:
      id:         { type: text, required: true }
      title:      { type: text, required: true }
      start_date: { type: date, required: true }
      end_date:   { type: date, required: true }
      status:     { type: enum, values: [done, in-progress, planned, delayed] }
      progress:   { type: integer, min: 0, max: 100 }
    checks:                         # 위반 행을 돌려주는 SQL. 한 행이라도 나오면 전체 롤백
      - name: 시작일이 종료일보다 늦음
        sql: SELECT id FROM raw.milestones WHERE start_date > end_date
      - name: 완료인데 진척률이 100이 아님
        sql: SELECT id FROM raw.milestones WHERE status = 'done' AND progress <> 100
  issues:
    primary_key: id
    columns:
      id:           { type: text, required: true, pattern: "^ISS-\\d+$" }
      title:        { type: text, required: true }
      severity:     { type: enum, values: [critical, major, minor, trivial] }
      status:       { type: enum, values: [open, in-progress, resolved, closed] }
      assignee:     { type: text, ref: members.id }
      milestone_id: { type: text, ref: milestones.id }
      created:      { type: date }
      updated:      { type: date }
    checks:
      - name: 생성일이 수정일보다 늦음
        sql: SELECT id FROM raw.issues WHERE created > updated
```

- 열 유형: `text, integer, number, boolean, date, timestamp, enum`
- 열 이름은 snake_case로 쓰고 SQL 예약어(`end`, `column`, `order` 등)는 쓰지 않는다.

### 5-2. `dashboard.yaml`: 앱, 데이터셋, 대시보드, 탭

아래는 `devtracker` 설정의 일부다. 문서 탭과 개요의 표 차트는 같은 방식이라 줄였다.

```yaml
app:
  title: 스마트홈 앱
  logo_text: SH
  as_of: "2026-11-19"          # 예시 데이터라 고정. 실제 운영은 today
  theme: { primary: { light: "#1428A0", dark: "#7B8CFF" } }

datasets:                       # 키마다 dash.<키> 뷰가 된다. 차트와 위젯은 데이터셋만 참조한다
  milestones:
    sql: |
      SELECT m.*, (m.end_date - m.start_date + 1) AS days,
             (m.end_date - dash.as_of()) AS days_left
      FROM raw.milestones m
    metrics:
      weighted_progress: "ROUND(SUM(progress * days)::numeric / NULLIF(SUM(days), 0))"
      next_due_days: "MIN(days_left) FILTER (WHERE status <> 'done' AND days_left >= 0)"
  issues:
    sql: |
      SELECT i.id, i.title, i.severity, i.status, i.created, i.updated,
             m.title AS milestone, p.name AS assignee_name,
             i.status IN ('open', 'in-progress') AS is_open,
             CASE WHEN i.status IN ('open', 'in-progress') THEN '❌' ELSE '✅' END AS resolved_mark
      FROM raw.issues i
      LEFT JOIN raw.milestones m ON m.id = i.milestone_id
      LEFT JOIN raw.members p ON p.id = i.assignee
    metrics:
      open_issues: "COUNT(*) FILTER (WHERE is_open)"
      critical_open: "COUNT(*) FILTER (WHERE is_open AND severity = 'critical')"
  kpi_snapshots: { table: kpi_snapshots }    # SELECT * FROM raw.kpi_snapshots 축약
  tasks:
    sql: |
      SELECT t.id, t.title, t.stage, m.title AS milestone, p.name AS assignee_name
      FROM raw.tasks t
      LEFT JOIN raw.milestones m ON m.id = t.milestone_id
      LEFT JOIN raw.members p ON p.id = t.assignee

dashboards:                     # Superset 대시보드
  overview:
    title: 개요
    charts:
      - { key: kpi_progress, title: 전체 진척률(%), viz: big_number_total, dataset: milestones, metric: weighted_progress }
      - { key: kpi_dday, title: 다음 마일스톤까지(일), viz: big_number_total, dataset: milestones, metric: next_due_days }
      - key: kpi_open
        title: 열린 이슈
        viz: big_number            # 추이선과 직전 주 대비 증감
        dataset: kpi_snapshots
        metric: { sql: "MAX(open_issues)", label: 열린 이슈 }
        time_column: week_start
        time_grain: P1W
      - key: trend_open
        title: 열린 이슈 추이
        viz: line
        dataset: kpi_snapshots
        x: week_start
        metrics: [{ sql: "MAX(open_issues)", label: 열린 이슈 }]
      - key: ms_bar
        title: 마일스톤별 진척률
        viz: bar
        dataset: milestones
        x: title
        metrics: [{ sql: "MAX(progress)", label: 진척률 }]
        params: { y_axis_format: ".0f" }   # 축약 문법에 없는 옵션은 Superset 원본 파라미터로
    layout:                      # 행 단위. 12칸 격자를 균등 분할, 필요하면 { chart, width }
      - [kpi_progress, kpi_dday, kpi_open]
      - [trend_open, ms_bar]
  issues:
    title: 이슈
    charts:
      - key: issue_table
        title: 이슈 목록
        viz: table
        dataset: issues
        columns: [resolved_mark, id, title, severity, status, assignee_name, milestone, updated]
        order_by: [[updated, desc]]
    layout: [[issue_table]]
    filters:                     # 열 이름이 같은 차트에 적용된다. 없는 열이면 make check가 막는다
      - { key: f_sev, label: 심각도, type: select, multi: true, dataset: issues, column: severity }
      - { key: f_status, label: 상태, type: select, multi: true, dataset: issues, column: status }

tabs:                           # 웹앱 Rail. 순서대로 표시
  - key: overview
    label: 개요
    icon: grid
    sections: [{ type: superset, dashboard: overview }]
  - key: schedule
    label: 일정
    icon: calendar
    sections:
      - type: gantt
        title: 마일스톤 타임라인
        dataset: milestones
        fields: { label: title, start: start_date, end: end_date, progress: progress, status: status }
        statuses:
          done:        { label: 완료, icon: "✅", tone: good }
          in-progress: { label: 진행, icon: "🔄", tone: primary }
          planned:     { label: 예정, icon: "⏳", tone: primary }
          delayed:     { label: 지연, icon: "⚠️", tone: warn }
      - type: kanban
        title: 칸반보드
        dataset: tasks
        group_by: stage
        columns:
          - { value: todo, label: To Do }
          - { value: doing, label: In Progress }
          - { value: review, label: Review }
          - { value: done, label: Done, icon: "✅" }
        card: { title: title, chips: [milestone, assignee_name] }
        filter: { column: milestone, label: 마일스톤 }
  - key: issues
    label: 이슈
    icon: alert
    sections: [{ type: superset, dashboard: issues }]
```

- 지표 참조: 문자열이면 데이터셋에 정의한 지표 이름, `{ sql, label }`이면 그 차트에서만 쓰는 식이다.
- `icon`은 웹앱에 내장된 아이콘 이름이다(`grid, calendar, alert, doc, chart, table, users, box` 등). 목록은 `contracts/`에 둔다.
- 설정의 SQL은 저장소를 고치는 사람이 쓰는 신뢰된 입력이다. 브라우저에서 들어온 값을 SQL에 넣는 경로는 없다.

## 6. 데이터가 화면에 도달하는 경로

### 6-1. 무엇을 바꾸면 어디를 고치나

| 바꾸고 싶은 것 | 고칠 곳 |
|---|---|
| 값(행 추가, 수치 변경) | `projects/<이름>/data/*.csv` |
| 테이블·열·검증 규칙 | `schema.yaml` |
| 계산 규칙, 조인, 파생 열(이모지 표시 포함) | `dashboard.yaml`의 `datasets` |
| 차트, 배치, 필터 | `dashboard.yaml`의 `dashboards` |
| 탭, 간트·칸반의 열 매핑과 라벨 | `dashboard.yaml`의 `tabs` |
| 제목, 로고, 기준일, 메인 컬러 | `dashboard.yaml`의 `app` |
| 다른 프로젝트로 전환 | `.env`의 `PROJECT` |
| 포트, 도메인, 비밀값 | `.env` |

반영 명령은 언제나 `make apply`다. 적용 전에 문법만 확인하려면 `make check`를 쓴다.

### 6-2. `make apply`가 하는 일

1. **검사(`dashkit check`):** 두 YAML을 JSON Schema로 검증하고 키 참조를 확인한다(차트 → 데이터셋, 배치 → 차트, 섹션 → 대시보드·데이터셋).
2. **적재(`dashkit load`):**
   - CSV를 `schema.yaml`로 검증한다. 오류는 `파일:행:열: 이유` 형식으로 **모두** 모아 출력하고, 하나라도 있으면 DB를 건드리지 않는다.
   - 트랜잭션 하나에서 `raw`, `dash` 스키마를 다시 만든다: 테이블(PK·FK 포함) → 데이터 → `dash.as_of()` → 데이터셋 뷰 → `checks` 실행 → 필터·위젯이 가리키는 열이 뷰에 있는지 확인 → `dash_reader` 권한 부여.
   - 어느 단계든 실패하면 롤백하고 이전 데이터가 그대로 남는다.
3. **반영(`dashkit sync`):**
   - 뷰의 열 정보와 `dashboard.yaml`로 asset 번들(DB 연결, 데이터셋, 차트, 대시보드, 필터)을 `.build/`에 만든다.
   - Superset에 덮어쓰기로 가져오고, 대시보드마다 임베드를 켜고, 이번 설정에 없는 이전 객체를 지운다.
   - 임베드 UUID와 관리 객체 목록을 `.state/`에 쓴다.

적재는 성공하고 반영이 실패하면 DB는 새 상태, Superset은 이전 상태다. 원인을 고치고 `make apply`를 다시 실행하면 된다(멱등).

### 6-3. 예시 프로젝트 `devtracker`의 화면별 경로

| 탭 | 화면 요소 | 그리는 곳 | 데이터 경로 |
|---|---|---|---|
| 개요 | 전체 진척률, 다음 마일스톤 D-day | Superset Big Number | `milestones.csv` → `dash.milestones`의 지표 |
| 개요 | 열린 이슈·Critical(지난주 대비) | Superset Big Number(추이선) | `kpi_snapshots.csv` |
| 개요 | 열린 이슈 추이, 마일스톤별 진척률 | Superset Line, Bar | `kpi_snapshots.csv`, `milestones.csv` |
| 개요 | 남은 마일스톤, 최근 갱신 이슈 5건 | Superset Table | `dash.milestones`, `dash.issues` |
| 일정 | 간트 타임라인 | 웹앱 `gantt` 위젯 | `GET /api/data/milestones` |
| 일정 | 칸반 | 웹앱 `kanban` 위젯 | `GET /api/data/tasks` |
| 이슈 | 필터 테이블 | Superset Table과 네이티브 필터 | `dash.issues` |
| 문서 | 문서 목록(🆕) | Superset Table과 유형 필터 | `dash.docs`. 🆕는 뷰 SQL에서 `dash.as_of()` 기준 7일 |

### 6-4. 실행 중 동작

- 웹앱은 시작할 때 `GET /api/app`으로 제목, 테마, 탭, 섹션, 임베드 ID를 받는다. 웹앱 코드에는 탭이 없다.
- Superset 섹션은 SDK가 `POST /api/guest-token`으로 토큰을 받아 iframe에 넘긴다. 토큰 수명은 짧게(5분) 두고 SDK가 만료 전에 다시 받는다.
- 다크모드를 바꾸면 웹앱 토큰과 함께 iframe의 Superset 테마도 바꾼다(SDK의 `themeMode` / `setThemeMode`, 버전 확인 필요).
- 대시보드는 보기 전용이다. 데이터 변경 경로는 CSV와 `make apply`뿐이다.

## 7. 기존 Artifact 버전과의 관계

- `reference/index.html`은 디자인 기준이자 `devtracker` 예시 데이터의 출처다. `reference/artifact-plan.md`는 그 버전의 계획서다.
- **그대로 옮기는 것:** 디자인 토큰(`#1428A0`, 다크모드 3단 구조), Rail과 하단 탭 바, 이모지 사용 범위, 모션 원칙, 간트·칸반의 모양.
- **바뀌는 것:**
  - KPI, 추이, 표, 필터는 Superset 차트가 된다. iframe 안에는 웹앱의 카운트업 같은 모션을 적용할 수 없다. 이모지는 뷰 SQL의 파생 열로 넣는다.
  - 간트·칸반은 `DATA` 객체가 아니라 API에서 데이터를 받고, 열 이름과 상태 라벨을 설정에서 받는다.
  - 이슈 제목 검색은 Superset 표의 검색 상자로 대체한다.
- `reference/core.test.js`의 기대값(열린 이슈 16, Critical 미해결 3, 가중 진척률 55, 다음 마일스톤 D-29, 최근 문서 D2·D3·D5·D6·D7)은 새 테스트에서도 같은 값으로 확인한다.

## 8. 범위 밖과 확장 지점

| 항목 | 1차 범위 | 확장 방법 |
|---|---|---|
| 로그인, 사용자별 데이터 범위 | 없음. 로컬 전용 | 앞단에 인증 프록시를 두고, 백엔드가 인증된 사용자로 guest token의 `rls_rules`를 채운다. `/api/data`에도 같은 조건을 적용해야 한다. |
| 데이터 원본 | CSV | 기존 DB를 쓰려면 적재를 건너뛰고 `datasets`의 SQL이 그 테이블을 읽게 한다. |
| 캐시 | 꺼짐 | Redis를 붙이고 캐시 시간을 설정한다. 적재 후 무효화가 필요해진다. |
| 위젯 유형 | `gantt`, `kanban` | `web/widgets/<유형>.js` 파일과 옵션 JSON Schema를 추가한다. |
| 웹앱 위젯과 Superset 필터 연동 | 없음. 각자 필터 | SDK의 필터 상태 API로 연결한다. |
| 페이지 안에서 데이터 수정 | 없음 | 범위 밖. |

## 9. 오케스트레이션 프롬프트 (Claude Code에 붙여 넣기)

이 저장소 폴더(`reference/`가 들어 있는 곳)에서 Claude Code를 실행하고 아래 블록 전체를 붙여 넣는다.

````text
# 역할
너는 이 작업의 오케스트레이터다. "Superset 임베드형 범용 대시보드 템플릿"을 만든다.
작업은 Explore → Plan → Implement → Verify 단계로 진행하고, 단계가 끝날 때마다 보고한 뒤 내 확인을 받고 다음으로 넘어간다.
Implement 단계에서는 서로 독립적인 작업을 서브에이전트에 병렬로 맡기고, 너는 계약 관리와 통합을 맡는다.

# 참고 자료
- reference/plan.md: 이 프로젝트의 설계다. 2절의 결정, 4절 저장소 구조, 5절 설정 형식, 6절 동작을 따른다. 프롬프트와 plan.md가 어긋나면 멈추고 나에게 묻는다.
- reference/index.html: 기존 단일 파일 대시보드. 디자인 토큰, Rail 레이아웃, 이모지 규칙, 모션 원칙, 간트·칸반 모양, 더미데이터(DATA 객체)의 출처다.
- reference/core.test.js: 기존 계산 검증. 기대값의 출처다.
- reference/artifact-plan.md: 기존 버전의 계획서. 디자인 시스템(6절)과 모션 원칙(7절)만 참고한다.

# 목표
- 범용 템플릿: projects/<이름>/ 폴더(data/*.csv, schema.yaml, dashboard.yaml)만 바꾸면 도메인이 다른 대시보드가 된다.
- 템플릿 코드(dashkit/, web/, superset/)에는 특정 도메인의 테이블명, 열 이름, 상태값, 탭 이름을 쓰지 않는다.
- 차트는 Superset이 그리고 Embedded SDK로 임베드한다. 간트와 칸반은 웹앱 위젯이 /api/data에서 데이터를 받아 그리며, 열 매핑과 라벨은 설정에서 받는다.
- `make env && make up && make apply` 후 http://localhost:8080 을 열면 완성된 대시보드가 보여야 한다.
- 예시 프로젝트 2개를 넣는다.
  - projects/devtracker: reference/index.html의 DATA를 CSV 6개로 옮긴 것. plan.md 6-3절 표의 화면을 모두 정의한다.
  - projects/sales-sample: 도메인이 다른 예시(예: 고객, 거래, 캠페인). 테이블명과 열 이름이 devtracker와 겹치지 않아야 하고, superset 섹션, gantt, kanban을 각각 하나 이상 쓴다.

# Explore 단계에서 확인하고 보고할 것
공식 문서와 해당 버전 소스로 확인한다. 기억에 의존하지 않는다. 결과는 contracts/superset-notes.md에 출처와 함께 남긴다.
1. Superset 최신 안정 버전과 그에 맞는 @superset-ui/embedded-sdk 버전. 공식 이미지에 PostgreSQL 드라이버가 들어 있는지.
2. asset 번들 가져오기: 엔드포인트 또는 CLI, 덮어쓰기 동작, UUID 기준 멱등성, DB 비밀번호 전달 방법, 데이터셋 열·지표와 네이티브 필터를 번들에 쓰는 형식.
3. 임베드 설정: 대시보드에 임베드를 켜는 API, 허용 도메인, 다시 호출했을 때 임베드 UUID가 유지되는지.
4. guest token 직접 서명: 필요한 claim(user, resources, rls_rules, iat, exp, aud, type), 서명 알고리즘, GUEST_TOKEN_JWT_AUDIENCE 설정.
5. guest 역할에 줄 최소 권한을 만드는 방법.
6. iframe 허용 설정(Talisman, frame-ancestors).
7. 축약 문법 6개 유형(big_number_total, big_number, line, bar, pie, table)의 차트 파라미터. 실제 인스턴스에서 차트를 만들어 내보낸 결과로 확인한다.
8. SDK의 themeMode / setThemeMode로 iframe 다크모드를 바꿀 수 있는지.
2번이 그 버전에서 불가능하면 구현하지 말고 대안(REST API로 객체별 생성·갱신)과 함께 보고한다.

# 환경과 제약
- Linux, Docker와 docker compose. 호스트에는 Docker와 make만 있다고 가정한다. 파이썬 작업(check, load, sync, 테스트)은 tools 컨테이너에서 실행한다.
- 버전: 모든 이미지 태그, 파이썬 패키지, SDK를 정확한 버전으로 고정한다(latest 금지).
- 컨테이너: data-db, superset-db, superset-init, superset, backend, web, tools(일회성). Redis는 쓰지 않는다.
- 포트는 모두 127.0.0.1에만 연다. 웹앱 8080, Superset 8088.
- 웹앱: 바닐라 JS와 @superset-ui/embedded-sdk만 쓴다. SDK는 고정 버전 파일을 web/vendor/에 넣는다. 빌드 단계와 다른 UI 프레임워크는 두지 않는다. nginx가 정적 파일을 제공하고 /api를 backend로 프록시한다.
- 파이썬: dashkit 패키지 하나, 이미지 하나. 설정 로딩과 검증은 dashkit/config.py 한 곳에만 둔다.
- 비밀값: .env.example만 커밋한다. `make env`가 임의 값을 채운 .env를 만든다. 대상은 Superset 관리자 비밀번호, SECRET_KEY, GUEST_TOKEN_JWT_SECRET, DB 비밀번호다. 브라우저에는 guest token만 나간다.
- DB 권한: 로더만 소유자 계정을 쓴다. Superset과 backend는 dash 스키마 SELECT만 있는 dash_reader 계정으로 접속한다.
- backend는 Superset 계정을 갖지 않는다. guest token은 직접 서명하고, 대시보드 리소스 하나만 담고, 수명은 5분으로 한다.
- 대시보드는 보기 전용이다. 데이터 변경 경로는 CSV와 `make apply`뿐이다.

# Superset 설정 (superset/superset_config.py)
- EMBEDDED_SUPERSET 기능 플래그
- GUEST_ROLE_NAME, GUEST_TOKEN_JWT_SECRET, GUEST_TOKEN_JWT_AUDIENCE, SECRET_KEY, 메타데이터 DB 주소는 환경변수에서 읽는다.
- 웹앱 출처만 iframe으로 띄울 수 있게 허용한다. CORS는 켜지 않는다.
- 차트 데이터 캐시는 끈다.

# 계약 (Plan 단계에서 네가 먼저 확정해 contracts/에 쓴다)
1. schema.yaml 형식(JSON Schema): tables, primary_key, columns(type, required, values, ref, min, max, pattern), checks(name, sql)
2. dashboard.yaml 형식(JSON Schema): app, datasets(sql 또는 table, metrics), dashboards(charts, layout, filters), tabs(sections: superset, gantt, kanban)
3. 차트 축약 문법 대응표: 6개 유형의 축약 필드가 Superset 파라미터 무엇이 되는지. 차트의 params는 마지막에 덮어쓴다.
4. 백엔드 API
   - GET /api/app → 제목, 로고, 기준일, 테마, Superset 주소, 탭 목록(섹션 옵션과 superset 섹션의 임베드 ID 포함)
   - POST /api/guest-token { dashboard } → { token, expires_at }. 설정에 없는 dashboard 키는 404
   - GET /api/data/{dataset} → { columns, rows }. gantt·kanban 섹션이 참조한 데이터셋만 허용하고 나머지는 404
   - GET /api/health
5. 위젯 계약: web/widgets/<유형>.js가 내보내는 함수의 인자(마운트 요소, 행, 옵션, 앱 정보)
6. 상태 파일: .state/embed.json(대시보드 키 ↔ 임베드 UUID), .state/managed.json(지난번에 만든 Superset 객체 UUID). backend는 embed.json을 읽기 전용으로 마운트하고 요청 때마다 변경을 반영한다.
7. Superset 객체 UUID 규칙: uuid5(고정 네임스페이스, "<프로젝트>/<종류>/<키>")
계약을 쓴 뒤 dashkit/config.py(로딩, JSON Schema 검증, 키 참조 검사)를 네가 직접 만들고, 그다음 A~E를 병렬로 맡긴다.

# 컴포넌트와 담당
- A. 인프라: docker-compose.yml, .env.example, Makefile(env, up, check, apply, test, reset, down, logs), superset/Dockerfile, superset_config.py, superset-init. `make up`은 Superset이 healthy가 될 때까지 기다린다.
- B. 로더와 예시 데이터: dashkit/loader, projects/devtracker, projects/sales-sample.
  - plan.md 6-2절 2번의 순서를 따른다. CSV 오류는 `파일:행:열: 이유`로 모두 모아 출력한다. checks 위반은 규칙 이름과 위반 행의 키를 출력한다.
  - devtracker 변환 규칙: 열 이름은 snake_case, 예약어 금지(start → start_date, end → end_date, column → stage). kpiHistory의 주차는 그 ISO 주의 월요일 날짜(week_start)로 바꾼다(W47 → 2026-11-16). app.as_of는 2026-11-19.
  - reference/index.html의 validateData 규칙을 schema.yaml의 열 규칙과 checks로 옮긴다. "스냅샷 마지막 행 = 현재 집계" 규칙도 checks로 쓴다.
- C. Superset 반영: dashkit/sync. plan.md 6-2절 3번을 따른다. 두 번 실행해도 결과가 같아야 하고, 설정에서 지운 객체는 Superset에서도 지워져야 한다.
- D. 백엔드: dashkit/api. 계약 4번. DB 조회는 식별자를 설정에서 온 허용 목록과 대조한 뒤 실행하고, 행 수 상한을 둔다.
- E. 웹앱: web/.
  - reference/index.html의 디자인 시스템(토큰, Rail, 600px 이하 하단 탭 바, 다크모드 3단 토큰, 이모지 규칙, reduced-motion)을 옮긴다. 메인 컬러는 /api/app의 테마 값으로 덮어쓴다.
  - 탭과 섹션은 /api/app에서 받아 그린다. 탭마다 해시 주소(#<키>)를 붙인다.
  - gantt, kanban 위젯은 옵션의 열 매핑, 상태 라벨·아이콘·색조, 칸반 필터를 그대로 따른다. 카드 드래그는 없다. 간트의 오늘 선은 기준일을 쓴다.
  - superset 섹션에는 로딩 상태와 오류 상태(Superset 연결 실패, 토큰 실패, 아직 apply하지 않음)를 디자인한다.
  - 다크모드를 바꾸면 iframe 테마도 함께 바꾼다. 그 버전에서 불가능하면 done.md에 한계를 쓴다.
  - 셸의 고정 문구("보기 전용", "테마" 등)는 web/strings.js 한 파일에 모은다.

# 테스트 (Verify 단계, tests/에 남긴다. `make test`로 실행)
- 설정 검사: 두 예시 프로젝트가 check를 통과한다. 없는 데이터셋 참조, 배치에 없는 차트 키, 뷰에 없는 필터 열을 각각 잡아낸다.
- 로더:
  - 정상 적재 후 행 수가 CSV와 같다.
  - 깨진 CSV(없는 참조, 잘못된 enum, 범위 밖 숫자, 패턴 불일치, 중복 키)는 오류를 모두 보고하고 DB가 이전 상태 그대로다.
  - checks 위반(예: created > updated)도 롤백된다.
  - dash_reader로 raw 스키마 조회와 쓰기가 거부된다.
- sync:
  - 두 번 실행해도 Superset 객체 수와 UUID가 같다.
  - dashboard.yaml의 차트 키가 모두 생성된다.
  - 차트 하나를 설정에서 지우고 다시 실행하면 Superset에서도 사라진다.
- 지표 정합성(devtracker, Superset 차트 데이터 API로 조회): 열린 이슈 16, Critical 미해결 3, 가중 진척률 55, 다음 마일스톤까지 29일, 🆕 문서는 D2·D3·D5·D6·D7.
- 백엔드:
  - guest token으로 임베드 대시보드와 차트 데이터 조회가 된다.
  - 같은 토큰으로 쓰기 요청과 다른 대시보드 조회는 거부된다.
  - /api/data는 위젯이 참조하지 않은 데이터셋에 404를 준다.
- 범용성:
  - PROJECT=sales-sample로 바꿔 apply하면 코드 변경 없이 탭, 차트, 위젯 데이터가 바뀌고 devtracker의 Superset 객체는 남지 않는다.
  - dashkit/, web/, superset/에 예시 도메인 단어(milestone, issue, severity, 마일스톤 등 두 예시의 테이블명·열 이름·상태값)가 없는지 grep으로 검사한다.
- UI 화면은 내가 직접 확인하므로 자동 테스트에서 뺀다.

# 산출물과 완료 조건
- README.md: 실행 순서, 그리고 "데이터 바꾸기 / 차트 바꾸기 / 탭과 위젯 바꾸기 / 새 프로젝트 만들기 / Superset 화면에서 만든 차트를 params로 옮기기" 5개 절. 각 절에 실제 명령과 고칠 파일을 쓴다. 인증이 없으므로 외부 공개 전에 인증 프록시가 필요하다는 점을 쓴다.
- done.md: 아래 체크리스트를 근거(테스트 출력, 파일 경로)와 함께 쓴다.
  - [ ] 모든 기능을 구현했고 테스트를 통과했는가? 테스트 코드가 남아 있는가?
  - [ ] plan에 없는 의존성을 추가하지 않았는가? 모든 버전을 고정했는가?
  - [ ] 템플릿 코드에 도메인 하드코딩이 0건인가(grep 테스트 결과)?
  - [ ] 두 예시 프로젝트가 PROJECT 전환만으로 동작하는가?
  - [ ] 비밀값이 커밋 대상에 없는가? backend에 Superset 계정이 없는가?
  - [ ] Explore에서 확인한 내용과 다르게 구현한 부분, 남은 한계, 모호한 부분을 나에게 알렸는가?

# 진행 규칙
- 모호하거나 상충되는 부분은 최대 3개로 추려 단계 시작 전에 묻는다.
- 각 단계가 끝나면 무엇을 했고 무엇이 남았는지 짧게 보고하고 멈춘다.
- 서브에이전트 결과는 네가 계약과 대조해 검토한 뒤 통합한다. 계약을 바꿔야 하면 먼저 나에게 알린다.
- 실패한 테스트를 숨기거나 건너뛰지 않는다.
- 외부 서비스(GitHub 등)에 푸시하거나 배포하지 않는다.
````

## 10. 다음 단계

1. 이 문서의 2절 결정에 이견이 있으면 알려준다.
2. 이 폴더에서 Claude Code를 실행하고 9절 프롬프트를 붙여 넣어 Explore 단계부터 진행한다.
