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
- backend는 `project`가 현재 `PROJECT`와 다르면 없는 것으로 취급한다(`embed_id: null`, guest-token 409).

## `.state/managed.json`

sync만 읽고 쓴다. 지난 sync가 만든 Superset 객체 목록이다. 다음 sync에서 이번 설정에 없는 UUID를 지운다(prune).

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
2. 번들을 `.build/<프로젝트>/bundle.zip`에 만든다. 같은 내용을 풀어 둔 `.build/<프로젝트>/bundle/`도 남겨 diff로 볼 수 있게 한다. 파일 내용은 결정적(키 정렬, 타임스탬프는 metadata.yaml 하나만)이어야 한다.
3. 관리자 계정으로 로그인(`/api/v1/security/login`, CSRF 토큰)한다.
4. `POST /api/v1/assets/import/`(overwrite). 실패하면 응답 앞부분과 함께 "Superset 로그를 확인하세요(`make logs`)"를 출력하고 종료 코드 1로 끝낸다.
5. 대시보드마다 `POST /api/v1/dashboard/<id>/embedded {"allowed_domains": WEB_ORIGINS}`를 부른다.
6. prune: `managed.json`의 (이전 목록 − 이번 목록)을 대시보드 → 차트 → 데이터셋 → DB 순으로 UUID로 찾아 지운다. 이미 없으면 넘어간다.
7. `embed.json`과 `managed.json`을 원자적으로 쓴다.

보강(구현 반영):
- 4단계 직전에 `managed.json`을 (이전 목록 ∪ 이번 목록)으로 한 번 먼저 쓴다. import가 중간에 실패해 일부 객체만 생겨도 다음 실행이 prune 대상으로 알 수 있다.
- `embed.json`은 5단계가 끝나면 prune 전에 쓴다.
- prune에서 삭제에 실패한 UUID는 `managed.json`에 남기고 SyncError로 끝낸다(다음 실행이 다시 시도).

멱등성: 같은 설정으로 두 번 실행하면 Superset 객체 집합, UUID, 임베드 UUID가 같다. 첫 실행이고 `managed.json`이 없으면 prune을 하지 않는다(`make reset`으로 Superset 메타데이터를 처음부터 다시 만들 수 있다).
