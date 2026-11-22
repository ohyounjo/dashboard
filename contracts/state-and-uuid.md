# 상태 파일과 Superset 객체 UUID

## UUID 규칙

```python
NAMESPACE = uuid.UUID("867a0be0-cbd2-5438-8e90-40ddf6d53a0d")
# = uuid5(NAMESPACE_URL, "https://dashkit.invalid/superset-objects"). 바꾸지 않는다.
def object_uuid(project: str, kind: str, key: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{project}/{kind}/{key}"))
```

| kind | key | 예 |
|---|---|---|
| `database` | `main` (프로젝트당 하나) | `devtracker/database/main` |
| `dataset` | `datasets`의 키 | `devtracker/dataset/issues` |
| `chart` | 차트 `key`(프로젝트 안에서 유일) | `devtracker/chart/kpi_progress` |
| `dashboard` | `dashboards`의 키 | `devtracker/dashboard/overview` |

구현은 `dashkit/config.py`의 `object_uuid` 하나뿐이다. sync와 테스트가 이 함수를 쓴다.

네이티브 필터 id는 UUID가 아니라 `NATIVE_FILTER-<필터 키>`다(대시보드 안에서 유일).

## 표식("외부에서 관리됨")

sync가 만드는 데이터베이스·데이터셋·차트·대시보드의 번들 YAML 끝에 두 키를 쓴다(`dashkit/sync/bundle.py`의 `managed_mark`). 근거는 `superset-notes.md` 11절이다.

```yaml
is_managed_externally: true                         # SYNC_LOCK_UI=0이면 false
external_url: dashkit://<프로젝트>/<종류>/<키>        # UUID 규칙의 이름과 같다. 예: dashkit://devtracker/chart/kpi_progress
```

| 키 | 하는 일 |
|---|---|
| `is_managed_externally: true` | Superset 화면이 그 객체의 편집을 막는다(대시보드 Edit 버튼 없음, 차트 덮어쓰기·데이터셋·DB 저장 비활성). REST와 import는 막지 않는다. prune이 "sync가 만든 객체"를 알아보는 표식이기도 하다 |
| `external_url` | 어느 설정의 어느 항목에서 나왔는지의 기록이다. Superset 메타데이터 DB에 저장되지만 REST로는 읽을 수 없어 sync는 이 값에 기대지 않는다 |

- 두 키는 **항상 쓴다.** import는 번들에 없는 키의 값을 그대로 두므로, 잠금을 풀 때도 `false`를 명시해야 한다.
- 두 키는 export에 나오지 않는 import 전용 키다(`superset-6.1.0-export/managed/import-only-keys.yaml`).

### `SYNC_LOCK_UI` (보강설계서 7절 D3)

sync(tools 컨테이너)가 읽는 환경변수다. 기본은 `1`(화면 편집 차단)이다.

| 값 | 번들 | Superset 화면 | 상태 파일 없는 prune이 기대는 것 |
|---|---|---|---|
| `1`(기본, 빈 값 포함) | `is_managed_externally: true` | 관리 객체 편집 차단 | 프로젝트 DB에서 따라간 객체 중 표식이 있는 것 |
| `0`(`false`, `no`, `off`도 같음) | `is_managed_externally: false` | 편집 가능(고친 내용은 다음 sync에서 덮어써진다) | 프로젝트 DB에서 따라간 객체 중 **UUID가 버전 5**인 것 |

`external_url`은 두 경우 모두 같다. 값을 바꾼 뒤 `make sync`(또는 `make apply`)를 다시 돌리면 이번 목록의 객체가 모두 새 값으로 바뀐다.

### sync가 만든 객체를 Superset에서 찾는 규칙 (`dashkit/sync/__init__.py`의 `discover_project_objects` · `find_marked`)

`.state/managed.json` 없이 prune 대상을 구할 때 쓴다. 목록 API는 표식으로 거를 수 없고 `external_url`을 읽을 수 없어서, 프로젝트는 데이터베이스에서 따라간다(`superset-notes.md` 11·16절).

1. 데이터베이스: 이름이 `dashkit-<프로젝트>`이고 UUID가 `uuid5(<프로젝트>/database/main)`인 것. 없으면 찾은 것이 없는 것으로 한다.
2. 데이터셋: 1의 데이터베이스에 속하고 `schema == "dash"`이며 UUID가 `uuid5(<프로젝트>/dataset/<table_name>)`인 것(`SYNC_LOCK_UI=0` 포함). 같은 DB에 사람이 추가한 데이터셋은 UUID가 버전 4라서 들어가지 않는다.
3. 차트: 2의 데이터셋을 쓰는 것(`datasource_type: table`) 중 `_made_by_sync`(`is_managed_externally`가 true이거나 UUID가 버전 5)인 것.
4. 대시보드: 3의 차트가 놓인 것 중 `_made_by_sync`이면서 `slug`가 `<프로젝트._slug>-<키._slug>` 형식이고 `uuid`가 `uuid5(<프로젝트>/dashboard/<키>)`와 일치하는 것. `SYNC_LOCK_UI=0` 등에서 다른 프로젝트의 관리 대시보드에 이 프로젝트의 차트가 놓였거나 `slug`가 바뀌어 소유를 확인할 수 없는 대시보드는 대상에서 제외해 보존한다(그리고 그 대시보드가 참조하는 차트·데이터셋·DB도 연쇄 보존한다).

Superset 화면이나 REST로 만든 객체는 UUID가 버전 4라서 1~4에 들어가지 않는다. 다른 프로젝트의 객체는 1에서 구분된다.

### 수동 의존성 보존과 역방향 실패 전파 차단 (`plan_prune`)

Superset 6.1.0에서는 수동 차트가 붙은 데이터셋이나 수동 대시보드가 포함하는 차트를 지우면 `DELETE`가 HTTP 200으로 성공하며 수동 객체가 고아화(`datasource_id=None`)되거나 차트 목록이 비워진다. 수동 데이터셋이 붙은 DB 삭제는 HTTP 422로 거절된다(`superset-notes.md` 16-3절). 따라서 `plan_prune()`은 삭제 실행 전에 아래 순서를 거꾸로 검사해 보존한다.

1. 삭제 대상이 아닌 대시보드(수동 또는 유지 대시보드)가 참조하는 관리 차트는 **보존**한다.
2. 삭제 대상이 아닌 차트(수동 차트, 1에서 보존된 차트, 현재 차트)가 참조하는 관리 데이터셋은 **보존**한다.
3. 삭제 대상이 아닌 데이터셋(수동 데이터셋, 2에서 보존된 데이터셋, 현재 데이터셋)이 소속된 관리 DB는 **보존**한다.

수동 의존성으로 보존한 객체는 오류가 아니므로 `managed.json`의 `leftovers`에 남기지 않고 로그로 알린다. 반대로 삭제 시도 중 실패가 나면 그 객체의 부모(대시보드 실패 → 차트 → 데이터셋 → DB) 삭제를 건너뛰고 `managed.json`에 남겨, 중단 뒤 상태 파일이 사라지더라도 `DB → 데이터셋 → 차트 → 대시보드` 계보가 끊어지지 않게 한다.

## `.state/embed.json`

sync가 쓰고 backend가 읽는다(읽기 전용 마운트). 쓰기는 임시 파일에 쓴 뒤 `os.replace`로 원자적으로 바꾼다.

```json
{
  "version": 1,
  "project": "devtracker",
  "synced_at": "2026-11-19T08:30:00Z",
  "dashboards": {
    "overview": { "dashboard_uuid": "…", "embed_uuid": "dbb16ffb-…" }
  }
}
```

- `embed_uuid`는 Superset이 만든다(`POST /api/v1/dashboard/<id>/embedded`, upsert라서 다시 불러도 유지된다).
- **backend는 이 파일의 `project`를 현재 프로젝트로 따른다**(마지막으로 sync에 성공한 프로젝트. 환경변수 `PROJECT`와 달라도 된다). 파일이 없거나 깨졌거나, 그 프로젝트 폴더가 없거나 설정을 읽을 수 없으면 환경변수 `PROJECT`를 쓰고 임베드는 없는 것으로 취급한다(`embed_id: null`, guest-token 409). 규칙은 [api.md](api.md)의 "현재 프로젝트".
- 그래서 sync에 성공하는 것이 곧 "웹앱이 이 프로젝트를 보여 준다"이다. `PROJECT=<다른 이름> make apply`와 Studio의 적용이 같은 방식으로 프로젝트를 바꾼다. sync가 실패하면 이 파일은 그대로이고 웹앱도 이전 프로젝트를 계속 보여 준다.

## `.state/managed.json`

sync만 읽고 쓴다. 지난 sync가 만든 Superset 객체 목록이다. 다음 sync에서 이번 설정에 없는 UUID를 지운다(prune). 상태 파일이 없을 때는 아래 순서로 이전 프로젝트를 식별한다.

1. **같은 프로젝트의 빠진 객체:** `discover_project_objects`로 현재 프로젝트의 관리 객체를 찾아 이번 설정에 없는 것을 지운다.
2. **프로젝트 전환 (`A → B`):**
   - `managed.json`이 없으면, 5단계에서 `embed.json`을 덮어쓰기 전에 읽은 `embed.json`의 이전 `project`와 `data-db`의 적재 기록(`dashkit_meta.load_snapshot.replaced_projects`, 현재 프로젝트 `B`로 적재된 스냅샷일 때만 신뢰, `dash_reader`로만 조회)에서 교체된 이전 프로젝트 목록을 구한다. 이 목록은 `embed.json`을 덮어쓰기 전에 `.state/managed.json`의 선택 키 `pending_projects`에도 함께 기록해, `fetch_snapshot`·DB 연결 확인이 일시 실패하거나 수동 참조로 이전 객체가 보존된 경우 다음 실행에서 재탐색할 수 있게 하고 모두 정리되면 비운다.
   - 각 이전 프로젝트 DB(`dashkit-<이전>`)는 `uuid5` 일치에 더해 `GET /api/v1/database/<id>/connection`의 대상 `(host, port, dbname)`이 현재 `DATA_DB_*`와 일치할 때만 자동 정리 대상으로 삼는다(불일치·조회 실패 시 비삭제 및 경고).
3. **자동 정리하지 않는 경계 (안전 우선):**
   - `.state/`와 `data-db` 전환 계보가 **모두** 없으면(`make reset` 직후 또는 `load` 없이 실행한 공존 프로젝트 등) Superset의 다른 `dashkit-*` DB를 자동 삭제하지 않고 비파괴 경고 로그만 남긴다.
   - `SYNC_LOCK_UI=0`으로 관리 차트를 수동으로 모두 뺀 뒤 빈 상태가 된 대시보드나, 외부·타 스택 DB를 가리키는 관리 DB는 `slug` 추측 등으로 지우지 않는다.

```json
{
  "version": 1,
  "project": "devtracker",
  "synced_at": "2026-11-19T08:30:00Z",
  "objects": {
    "database": ["…"],
    "dataset": ["…"],
    "chart": ["…"],
    "dashboard": ["…"]
  }
}
```

## sync 순서 (plan 6-2절 3번)

1. 설정을 읽고 뷰의 열 정보를 `information_schema`에서 읽고(`dash_reader`), `dashkit_meta.load_snapshot`이 현재 프로젝트의 것이면 `replaced_projects`를 함께 읽는다.
2. 번들을 `.build/<프로젝트>/bundle.zip`에 만든다. 같은 내용을 풀어 둔 `.build/<프로젝트>/bundle/`도 남겨 diff로 볼 수 있게 한다. import에 성공하면 같은 내용을 `.build/<프로젝트>/applied/`에도 남긴다. `dashkit plan`이 새 번들과 비교하는 "지난 번들"은 이 `applied/`다([plan.md](plan.md). plan은 이 폴더와 상태 파일을 읽기만 한다). `bundle/`은 import 전에 쓰므로 실패한 sync의 것일 수 있다. 파일 내용은 결정적(키 정렬, 타임스탬프는 metadata.yaml 하나만)이어야 한다.
3. 관리자 계정으로 로그인(`/api/v1/security/login`, CSRF 토큰)한다.
4. `POST /api/v1/assets/import/`(overwrite). 실패하면 응답 앞부분과 함께 "Superset 로그를 확인하세요(`make logs`)"를 출력하고 종료 코드 1로 끝낸다.
5. 대시보드마다 `POST /api/v1/dashboard/<id>/embedded {"allowed_domains": WEB_ORIGINS}`를 부른다. 그 직전에 `embed.json`에 남아 있던 이전 `project`를 읽어 둔 뒤, 새 `embed.json`을 원자적으로 쓴다.
6. prune: **(`managed.json`의 이전 목록 ∪ 현재 프로젝트의 관리 객체 ∪ 검증된 이전 프로젝트의 관리 객체) − 이번 목록 − 수동 의존 보존**을 대시보드 → 차트 → 데이터셋 → DB 순으로 UUID로 찾아 지운다. 이미 없으면 넘어간다. 하위 삭제에 실패하면 상위 부모 삭제는 건너뛴다.
7. `managed.json`을 `(이번 목록 ∪ prune 실패 목록)`으로 원자적으로 쓴다.

보강(구현 반영):
- 4단계 직전에 `managed.json`을 (이전 목록 ∪ 이번 목록)으로 한 번 먼저 쓴다. import가 중간에 실패해 일부 객체만 생겨도 다음 실행이 prune 대상으로 알 수 있다.
- `embed.json`은 5단계가 끝나면 prune 전에 쓴다.
- prune에서 삭제에 실패한 UUID는 `managed.json`에 남기고 SyncError로 끝낸다(다음 실행이 다시 시도).

멱등성: 같은 설정으로 두 번 실행하면 Superset 객체 집합, UUID, 임베드 UUID가 같다.

### 운영 복구 방법 (상태 파일과 적재 계보가 모두 없을 때)

- **권장 — 명시적 이전 프로젝트 정리 CLI (`tools` 전용):**
  ```bash
  docker compose --env-file .env.example --env-file .env run --rm tools python -m dashkit prune-project <이전_프로젝트> --dry-run
  docker compose --env-file .env.example --env-file .env run --rm tools python -m dashkit prune-project <이전_프로젝트>
  ```
  `prune-project`는 현재 활성 프로젝트(`.state/embed.json`, `dashkit_meta.load_snapshot.project`, 둘 다 없으면 `PROJECT` 환경변수)는 거부하고, 동일한 `uuid5`·`_made_by_sync`·수동 의존성 보존 규칙으로 지정한 이전 프로젝트의 관리 객체만 정리한다.
- **수동 객체 때문에 부모가 보존된 경우:** Superset 화면에서 해당 수동 차트·대시보드·데이터셋을 정리한 뒤 `make sync`(또는 `prune-project`)를 다시 실행하면 남아 있던 이전 부모 객체가 정리된다.
- **전체 초기화(최후 수단):** 인증 DB·적재 데이터·수동 Superset 객체를 모두 지워도 되는 개발 환경에서만 `make reset CONFIRM=1`을 사용한다(운영 볼륨이 있는 환경에서는 사용하지 않는다).
