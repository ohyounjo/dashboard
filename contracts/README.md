# 계약 색인

설정 형식과 컴포넌트 사이 약속의 유일한 기준이다. 바꿀 때는 이 폴더를 먼저 고치고 구현을 맞춘다.

| # | 계약 | 파일 |
|---|---|---|
| 1 | `schema.yaml` 형식 | [schema.schema.json](schema.schema.json) |
| 2 | `dashboard.yaml` 형식 | [dashboard.schema.json](dashboard.schema.json), 아이콘 [icons.md](icons.md) |
| 3 | 차트 축약 문법 → Superset 파라미터 | [chart-shorthand.md](chart-shorthand.md). 유형별 대응의 원본은 차트 유형 레지스트리 `dashkit/charts/<viz>.yaml`이고, 대응표와 스키마의 유형별 정의는 거기서 생성·합성한다 |
| 4 | 백엔드 API, 현재 프로젝트 규칙(`embed.json`의 `project`를 따른다) | [api.md](api.md) |
| 5 | 웹앱 위젯 | [widgets.md](widgets.md) |
| 6 | 상태 파일 `.state/embed.json`, `.state/managed.json` | [state-and-uuid.md](state-and-uuid.md) |
| 7 | Superset 객체 UUID | [state-and-uuid.md](state-and-uuid.md) |
| 8 | `make new`의 추론 결과(JSON)와 추론 규칙 | [scaffold.md](scaffold.md). Studio의 프로젝트 마법사가 같은 형식을 주고받는다 |
| 9 | 적용 전 차이 보기(`make plan`)의 비교 규칙·권한·결과 형식(JSON), 로더가 남기는 적재 기록(`dashkit_meta` 스키마) | [plan.md](plan.md). plan은 `dash_reader` 계정만 쓴다. Studio의 데이터 화면이 같은 함수를 쓴다 |
| 10 | 설정 형식 버전(`version`)과 `dashkit migrate` | 아래 "설정 버전", 형식 변경 기록 [../CHANGELOG.md](../CHANGELOG.md) |
| 11 | Studio(구성 화면) API, 변경 연산, 요청 보호, 저장 방식 | [studio.md](studio.md). 화면(`web/studio/`)과 API(`dashkit/studio/`)의 약속이다. 프로젝트 마법사는 8번, 데이터 화면의 차이는 9번의 형식을 그대로 쓴다 |
| 12 | 로그인·세션·요청 보호, 등급과 계정 상태, 인증 DB(`dashkit_auth` 스키마, `dash_auth` 계정), `/api/auth/*`, `make auth-init`·`make admin` | [auth.md](auth.md). 기존 웹앱 API의 로그인 요구는 4번(api.md)에도 적는다 |
| 13 | 데이터 추가 적재: 테이블 방식(`mode`·`on_conflict`), 배치 폴더와 정규형, 접수(파서·머리글 맞추기·값 다듬기), 충돌 정책, 배치 기록(`dashkit_meta.batch`)과 데이터 설정 지문, 쓰기 잠금, 폴더를 바꾸는 작업의 공통 순서, 묶음 기록과 끊김 복구, 구조 변경과 설정 변경 이력, 합치기, 계보, 운용자 조회 계정(`dash_inspect`, `make data-init`) | [ingest.md](ingest.md). 로더·관리 서비스·CLI·Studio가 `dashkit/ingest/` 한 벌을 쓴다 |
| 14 | 관리 서비스(`/api/manage`, `make manage`)의 경로·권한·요청 보호·한도 | [manage.md](manage.md). 형식과 동작은 13번, 설정 변경 연산은 11번을 그대로 쓴다 |
| — | Superset 6.1.0 확인 기록(Explore) | [superset-notes.md](superset-notes.md), 원본 export [superset-6.1.0-export/](superset-6.1.0-export/)(`managed/`는 sync가 만든 객체의 export) |
| — | Superset 버전 관리와 업그레이드 절차 | [superset-upgrade.md](superset-upgrade.md). 버전은 `.env.example`의 `SUPERSET_VERSION` 한 곳, 키 대조는 `tests/test_superset_keys.py` |
| — | 구현 계획(A~E 분담, 환경변수, 의존성) | [implementation-plan.md](implementation-plan.md) |

## 설정 버전 (보강설계서 5-3)

- `schema.yaml`과 `dashboard.yaml` 최상위에 `version: <정수>`를 둘 수 있다. **없으면 1로 본다.** 두 파일의 버전은 따로 본다.
- 이 dashkit이 읽는 버전은 `dashkit/config.py`의 `CONFIG_VERSION`(지금 1) 하나다. 검사는 JSON Schema보다 먼저 하고, 버전이 맞지 않는 파일은 형식 검사를 하지 않고 버전 오류 하나만 보고한다.

  | 파일의 버전 | 결과 |
  |---|---|
  | `CONFIG_VERSION`과 같음(생략은 버전 1로 판단) | 통과 |
  | 더 큼 | `<파일>:version: 이 dashkit은 설정 버전 1까지 지원함(파일은 버전 2). dashkit을 업그레이드하세요` |
  | 더 작음(버전이 올라간 뒤) | `<파일>:version: 설정 버전 1은 더 이상 그대로 읽지 않음(현재 버전 2). `python -m dashkit migrate`(make migrate)로 파일을 고치세요` |
  | 1 이상의 정수가 아님 | JSON Schema 오류(`<파일>:version: …`) |

- **버전을 올리는 때는 형식을 깨는 변경뿐이다**(키 이름·뜻이 바뀌거나 없어져, 고치지 않은 기존 파일이 통과하지 못하게 되는 변경). 선택 키나 새 유형을 **추가**하는 변경은 버전을 올리지 않는다. 어느 쪽이든 [CHANGELOG.md](../CHANGELOG.md)에 적는다.
- 버전을 올릴 때 할 일: ① `CONFIG_VERSION`을 올린다 ② `dashkit/migrate.py`의 `MIGRATIONS[이전 버전]`에 변환 함수 하나를 등록한다(`(파일 이름, 텍스트) -> 텍스트`. `version:` 줄은 migrate가 고친다) ③ CHANGELOG.md에 적는다 ④ 예시 프로젝트에 `make migrate`를 돌린다.
- 정규화한 `Project.schema`, `Project.dashboard`에는 `version`이 항상 들어 있다(없던 파일은 1). 번들에는 들어가지 않는다.

### `python -m dashkit migrate [--check]`

`PROJECTS_DIR/<PROJECT>/`의 두 설정 파일을 최신 버전 형식으로 고친다.

- 텍스트 수준에서 고친다(YAML을 읽어 다시 쓰지 않는다). 주석·순서·따옴표·줄 끝 문자가 그대로 남는다.
- `version` 키가 없으면 `version: 1` 줄을 맨 위에 넣는다(BOM과 `---` 표시가 있으면 그 뒤. 다음 줄이 비어 있지 않으면 빈 줄 하나를 둔다). 그다음 등록된 변환을 버전 순서대로 적용한다. 지금은 버전이 1뿐이라 변환이 없다.
- 명시적 버전이 이미 최신이면 아무것도 쓰지 않는다(일반 migrate는 생략된 키를 추가하고, 다시 실행하면 변경하지 않는다).
- 두 파일을 모두 변환해 본 뒤에 쓴다. 한 파일이라도 실패하면(YAML 문법 오류, 지원보다 높은 버전, 등록되지 않은 변환) 아무 파일도 바꾸지 않고 종료 코드 1. 쓰기는 임시 파일 뒤 `os.replace`다.
- `--check`: 파일을 바꾸지 않고 두 파일의 실제 버전 변환 필요 여부를 확인한다. 생략된 버전은 1로 보며 현재 버전이면 키 삽입을 요구하지 않고 명시적 현재 버전과 같이 종료 코드 0이다. 구버전이면 파일과 등록된 변환 단계를 출력하고 1이다. 잘못된 버전(1 미만, 정수가 아닌 값, bool, 지원보다 높은 값), YAML 오류, 누락 파일 또는 미등록 변환은 오류를 보고하고 1이다. 하나라도 변환이 필요하거나 오류이면 전체 검사는 실패한다.
- migrate는 형식 버전만 다룬다. 내용 검사는 `dashkit check`가 한다.

## 참조 검사 (`dashkit/config.py`, JSON Schema 다음 단계)

오류 메시지 형식: `<파일>:<YAML 경로>: <이유>` (예: `dashboard.yaml:dashboards.overview.layout[1][0]: 차트 'ms_bar2'가 이 대시보드의 charts에 없음`). 오류는 모두 모아 한 번에 보고한다.

### 정적 검사 (`dashkit check`, DB 없이)

| 대상 | 규칙 |
|---|---|
| schema 테이블·열 이름 | SQL 예약어 금지(`dashkit/config.py`의 `RESERVED_WORDS`, PostgreSQL 예약어 목록) |
| schema `primary_key` | `columns`에 있는 열 |
| schema `ref` | `<테이블>.<열>`이 있고, 그 열이 대상 테이블의 단일 열 PK. 참조 열과 대상 열의 type이 같음. 테이블 참조에 순환이 없음 |
| schema `min`/`max` | 열 type에 맞는 값(date는 날짜 문자열), `min ≤ max` |
| schema `pattern` | 컴파일되는 정규식 |
| schema `on_conflict` | `mode: append`인 테이블에만 |
| schema `default` | 열의 type·규칙(`enum`·`min`·`max`·`pattern`)을 통과하는 값. 기본 키 열에는 쓸 수 없음 |
| schema `aliases` | 같은 테이블의 다른 열 이름이나 다른 열의 `aliases`와 겹치지 않음 |
| 누적 테이블의 배치 파일 이름(`dashkit check`, load) | `data/<테이블>/` 바로 아래의 `<번호>[-<덧붙임>].csv(.gz)`. 같은 번호가 둘이면 오류([ingest.md](ingest.md) "배치 폴더") |
| `datasets.*.table` | schema의 테이블 |
| `datasets` 키 | 예약어 금지 |
| 차트 `key` | 프로젝트 전체에서 유일 |
| 차트 `dataset` | `datasets`에 있음 (**없는 데이터셋 참조**) |
| 차트 지표 문자열 | 그 데이터셋의 `metrics`에 있음(레지스트리의 `kind`가 `metric`·`metrics`인 필드) |
| 차트의 열·지표 라벨 (**중복 라벨**) | 같은 쿼리에서 서로 다른 지표가 화면에 의해 제거되거나, 남은 열·지표의 서버 라벨이 겹치면 오류. 최종 `params`와 `viz_type`, 쿼리 경계 및 실제 YAML 경로를 반영한다. 지원 유형·모드와 미지원 원본 구성은 [chart-shorthand.md](chart-shorthand.md) "중복 라벨 검사", 실측은 [superset-notes.md](superset-notes.md) 14절 |
| `layout` 항목 | 차트 항목은 같은 대시보드 `charts`에 있음 (**배치에 없는 차트 키**). 모든 차트가 대시보드 안 탭(`tabs[].rows`)을 포함해 정확히 한 번 배치됨. 행(탭 안 행 포함)의 지정 너비 합 ≤ 12, 너비 없는 항목(차트·글) 수 ≤ 남은 칸. 제목(`header`)은 한 행에 혼자. 제목·글 항목의 형식(빈 글 금지, 허용 필드)은 스키마가 본다 |
| `filters[].key` | 대시보드 안에서 유일 |
| `filters[].type` | 생략하면 `select`. 유형별 필수 필드(스키마): `select`·`number_range`는 `dataset`·`column`, `time_grain`은 `dataset`. 유형에 없는 필드 금지: `time_range`에는 `dataset`·`column`·`multi`·`parent`, `number_range`에는 `multi`·`parent`, `time_grain`에는 `column`·`multi`·`parent` |
| `filters[].dataset` | `datasets`에 있음 |
| `filters[].default` | 유형에 맞는 형식. `select`: 값 또는 비어 있지 않은 값 목록(`multi: false`면 하나), `number_range`: `[최소, 최대]`(한쪽 `null` 가능, 최소 ≤ 최대), `time_range`: 비어 있지 않은 문자열, `time_grain`: `P1D`·`P1W`·`P1M`·`P3M`·`P1Y` |
| `filters[].parent` | 같은 대시보드의 필터 키. 자기 자신이 아니고 순환하지 않음 |
| `filters[].charts` | 같은 대시보드 `charts`의 키(비어 있지 않고 중복 없음) |
| 탭 `key` | 유일 |
| superset 섹션 `dashboard` | `dashboards`에 있음 |
| 위젯 섹션 `dataset` | `datasets`에 있음 |
| 위젯 매니페스트의 `x-unique` 표시(kanban `columns[].value`) | 배열 안에서 유일 |
| 위젯 매니페스트의 `x-date-range` 표시(gantt `range`) | start ≤ end |
| `table:` 데이터셋의 열 참조 | schema 열로 즉시 확인(아래 "열 참조" 목록과 같은 대상) |

### 열 참조 검사 (DB 필요: `dashkit load`가 뷰를 만든 직후, 같은 트랜잭션 안)

`config.referenced_columns(project)`가 `{데이터셋: {열: [출처, …]}}`를 돌려준다. 로더는 `information_schema.columns`의 실제 뷰 열과 비교하고, 없으면 롤백하며 `dashboard.yaml:<출처>: 데이터셋 '<d>' 뷰에 열 '<c>'가 없음`을 출력한다. **뷰에 없는 필터 열**도 이 단계에서 잡는다.

출처: 데이터셋 `labels`의 열(지표 이름 제외), 필터 `column`(`select`·`number_range`. `time_range`·`time_grain` 필터는 열을 참조하지 않는다), 축약 차트에서 레지스트리의 `kind`가 `column`·`columns`·`order_by`인 필드(예: `x`·`time_column`·`groupby`·`columns`·`order_by`, `heatmap`의 `y`, `sankey`의 `source`·`target`. 유형별 필드는 `chart-shorthand.md`의 표. `order_by`의 지표 라벨은 제외), 위젯 섹션에서 매니페스트(`web/widgets/<유형>/widget.json`, [widgets.md](widgets.md))에 `x-column` 표시가 있는 옵션(gantt `fields.*`·`sort`, kanban `group_by`·`card.title`·`card.chips`·`filter.column`). `viz_type` 원본 차트의 `params`는 검사하지 않는다.

### 적재 기록 (`dashkit load`가 같은 트랜잭션에서 남긴다)

로더는 raw·dash를 다시 만드는 트랜잭션에서 `dashkit_meta` 스키마(행 지문 `row_fingerprint`, 배치 기록 `batch`, 적재 스냅샷 `load_snapshot`)도 통째로 다시 만들고 `dash_reader`(와 있으면 `dash_inspect`)에게 USAGE·SELECT만 준다. raw의 원본 값은 하나도 넣지 않는다(기본 키의 해시와 행 해시, 파일 이름·내용 해시·행 수뿐). `dashkit plan`이 owner 계정 없이 원본 파일과 지난 적재를 비교하는 데 쓰고, 추가 적재가 대조와 중복 확인에 쓴다. 테이블 정의와 지문 규칙은 [plan.md](plan.md) "적재 기록". 데이터 DB의 스키마는 `raw`(owner만, `dash_inspect` 읽기), `dash`(reader·inspect 읽기), `dashkit_meta`(reader·inspect 읽기) 셋이다.

## `dashkit/config.py` 공개 인터페이스

```python
class ConfigError(Exception):
    errors: list[str]                      # "<파일>:<경로>: <이유>" 목록

@dataclass(frozen=True)
class Project:
    name: str                              # 폴더 이름 = PROJECT
    root: Path                             # projects/<name>
    schema: dict                           # schema.yaml (검증 완료, 기본값 채움: required=False, checks=[])
    dashboard: dict                        # dashboard.yaml (검증 완료, 섹션 선택 필드 None 채움)

CONFIG_VERSION = 1                         # 이 dashkit이 읽는 설정 형식 버전("설정 버전" 절)

def load_project(name: str, projects_dir: Path = Path("projects")) -> Project   # 실패 시 ConfigError
def config_version(doc: dict) -> int                            # 설정 파일 내용의 version(없으면 1)
def object_uuid(project: str, kind: str, key: str) -> str
def resolve_as_of(project: Project, today: date | None = None) -> date
def table_order(project: Project) -> list[str]                  # ref 위상 정렬(적재 순서)
def widget_datasets(project: Project, tab_keys: set[str] | None = None) -> set[str]   # 위젯 섹션이 참조한 데이터셋 = /api/data 허용 목록(tab_keys: 그 탭들만)
def superset_dashboards(project: Project, tab_keys: set[str] | None = None) -> set[str]   # superset 섹션이 참조한 대시보드 키(tab_keys: 그 탭들만)
def referenced_columns(project: Project) -> dict[str, dict[str, list[str]]]
def check_view_columns(project: Project, view_columns: dict[str, set[str]]) -> list[str]   # 오류 목록(없으면 [])
def charts(project: Project) -> Iterator[tuple[str, dict]]       # (대시보드 키, 차트)
def layout_blocks(dashboard: dict) -> list[dict]                 # layout → 블록(row/header/tabs). 너비·높이 기본값을 채운다
def load_schema(name: str) -> dict                               # 계약의 JSON Schema(dashboard.schema.json은 차트 유형 정의와 위젯 섹션 정의를 합성한 결과)
```

CLI: `python -m dashkit {check|plan|load|sync|migrate|api|gen|manage|auth}`. `manage`는 관리 서비스를 띄운다([manage.md](manage.md)). `auth`는 인증 DB를 다룬다([auth.md](auth.md) "CLI", PROJECT를 쓰지 않는다). `plan`은 적용 전에 차이를 보여 주고 아무것도 바꾸지 않는다([plan.md](plan.md), `--json`). `migrate`는 설정 파일을 최신 형식 버전으로 고친다(위 "설정 버전", `--check`는 확인만). `gen`은 계약 문서의 생성 구간을 레지스트리에서 다시 쓴다(`--check`는 확인만). 모든 하위 명령은 `PROJECT`, `PROJECTS_DIR`(기본 `/app/projects`) 환경변수를 쓴다.

CLI(프로젝트 초안): `python -m dashkit new <이름> --from <CSV 폴더> [--dry-run]`. CSV 폴더에서 `PROJECTS_DIR/<이름>/`을 만든다([scaffold.md](scaffold.md)). `PROJECT`는 쓰지 않고 DB도 필요 없다. `--dry-run`은 파일을 쓰지 않고 추론 결과(JSON)만 출력한다. 이미 있는 프로젝트는 덮어쓰지 않고 종료 코드 1로 끝낸다.
