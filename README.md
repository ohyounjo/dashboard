# Superset 임베드형 범용 대시보드 템플릿

`projects/<이름>/` 폴더 하나(CSV, `schema.yaml`, `dashboard.yaml`)만 바꾸면 도메인이 다른 대시보드가 됩니다.

- 차트(KPI, 추이, 표, 필터)는 Apache Superset이 그리고, 웹앱이 Embedded SDK로 iframe에 띄웁니다.
- 화면 틀(Rail, 다크모드)과 간트·칸반 위젯은 웹앱이 직접 그립니다.
- Superset 객체(데이터셋, 차트, 대시보드, 필터)는 설정 파일에서 생성합니다. 화면에서 클릭해 만들지 않습니다.

> ⚠️ **인증이 없습니다.** 모든 포트는 `127.0.0.1`에만 열려 있고, 이 컴퓨터에서만 볼 수 있다는 전제로 만들었습니다.
> 외부에 공개하려면 웹앱(8080) 앞에 **인증 프록시**(예: oauth2-proxy, 사내 SSO 게이트웨이)를 두세요. Superset(8088)은 직접 공개하지 마세요.
> 브라우저로 나가는 것은 수명 5분짜리 guest token뿐입니다. 하지만 그 토큰으로 임베드한 대시보드가 쓰는 데이터셋의 다른 열과 집계도 조회할 수 있습니다(`done.md`의 "남은 한계"). 그러니 `dash` 뷰에는 보여도 되는 열만 두세요.

## 실행 순서

호스트에는 Docker(Compose v2 포함)와 make만 있으면 됩니다. 파이썬 작업은 모두 `tools` 컨테이너에서 실행합니다.

```bash
make env
```
`.env.example`을 복사해 `.env`를 만들고 비밀값(관리자 비밀번호, SECRET_KEY, guest token 서명 키, DB 비밀번호)을 임의 값으로 채웁니다. `.env`가 이미 있으면 건드리지 않습니다.

```bash
make up
```
컨테이너를 빌드·기동하고 superset, backend, web이 healthy가 될 때까지 기다립니다(처음에는 이미지 내려받기 때문에 몇 분 걸립니다).

```bash
make apply
```
check(설정 검사) → load(CSV 검증·적재) → sync(Superset 반영) 순서로 실행합니다. backend는 `projects/`의 설정 파일이 바뀌면 스스로 다시 읽으므로 재시작하지 않습니다.

```bash
make admin EMAIL=<내 사내 메일 주소>
```
웹앱은 로그인해야 볼 수 있습니다. 첫 admin 계정을 만들고 비밀번호는 프롬프트로 정합니다(코드·`.env`에 넣지 않습니다). 주소의 도메인은 `.env`의 `AUTH_EMAIL_DOMAINS`(기본 `example.com`, 회사 도메인으로 바꿉니다)에 있어야 합니다. 로그인 이전에 만든 스택이라면 먼저 `make auth-init`으로 인증 DB(`dashkit_auth` 스키마와 `dash_auth` 계정)를 더합니다(볼륨은 그대로 둡니다). 규칙은 [contracts/auth.md](contracts/auth.md)에 있습니다.

브라우저에서 http://localhost:8080 을 열고 로그인합니다. Superset 관리 화면은 http://localhost:8088 이고, 계정은 `.env`의 `SUPERSET_ADMIN_USER`/`SUPERSET_ADMIN_PASSWORD`입니다.

| 명령 | 하는 일 |
|---|---|
| `make env` | `.env` 생성 |
| `make up` | 기동 후 healthy 대기 |
| `make auth-init` | 인증 DB 만들기·최신으로 올리기(여러 번 실행해도 됨) |
| `make admin EMAIL=<주소>` | 첫 admin 계정 만들기(비밀번호는 프롬프트) |
| `make data-init` | 운용자 조회 계정(`dash_inspect`) 만들기(이미 있는 볼륨. 여러 번 실행해도 됨) |
| `make manage` / `make manage-down` | 관리 서비스(운용 화면의 데이터 메뉴, `/api/manage`) 띄우기·내리기(선택) |
| `make check` | 설정만 검사(DB 불필요) |
| `make plan` | 적용하면 무엇이 바뀌는지 미리 보기(아무것도 바꾸지 않음. 선택) |
| `make apply` | 검사 → 적재 → Superset 반영. **`projects/`에서 무엇을 바꿨든 이것 하나** |
| `make migrate` | 설정 파일을 최신 형식 버전으로 고침(아래 "설정 버전") |
| `make test-unit` | 단위 테스트(스택 불필요) |
| `make test` | 통합 테스트(별도 테스트 스택, 아래 "테스트") |
| `make golden` | 번들 스냅샷 갱신 |
| `make logs` | 로그 보기 |
| `make down` | 중지(데이터 유지) |
| `make reset CONFIRM=1` | 컨테이너·볼륨·`.state/`·`.build/` 삭제(처음부터 다시) |

## 저장소 구조

```
projects/<이름>/        ★ 사용자가 바꾸는 곳: data/*.csv, schema.yaml, dashboard.yaml
contracts/              설정 형식(JSON Schema), API·위젯·상태 파일 계약, Superset 6.1.0 확인 기록
dashkit/                파이썬 패키지: config.py(설정 검증), loader/, sync/, api/, studio/(구성 화면 API)
superset/               Superset 이미지·설정·초기화(guest 역할)
web/                    웹앱(바닐라 JS): 셸, strings.js(고정 문구), widgets/<유형>/(widget.js + widget.json)
data-db/init/           데이터 DB 계정 초기화
tests/                  pytest
.build/  .state/        생성된 asset 번들, 임베드 UUID·관리 객체 목록(커밋 제외)
```

예시 프로젝트는 두 개입니다. `.env`의 `PROJECT`로 고릅니다.
- `devtracker`: 스마트홈 앱 개발 현황(기본)
- `sales-sample`: B2B 영업 현황

---

## 1. 데이터 바꾸기

값(행 추가, 수치 변경)만 바꿀 때는 CSV를 고칩니다.

- 파일: `projects/<이름>/data/<테이블>.csv`
- CSV 형식:
  - UTF-8, 첫 줄은 머리글이고 `schema.yaml`의 열과 같은 집합이어야 합니다.
  - 빈 칸은 NULL입니다.
  - 날짜는 `YYYY-MM-DD`, boolean은 `true`/`false`입니다.

```bash
make apply
```

CSV에 오류가 있으면 `파일:행:열: 이유` 형식으로 **모두** 출력하고, DB는 이전 상태 그대로 둡니다.

```
data/issues.csv:3:severity: 허용되지 않은 값 'blocker' (허용: critical, major, minor, trivial)
data/tasks.csv:18:id: 기본 키 중복 (T1, 처음 나온 줄 2)
```

열을 더하거나 검증 규칙을 바꾸려면 `projects/<이름>/schema.yaml`을 고칩니다.
- 열 규칙: `type`(text, integer, number, boolean, date, timestamp, enum), `required`, `values`, `ref`, `min`, `max`, `pattern`
- 테이블 규칙: `checks`. 위반 행을 돌려주는 SQL을 씁니다. 한 행이라도 나오면 적재 전체가 롤백됩니다.
- 열 이름은 snake_case로 쓰고 SQL 예약어(`end`, `start`, `order` 등)는 쓸 수 없습니다.
- 형식의 기준은 [contracts/schema.schema.json](contracts/schema.schema.json)입니다.

### 계속 쌓이는 데이터: 누적(append) 테이블

기록이 계속 쌓이는 테이블은 `mode: append`로 두면 파일을 통째로 바꾸지 않고 **배치를 더합니다**.

```yaml
tables:
  records:
    mode: append          # replace(기본) | append
    on_conflict: update   # 같은 키가 다시 오면: error(기본) | skip | update
    primary_key: id
    columns: { … }        # 열마다 default(새 행에 빠진 열의 값), aliases(올린 파일의 다른 머리글 이름)를 쓸 수 있다
```

- 원본은 `data/<테이블>.csv`(있으면 첫 배치)와 `data/<테이블>/`의 배치 파일(`<번호>-….csv` 또는 `.csv.gz`)이고, 로더는 번호순으로 차례로 넣습니다. 배치는 머리글에 일부 열만 있어도 됩니다(기본 키 열은 반드시).
- DB는 여전히 이 파일들의 결과물입니다. 새 머신에서 `make apply`하면 같은 행이 나옵니다. 운영 프로젝트의 배치 폴더는 저장소에 넣지 않고 폴더째 백업합니다.
- 형식과 규칙은 [contracts/ingest.md](contracts/ingest.md)에 있습니다.

배치를 더하는 방법은 셋입니다. 어느 길이든 같은 검증·미리보기·중복 확인·되돌리기가 붙습니다.

- **운용 화면**: `make manage`로 관리 서비스를 띄우면 운용 화면(`/admin/`)에 **데이터** 메뉴가 생깁니다. 테이블의 "추가"에서 한 행씩 입력하거나(키보드만으로 "저장하고 계속"), 엑셀에서 표를 붙여넣거나, CSV 파일을 놓습니다(UTF-8·CP949, 쉼표·탭·세미콜론, 표시 이름 머리글을 저절로 맞춥니다). 미리보기를 본 뒤 넣고, 잘못 넣었으면 "방금 추가 취소"로 뺍니다. admin과 operator가 쓸 수 있고 operator는 자기가 더한 배치만 되돌립니다. admin은 운용 화면에서 구조도 바꿉니다: CSV 하나로 새 테이블(과 데이터셋·초안 그래프) 만들기, 열 추가·규칙 고치기, 데이터셋 만들기, 쓰이는 곳 보기, 배치 합치기. 바꾼 설정은 "설정 이력"에서 되돌리고, 설정 파일을 내려받아 저장소에 커밋합니다.
- **명령**: `make ingest TABLE=<테이블> FILE=<CSV> [DRY_RUN=1]`.
- **폴더**: 정규형 파일을 `data/<테이블>/`에 번호를 붙여 넣고 `make apply`.

## 2. 차트 바꾸기

`projects/<이름>/dashboard.yaml`에서 두 곳을 고칩니다.
- **계산 규칙과 파생 열:** `datasets`. 데이터셋마다 `dash.<키>` 뷰가 되고, Superset과 웹앱 위젯이 같은 뷰를 읽습니다.
- **차트, 배치, 필터:** `dashboards`

```yaml
datasets:
  orders:
    sql: |
      SELECT o.*, (dash.as_of() - o.ordered_on) AS age_days   # dash.as_of() = app.as_of 기준일
      FROM raw.orders o
    metrics:
      revenue: "SUM(amount)"
    labels: { ordered_on: 주문일, age_days: 경과 일수, revenue: 매출 }   # 표 머리글·범례 이름

dashboards:
  main:
    title: 메인
    charts:
      - { key: kpi_rev, title: 매출, viz: big_number_total, dataset: orders, metric: revenue, format: ",d" }
      - { key: trend, title: 주별 매출, viz: line, dataset: orders, x: ordered_on, time_grain: P1W, metrics: [revenue] }
      - key: recent
        title: 최근 주문
        viz: table
        dataset: orders
        columns: [id, ordered_on, amount]
        order_by: [[ordered_on, desc]]
        row_limit: 10
    layout:                      # 행 목록. 12칸 격자를 균등 분할, { chart, width } 로 너비 지정
      - [kpi_rev, { chart: trend, width: 8 }]
      - [recent]
    filters:
      - { key: f_region, label: 지역, type: select, dataset: orders, column: region }
```

```bash
make apply
```

- 축약 문법은 22개 유형을 지원합니다: 기본 6종(`big_number_total`, `big_number`, `line`, `bar`, `pie`, `table`)과 추이(`area`, `scatter`, `mixed`, `waterfall`), 비율·단계(`funnel`, `gauge`, `treemap`, `sunburst`), 교차·분포(`heatmap`, `pivot_table`, `histogram`, `box_plot`, `bubble`), 관계·기타(`radar`, `sankey`, `word_cloud`). 유형별 예시는 [reference/chart-catalog.md](reference/chart-catalog.md) 3절과 `projects/chart-gallery/`에, 필드 대응은 [contracts/chart-shorthand.md](contracts/chart-shorthand.md)에 있습니다.
- 축약 문법에 없는 옵션은 `params:`에 Superset 원본 파라미터로 씁니다. 생성된 값 위에 마지막으로 덮어씁니다.
- 지표 참조: 문자열이면 데이터셋 `metrics`의 이름, `{ sql, label }`이면 그 차트에서만 쓰는 식입니다.
- 설정에서 지운 차트는 Superset에서도 지워집니다(prune). sync가 만든 객체에는 "외부에서 관리됨" 표식이 붙어서, `.state/`를 지운 뒤에도 이 프로젝트의 이전 객체를 찾아 지웁니다. 화면에서 직접 만든 객체는 지우지 않습니다.
- sync가 만든 대시보드·차트·데이터셋은 Superset 화면에서 고칠 수 없습니다(편집 버튼이 없거나 저장이 막힙니다). 화면에서 고친 내용이 다음 `make apply`에서 말없이 사라지는 일을 막기 위해서입니다. 막지 않으려면 `SYNC_LOCK_UI=0`으로 sync합니다.
- 배치에는 차트 말고도 제목 `[{ header: 글 }]`, 글 `{ markdown: "글", width: 4 }`, 대시보드 안 탭 `- tabs: [{ label, rows: [[…]] }]`을 놓을 수 있고, `cross_filters: true`면 차트를 눌러 다른 차트를 거를 수 있습니다.
- 필터는 `type`으로 네 가지를 씁니다: `select`(기본), `time_range`(기간), `number_range`(숫자 범위), `time_grain`(시간 단위). `default`(기본값), `parent`(상위 필터에 따라 선택지가 줄어드는 연쇄), `charts`(적용할 차트)를 줄 수 있습니다. 문법은 [contracts/chart-shorthand.md](contracts/chart-shorthand.md)의 "대시보드와 필터", 견본은 `projects/chart-gallery/`의 `shelf_blocks` 대시보드입니다. 임베드 화면에서는 기간 필터의 값을 바꿀 수 없고 `default`만 적용됩니다(guest 권한 제한).
- 생성된 asset 번들은 `.build/<이름>/bundle/`에 남아 diff로 볼 수 있습니다.

## 3. 탭과 위젯 바꾸기

`dashboard.yaml`의 `tabs`가 웹앱의 탭(Rail)과 섹션을 정의합니다. 웹앱 코드에는 탭이 없습니다. 탭마다 주소 `#<key>`가 붙습니다.

```yaml
tabs:
  - key: home
    label: 홈
    icon: grid                   # grid calendar alert doc chart table users box list flag target cart megaphone home
    sections: [{ type: superset, dashboard: main }]
  - key: plan
    label: 일정
    icon: calendar
    sections:
      - type: gantt
        title: 프로젝트 일정
        dataset: phases
        fields: { label: phase_name, start: begins_on, end: ends_on, progress: pct, status: state }
        statuses:                # 열 값 → 라벨·이모지·색조(primary good warn bad neutral)
          done:   { label: 완료, icon: "✅", tone: good }
          late:   { label: 지연, icon: "⚠️", tone: warn }
        range: { start: "2026-01-01", end: "2026-12-31" }   # 생략하면 데이터 범위
      - type: kanban
        title: 보드
        dataset: cards
        group_by: lane
        columns:
          - { value: todo, label: 할 일 }
          - { value: done, label: 완료, icon: "✅" }
        card: { title: card_title, chips: [owner_name] }
        filter: { column: owner_name, label: 담당 }
```

```bash
make apply
```

- 간트의 오늘 선은 `app.as_of`(기준일)에 그립니다. 칸반은 보기 전용이라 카드를 끌어 옮길 수 없습니다.
- 위젯 데이터는 `GET /api/data/<데이터셋>`으로 받습니다. 간트·칸반 섹션이 참조한 데이터셋만 열리고, 나머지는 404입니다.
- 제목, 로고 글자, 기준일, 메인 컬러는 `app`에서 바꿉니다(`title`, `logo_text`, `as_of: today | YYYY-MM-DD`, `theme.primary.light/dark`).
- 셸의 고정 문구("보기 전용", "테마" 등)는 `web/strings.js` 한 파일에 있습니다.
- 새 위젯 유형은 `web/widgets/<유형>/` 폴더 하나(`widget.js`, `widget.json`)로 추가합니다. [contracts/widgets.md](contracts/widgets.md)의 "새 위젯 유형 추가"를 보세요.

## 4. 새 프로젝트 만들기

CSV 폴더가 있으면 초안을 자동으로 만듭니다(스택을 띄우지 않아도 됩니다).

```bash
make new NAME=myproject FROM=~/exports/
```

`FROM` 폴더 바로 아래의 `*.csv`를 읽어 `projects/myproject/`에 `schema.yaml`, `dashboard.yaml`, `data/*.csv`를 만듭니다. 원본 폴더는 읽기만 합니다. 이미 있는 프로젝트는 덮어쓰지 않습니다. 이름은 소문자·숫자·`_`·`-`만 씁니다.

추론 규칙(자세한 기준은 [contracts/scaffold.md](contracts/scaffold.md)):

| 대상 | 규칙 |
|---|---|
| 유형 | 모든 값이 정수면 `integer`, 숫자면 `number`, `YYYY-MM-DD`면 `date`, `true/false`면 `boolean`, ISO 8601 시각이면 `timestamp`, 아니면 `text`. `007`처럼 0으로 시작하는 숫자는 `text`. 빈 값이 없으면 `required: true` |
| `enum` | 고유값이 12개 이하이고 값 있는 행의 절반 이하인 text 열 |
| `primary_key` | 값이 유일하고 비어 있지 않은 첫 열. 없으면 함께 쓰면 유일한 두 열. 그래도 없으면 만들지 않고 어느 파일인지 알려 줍니다 |
| `ref` | `<테이블>_id` 꼴 열, 또는 다른 테이블의 기본 키와 이름이 같은 열. 값이 모두 대상에 있을 때만 |
| 이름 | 대문자·공백·한글·예약어가 있는 머리글과 파일 이름은 snake_case로 바꾸고(`Order Date` → `order_date`, `select` → `select_col`, `금액` → `column_4`) 머리글을 고친 CSV 사본을 씁니다. 원래 이름은 화면 표시 이름(`labels`)으로 남깁니다 |
| 차트 | 테이블마다 건수와 원본 행 표. date+숫자 열 → 주별 합계 선, enum 열 → 값별 건수 막대, enum+숫자 열 → 값별 합계 원. 테이블마다 최대 6개 |
| 위젯 | 시작·종료 꼴 이름의 date 열 2개 → 간트, `status`·`state`·`step`으로 끝나는 enum 열 → 칸반 |

만든 뒤 손볼 곳:

1. 두 YAML에서 `# 추론: 확인 필요`를 찾아 유형·enum 값·기본 키·ref를 확인합니다. 맞으면 주석을 지웁니다.
2. `schema.yaml`에 `min`/`max`/`pattern`과 `checks`를 더합니다(추론하지 않습니다). 코드 값이 enum으로 잡혔으면 `text`로 되돌립니다.
3. `dashboard.yaml`에서 `app.title`, 차트 제목, 탭 라벨·아이콘을 고치고, 필요 없는 차트와 탭을 지웁니다. 조인이나 계산 열이 필요하면 데이터셋의 `table:`을 `sql:`로 바꿉니다. 칸반의 칸 순서와 라벨, 간트의 `statuses`도 여기서 정합니다.
4. `.env`에서 `PROJECT=myproject`로 바꿉니다.

파일을 만들지 않고 추론 결과만 보려면 `python -m dashkit new <이름> --from <폴더> --dry-run`(JSON 출력)을 씁니다. 손으로 시작하려면 `cp -r projects/sales-sample projects/myproject`로 예시를 복사해 고쳐도 됩니다.

```bash
make check
```
설정만 먼저 검사합니다. 오류는 `dashboard.yaml:dashboards.main.layout[1][0]: 차트 'x'가 이 대시보드의 charts에 없음`처럼 위치와 함께 모두 출력됩니다.

```bash
make apply
```

이전 프로젝트의 Superset 객체는 이번 sync에서 지워집니다. 한 번에 한 프로젝트만 적용되며, 데이터 DB의 `raw`·`dash` 스키마도 매번 다시 만듭니다. 잠깐 다른 예시를 보려면 `.env`를 고치지 않고 이렇게 해도 됩니다.

```bash
PROJECT=sales-sample make apply
```

웹앱은 **마지막으로 적용에 성공한 프로젝트**를 보여 줍니다(`.state/embed.json`의 `project`. 규칙은 `contracts/api.md`의 "현재 프로젝트"). `.env`의 `PROJECT`는 `make check`·`make apply`가 다룰 프로젝트이고, 아직 아무것도 적용하지 않았을 때 웹앱이 보여 줄 프로젝트입니다.

## 5. Superset 화면에서 만든 차트를 params로 옮기기

축약 문법에 없는 차트 유형이나 세부 옵션은 Superset 화면에서 만든 뒤, 그 파라미터를 설정으로 옮깁니다.

1. http://localhost:8088 에 관리자로 로그인합니다.
2. **Charts → + Chart**에서 `make apply`가 만든 데이터셋(`dash.<키>`)을 고르고, 원하는 대로 만든 다음 **Save**합니다(대시보드에는 추가하지 않습니다).
3. 차트 목록에서 그 차트를 체크하고 **Bulk select → Export**를 누릅니다. zip이 내려받아집니다.
4. zip 안의 `charts/<이름>.yaml`을 엽니다. `viz_type`과 `params`를 `dashboard.yaml`의 차트로 옮깁니다. `params`에서 `datasource`, `slice_id`, `dashboards`, `extra_form_data` 키는 지웁니다.

   ```yaml
   - key: heat
     title: 요일·시간대 분포
     viz_type: heatmap_v2          # yaml의 viz_type 그대로
     dataset: orders               # 화면에서 고른 데이터셋의 키
     params:                       # yaml의 params에서 datasource·slice_id·dashboards·extra_form_data를 뺀 나머지
       x_axis: weekday
       groupby: hour
       metric: revenue
       linear_color_scheme: blue_white_yellow
   ```

5. `layout`에 차트 키를 배치하고 실행합니다.

   ```bash
   make apply
   ```

6. 화면에서 임시로 만든 차트는 sync가 관리하지 않으므로 Superset에서 직접 지웁니다. sync가 만든 차트는 화면에서 덮어쓸 수 없고(Explore의 저장 창에서 "Save as…"만 됩니다), `SYNC_LOCK_UI=0`으로 풀어 고치더라도 다음 `make apply`에서 덮어써집니다.

축약 차트에 옵션만 하나 더하고 싶을 때(예: 막대 값 표시)도 같은 방법으로 키 이름을 찾아 `params: { show_value: true }`처럼 씁니다.

---

## Studio: 화면에서 구성하기 (선택)

```bash
make studio        # http://localhost:8090
make studio-down   # 끄기(웹앱·Superset의 프레임 허용도 원래대로)
```

Studio는 `projects/<이름>/`의 파일을 고치고 `make apply`와 같은 일을 실행하는 **YAML 편집기**입니다. 전용 저장소나 DB가 없어서, Studio로 만든 것과 손으로 쓴 것이 같은 파일입니다. 손 편집과 섞어 써도 됩니다.

| 화면 | 하는 일 |
|---|---|
| 프로젝트 마법사 | CSV 여러 개를 놓으면 `make new`와 같은 추론 결과(유형, 키, 참조, 초안 차트)를 보여 주고, 고친 뒤 프로젝트를 만듭니다 |
| 데이터 | CSV를 테이블에 놓으면 검증 결과와 지난 적재와의 차이(`make plan`의 방식)를 먼저 보여 주고, 통과한 파일만 `data/<테이블>.csv`로 바꿉니다 |
| 탭 구성 | 탭과 섹션의 순서·추가·삭제, 섹션 옵션. `dashboard.yaml`의 `tabs`를 고칩니다 |
| 앱 | 제목, 로고, 기준일, 색. `dashboard.yaml`의 `app`을 고칩니다 |
| 대시보드 배치·필터 | 12칸 격자에서 차트를 더하고, 끌어 옮기고, 너비를 조절합니다. 차트 설정은 유형별 폼으로, 필터는 목록으로 고칩니다. `dashboard.yaml`의 `dashboards`(`charts`, `layout`, `filters`)를 고치며, Superset에는 적용할 때 반영됩니다 |
| 적용 | check → load → sync를 실행하고 진행 로그를 보여 줍니다. 끝나면 오른쪽 미리보기(실제 웹앱)가 그 프로젝트를 보여 줍니다 |

- **외부에 열지 마세요.** Studio에는 인증이 없고, owner DB 계정과 Superset 관리자 계정, `projects/` 쓰기 권한을 가집니다. `127.0.0.1`에만 열리며 운영 배포에 넣지 않습니다. 다른 사이트가 브라우저를 통해 요청을 보내지 못하도록 Host·Origin·Content-Type을 검사합니다(`contracts/studio.md` "요청 보호").
- **미리보기와 로그인.** 미리보기 안의 웹앱도 로그인을 요구하므로 틀 안의 로그인 화면에서 로그인합니다. Studio는 안내된 주소(`http://localhost:<포트>`)로 여세요. `127.0.0.1`로 열면 웹앱이 틀을 거절하고 로그인 쿠키도 실리지 않아, 미리보기 자리에 바른 주소로 가는 링크가 나옵니다(`contracts/studio.md` "프레임 허용").
- **저장 방식.** 변경을 임시 사본에 적용해 `make check`와 같은 검사를 통과한 뒤에만 원래 파일을 원자적으로 바꿉니다. 실패하면 파일은 그대로이고 오류가 화면에 나옵니다. YAML은 고친 곳만 다시 쓰므로 손으로 쓴 주석, 키 순서, 줄 모양이 남습니다(건드리지 않은 줄은 바이트 단위로 같습니다).
- **되돌리기는 git으로 합니다.** Studio는 이번 실행에서 자기가 쓴 파일 목록만 보여 줍니다(`git diff`, `git checkout -- <파일>`).
- 한계: 차트 폼은 축약 문법(`contracts/chart-shorthand.md`)의 필드만 다룹니다(Superset Explore 수준의 편집기가 아닙니다). 데이터셋(SQL, 지표)은 `dashboard.yaml`을 직접 고칩니다. `schema.yaml`은 마법사가 만들 때만 쓰고 그 뒤로는 고치지 않습니다. 앵커·별칭(`&a`, `*a`)이 있는 YAML은 파일 전체를 다시 쓰므로 주석과 키 순서는 남지만 줄 모양이 표준 꼴로 바뀝니다. 한 번에 하나의 적용만 실행합니다.

API와 저장 규칙은 `contracts/studio.md`에 있습니다.

## 적용 전에 차이 보기

```
$ make plan
데이터   members  +1행 −0행 ~1행
         변경 없음: milestones, tasks
뷰       dash.kpi_snapshots  열 추가: age_days
         dash.docs  적용해야 알 수 있음 (쿼리가 바뀜)
Superset 차트 +1 (extra_total)  ~1 (kpi_dday: 제목 변경)
         대시보드 ~1 (overview: 배치 변경)
```

- 데이터는 CSV와 **지난 적재의 기록**을 기본 키로 비교합니다(`+` 추가, `−` 삭제, `~` 변경 행 수). 기록은 `make apply`(load)가 적재할 때 DB의 `dashkit_meta` 스키마에 남기는 행 지문(기본 키의 해시와 행의 해시. 원본 값은 없습니다)입니다. 삭제될 행은 개수만 나옵니다. 기록이 아직 없으면 "지난 적재 기록 없음: 비교할 수 없음"으로 나오고, 한 번 적용하면 생깁니다. 열 구성이나 기본 키를 바꾼 테이블은 행을 맞추지 않고 전부 추가·전부 삭제로 셉니다. CSV에 오류가 있으면 차이 대신 검증 오류를 보여 줍니다.
- 뷰는 새 설정으로 만들어질 열과 지금 열을 비교합니다. `table:` 데이터셋은 적용 전에 열 변화를 알 수 있습니다. `sql:` 데이터셋은 쿼리나 쿼리가 읽는 테이블의 구조가 바뀌면 "적용해야 알 수 있음"으로 나옵니다.
- Superset은 새 번들과 마지막으로 가져오기에 성공한 번들(`.build/<이름>/applied/`)을 비교합니다. `−`로 나온 차트는 적용할 때 Superset에서 지워집니다.
- DB, Superset, `.state/`, `.build/`를 바꾸지 않고, DB는 읽기 계정(`dash_reader`)으로만 읽습니다. `make apply`는 plan 없이도 그대로 적용합니다. JSON이 필요하면 `python -m dashkit plan --json`. 규칙과 형식은 [contracts/plan.md](contracts/plan.md)에 있습니다.

## 설정 버전

`schema.yaml`과 `dashboard.yaml` 맨 위에 `version: 1`을 둘 수 있습니다. 없으면 1로 봅니다(기존 파일은 고치지 않아도 됩니다). 형식을 깨는 변경이 생기면 버전이 올라가고, 그때 `make migrate`가 파일을 새 형식으로 고칩니다(주석과 나머지 내용은 그대로 둡니다. `python -m dashkit migrate --check`는 고칠 것이 있는지만 확인합니다). 지원하지 않는 버전의 파일은 `make check`가 `dashboard.yaml:version: …` 오류로 알려 줍니다. 형식이 언제 어떻게 바뀌었는지는 [CHANGELOG.md](CHANGELOG.md)에 적습니다.

## 템플릿 코드를 바꿨을 때

`make apply`는 `projects/`의 변경만 반영합니다. backend 이미지에 들어 있는 것(`dashkit/`의 파이썬 코드, `contracts/`의 스키마, 위젯 매니페스트 `web/widgets/*/widget.json`)을 바꿨으면 `make up`으로 이미지를 다시 만들고 컨테이너를 바꿔야 합니다. `web/`의 화면 코드(JS, CSS)는 마운트되어 있어 새로 고침이면 됩니다.

---

## 테스트

```bash
make test-unit   # 스택 없이(tools 이미지만). PR마다 CI가 돌린다
make test        # 전체 스택이 필요한 통합 테스트
```

**`make test-unit`** 은 DB와 Superset 없이 돕니다.

- 설정 검사(`tests/test_config.py`), CSV 검증(`tests/test_validate.py`), 템플릿 코드의 도메인 단어 grep
- plan의 차이 계산(`tests/test_plan_diff.py`), 설정 버전과 migrate(`tests/test_migrate.py`), backend의 설정 다시 읽기(`tests/test_reload.py`)
- 번들 스냅샷(`tests/test_bundle_golden.py`): 예시 프로젝트의 번들을 고정된 뷰 열 정보(`tests/golden/<이름>/view_columns.json`)로 만들어 `tests/golden/<이름>/bundle/`과 한 바이트도 다르지 않은지 봅니다. 번들이 바뀌는 것이 의도라면 `make golden`으로 갱신하고 diff를 PR에서 검토합니다. `sql:` 데이터셋의 열이 바뀌었으면 `PROJECT=<이름> make golden COLUMNS=1`로 열 정보부터 다시 읽습니다(그 프로젝트가 적용된 스택 필요).

**`make test`** 는 개발 스택과 분리된 테스트 스택에서 돕니다. 쓰던 화면(8080, 8088)과 `.state/`, `.build/`는 바뀌지 않습니다.

- 테스트 스택: compose 프로젝트 `dashkit-test-<식별자>`, 별도 포트·볼륨, 상태·번들은 `.test/`. 이름과 포트는 작업 폴더마다 다릅니다(`make info`로 확인, `make test`도 시작할 때 출력). 이미지는 같은 폴더의 개발 스택과 같이 씁니다.
- 순서:
  1. devtracker로 `up`, `apply`한 뒤 `tests/` 전체를 실행합니다. 단위 테스트에 더해 로더(임시 DB), sync(임시 프로젝트 이름), 지표 정합성(Superset 차트 데이터 API), 백엔드와 guest token 권한 경계, plan(`tests/test_plan.py`: 적용 직후 "변경 없음", 고친 사본의 숫자, plan 전후로 DB·Superset·상태 파일이 그대로인지)을 봅니다.
  2. sales-sample로 `apply`한 뒤 전환을 검사합니다(`tests/test_switch.py`). 탭·차트·위젯 데이터가 바뀌고 이전 Superset 객체가 남지 않는지 봅니다.
  3. chart-gallery로 `apply`한 뒤 차트 유형마다 데이터 조회가 되는지 봅니다(`tests/test_gallery.py`).
  4. 성공하든 실패하든 테스트 스택과 그 볼륨, `.test/`를 지웁니다. `make test KEEP=1`이면 남기고, `make test-down`으로 내립니다.
- Superset을 하나 더 띄우므로 메모리가 2GB쯤 더 필요합니다. 부족하면 `make down`으로 개발 스택을 내린 뒤 실행하세요.

### 여러 작업 폴더에서 동시에

같은 컴퓨터에서 이 저장소의 복제본이나 git worktree 여러 개로 동시에 작업할 수 있습니다(예: 브랜치마다 폴더 하나). git 브랜치는 소스 파일만 나눠 주고, Docker 이미지 태그·compose 프로젝트 이름·포트는 컴퓨터 전체가 함께 쓰는 자원이라 따로 나눠야 합니다. Makefile이 **작업 폴더 경로에서 만든 식별자**로 이를 나눕니다.

```bash
make info
```

| 자원 | 폴더마다 | 값 |
|---|---|---|
| 직접 빌드하는 이미지 | 다름 | `dashkit-app:local-<식별자>`, `dashkit-studio:local-<식별자>`, `dashkit-superset:<버전>-<식별자>` |
| 테스트 스택(`make test`) | 다름 | 이름 `dashkit-test-<식별자>`, 포트는 20000~39999에서 폴더별로 정해지는 구간(웹앱 +0, Superset +8, Studio +9) |
| 개발 스택(`make up`) | **폴더 이름이 같으면 같음** | compose 프로젝트 이름 = 폴더 이름, 포트 8080·8088(`.env`) |

- 그래서 `make test`와 `make test-unit`은 폴더끼리 순서를 맞추지 않아도 됩니다. 다른 프로그램이 같은 포트를 쓰고 있으면 `make test`가 시작할 때 알려 주고 멈춥니다(`TEST_PORT_BASE=<값> make test`로 구간을 바꿉니다).
- 개발 스택의 이름은 일부러 바꾸지 않았습니다. 이름을 바꾸면 지금 쓰는 스택과 데이터 볼륨이 새 이름과 끊어지기 때문입니다. 대신 **폴더 이름이 같은 다른 폴더가 이미 스택을 띄웠으면** `make up`·`apply`·`down`·`reset`·`studio` 등 그 스택에 닿는 명령이 실행 전에 멈춥니다. 두 번째 폴더에서도 개발 스택을 띄우려면 이름과 포트를 따로 줍니다.

  ```bash
  COMPOSE_PROJECT_NAME=dashboard-b WEB_PORT=8180 SUPERSET_PORT=8188 make up
  ```

  (`.env`의 `WEB_ORIGINS`, `SUPERSET_PUBLIC_URL`도 그 포트에 맞춰야 임베드가 됩니다.)
- `make` 없이 `docker compose`를 직접 부르면 접미사 없는 이미지 이름(`dashkit-app:local`)을 씁니다.
- 폴더를 지운 뒤 남은 이미지는 `docker image ls 'dashkit-*'`로 보고 지웁니다.

CI: PR마다 `make test-unit`을 돌리고(`.github/workflows/tests.yml`), 코드·설정·계약이 바뀐 PR은 화면 캡처 워크플로가 `make test`도 돌립니다.

UI 화면은 자동 테스트에 넣지 않았습니다.

## 문제 해결

| 증상 | 확인 |
|---|---|
| 섹션에 "아직 반영되지 않았습니다" | `make apply`를 실행했는지 |
| Superset 섹션에 "Superset에 연결할 수 없습니다" | `make logs`에서 superset 상태, `.env`의 `SUPERSET_PUBLIC_URL`이 브라우저에서 열리는지 |
| `make apply`의 sync가 "asset 번들 가져오기 실패" | Superset이 검증 오류를 500으로만 돌려줍니다. `make logs`에서 superset 로그를 보세요. 번들은 `.build/<이름>/bundle/`에 있습니다 |
| 웹앱을 `127.0.0.1:8080`으로 열면 임베드 403 | `.env`의 `WEB_ORIGINS`에 그 출처가 있는지(기본값은 localhost와 127.0.0.1 둘 다) |
| `.env`의 DB 비밀번호를 바꿨더니 접속 실패 | DB 계정은 처음 만들 때만 생깁니다. `make reset CONFIRM=1` 후 `make up && make apply` |

버전: Superset 6.1.0, @superset-ui/embedded-sdk 0.4.0, PostgreSQL 16.10, nginx 1.29.3, Python 3.12.16. 모든 이미지와 패키지는 정확한 버전으로 고정했습니다.

Superset 버전은 `.env.example`의 `SUPERSET_VERSION` 한 곳에 있습니다. 올리는 절차는 [contracts/superset-upgrade.md](contracts/superset-upgrade.md)에 있습니다. `docker compose`를 make 없이 직접 부를 때는 `docker compose --env-file .env.example --env-file .env …`처럼 두 파일을 함께 줍니다(예전에 만든 `.env`에 없는 변수를 `.env.example`에서 읽습니다).
