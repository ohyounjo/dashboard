# 프로젝트 전환 시 상태 파일 없는 Superset 객체 정리 설계

> 대상: [이슈 #52](https://github.com/ohyounjo/dashboard/issues/52) · 기준 커밋: `origin/main` (`fec2406`) · 상태: 실측 완료·구현 반영 (2026-11-22)
> 관련 문서: [보강 보고 7절](enhancement-report.md#7-남은-한계) · [상태 파일과 UUID 계약](../contracts/state-and-uuid.md) · [Superset 확인 기록](../contracts/superset-notes.md#11-외부에서-관리됨-표식과-상태-파일-없는-prune-보강설계서-3-5-7절-d3) · [적용 전 차이 보기 계약](../contracts/plan.md)

아래 설계는 `origin/main` 코드 분석과 격리 테스트 스택 실측(`contracts/superset-notes.md` 16절)을 거쳐 구현에 반영했다.

---

## 1. 현행 동작과 문제 진단 (`origin/main` 기준)

[`dashkit/sync/__init__.py`](../dashkit/sync/__init__.py)의 `find_marked()`와 `sync()` prune 단계는 다음과 같이 동작한다.

1. **동일 프로젝트 동기화 (상태 파일 없음):**
   - `find_marked(client, project)`가 현재 프로젝트의 DB(`database_name == "dashkit-<project>"`, `uuid == object_uuid(project, "database", "main")`) 하나만 조회해 `DB → 데이터셋 → 차트 → 대시보드`를 따라가며 `_made_by_sync()`(`is_managed_externally is True` 또는 UUID v5) 객체만 수집한다.
   - 따라서 `.state/managed.json`이 없어도 같은 프로젝트에서 빠진 차트·데이터셋·대시보드는 정리된다(`SYNC_LOCK_UI=0` 포함).
2. **프로젝트 전환 (상태 파일 없음):**
   - `sync()`는 이전 프로젝트 객체 삭제를 오직 `_read_managed(state_dir / "managed.json")`에 의존한다.
   - `.state/managed.json`이 없는 상태에서 프로젝트 `A → B`로 전환하면 `find_marked()`는 `dashkit-B`만 탐색하고 `found["database"]`도 채우지 않는다. 그 결과 `A`의 DB·데이터셋·차트·대시보드(및 `embedded_dashboards` 행)가 Superset에 고아 객체로 남는다.
   - `.state/embed.json`은 prune 이전에 새 프로젝트로 덮어써지므로 `managed.json`만 손실된 경우에도 이전 프로젝트 힌트를 잃는다.
3. **함께 확인된 경계·복구 취약점:**
   - **수동 객체의 부모 의존성 미검사:** `dashkit-A`의 데이터셋 위에 수동 차트(UUID v4, `is_managed_externally=False`)가 있거나 `dashkit-A` DB 아래 수동 데이터셋이 있을 때, prune이 부모 데이터셋·DB 삭제를 시도한다. Superset이 거절하면 해당 UUID가 `leftovers`에 남아 이후 `sync`가 계속 실패하고, 반대로 연쇄 삭제하면 수동 객체가 소실된다(실측으로 확정).
   - **부분 실패 시 계보 단절 위험:** prune 루프에서 대시보드 삭제가 실패해도 다음 단계로 넘어가 해당 대시보드의 차트를 삭제한다. 차트가 사라진 대시보드는 이후 상태 파일이 없을 때 `DB → 데이터셋 → 차트 → 대시보드` 경로로 찾을 수 없게 된다.
   - **전환 중 `make plan` 괴리:** [`dashkit/plan.py`](../dashkit/plan.py)의 `read_other_project()`는 `managed.json`만 보므로, `data-db`의 `dashkit_meta`는 `A`를 기록하고 있어도 `managed.json`이 없으면 `Superset` 절이 전환 사실을 인지하지 못한다.

---

## 2. 핵심 설계 과제: "교체된 이전 프로젝트"와 "타 프로젝트"의 경계

Superset 안의 모든 `dashkit-*` DB를 일괄 탐색해 지우면 오삭제가 발생한다.

- **테스트 격리 위반:** [`tests/test_sync.py`](../tests/test_sync.py)는 `devtracker`가 떠 있는 같은 Superset 인스턴스에 임시 프로젝트 `zz-synctest`를 빈 `state_dir`로 동기화하며, `test_prune_without_state_file`은 다른 프로젝트 객체 보존을 명시적으로 검증한다.
- **공유 Superset / 다중 프로젝트 경계:** 같은 Superset에 별도 `data-db`를 쓰는 다른 dashkit 스택이나 공존 프로젝트([enhancement-plan.md 5-6절](enhancement-plan.md#5-6-여러-프로젝트-동시-운영후순위))가 있을 수 있다.

자동 정리는 반드시 다음 두 조건을 **모두** 만족할 때만 허용한다.

1. **프로젝트 범위 증명:** 현재 `sync` 대상 프로젝트가 이전 프로젝트를 **실제로 대체했음**을 신뢰 가능한 상태 출처로 증명할 수 있어야 한다.
2. **객체 소유 증명:** 그 프로젝트 범위 안에서도 `dashkit`의 결정적 UUID(`NAMESPACE`)·표식과 수동 의존성 검사를 통과한 객체만 삭제한다. **안전하게 식별하지 못하면 삭제하지 않는다.**

---

## 3. 후보 비교 및 기술 결정

| 후보 | 식별 원리 | 장점 | 한계 | 결정 |
|---|---|---|---|---|
| **A. 전역 `dashkit-*` DB 자동 스캔** | Superset의 모든 `dashkit-<other>` DB 중 `uuid5` 일치분 삭제 | `.state/`·`data-db` 모두 비어도 동작 | `test_sync.py`의 `devtracker` 파괴. 공존·타 스택 프로젝트 구분 불가 | **기각** |
| **B. Superset 메타데이터 DB(`superset-db`) 직접 조회** | `external_url` 등을 Postgres에서 직접 읽기 | REST 비공개 필드 접근 가능 | `tools`/`studio`/`manager`에 `SUPERSET_DB_PASSWORD` 부여 필요(권한 경계 붕괴), 내부 스키마 강결합 | **기각** |
| **C. 번들/객체에 별도 태그·메타 마커 추가** | `tags`·`dashboard.metadata` 등에 스택 ID 기록 | 차트 없는 대시보드 등 추가 식별 가능 | 스택 ID도 외부 저장소가 필요함. golden 스냅샷 전체 변경 및 [`test_superset_keys.py`](../tests/test_superset_keys.py) 비용 증가 | **보류** |
| **D. `data-db`(`dashkit_meta`) 전환 계보 + `embed.json` 보조 + 명시적 정리 CLI** | `load`가 `data-db` 스키마 교체 시 직전 프로젝트 계보를 `dashkit_meta`에 남기고, `sync`가 `dash_reader`로 이를 읽어 교체가 증명된 프로젝트만 정리 | ① `.state/` 전체 손실·왕복·재실행 자동 복구<br>② `load` 없는 `test_sync.py` 및 타 DB·공존 프로젝트 보존<br>③ 번들·golden·DB 권한 분리 유지<br>④ 기존 `data-db` 볼륨 호환 | `data-db`까지 초기화되고 `.state/`도 없으면 자동 삭제 불가 → 비파괴 경고 + 명시적 정리 명령 제공 | **채택** |
| **E. 현행 유지 + 계약·복구 문서 보강만** | 상태 파일 없는 전환 정리 미지원 유지 | 구현 리스크 0 | `.state/` 손실 시 이전 프로젝트 왕복 또는 `make reset` 필요 | **예비 대안** (실측 불가 시) |

### 확정된 기술 결정

1. **전환 정리 방식:** **후보 D**를 채택한다(`dashkit_meta.load_snapshot.replaced_projects` + `.state/embed.json` + 수동 의존성 보호 + 역방향 실패 전파 차단).
2. **명시적 운영 복구 CLI:** 별도 하위 명령 **`python -m dashkit prune-project <이름> [--dry-run]`**을 둔다(`tools` 컨테이너 전용, `backend`/웹 API 미노출).
3. **`EmbedGuest` 권한 유지:** 현행 `origin/main`의 권한 4개(`can_read on CurrentUserRestApi`, `can_read on Dashboard`, `can_read on Chart`, `can_time_range on Api`)를 유지하고 넓히지 않는다.
4. **실측 기반 확정 규칙 (격리 스택 확인 완료, [`contracts/superset-notes.md` 16절](../contracts/superset-notes.md#16-상태-파일-없는-프로젝트-전환-prune-실측-이슈-52)):**
   - **DB 대상 검증:** `GET /api/v1/database/` 및 `GET /api/v1/database/<id>`에는 `sqlalchemy_uri`·`parameters`가 없지만, **`GET /api/v1/database/<id>/connection`**이 `sqlalchemy_uri`, `parameters` (`host`, `port`, `database`), `is_managed_externally`, `uuid`를 모두 반환함을 확인했다. 자동 `sync()`에서는 이 응답의 `(host, port, dbname)`이 현재 `DATA_DB_HOST/PORT/NAME`과 일치할 때만 이전 프로젝트 DB를 삭제 대상으로 삼는다(조회 실패·불일치 시 비삭제 + 경고 로그). `prune-project`는 운영자가 직접 지정한 프로젝트이므로 연결 대상만 로그에 알리고 `DATA_DB_*` 일치를 강제하지 않는다.
   - **데이터셋 엄격 식별:** `GET /api/v1/dataset/` 목록 API가 `id`, `uuid`, `table_name`, `schema`, `database.id`를 모두 반환하므로, `schema == "dash"` **AND** `str(uuid) == object_uuid(project, "dataset", table_name)`인 것만 관리 데이터셋으로 식별한다(N+1 단건 조회 불필요, `SYNC_LOCK_UI=0` 동일 동작, 수동·외부 데이터셋 차단).
   - **수동 객체 3종 연쇄 보존:** 실측에서 `DELETE /api/v1/dataset/<id>`는 수동 차트를 `datasource_id=None`으로 고아화(HTTP 200), `DELETE /api/v1/chart/<id>`는 수동 대시보드의 차트 목록을 비움(HTTP 200), `DELETE /api/v1/database/<id>`는 수동 데이터셋이 남아 있으면 HTTP 422로 거절함을 확인했다. 따라서 `plan_prune()`은 **수동 대시보드 → 관리 차트 → 관리 데이터셋 → 관리 DB** 3단계 연쇄 보존을 적용하고, 보존 항목은 오류(`leftovers`)로 취급하지 않는다.

---

## 4. 후보 D 상세 설계

### 4-1. 1단계 — 교체 대상 이전 프로젝트 목록 결정 (프로젝트 경계)

`sync(project)`는 삭제 후보가 될 이전 프로젝트 이름 집합 `prev_projects`를 아래 3개 출처에서만 수집한다(`project.name` 자신은 항상 제외, 프로젝트 이름 형식 `^[a-z0-9][a-z0-9_-]*$` 검증).

| 우선순위 | 출처 | 읽는 시점·권한 | `prev_projects` 채택 조건 | 손실 내성 |
|---|---|---|---|---|
| 1 | `.state/managed.json` (기존) | `sync` 시작 시 파일 읽기 | UUID 목록을 그대로 후보에 합집합(기존 동작 유지) | 파일 유지 시 |
| 2 | `.state/embed.json` | **5단계 덮어쓰기 전** 파일 읽기 | JSON이 유효하고 `embed.project != project.name`일 때 `embed.project` 추가 | `managed.json`만 손실 시 |
| 3 | `data-db` `dashkit_meta.load_snapshot` | 1단계와 같은 `dash_reader` 읽기 전용 연결 | **`snapshot.project == project.name`**일 때만 `snapshot.replaced_projects`의 이름들 추가 | **`.state/`·`.build/` 전체 손실 시** |

#### `dashkit_meta.load_snapshot` 계보 기록 규칙 ([`dashkit/loader/__init__.py`](../dashkit/loader/__init__.py), [`dashkit/loader/record.py`](../dashkit/loader/record.py))

- `load`는 `raw`·`dash` 스키마를 통째로 교체하는 **같은 트랜잭션 안**에서 `record.write()`가 `dashkit_meta`를 재생성하기 직전에 기존 `dashkit_meta.load_snapshot`(`id = 1`)을 savepoint로 읽는다(스키마·테이블이 없으면 `None`).
- 새 스냅샷의 `replaced_projects: list[str]` 계산 규칙:

  ```python
  prev_list = [p for p in (prev_snap.get("replaced_projects") or []) if isinstance(p, str)]
  prev_proj = prev_snap.get("project") if isinstance(prev_snap, dict) else None
  if isinstance(prev_proj, str) and prev_proj != project.name and prev_proj not in prev_list:
      prev_list.append(prev_proj)
  replaced_projects = [p for p in prev_list if p != project.name]
  ```

- **성질:**
  - **전환(`A → B`):** `load(B)`가 `A`의 `dash` 뷰를 대체하며 `project="B"`, `replaced_projects=["A"]`를 기록한다.
  - **중단·재실행:** `load(B)` 뒤 `sync(B)`가 실패하거나 `.state/`가 사라져도 `make sync`·`make apply` 모두 `replaced_projects=["A"]`를 보존한다.
  - **연속 전환(`A → B → C`, `B` 미동기화):** `replaced_projects=["A", "B"]`로 누적되어 `sync(C)`가 둘 다 정리한다.
  - **왕복(`A → B → A`):** `load(A)` 시 `"A"`는 제외되고 `"B"`가 들어가므로 `sync(A)`는 `B`만 지우고 `A`는 유지한다.
  - **`test_sync.py`·타 프로젝트 보존:** `tests/test_sync.py`는 `load` 없이 `sync("zz-synctest")`를 실행하므로 `snapshot.project`가 `"devtracker"`(`!= "zz-synctest"`)다. 조건 불일치로 `replaced_projects`를 읽지 않아 `devtracker`를 건드리지 않는다.
  - **DB 권한 준수:** `load`는 기존 `dash_owner` 트랜잭션 안에서만 쓰고, `sync`는 기존 `dash_reader` 읽기 전용 연결로만 읽는다. `replaced_projects`에는 프로젝트 이름만 남기며 `raw` 원본 값은 저장하지 않는다.

### 4-2. 2단계 — Superset 객체 식별 및 보존 경계 (객체 경계)

`find_marked`를 프로젝트 이름 단위로 일반화(`discover_project_objects`, `find_marked`는 현재 프로젝트용 래퍼 유지)하고, 현재 프로젝트와 `prev_projects` 각각에 대해 아래 기준을 적용한다.

| 대상 종류 | 삭제 후보 판정 기준 | 수동 / 타 객체 차단 근거 |
|---|---|---|
| **Database** | ① `database_name == f"dashkit-{p}"`<br>② `str(uuid) == object_uuid(p, "database", "main")`<br>③ 자동 `sync` 시 `GET /api/v1/database/<id>/connection`의 `(host, port, dbname)`이 현재 `DATA_DB_*`와 일치 | 수동 생성 DB는 UUID v4라 ②에서 차단. 타 스택 `data-db`를 가리키는 DB는 ③에서 차단 |
| **Dataset** | ① 위 검증된 DB에 소속(`database.id == db_id`)<br>② `schema == "dash"` 및 `str(uuid) == object_uuid(p, "dataset", table_name)` | 같은 DB에 수동 추가된 데이터셋은 UUID v4라 차단(`SYNC_LOCK_UI=0`도 동일하게 UUID v5 일치). 이후 차트 탐색도 검증된 관리 데이터셋 ID만 기준 삼음 |
| **Chart** | ① `datasource_type == "table"` 및 `datasource_id in managed_dataset_ids`<br>② `_made_by_sync(row)` (`is_managed_externally is True` 또는 UUID v5) | Explore "Save as…" 등으로 만든 수동 차트는 `is_managed_externally=False`, UUID v4라 차단 |
| **Dashboard** | ① 위 관리 데이터셋·관리 차트에 연결된 대시보드<br>② `_made_by_sync(row)` (`is_managed_externally is True` 또는 UUID v5) | 수동 대시보드는 UUID v4로 차단. `slug` 접두사(`f"{_slug(p)}-"`) 추측은 하지 않음(`sales` vs `sales-sample` 충돌 위험) |

> **안전 불변식 — "식별하지 못하면 삭제하지 않는다":**
> - `data-db` 초기화와 `.state/` 손실이 동시에 발생해 `prev_projects`가 비어 있으면 다른 `dashkit-*` DB를 자동 삭제하지 않는다. 대신 Superset DB 목록에서 남아 있는 타 `dashkit-<other>`(`uuid5` 일치) DB를 감지해 **비파괴 경고 로그**와 `python -m dashkit prune-project <이름>` 명령을 안내한다.
> - `SYNC_LOCK_UI=0`으로 수동 편집해 차트가 모두 제거된 대시보드는 `slug` 추측으로 삭제하지 않고 미식별 경계로 계약에 기록한다.

### 4-3. 3단계 — 역방향 의존성 불변식과 수동 객체 보호 (`plan_prune` 순수 함수)

Superset에서 읽어 온 객체 관계 그래프를 바탕으로 **순수 함수 `plan_prune()`**으로 삭제·보존 계획을 먼저 세운 뒤 역순(`dashboard → chart → dataset → database`)으로 삭제한다.

1. **수동 하위 객체가 참조하는 관리 부모 보존:**
   - **수동 대시보드 → 관리 차트:** 삭제 대상이 아닌 대시보드(수동 또는 타 대시보드)가 참조하는 관리 차트는 삭제하지 않고 보존 처리한다(`M_dash`에서 차트가 사라지는 것을 방지).
   - **수동 차트(또는 보존 차트) → 관리 데이터셋:** 삭제 대상이 아닌 차트가 참조하는 관리 데이터셋 `ds`는 삭제하지 않고 보존 처리한다(`M_chart.datasource_id = None` 고아화 방지).
   - **수동 데이터셋(또는 보존 데이터셋) → 관리 DB:** 삭제 대상이 아닌 데이터셋이 소속된 관리 DB는 삭제하지 않고 보존 처리한다(`DELETE /api/v1/database/<id>` 422 차단 방지).
   - **실패 vs 보존 구분:** 수동 의존성으로 보존한 객체는 `leftovers`나 `SyncError`로 취급하지 않고(재실행 영구 실패 방지), 보존 이유와 객체 UUID를 로깅한다.
2. **부분 실패 시 역방향 의존성 불변식 유지:**
   - 관리 대시보드 `D` 삭제가 실패하면 `D`가 참조하는 차트 삭제를 건너뛰고 `leftovers`에 남긴다.
   - 관리 차트 `C` 삭제가 실패하면 `C`의 데이터셋 삭제를 건너뛰고 `leftovers`에 남긴다.
   - 관리 데이터셋 `DS` 삭제가 실패하면 `DS`의 DB 삭제를 건너뛰고 `leftovers`에 남긴다.
   - 어느 시점에 중단되고 `.state/managed.json`이 소실돼도 남아 있는 모든 관리 객체는 `dashkit-<p> → dataset → chart → dashboard` 경로로 재도달 가능하다.

### 4-4. 4단계 — `make plan` 및 명시적 운영 복구 CLI

1. **명시적 정리 명령 (`python -m dashkit prune-project <이름> [--dry-run]`):**
   - `tools` 컨테이너 전용이며 `backend`/웹 API에는 열지 않는다.
   - 프로젝트 이름 정규식(`^[a-z0-9][a-z0-9_-]*$`)을 검증한다.
   - 현재 활성 프로젝트(`.state/embed.json`의 `project`, `dashkit_meta.load_snapshot.project`, `PROJECT` 환경변수)는 삭제를 거부한다.
   - 4-2·4-3절과 동일한 `uuid5` 검증·`_made_by_sync`·수동 의존성 보존 규칙을 적용하며, `--dry-run`으로 삭제·보존 대상을 미리 본다.
2. **`dashkit plan` 정합성 ([`dashkit/plan.py`](../dashkit/plan.py)):**
   - `managed.json`이 없어도 `embed.json`의 `project` 또는 `dashkit_meta`의 `other_load`(`!= project.name`)가 감지되면 `old_files = None`(`first: true`)으로 취급하고, `superset.replaces`에 이전 프로젝트 전환 사실(`objects: null`)을 알린다(Superset 접속은 기존 계약대로 하지 않는다).

---

## 5. 모듈별 변경 범위

| 파일 | 변경 내용 |
|---|---|
| [`dashkit/loader/__init__.py`](../dashkit/loader/__init__.py) | `_rebuild()` 트랜잭션에서 `record.write()` 전 기존 `load_snapshot` 조회(savepoint) 후 `replaced_projects` 계산·전달 |
| [`dashkit/loader/record.py`](../dashkit/loader/record.py) | `snapshot()`에 선택 인자 `replaced_projects` 추가(하위 호환 유지) |
| [`dashkit/loader/current.py`](../dashkit/loader/current.py) | `LoadRecord`에 `replaced_projects` 프로퍼티 또는 스냅샷 경량 조회 헬퍼 추가(`dash_reader` 전용) |
| [`dashkit/sync/__init__.py`](../dashkit/sync/__init__.py) | ① `dash_reader`로 `replaced_projects` 읽기(주입 가능, `view_columns` 주입 단위 테스트 호환)<br>② `embed.json` 덮어쓰기 전 이전 프로젝트 읽기<br>③ `find_marked` 일반화 + `plan_prune()` 순수 함수(수동 의존성 보존·역방향 실패 전파 차단)<br>④ 미정리 타 `dashkit-*` DB 비파괴 경고 로그 |
| [`dashkit/sync/client.py`](../dashkit/sync/client.py) | 필요 시 DB 상세 조회/`list_objects` 컬럼 정리(기존 방어적 재검증 유지) |
| [`dashkit/__main__.py`](../dashkit/__main__.py) | `prune-project <이름> [--dry-run]` 하위 명령 추가 |
| [`dashkit/plan.py`](../dashkit/plan.py) | `managed.json` 부재 시 `embed.json`/`dashkit_meta` 기반 프로젝트 전환 표시 |
| [`contracts/state-and-uuid.md`](../contracts/state-and-uuid.md) | 상태 파일 없는 전환 정리 지원 범위, 수동/타 프로젝트 보존 경계, 실패·중단 수렴, 운영 복구 절차 기록 |
| [`contracts/superset-notes.md`](../contracts/superset-notes.md) | **16절**에 격리 스택 실측 결과(REST 응답 컬럼, 수동 객체 참조 시 삭제 응답·연쇄 여부, guest 권한 경계) 기록 |
| [`contracts/plan.md`](../contracts/plan.md) | `dashkit_meta.load_snapshot.replaced_projects` 및 `superset.replaces` 동작 기록 |

> 변경하지 않는 것: [`dashkit/sync/bundle.py`](../dashkit/sync/bundle.py) 및 `tests/golden/` 스냅샷(0바이트 diff), [`superset/init_guest_role.py`](../superset/init_guest_role.py)(`EmbedGuest` 권한 4개 유지), [`docker-compose.yml`](../docker-compose.yml) 계정·비밀번호 분리.

---

## 6. 격리 스택 재현·실측 및 구현 순서

1. **격리 워크트리 및 테스트 스택 준비:** `origin/main` 기준 별도 worktree와 `dashkit-test-<WT_ID>` 스택을 사용한다(기존 개발 스택과 다른 worktree를 건드리지 않는다).
2. **현행 `origin/main` 재현 및 Superset 6.1.0 실측 (선행 필수):**
   - **실측 1 (REST 응답 필드):** `database`, `dataset`, `chart`, `dashboard` 목록·단건 응답의 `sqlalchemy_uri`, `table_name`, `schema`, `database.id`, `uuid`, `is_managed_externally` 확인.
   - **실측 2 (현행 동작 재현):** 동일 프로젝트 동기화(상태 파일 없음), `managed.json` 단독 삭제 후 전환, `.state/` 전체 삭제 후 전환의 잔존 객체·임베드 수 기록.
   - **실측 3 (수동 객체 의존성·삭제 제약):**
     - `3a`: 관리 데이터셋 참조 수동 차트 존재 시 `DELETE /api/v1/dataset/<id>` 응답과 수동 차트 생존 여부.
     - `3b`: 관리 차트 포함 수동 대시보드 존재 시 `DELETE /api/v1/chart/<id>` 응답과 수동 대시보드·연결 생존 여부.
     - `3c`: 관리 DB 아래 수동 데이터셋 존재 시 `DELETE /api/v1/database/<id>` 응답과 수동 데이터셋 생존 여부.
     - `3d`: 이름이 `dashkit-<이름>`인 수동 DB의 UUID v4 및 `object_uuid` 불일치 확인.
   - **실측 4 (Guest 권한 경계):** 삭제된 대시보드 임베드 접근, 새 대시보드 guest token으로 잔존 이전 대시보드·보존된 이전 데이터셋 접근 차단(404/403) 확인.
3. **구현 확정 게이트:** 실측 결과로 3절의 5개 미확정 항목을 확정한 뒤 후보 D를 구현한다(식별·보호 불가가 드러나면 해당 범위는 비삭제 경계로 축소).
4. **단위·통합 테스트 및 계약 문서 반영:** `make test-unit`과 `make test`를 통과시키고 실측표·운영 복구 절차를 계약에 반영한다.

---

## 7. 테스트·검증 계획

### 단위 테스트 (`make test-unit`, 스택 불필요)

- **`replaced_projects` 계산:** 최초 적재, 동일 프로젝트 재적재, `A → B` 전환, `A → B → C` 누적, `A → B → A` 왕복(현재 프로젝트 제외), 구버전 스냅샷(`replaced_projects` 없음) 호환.
- **`plan_prune()` 순수 로직:** `managed.json` 없음 + `embed.json`/`dashkit_meta` 힌트, 둘 다 없음(타 `dashkit-*` DB 미삭제 + 경고), 수동 DB(UUID v4) 보존, 타 프로젝트(`prev_projects` 밖) 보존, 수동 차트·데이터셋·대시보드(실측 확정 정책)에 따른 관리 부모 보존, `SYNC_LOCK_UI=0` 식별, 삭제 실패 시 상위 부모 건너뛰기 및 `leftovers` 기록, `slug` 접두사 충돌 오삭제 없음.
- **`plan.py` 및 `prune-project` CLI:** 활성 프로젝트 삭제 거부, 잘못된 프로젝트 이름 거부, `--dry-run` 무변경.
- **도메인 단어 검사:** [`tests/test_domain_words.py`](../tests/test_domain_words.py) 통과(`dashkit/` 내 예시 도메인 단어 혼입 금지).

### 격리 스택 통합 테스트 (`make test`, 실제 Superset 6.1.0 + Postgres)

| 시나리오 | 검증 항목 |
|---|---|
| **프로젝트 왕복 (`A → B → A`)** | 매 전환 전 `managed.json` 단독 삭제·`.state/` 전체 삭제 후 `apply`. 이전 프로젝트 DB·데이터셋·차트·대시보드·임베드 행 0건, 복귀 후 동일 UUID·렌더링 확인 |
| **동일 프로젝트 동기화 & `SYNC_LOCK_UI=0`** | 상태 파일 없는 동일 프로젝트 차트·데이터셋 제거 및 `SYNC_LOCK_UI=0` 전환 정리 확인 |
| **중단·재실행** | `load` 후 `sync` 중단·상태 파일 삭제 후 재실행 수렴, 대시보드 삭제 실패 주입 시 하위 차트 보존 및 재실행 완료 확인 |
| **수동 객체 보존** | 별도 수동 객체, `dashkit-<이전>` 수동 DB(UUID v4), 이전 관리 데이터셋·차트·DB 위 수동 차트·대시보드·데이터셋 보존 및 `sync` 정상 완료 확인 |
| **타 프로젝트 보존** | [`tests/test_sync.py`](../tests/test_sync.py) (`zz-synctest` 시 `devtracker` 무손상) 및 `prev_projects` 밖 `dashkit-*` 프로젝트 보존 + 경고 확인 |
| **권한 경계** | `sync`·`plan`의 `dash_reader` 전용 동작(owner 미사용), `dash_reader`의 `dashkit_meta` 쓰기 차단, `EmbedGuest` 권한 4개 유지, 새 guest token의 이전/보존 객체 접근 404/403 확인 |

---

## 8. 완료 조건 체크리스트

- [ ] 격리 스택의 현행 `origin/main`에서 상태 파일 없는 프로젝트 전환, 동일 프로젝트 동기화, Superset 수동 객체 및 삭제 제약(FK/연쇄)을 실측·기록했다.
- [ ] 프로젝트 범위(`dashkit_meta` + `embed.json`)와 객체 소유(`uuid5` + 표식 + 수동 의존성 보존 + 역방향 실패 전파 차단) 경계를 확정하고, 식별 불가 시 비삭제 원칙을 검증했다.
- [ ] 프로젝트 왕복, 상태 파일 손실, 중단·재실행, DB/guest 권한 경계, 수동·타 프로젝트 객체 보존 단위·통합 테스트가 통과한다(`make test-unit`, `make test`).
- [ ] 지원 범위, 미지원/비삭제 경계, `python -m dashkit prune-project` 운영 복구 절차를 [`contracts/state-and-uuid.md`](../contracts/state-and-uuid.md), [`contracts/superset-notes.md`](../contracts/superset-notes.md)(16절), [`contracts/plan.md`](../contracts/plan.md)에 기록했다.
