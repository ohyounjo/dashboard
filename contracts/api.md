# 백엔드 API 계약 (`dashkit/api`, FastAPI)

- 브라우저는 웹앱과 같은 출처(`/api/...`, nginx 프록시)로만 부른다. CORS는 없다.
- 모든 응답은 `Content-Type: application/json; charset=utf-8`, `Cache-Control: no-store`.
- 오류 본문은 `{"detail": "<영문 코드>", "message": "<사람이 읽는 설명>"}`. 웹앱은 `detail`로 분기하고 `message`는 표시하지 않는다(문구는 `web/strings.js`).
- 공통 오류 코드: 없는 경로 404 `not_found`, 허용하지 않는 메서드 405 `method_not_allowed`, 처리하지 못한 오류 500 `internal_error`.
- OpenAPI 문서 경로(`/docs`, `/openapi.json`)는 만들지 않는다.
- **로그인이 필요하다.** `/api/app`, `/api/guest-token`, `/api/data/{dataset}`는 세션 쿠키(`dashkit_session`)가 없거나 만료됐으면 401 `unauthenticated`, 비밀번호 변경이 필요한 계정이면 403 `password_change_required`다. `/api/health`는 그대로 인증이 없다(compose healthcheck). 로그인·세션·요청 보호와 `/api/auth/*`는 [auth.md](auth.md).
- 상태를 바꾸는 요청(POST 등)은 `Content-Type: application/json`이어야 하고 `Origin`이 있으면 `WEB_ORIGINS` 중 하나여야 한다(415 `unsupported_media_type`, 403 `forbidden_origin`. [auth.md](auth.md) "요청 보호").
- 인증 DB를 쓸 수 없으면 로그인이 필요한 경로는 503 `auth_unavailable`.
- backend가 보여 주는 프로젝트(**현재 프로젝트**)는 `.state/embed.json`의 `project`, 곧 마지막으로 sync에 성공한 프로젝트다. 쓸 수 없으면 환경변수 `PROJECT`다. 아래 "현재 프로젝트" 참조. 시작할 때 어느 쪽 설정도 유효하지 않으면 시작하지 않는다.
- **요청마다** 현재 프로젝트의 `projects/<이름>/schema.yaml`, `dashboard.yaml`의 서명(mtime, 크기, inode)을 확인해 바뀌었으면 다시 읽는다(`dashkit/api/state.py`의 `ProjectState`). 설정을 바꾼 뒤 backend를 재시작하거나 이미지를 다시 만들 필요가 없다(보강설계서 5-2, M9). 아래 "설정 다시 읽기" 참조.
- 위젯 매니페스트(`WIDGETS_DIR`, 기본 `/app/web/widgets`)는 **이미지에 들어 있고 프로세스당 한 번만 읽는다**(`dashkit/widgets.py`의 캐시). 계약의 JSON Schema와 차트 유형 레지스트리도 이미지에 들어 있다. 그래서 템플릿 코드(`dashkit/`, `contracts/`, `web/widgets/*/widget.json`)를 바꿨으면 `make up`으로 이미지를 다시 만들어야 한다.
- `.state/embed.json`은 **요청마다** 파일 서명을 확인해 바뀌었으면 다시 읽는다. 프로젝트를 바꿔 적용해도(`PROJECT=<다른 이름> make apply`, Studio의 적용) backend를 다시 만들 필요가 없다. `.state/`는 디렉터리째 읽기 전용으로 마운트한다(원자적 rename을 반영하려면 파일 단위 마운트를 쓰면 안 된다).

## 현재 프로젝트

`/api/app`, `/api/data`, `/api/guest-token`, `/api/health`는 모두 같은 "현재 프로젝트"를 쓴다(`dashkit/api/state.py`의 `CurrentState`). 요청 하나는 처음 받은 프로젝트·설정·임베드 목록을 끝까지 쓴다.

| 상황 | 현재 프로젝트 | 임베드(`embed_id`) |
|---|---|---|
| `embed.json`의 `project`가 있고 그 프로젝트의 설정을 읽을 수 있음 | **그 프로젝트**(환경변수 `PROJECT`와 달라도) | `embed.json`의 것 |
| `embed.json`이 없음(한 번도 적용하지 않음) · JSON이 깨짐 · `project`가 문자열이 아니거나 이름 형식이 틀림 | 환경변수 `PROJECT` | 없음(미적용) |
| `embed.json`의 프로젝트 폴더가 없음(지웠거나 다른 체크아웃) | 환경변수 `PROJECT` | 없음(미적용) |
| `embed.json`의 프로젝트 설정이 유효하지 않고, backend가 그 프로젝트를 유효하게 읽은 적이 없음 | 환경변수 `PROJECT` | 없음(미적용) |
| 현재 프로젝트의 설정이 **읽은 뒤에** 깨짐 | 그대로(마지막으로 유효했던 설정. 아래 "설정 다시 읽기") | 그대로 |
| 어느 쪽 설정도 읽을 수 없음 | 시작할 때는 시작하지 않는다. 실행 중에는 마지막으로 응답한 상태를 유지한다(`/api/health`의 `config.valid: false`) | 그대로 |

- 환경변수 `PROJECT`로 넘어간 경우 `embed.json`이 그 프로젝트의 것이 아니므로 `embed_id: null`, `applied: false`, guest-token 409 `not_applied`다(적용 전과 같은 응답).
- `embed.json`의 프로젝트를 쓸 수 없다는 판단은 그 프로젝트의 설정 파일(또는 `embed.json`)이 바뀔 때까지 다시 하지 않는다. 요청마다 검증을 되풀이하지 않고, 로그도 변경 한 번에 한 번이다. 파일을 고치면 다음 요청부터 그 프로젝트로 넘어간다.
- 프로젝트가 바뀌면 로그에 `현재 프로젝트: '<이름>' (embed.json의 project | env PROJECT)`를 남긴다.
- `/api/data`가 읽는 DB에는 마지막으로 **load**에 성공한 프로젝트가 있다. apply가 load까지만 성공하고 sync에서 실패하면 backend는 이전 프로젝트의 설정으로, DB는 새 프로젝트의 뷰로 남는다. 이 구간의 위젯 데이터는 503 `not_loaded`이거나(뷰 이름이 다를 때) 새 프로젝트의 행이다. sync를 다시 성공시키면 맞춰진다.
- backend는 `projects/` 전체를 읽기 전용으로 마운트하므로 환경변수 `PROJECT`가 아닌 프로젝트도 읽을 수 있다. Superset 계정과 owner DB 계정은 여전히 갖지 않는다.

## 설정 다시 읽기

| 상황 | 동작 |
|---|---|
| 두 파일의 서명이 지난번과 같음 | 읽지 않는다(요청마다 `stat` 두 번) |
| 서명이 바뀌었고 새 설정이 유효함 | 그 요청부터 새 설정으로 응답한다. 로그 `설정 변경을 감지해 프로젝트 '<이름>'를 다시 읽음` |
| 서명이 바뀌었지만 검증에 실패함(문법 오류, 참조 오류, 저장 도중의 반쪽 파일, 파일 없음) | **마지막으로 유효했던 설정으로 계속 응답한다.** 오류 목록을 ERROR 로그에 남긴다. 같은 서명에서는 다시 읽지 않으므로 로그는 변경 한 번에 한 번이다. 파일이 다시 바뀌면 다시 시도한다 |

- 요청 하나는 처음 받은 설정을 끝까지 쓴다(응답 안에서 옛 설정과 새 설정이 섞이지 않는다). 서명 확인과 읽기는 잠금 하나로 묶여 있어 동시 요청이 여럿이어도 변경 한 번에 한 번만 읽는다.
- 다시 읽는 대상은 설정 파일 둘뿐이다. CSV(`data/`)는 backend가 읽지 않는다.
- `/api/app`의 탭·섹션, `/api/guest-token`의 대시보드 키, `/api/data`의 허용 목록이 모두 새 설정을 따른다. `make apply`보다 먼저 설정 파일만 저장하면 backend는 새 설정을, DB와 Superset은 옛 상태를 갖는 구간이 생긴다. 이 구간에서 새 대시보드는 `embed_id: null`(409 `not_applied`), 새 뷰는 503 `not_loaded`로 응답한다(적용 전과 같은 응답이다).
- backend는 여전히 Superset 계정과 owner DB 계정을 갖지 않는다. `projects/`는 읽기 전용 마운트다.

## GET /api/app

웹앱이 시작할 때 한 번 부른다. 로그인 필요(401이면 웹앱은 `/login.html`로 보낸다).

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
  ],
  "user": { "email": "kim@example.com", "role": "admin" }
}
```

- `user`: 로그인한 사용자의 이메일과 등급(`admin` · `operator` · `user`).
- `tabs`: **로그인한 사용자가 볼 수 있는 탭만**, 설정 순서대로. 운용자(`admin`·`operator`)는 전부, `user`는 속한 그룹들이 허용한 탭([auth.md](auth.md) "접근 제어"). 하나도 없으면 빈 배열이고 웹앱은 "접근할 수 있는 대시보드가 없습니다"를 보여 준다.
- `app.as_of`: `"today"`이면 요청 시점 날짜(`TZ` 환경변수)로 바꿔 돌려준다. 항상 `YYYY-MM-DD`.
- `app.logo_text`, `app.theme`: 설정에 없으면 `null`. 웹앱은 `null`이면 기본 토큰을 쓴다.
- `superset.url`: 환경변수 `SUPERSET_PUBLIC_URL`(브라우저가 접속할 주소). 비어 있으면 `null`.
- 섹션의 선택 필드는 설정에 없으면 `null`로 채워 **키가 항상 있다**.
- `project`: 현재 프로젝트의 이름(위 "현재 프로젝트").
- `embed_id`: `.state/embed.json`에서 찾은 임베드 UUID. 없거나, 현재 프로젝트가 환경변수 `PROJECT`로 넘어간 것이라 embed.json이 그 프로젝트의 것이 아니면 `null`.
- `applied`: 돌려준 탭의 모든 superset 섹션의 `embed_id`가 있으면 `true`(남은 탭이 없으면 `true`).
- `widgets`: 위젯 매니페스트(`web/widgets/<유형>/widget.json`, [widgets.md](widgets.md))에서 읽은 유형 목록(이름순). 이 프로젝트가 쓰지 않는 유형도 들어 있다. 웹앱은 섹션의 `type`이 이 목록의 `type`과 정확히 같을 때만 `./widgets/<type>/widget.js`를 불러온다. `superset`은 위젯이 아니므로 목록에 없다.
- 위젯 섹션의 키는 유형의 매니페스트가 정한다(공통 키 `type`, `title` + 옵션). 위 예의 `gantt`, `kanban`은 저장소에 들어 있는 두 유형이다.

## POST /api/guest-token

로그인 필요. 요청 `{"dashboard": "<dashboards 키>"}` → 200

```json
{ "token": "<JWT>", "expires_at": "2026-11-19T08:30:00Z" }
```

| 상황 | 상태 | detail |
|---|---|---|
| 로그인하지 않음(본문 형식보다 먼저 본다) | 401 | `unauthenticated` |
| 본문 형식 오류 | 422 | `invalid_request` |
| 설정에 없는 키 | 404 | `unknown_dashboard` |
| 설정에는 있지만 볼 수 있는 탭의 superset 섹션이 참조하지 않음(`user` 등급. 운용자는 설정의 모든 대시보드) | 403 | `forbidden` |
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

로그인 필요(401 `unauthenticated`). 위젯 섹션(매니페스트에 `dataset` 옵션이 있는 유형)이 `dataset`으로 참조한 데이터셋만 허용한다. 나머지는 404 `unknown_dataset`(설정에 있는 데이터셋이어도 위젯이 참조하지 않으면 404). `user` 등급은 **볼 수 있는 탭의** 위젯 섹션이 참조한 데이터셋만 허용하고, 다른 탭의 데이터셋도 같은 404다.

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
{ "status": "ok", "project": "devtracker", "db": true, "applied": true, "config": { "valid": true, "errors": 0 } }
```

로그인 불필요. DB에 `SELECT 1`이 실패하면 503, `status: "degraded"`, `db: false`. compose healthcheck가 이 경로를 쓴다.

- `project`: 현재 프로젝트의 이름. 환경변수 `PROJECT`와 다를 수 있다.

- `config.valid`: 지금 응답에 쓰는 설정이 디스크의 설정 파일과 같으면 `true`. 파일이 바뀌었는데 검증에 실패해 마지막으로 유효했던 설정을 쓰는 중이면 `false`이고 `config.errors`가 오류 개수다. 오류 내용은 응답에 싣지 않는다(backend 로그, 또는 `make check`).
- `config.valid`가 `false`여도 상태 코드는 200, `status`는 `ok`다. backend는 정상으로 응답하고 있고, 저장 도중의 설정 때문에 컨테이너가 unhealthy가 되면 안 되기 때문이다.
