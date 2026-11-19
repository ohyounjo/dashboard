# 백엔드 API 계약 (`dashkit/api`, FastAPI)

- 브라우저는 웹앱과 같은 출처(`/api/...`, nginx 프록시)로만 부른다. CORS는 없다.
- 모든 응답은 `Content-Type: application/json; charset=utf-8`, `Cache-Control: no-store`.
- 오류 본문은 `{"detail": "<영문 코드>", "message": "<사람이 읽는 설명>"}`. 웹앱은 `detail`로 분기하고 `message`는 표시하지 않는다(문구는 `web/strings.js`).
- 공통 오류 코드: 없는 경로 404 `not_found`, 허용하지 않는 메서드 405 `method_not_allowed`, 처리하지 못한 오류 500 `internal_error`.
- OpenAPI 문서 경로(`/docs`, `/openapi.json`)는 만들지 않는다.
- 설정은 프로세스 시작 때 `dashkit.config.load_project(PROJECT)`로 읽는다. 위젯 매니페스트(`WIDGETS_DIR`, 기본 `/app/web/widgets`)도 이때 읽는다. 설정을 바꾸면 `make apply`가 backend를 재시작한다.
- `.state/embed.json`은 **요청마다** 파일 mtime을 확인해 바뀌었으면 다시 읽는다. `.state/`는 디렉터리째 읽기 전용으로 마운트한다(원자적 rename을 반영하려면 파일 단위 마운트를 쓰면 안 된다).

## GET /api/app

웹앱이 시작할 때 한 번 부른다.

```json
{
  "project": "devtracker",
  "app": {
    "title": "…",
    "logo_text": "…",
    "as_of": "2026-11-19",
    "theme": { "primary": { "light": "#1428A0", "dark": "#7B8CFF" } }
  },
  "superset": { "url": "http://localhost:8088" },
  "applied": true,
  "widgets": [ { "type": "gantt", "label": "…" }, { "type": "kanban", "label": "…" } ],
  "tabs": [
    {
      "key": "overview", "label": "…", "icon": "grid",
      "sections": [
        { "type": "superset", "title": null, "dashboard": "overview", "embed_id": "dbb16ffb-…", "height": null }
      ]
    },
    {
      "key": "schedule", "label": "…", "icon": "calendar",
      "sections": [
        { "type": "gantt", "title": "…", "dataset": "…", "fields": { "label": "…", "start": "…", "end": "…", "progress": "…", "status": "…" },
          "statuses": { "<값>": { "label": "…", "icon": "…", "tone": "good" } }, "range": null, "sort": null },
        { "type": "kanban", "title": "…", "dataset": "…", "group_by": "…",
          "columns": [ { "value": "…", "label": "…", "icon": null, "tone": null } ],
          "card": { "title": "…", "chips": ["…"] }, "filter": { "column": "…", "label": "…" } }
      ]
    }
  ]
}
```

- `app.as_of`: `"today"`이면 요청 시점 날짜(`TZ` 환경변수)로 바꿔 돌려준다. 항상 `YYYY-MM-DD`.
- `app.logo_text`, `app.theme`: 설정에 없으면 `null`. 웹앱은 `null`이면 기본 토큰을 쓴다.
- `superset.url`: 환경변수 `SUPERSET_PUBLIC_URL`(브라우저가 접속할 주소). 비어 있으면 `null`.
- 섹션의 선택 필드는 설정에 없으면 `null`로 채워 **키가 항상 있다**.
- `embed_id`: `.state/embed.json`에서 찾은 임베드 UUID. 없거나 embed.json의 `project`가 현재 프로젝트와 다르면 `null`.
- `applied`: 모든 superset 섹션의 `embed_id`가 있으면 `true`.
- `widgets`: 위젯 매니페스트(`web/widgets/<유형>/widget.json`, [widgets.md](widgets.md))에서 읽은 유형 목록(이름순). 이 프로젝트가 쓰지 않는 유형도 들어 있다. 웹앱은 섹션의 `type`이 이 목록의 `type`과 정확히 같을 때만 `./widgets/<type>/widget.js`를 불러온다. `superset`은 위젯이 아니므로 목록에 없다.
- 위젯 섹션의 키는 유형의 매니페스트가 정한다(공통 키 `type`, `title` + 옵션). 위 예의 `gantt`, `kanban`은 저장소에 들어 있는 두 유형이다.

## POST /api/guest-token

요청 `{"dashboard": "<dashboards 키>"}` → 200

```json
{ "token": "<JWT>", "expires_at": "2026-11-19T08:30:00Z" }
```

| 상황 | 상태 | detail |
|---|---|---|
| 본문 형식 오류 | 422 | `invalid_request` |
| 설정에 없는 키 | 404 | `unknown_dashboard` |
| 설정에는 있지만 embed.json에 없음(apply 전) | 409 | `not_applied` |
| `GUEST_TOKEN_JWT_SECRET` 또는 `GUEST_TOKEN_JWT_AUDIENCE` 미설정(시작 시 경고 로그) | 500 | `token_unavailable` |

JWT(HS256, 키 `GUEST_TOKEN_JWT_SECRET`):

```json
{
  "user": { "username": "embed-viewer", "first_name": "Embed", "last_name": "Viewer" },
  "resources": [ { "type": "dashboard", "id": "<embed_id>" } ],
  "rls_rules": [],
  "iat": 1795000000,
  "exp": 1795000300,
  "aud": "<GUEST_TOKEN_JWT_AUDIENCE>",
  "type": "guest"
}
```

- `resources`에는 요청한 대시보드 **하나만** 넣는다. 수명은 300초(`exp - iat`).
- backend는 Superset 계정·비밀번호를 갖지 않고 Superset에 요청하지도 않는다.

## GET /api/data/{dataset}

위젯 섹션(매니페스트에 `dataset` 옵션이 있는 유형)이 `dataset`으로 참조한 데이터셋만 허용한다. 나머지는 404 `unknown_dataset`(설정에 있는 데이터셋이어도 위젯이 참조하지 않으면 404).

```json
{
  "columns": [ { "name": "id", "type": "text" }, { "name": "start_date", "type": "date" } ],
  "rows": [ { "id": "…", "start_date": "2026-08-03" } ],
  "truncated": false
}
```

- `rows`는 열 이름을 키로 하는 객체 배열, 순서는 뷰가 돌려준 순서.
- `type`: `text | integer | number | boolean | date | timestamp`(Postgres 타입을 이 6개로 묶는다).
- 값: date → `"YYYY-MM-DD"`, timestamp → ISO 8601, numeric → JSON 숫자, NULL → `null`.
- 실행 SQL은 `SELECT * FROM dash.<dataset> LIMIT <DATA_MAX_ROWS + 1>`이다. 식별자는 허용 목록과 대조한 뒤 `psycopg.sql.Identifier`로 넣는다. 사용자 입력이 SQL에 들어가는 경로는 없다.
- 행 수 상한은 `DATA_MAX_ROWS`(기본 5000)다. 넘으면 앞 5000행과 `truncated: true`를 돌려준다.
- DB 연결 실패·쿼리 시간 초과(`DATA_STATEMENT_TIMEOUT_MS`, 기본 10000)·그 밖의 DB 오류는 503 `db_unavailable`, 뷰가 없으면(적재 전) 503 `not_loaded`.
- 연결은 읽기 전용 트랜잭션(`default_transaction_read_only=on`)이다.
- 접속 계정은 `dash_reader`다.

## GET /api/health

```json
{ "status": "ok", "project": "devtracker", "db": true, "applied": true }
```

DB에 `SELECT 1`이 실패하면 503, `status: "degraded"`, `db: false`. compose healthcheck가 이 경로를 쓴다.
