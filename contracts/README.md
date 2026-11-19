# 계약 색인

설정 형식과 컴포넌트 사이 약속의 유일한 기준이다. 바꿀 때는 이 폴더를 먼저 고치고 구현을 맞춘다.

| # | 계약 | 파일 |
|---|---|---|
| 1 | `schema.yaml` 형식 | [schema.schema.json](schema.schema.json) |
| 2 | `dashboard.yaml` 형식 | [dashboard.schema.json](dashboard.schema.json), 아이콘 [icons.md](icons.md) |
| 3 | 차트 축약 문법 → Superset 파라미터 | [chart-shorthand.md](chart-shorthand.md). 유형별 대응의 원본은 차트 유형 레지스트리 `dashkit/charts/<viz>.yaml`이고, 대응표와 스키마의 유형별 정의는 거기서 생성·합성한다 |
| 4 | 백엔드 API | [api.md](api.md) |
| 5 | 웹앱 위젯 | [widgets.md](widgets.md) |
| 6 | 상태 파일 `.state/embed.json`, `.state/managed.json` | [state-and-uuid.md](state-and-uuid.md) |
| 7 | Superset 객체 UUID | [state-and-uuid.md](state-and-uuid.md) |
| — | Superset 6.1.0 확인 기록(Explore) | [superset-notes.md](superset-notes.md), 원본 export [superset-6.1.0-export/](superset-6.1.0-export/)(`managed/`는 sync가 만든 객체의 export) |
| — | Superset 버전 관리와 업그레이드 절차 | [superset-upgrade.md](superset-upgrade.md). 버전은 `.env.example`의 `SUPERSET_VERSION` 한 곳, 키 대조는 `tests/test_superset_keys.py` |
| — | 구현 계획(A~E 분담, 환경변수, 의존성) | [implementation-plan.md](implementation-plan.md) |

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
| `datasets.*.table` | schema의 테이블 |
| `datasets` 키 | 예약어 금지 |
| 차트 `key` | 프로젝트 전체에서 유일 |
| 차트 `dataset` | `datasets`에 있음 (**없는 데이터셋 참조**) |
| 차트 지표 문자열 | 그 데이터셋의 `metrics`에 있음(레지스트리의 `kind`가 `metric`·`metrics`인 필드) |
| `layout` 항목 | 같은 대시보드 `charts`에 있음 (**배치에 없는 차트 키**). 모든 차트가 정확히 한 번 배치됨. 행의 지정 너비 합 ≤ 12, 너비 없는 항목 수 ≤ 남은 칸 |
| `filters[].key` | 대시보드 안에서 유일 |
| `filters[].dataset` | `datasets`에 있음 |
| 탭 `key` | 유일 |
| superset 섹션 `dashboard` | `dashboards`에 있음 |
| gantt·kanban `dataset` | `datasets`에 있음 |
| kanban `columns[].value` | 칸 안에서 유일 |
| gantt `range` | start ≤ end |
| `table:` 데이터셋의 열 참조 | schema 열로 즉시 확인(아래 "열 참조" 목록과 같은 대상) |

### 열 참조 검사 (DB 필요: `dashkit load`가 뷰를 만든 직후, 같은 트랜잭션 안)

`config.referenced_columns(project)`가 `{데이터셋: {열: [출처, …]}}`를 돌려준다. 로더는 `information_schema.columns`의 실제 뷰 열과 비교하고, 없으면 롤백하며 `dashboard.yaml:<출처>: 데이터셋 '<d>' 뷰에 열 '<c>'가 없음`을 출력한다. **뷰에 없는 필터 열**도 이 단계에서 잡는다.

출처: 데이터셋 `labels`의 열(지표 이름 제외), 필터 `column`, 축약 차트에서 레지스트리의 `kind`가 `column`·`columns`·`order_by`인 필드(예: `x`·`time_column`·`groupby`·`columns`·`order_by`, `heatmap`의 `y`, `sankey`의 `source`·`target`. 유형별 필드는 `chart-shorthand.md`의 표. `order_by`의 지표 라벨은 제외), gantt `fields.*`·`sort`, kanban `group_by`·`card.title`·`card.chips`·`filter.column`. `viz_type` 원본 차트의 `params`는 검사하지 않는다.

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

def load_project(name: str, projects_dir: Path = Path("projects")) -> Project   # 실패 시 ConfigError
def object_uuid(project: str, kind: str, key: str) -> str
def resolve_as_of(project: Project, today: date | None = None) -> date
def table_order(project: Project) -> list[str]                  # ref 위상 정렬(적재 순서)
def widget_datasets(project: Project) -> set[str]               # gantt·kanban이 참조한 데이터셋 = /api/data 허용 목록
def referenced_columns(project: Project) -> dict[str, dict[str, list[str]]]
def check_view_columns(project: Project, view_columns: dict[str, set[str]]) -> list[str]   # 오류 목록(없으면 [])
def charts(project: Project) -> Iterator[tuple[str, dict]]       # (대시보드 키, 차트)
def load_schema(name: str) -> dict                               # 계약의 JSON Schema(dashboard.schema.json은 차트 유형 정의를 합성한 결과)
```

CLI: `python -m dashkit {check|load|sync|api|gen}`. `gen`은 계약 문서의 생성 구간을 레지스트리에서 다시 쓴다(`--check`는 확인만). 모든 하위 명령은 `PROJECT`, `PROJECTS_DIR`(기본 `/app/projects`) 환경변수를 쓴다.
