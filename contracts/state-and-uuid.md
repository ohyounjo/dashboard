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

### sync가 만든 객체를 Superset에서 찾는 규칙 (`dashkit/sync/__init__.py`의 `find_marked`)

`.state/managed.json` 없이 prune 대상을 구할 때 쓴다. 목록 API는 표식으로 거를 수 없고 `external_url`을 읽을 수 없어서, 프로젝트는 데이터베이스에서 따라간다.

1. 데이터베이스: 이름이 `dashkit-<프로젝트>`이고 UUID가 `uuid5(<프로젝트>/database/main)`인 것. 없으면 찾은 것이 없는 것으로 한다.
2. 데이터셋: 그 데이터베이스에 속한 것.
3. 차트: 2의 데이터셋을 쓰는 것(`datasource_type: table`).
4. 대시보드: 3의 차트가 놓인 것.
5. 2~4 가운데 **`is_managed_externally`가 true이거나 UUID가 버전 5인 것**만 "sync가 만든 객체"다. Superset 화면이나 REST로 만든 객체는 표식이 없고 UUID가 버전 4라서 들어가지 않는다. 다른 데이터베이스의 객체(다른 프로젝트, 사람이 만든 것)는 1에서 걸러진다.

UUID 버전 조건은 `SYNC_LOCK_UI=0`으로 만든 객체와, 잠금 값을 바꾸는 사이에 설정에서 빠진 객체를 놓치지 않기 위한 것이다.

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

sync만 읽고 쓴다. 지난 sync가 만든 Superset 객체 목록이다. 다음 sync에서 이번 설정에 없는 UUID를 지운다(prune). 표식으로 찾는 방법(위 "표식")이 생긴 뒤로는 보조 수단이다. 이 파일만 할 수 있는 일은 **다른 프로젝트로 바꿨을 때 이전 프로젝트의 객체를 지우는 것**과 prune에 실패한 객체를 다음 실행에 다시 시도하는 것이다.

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

1. 설정을 읽고 뷰의 열 정보를 `information_schema`에서 읽는다(dash_reader).
2. 번들을 `.build/<프로젝트>/bundle.zip`에 만든다. 같은 내용을 풀어 둔 `.build/<프로젝트>/bundle/`도 남겨 diff로 볼 수 있게 한다. import에 성공하면 같은 내용을 `.build/<프로젝트>/applied/`에도 남긴다. `dashkit plan`이 새 번들과 비교하는 "지난 번들"은 이 `applied/`다([plan.md](plan.md). plan은 이 폴더와 상태 파일을 읽기만 한다). `bundle/`은 import 전에 쓰므로 실패한 sync의 것일 수 있다. 파일 내용은 결정적(키 정렬, 타임스탬프는 metadata.yaml 하나만)이어야 한다.
3. 관리자 계정으로 로그인(`/api/v1/security/login`, CSRF 토큰)한다.
4. `POST /api/v1/assets/import/`(overwrite). 실패하면 응답 앞부분과 함께 "Superset 로그를 확인하세요(`make logs`)"를 출력하고 종료 코드 1로 끝낸다.
5. 대시보드마다 `POST /api/v1/dashboard/<id>/embedded {"allowed_domains": WEB_ORIGINS}`를 부른다.
6. prune: **(`managed.json`의 이전 목록 ∪ Superset에서 찾은 이 프로젝트의 sync 객체) − 이번 목록**을 대시보드 → 차트 → 데이터셋 → DB 순으로 UUID로 찾아 지운다. 이미 없으면 넘어간다. 뒤쪽 집합은 위 "sync가 만든 객체를 Superset에서 찾는 규칙"으로 구하고 `managed.json`이 없어도 된다. 표식이 없는 객체와 다른 데이터베이스의 객체는 대상이 아니다.
7. `embed.json`과 `managed.json`을 원자적으로 쓴다.

보강(구현 반영):
- 4단계 직전에 `managed.json`을 (이전 목록 ∪ 이번 목록)으로 한 번 먼저 쓴다. import가 중간에 실패해 일부 객체만 생겨도 다음 실행이 prune 대상으로 알 수 있다.
- `embed.json`은 5단계가 끝나면 prune 전에 쓴다.
- prune에서 삭제에 실패한 UUID는 `managed.json`에 남기고 SyncError로 끝낸다(다음 실행이 다시 시도).

멱등성: 같은 설정으로 두 번 실행하면 Superset 객체 집합, UUID, 임베드 UUID가 같다.

`managed.json`이 없을 때(`.state/`를 지웠거나 다른 곳에서 체크아웃한 경우): 이 프로젝트의 이전 객체는 표식으로 찾아 지운다. **다른 프로젝트**가 남긴 객체는 찾지 않으므로 남는다(그 프로젝트로 한 번 sync한 뒤 돌아오거나, `make reset`으로 Superset 메타데이터를 처음부터 다시 만든다).
