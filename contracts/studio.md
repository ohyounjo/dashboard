# Studio 계약 (구성 화면, 보강설계서 6절)

Studio는 `projects/<이름>/`의 파일을 고치고 `make apply`와 같은 일(check → load → sync)을 실행하는 **YAML 편집기**다. Studio 전용 저장소나 DB는 없다. Studio로 만든 것과 손으로 쓴 것은 같은 파일이다.

- 실행: `make studio` → http://localhost:8090 (`STUDIO_PORT`). compose `studio` 프로필의 별도 컨테이너(`dashkit-studio:local`, `dashkit/Dockerfile`의 `studio` 단계). 컨테이너 안에서는 `python -m dashkit studio`(0.0.0.0:8090).
- 권한: tools 컨테이너와 같다(owner DB 계정, Superset 관리자 계정, `projects/` 쓰기). backend(읽기 전용, Superset 계정 없음)와 분리한다.
- 인증: 없다. `127.0.0.1`에만 연다. **외부에 열지 않는다. 운영 배포에 넣지 않는다.**
- 코드: API(라우팅과 요청 보호)는 `dashkit/studio/`, 로직(저장, 연산, YAML 다시 쓰기, apply 실행기, 업로드 읽기)은 관리 서비스와 함께 쓰는 공용 코어 `dashkit/project/`(`dashkit/studio/`의 같은 이름 모듈은 다시 내보내기), 화면은 `web/studio/`(바닐라 JS, 기존 디자인 토큰, 빌드 단계 없음). `ruamel.yaml`은 주석과 순서를 보존하는 YAML 쓰기의 예비 경로에만 쓴다(studio 이미지 전용. 아래 "YAML을 다시 쓰는 방법").

## 요청 보호

인증이 없는 로컬 서비스이므로, 브라우저의 다른 사이트가 사용자를 대신해 요청을 보내는 것(CSRF, DNS rebinding)을 막는다. 구현은 `dashkit/studio/guard.py`(ASGI 미들웨어. 없는 경로와 정적 파일에도 적용된다)와 `upload.py`.

| 규칙 | 내용 |
|---|---|
| Host 검사 | 모든 요청의 `Host`가 `STUDIO_ORIGIN`의 host:port 또는 같은 포트의 `127.0.0.1`·`localhost`가 아니면 403 `forbidden_host`. 컨테이너 안에서 듣는 포트(8090)의 `127.0.0.1`·`localhost`도 받는다(compose healthcheck용. 이름이 로컬 이름이라 rebinding으로는 만들 수 없다) |
| Origin 검사 | 상태를 바꾸는 요청(POST, PUT, PATCH, DELETE)은 `Origin`이 있으면 Studio 출처(위 Host 목록과 같은 host:port, `STUDIO_ORIGIN`의 scheme)와 같아야 한다. 다르면 403 `forbidden_origin`. 브라우저가 붙이는 `Sec-Fetch-Site`가 `cross-site`·`same-site`여도 403 `forbidden_origin`(웹앱·Superset은 포트만 다른 same-site다) |
| 본문 형식 | 상태를 바꾸는 JSON 요청은 `Content-Type: application/json`만 받는다(단순 폼 전송으로는 보낼 수 없게). 업로드는 `multipart/form-data`. 그 밖의 Content-Type은 415 `unsupported_media_type`. 본문이 없는 `apply`는 Content-Type 없이 보내도 된다(`application/json`과 빈 본문·`{}`도 된다) |
| 경로 값 | `{p}`, `{table}`은 실제 목록(프로젝트 폴더, 그 프로젝트 `schema.yaml`의 테이블)과 대조한 뒤에만 파일 경로로 쓴다. 이름 형식은 `load_project`·스키마의 패턴과 같다. 목록에 없으면 404 `unknown_project`·`unknown_table` |
| 업로드 | 파일 하나 `STUDIO_MAX_UPLOAD_MB`(기본 20) 이하, 요청 하나 파일 50개 이하이고 본문 전체 `STUDIO_MAX_REQUEST_MB`(기본 100) 이하. 넘으면 413 `upload_too_large`·400 `too_many_files`. 파일 이름은 `<이름>.csv`만(확장자 대소문자 무시), 경로 문자(`/`, `\`, `..`, NUL)·제어 문자·앞뒤 공백·점으로 시작하는 이름 금지(400 `invalid_filename`). 업로드한 파일 이름은 표시와 추론(표 이름)에만 쓰고 파일 경로로 쓰지 않는다. `…/data/{table}`에 올리는 파일은 이름이 테이블과 달라도 된다(어디에 쓸지는 경로의 `{table}`이 정한다) |
| 응답 헤더 | 모든 응답에 `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, CSP: `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src <WEB_PUBLIC_URL의 출처>; frame-ancestors 'none'; base-uri 'self'; form-action 'self'`. 화면은 인라인 스크립트와 외부 자원을 쓸 수 없다 |

오류 응답은 백엔드 API와 같은 형식이다: `{"detail": "<코드>", "message": "<한국어 설명>"}`. 검증 오류는 `errors: ["<파일>:<위치>: <이유>", …]`를 함께 싣는다.

| 상태 | detail | 언제 |
|---|---|---|
| 400 | `invalid_request` · `bad_request` | 본문이 JSON 객체가 아님 · multipart 형식 오류, 파일 부분 없음 |
| 400 | `invalid_op` | 연산 형식 오류·대상 없음. `message`와 `errors[0]`이 `ops[<번호>] <연산>: <이유>`, `op_index`가 번호(0부터) |
| 400 | `invalid_filename` · `too_many_files` | 업로드 규칙 위반 |
| 403 | `forbidden_host` · `forbidden_origin` | 요청 보호 |
| 404 | `unknown_project` · `unknown_table` · `not_found` | 목록에 없는 경로 값 · 없는 경로 |
| 409 | `project_exists` · `apply_running` · `stale_config` · `append_table` | 같은 이름의 프로젝트 · 적용 실행 중(같은 프로젝트의 저장도 이 코드로 거절) · `revision` 불일치 · 누적 테이블의 CSV를 통째로 바꾸려 함 |
| 410 | `upload_expired` | `upload_id`를 찾을 수 없음(1시간이 지났거나 그 뒤에 3번 넘게 새로 올림, 또는 Studio를 다시 띄움) |
| 413 | `upload_too_large` | 업로드 한도 초과(JSON 본문은 5MB) |
| 415 | `unsupported_media_type` | Content-Type |
| 422 | `invalid_config` · `invalid_csv` · `invalid_inference` · `invalid_name` | 설정 검사 실패 · CSV 검증 실패(읽을 수 없는 CSV 포함) · 추론 결과로 만들 수 없음 · 프로젝트 이름 형식. `errors`가 있다 |
| 500 | `cannot_write` · `internal_error` | 다시 쓴 YAML이 기대와 달라 저장하지 않음 · 처리하지 못한 오류 |
| 503 | `plan_unavailable` | plan이 DB를 읽지 못함 |

## 저장 방식

1. 임시 폴더에 프로젝트 사본을 만들고 변경을 적용한다.
2. 사본을 `config.load_project`로 검증한다. CSV를 바꿨으면 `validate_csv`도 돌린다.
3. 통과하면 바뀐 파일만 원래 자리에 원자적으로 바꿔 넣는다(같은 폴더의 임시 파일 → `os.replace`). 실패하면 파일을 건드리지 않고 오류를 돌려준다.

같은 프로젝트의 저장과 apply는 잠금 하나로 순서를 세운다(apply가 도는 동안의 저장은 409 `apply_running`). 이 잠금은 Studio 프로세스 안의 것이고, apply의 load 단계는 그와 별도로 **데이터 DB의 쓰기 잠금**을 잡는다([ingest.md](ingest.md) "쓰기 잠금"). 그래서 관리 서비스의 추가 적재나 `make apply`·`make ingest`와 겹치지 않는다(잠금을 10초 안에 못 잡으면 load가 실패한다). DB를 건드리지 않는 저장(탭 편집 등)은 DB 잠금에 들어오지 않는다. 운영 서버의 같은 프로젝트 폴더에 Studio를 함께 띄우지 않는다.

누적(`mode: append`) 테이블은 배치 폴더가 원본이라 Studio의 데이터 화면에서 CSV를 통째로 바꾸지 않는다: 데이터 검사·교체 경로는 409 `append_table`이다. 저장 때의 CSV 검증(`validate_csv`)도 `replace` 테이블만 본다.

### YAML을 다시 쓰는 방법 (손으로 쓴 주석·키 순서·모양 보존)

- **고친 곳만 다시 쓴다**(`dashkit/project/yamledit.py`). 파일을 통째로 다시 내보내지 않고, YAML 노드의 위치로 원문을 조각내 바뀌지 않은 조각은 원문 글자 그대로 이어 붙인다. 그래서 **건드리지 않은 줄은 바이트 단위로 같다**(주석, 빈 줄, 인용 부호, 들여쓰기, `{ a: b }` 같은 한 줄 표기, 키 뒤의 줄 맞춤 공백).
  - 값을 바꾸면 그 값의 글자만 바뀐다(같은 줄의 주석은 남는다). 키를 지우면 그 줄(과 바로 위의 주석)이 없어진다. 새 키는 그 매핑의 끝에 붙는다.
  - 항목(탭, 섹션)을 옮기면 그 항목의 줄들이 바로 위의 주석과 함께 그대로 옮겨진다. 항목 사이의 빈 줄은 자리에 남는다.
  - 새 항목은 이웃과 같은 꼴로 쓴다: 한 줄 목록(`sections: [{ … }]`)에는 한 줄로, 여러 줄 목록에는 여러 줄로. 한 줄 목록에 여러 줄짜리 섹션을 옮겨 넣거나 새 항목 때문에 줄이 80자를 넘으면 그 목록만 여러 줄로 푼다.
  - 항목마다 한 줄씩 쓴 목록(`charts:`·`filters:` 아래의 `- { key: …, … }`, `layout:` 아래의 `- [a, b]`)에 더하는 새 항목은, 이웃이 **모두** 한 줄이면 한 줄로 쓴다(160자 또는 가장 긴 이웃의 길이까지. 넘으면 여러 줄). 이웃에 여러 줄 항목이 섞여 있으면 새 차트·필터는 여러 줄로 쓴다.
  - `layout.set`은 layout 전체를 받지만 통째로 다시 쓰지 않는다. 받은 layout과 파일의 layout을 **행 단위로 견줘** 내용이 같은 행은 원문 줄을 그대로 쓰고(자리만 옮긴다. 탭 안팎으로도), 달라진 행은 항목을 가장 많이 공유하는 원래 행을 골라 그 안에서 바뀐 항목만 고친다(`{ chart: a, width: 4 }`의 `4`만 바뀌고 같은 줄의 주석은 남는다). 짝이 없는 행만 새로 쓴다. 제목(header)·글(markdown)은 글자를 고쳐도 원래 항목 자리에 쓴다.
  - 여러 줄 문자열(글 블록의 markdown 등)을 새로 쓸 때는 literal block(`|`)으로 쓴다. 그렇게 쓸 수 없는 글(첫 줄이 공백으로 시작하는 등)만 `"…\n…"`으로 쓴다.
  - 여러 줄로 쓴 항목(예: `- markdown: |` 글 블록)을 한 줄 행·한 줄 탭(`{ label: …, rows: [[a]] }`) 안으로 옮기면 그 행·탭만 여러 줄로 풀어 쓴다(안의 바뀌지 않은 값은 원문 그대로).
- 이 방법으로 다룰 수 없는 파일·변경(앵커·별칭·병합 키, CR 줄바꿈, 여러 줄에 걸친 `{ … }` 안의 수정 등)은 **`ruamel.yaml` round-trip**으로 파일 전체를 다시 쓴다(`dashkit/project/roundtrip.py`). 주석과 키 순서는 남지만 모양은 표준 꼴로 바뀐다(`{ a: b }` → `{a: b}`, 줄 맞춤 공백 제거, 겹친 목록의 들여쓰기). 응답의 `written`이 파일마다 어느 쪽이었는지 알려 준다(`exact` | `roundtrip`).
- 어느 쪽이든 다시 쓴 글을 PyYAML로 읽어 "연산을 적용한 값"과 같은지 확인한 뒤에만 저장 단계(위 1~3)로 넘어간다. 다르면 500 `cannot_write`이고 파일은 그대로다.

되돌리기는 git에 맡긴다. Studio는 이번 실행에서 자기가 쓴 파일 목록만 보여 준다(`GET /studio/api/changes`).

## 현재 프로젝트

Studio에서 적용(apply)하면 그 프로젝트가 웹앱에 보인다. backend는 `.state/embed.json`의 `project`(마지막으로 sync에 성공한 프로젝트)를 따른다. 자세한 규칙은 `contracts/api.md`의 "현재 프로젝트".

- `GET /studio/api/projects`의 `current`가 그 프로젝트다(한 번도 적용하지 않았으면 null). 미리보기(웹앱)가 보여 주는 것은 `current`이지 Studio에서 열어 둔 프로젝트가 아니다.
- `current`인 프로젝트의 설정을 저장하면 backend가 파일 변경을 감지해 **적용 없이도** 다음 요청부터 반영한다: 탭·섹션의 순서와 옵션, `app`(제목, 로고, 기준일, 색). 미리보기를 새로 고치면 보인다.
- 데이터(CSV)와 Superset 쪽(데이터셋, 차트, 대시보드 배치, 필터)은 적용해야 바뀐다. 새 대시보드를 가리키는 섹션은 적용 전까지 `embed_id: null`이다.
- 적용이 load까지만 성공하고 sync에서 실패하면 `current`는 그대로다(웹앱은 이전 프로젝트를 보여 준다).

## API

모든 경로는 `/studio/api/` 아래다. 화면은 `/`(= `web/studio/index.html`), `/studio/static/<경로>`(= `web/studio/<경로>`. 폴더는 `STUDIO_WEB_DIR`로 바꿀 수 있다), 운용 화면과 함께 쓰는 화면 모듈 `/studio/shared/<경로>`(= `web/shared/<경로>`. 폴더는 `STUDIO_SHARED_DIR`. `web/studio`의 모듈이 `../shared/…`로 import한다). 화면 파일이 없어도 API는 뜬다.

| 메서드 경로 | 내용 |
|---|---|
| `GET /studio/api/health` | `{"status": "ok"}` |
| `GET /studio/api/projects` | `{"projects": [{"name", "title", "valid", "errors": n}], "current": "<이름>" 또는 null, "web_url": "<미리보기 주소>"}`. `current`는 지금 웹앱에 적용된 프로젝트(`.state/embed.json`의 `project`). `title`은 읽을 수 없으면 null |
| `POST /studio/api/projects/infer` | multipart `files`(CSV 여러 개) → `{"inference": {…}, "upload_id": "…"}`. 파일을 `projects/`에 쓰지 않는다(메모리에 1시간, 최근 3개). `inference` 형식은 `contracts/scaffold.md`. 읽을 수 없는 CSV는 422 `invalid_csv` + `errors` |
| `POST /studio/api/projects` | `{"name", "upload_id", "inference"}` → 프로젝트 생성(`scaffold.render` + `write_project`). 201 `{"name"}`. 이름이 이미 있으면 409 `project_exists`. `inference.tables`를 고쳤으면 서버가 `suggest_dashboard`로 초안 차트를 다시 고른다(요청에 `"resuggest": true`. `inference.dashboard.app`은 유지한다). 만들 수 없으면 422 `invalid_inference` + `errors`(`problems`가 남아 있음, 검증 실패 등) |
| `GET /studio/api/projects/{p}/config` | `{"name", "schema": {…}, "dashboard": {…}, "valid", "errors": […], "columns": {"<데이터셋>": [{"name","type"}]}, "columns_source": "db"\|"schema", "metrics": {"<데이터셋>": ["<지표 이름>"]}, "tables": {"<테이블>": {"rows": n 또는 null, "columns": [{"name","type"}]}}, "revision": "…"}`. `schema`·`dashboard`는 파일을 읽은 그대로(기본값을 채우지 않은 것. 읽을 수 없으면 null). `columns`는 지금 DB의 뷰 열이다(이 프로젝트가 적재돼 있을 때. `type`은 `/api/data`와 같은 6가지). 아니면 `table:` 데이터셋만 스키마에서 채운다(`sql:` 데이터셋은 키가 없다). `tables.<테이블>.rows`는 `data/<테이블>.csv`의 행 수, `columns[].type`은 스키마의 유형(`enum` 포함) |
| `PATCH /studio/api/projects/{p}/config` | `{"ops": [연산…], "revision"?: "…"}` → 전부 적용해 검증한 뒤 저장. 200 `{"changed": ["dashboard.yaml"], "dashboard": {…}, "schema": {…}, "revision": "…", "written": {"dashboard.yaml": "exact"}}`. 검증 실패는 422 `invalid_config` + `errors`(파일은 그대로). `revision`(GET이 준 값)을 보내면 그 사이에 파일이 바뀌었을 때 409 `stale_config`로 거절한다(생략하면 보지 않는다) |
| `POST /studio/api/projects/{p}/data/{table}/check` | multipart `file`(CSV 하나) → 검증만. 200 `{"valid", "errors": […], "diff": {added, removed, changed, unchanged, comparable, rows, …}, "rows": n}`. 검증에 실패해도 200이고 `valid: false`, `diff: null`이다. 차이는 `dashkit.plan`의 방식(적재 기록의 행 지문과 비교, `dash_reader` 계정)이고 `diff`는 `plan.md`의 `data[]` 한 항목이다(`status`, `reason`, `keys`, `columns` 포함. 적재 기록이 없으면 `status: unrecorded`). DB를 읽지 못하면 `diff: null`과 `diff_error`(이유). 파일을 쓰지 않는다. 다른 테이블의 CSV와의 참조도 함께 검증한다 |
| `PUT /studio/api/projects/{p}/data/{table}` | multipart `file` → 검증을 통과하면 `data/<table>.csv`를 바꾼다. 200 `{"changed": ["data/<table>.csv"], "rows": n}`(내용이 같으면 `changed: []`). 실패는 422 `invalid_csv` + `errors`. 적재는 하지 않는다(적용은 apply) |
| `POST /studio/api/projects/{p}/apply` | check → load → sync. 진행 로그를 스트림으로(`text/event-stream`: `event: log` 한 줄씩, 마지막에 `event: done` `{"ok": bool, "step": "check\|load\|sync", "errors": […], "project": "<이름>"}`). 한 번에 하나만 실행한다(실행 중이면 409 `apply_running`) |
| `GET /studio/api/apply` | `{"running": bool, "project", "started_at", "last": {ok, step, errors, project, started_at, finished_at, log: […]} 또는 null}`. 스트림이 끊겼을 때 결과를 다시 확인하는 용도 |
| `GET /studio/api/projects/{p}/plan` | 적용 전 차이(`dashkit plan --json`과 같은 구조). CSV 검증 실패는 422 `invalid_csv` + `errors`, DB를 읽지 못하면 503 `plan_unavailable` |
| `GET /studio/api/registry` | `{"charts": [{viz, label, viz_type, fields: [{name, kind, required, default?, values?, description?, min?, max?, min_items?}]}], "widgets": [{type, label, options(JSON Schema)}], "icons": […], "tones": […], "defs": {"<이름>": JSON Schema}}`. `widgets[].options`는 `widget.json` 그대로라 `$ref: "#/$defs/<이름>"`이 남아 있고, 그 정의(따라가며 필요한 것 전부)가 `defs`에 있다 |
| `GET /studio/api/changes` | `{"files": [{"path": "projects/<p>/dashboard.yaml", "at": "<시각>"}]}`. 이 Studio 프로세스가 쓴 파일(파일마다 마지막으로 쓴 시각, 쓴 순서) |

### apply 스트림

- `event: log`의 `data:`는 로그 한 줄이다(글자 그대로. JSON이 아니다). 단계마다 `▶ <단계>: …`로 시작해 `✔ <단계>: …`로 끝나고, sync는 `· 번들`, `· import`, `· embed`, `· prune` 줄을 낸다.
- `event: done`의 `data:`는 JSON 한 줄이다. 실패하면 `ok: false`, `step`이 실패한 단계, `errors`가 이유다(`make apply`가 출력하는 것과 같은 줄).
- 로그가 15초 동안 없으면 주석 줄(`: keep-alive`)을 보낸다.
- 브라우저가 연결을 끊어도 작업은 끝까지 간다. 결과는 `GET /studio/api/apply`로 본다.
- 로그와 오류 글에 환경변수의 비밀값(이름에 `PASSWORD`·`SECRET`이 있거나 `_TOKEN`·`_KEY`로 끝나는 것)이 들어 있으면 `***`로 가린다.
- owner DB 계정은 load 단계만 쓴다. plan과 check의 차이 계산, config의 열 목록은 `dash_reader`만 쓴다.

### 변경 연산 (`PATCH …/config`의 `ops`)

연산은 `{"op": "<이름>", …필드}` 객체이고 순서대로 적용한다. 하나라도 형식이 틀리거나(모르는 연산·필드 포함) 대상이 없으면 400 `invalid_op`(몇 번째 연산인지와 이유)이고 아무것도 저장하지 않는다. 연산을 모두 적용한 결과가 설정 검사를 통과하지 못하면 422 `invalid_config`다(중간 상태는 검사하지 않는다).

5a단계(탭 구성, 앱):

| 연산 | 필드 | 결과 |
|---|---|---|
| `app.update` | `values: {title?, logo_text?, as_of?, theme?}` | `app`의 해당 키를 바꾼다(`null`이면 지운다). 값은 그 키의 값 전체다(`theme`도 통째로) |
| `tab.add` | `tab: {key, label, icon}`, `index?` | 탭 추가(섹션 없음 → 저장하려면 같은 요청에서 `section.add`가 뒤따라야 한다. 스키마가 섹션 1개 이상을 요구한다). `index`를 생략하면 맨 뒤. 같은 `key`가 있으면 `invalid_op` |
| `tab.update` | `key`, `values: {label?, icon?}` | 탭 라벨·아이콘 변경(`key`는 바꿀 수 없다) |
| `tab.move` | `key`, `index` | 탭 순서 변경. `index`는 **옮긴 뒤의 자리**(원래 자리에서 뺀 뒤 넣을 자리, 0부터) |
| `tab.remove` | `key` | 탭 삭제 |
| `section.add` | `tab`, `index?`, `section: {type, …옵션}` | 섹션 추가. `type`은 `superset` 또는 위젯 유형. `index`를 생략하면 맨 뒤. 값이 `null`인 옵션은 쓰지 않는다 |
| `section.update` | `tab`, `index`, `values: {…옵션}` | 섹션 옵션 변경. **최상위 키만 바꾸는 얕은 병합**이다(중첩 객체·배열은 그 키의 값 전체를 보낸다). `type`은 바꿀 수 없다. `null`이면 그 옵션을 지운다 |
| `section.move` | `tab`, `index`, `to_index`, `to_tab?` | 섹션 순서 변경(다른 탭으로도). `to_index`는 **옮긴 뒤의 자리**(같은 탭이면 원래 자리에서 뺀 뒤 넣을 자리, 다른 탭이면 그 탭에 끼워 넣을 자리) |
| `section.remove` | `tab`, `index` | 섹션 삭제 |

연산은 `dashkit/project/ops.py`의 등록 표(`@op("이름")`)에 있다. 함수 하나를 더하면 연산 하나가 는다.

`schema.yaml`을 고치는 연산(데이터 운용. 관리 서비스와 함께 쓴다, [manage.md](manage.md)):

| 연산 | 필드 | 결과 |
|---|---|---|
| `column.update` | `table`, `column`, `set: {aliases?, description?}` | 그 열의 키를 바꾼다(`null`은 지움). 지금은 DB의 내용과 상관없는 `aliases`·`description`만 받는다(유형·규칙은 구조 변경 단계에서 더한다) |

저장 로직(고친 곳만 다시 쓰기 → 사본 검증 → 원자적 교체, `revision` 확인)은 `dashkit/project/store.py`의 `save_ops` 하나이고 Studio의 `PATCH …/config`와 관리 서비스가 함께 쓴다.

5b단계(대시보드 배치, 필터):

| 연산 | 필드 | 결과 |
|---|---|---|
| `dashboard.update` | `dashboard`, `values: {title?, cross_filters?}` | 대시보드 제목·교차 필터 변경(`null`이면 지운다). 그 밖의 키는 `invalid_op` |
| `chart.add` | `dashboard`, `chart: {key, title, dataset, viz 또는 viz_type, …필드}`, `row?`, `index?`, `width?` | 차트를 `charts`에 더하고 `layout`에 배치한다. `row`(layout 최상위 행의 자리)가 있으면 그 행의 `index` 자리(없으면 끝)에, 없으면 맨 아래에 새 행으로. `width`가 있으면 `{chart, width}` 항목으로 쓴다. 대시보드 안 탭(`tabs`) 속 행에 넣으려면 `chart.add` 뒤에 `layout.set`을 쓴다. `chart`의 값이 `null`인 필드는 쓰지 않고, `key`·`title`을 맨 앞에 쓴다. 같은 `key`가 프로젝트의 어느 대시보드에든 있거나, `row`가 탭 묶음(`{tabs}`)의 자리이거나, `row` 없이 `index`만 있으면 `invalid_op` |
| `chart.update` | `key`, `values: {…}` | 차트 필드의 얕은 병합(`null`이면 그 필드를 지운다). 차트 키는 프로젝트 전체에서 유일하므로 `dashboard` 필드가 없다(보내면 `invalid_op`. `chart.remove`도 같다). `key`는 바꿀 수 없다(Superset 객체 UUID가 키에서 나온다). `viz`를 바꾸면 그 유형에 없는 필드는 함께 `null`로 보내야 한다(서버는 추측해서 지우지 않는다) |
| `chart.remove` | `key` | 차트를 `charts`와 `layout`(탭 속 포함)에서 지운다. 비게 된 행은 지우고, 그래서 행이 하나도 없게 된 탭은 탭째, 탭이 하나도 없게 된 탭 묶음은 묶음째 지운다(제목·글 행이 남은 탭은 남는다). 필터의 `charts` 목록에서도 뺀다(그 목록이 비게 되면 `charts` 키를 지운다 = 모든 차트에 적용) |
| `layout.set` | `dashboard`, `layout: […]` | `layout` 전체를 바꾼다. 형식은 dashboard.yaml의 `layout`과 같다(행 = 항목 목록. 항목은 차트 키 문자열, `{chart, width?, height?}`, `{header}`, `{markdown, width?, height?}`. 최상위 항목으로 `{tabs: [{label, rows}]}`). 배치 편집기는 끌기·너비 조절이 끝날 때마다 이 연산 하나를 보낸다. 서버는 바뀌지 않은 행의 글자를 그대로 둔다. 파일에 있던 항목(`{chart, width, height}` 등)은 **있던 키의 순서를 유지**하고 새 키만 뒤에 붙이므로, 저장된 항목의 키 순서는 보낸 것과 다를 수 있다(값은 같다. 응답의 `dashboard`가 저장된 그대로다). `layout`이 배열이 아니거나, 최상위 항목이 행(배열)·`{tabs: [{…, rows: [행…]}]}`이 아니거나, 행의 항목이 문자열·객체가 아니면 `invalid_op`. 그 밖의 형식(모르는 키, 너비 범위, 빈 행 등)은 설정 검사가 본다(422) |
| `filter.add` | `dashboard`, `filter: {key, label, type?, dataset?, column?, multi?, default?, parent?, charts?}`, `index?` | 필터 추가(`index`를 생략하면 맨 뒤). 대시보드에 `filters`가 없으면 만든다. 값이 `null`인 필드는 쓰지 않고, `key`·`label`을 맨 앞에 쓴다. 같은 `key`가 그 대시보드에 있으면 `invalid_op` |
| `filter.update` | `dashboard`, `key`, `values: {…}` | 필터 필드의 얕은 병합(`null`이면 지운다). `key`는 바꿀 수 없다(다른 필터의 `parent`가 가리킨다) |
| `filter.move` | `dashboard`, `key`, `index` | 필터 순서 변경(`index`는 옮긴 뒤의 자리) |
| `filter.remove` | `dashboard`, `key` | 필터 삭제. 다른 필터가 `parent`로 가리키고 있으면 400 `invalid_op`(먼저 그 필터의 `parent`를 지워야 한다. 같은 요청에서 `filter.update`로 `parent: null`을 앞에 보내도 된다). 마지막 필터를 지우면 `filters` 키도 지운다 |

모든 연산 뒤의 검증은 5a와 같다(`config.load_project`). 차트 필드의 열·지표 이름, 행 너비 합(12칸), 모든 차트가 정확히 한 번 배치되는지, 필터 유형별 필수·금지 필드는 설정 검사가 잡고 422 `invalid_config`의 `errors`로 돌려준다.

`GET /studio/api/projects/{p}/config`의 응답에 배치·필터 편집에 필요한 것을 더한다: `metrics: {"<데이터셋>": ["<지표 이름>", …]}`(차트 폼의 지표 선택용. `dashboard.datasets.<키>.metrics`의 키와 같고 파일에 쓴 순서다. 지표가 없는 데이터셋은 빈 배열, `dashboard.yaml`을 읽을 수 없으면 `{}`).

## 프레임 허용 (미리보기)

Studio 오른쪽의 미리보기는 실제 웹앱을 iframe으로 띄운다(별도 렌더러 없음): Studio(`STUDIO_ORIGIN`) → 웹앱(`WEB_PUBLIC_URL`) → Superset.

- 웹앱(nginx)의 CSP `frame-ancestors`는 평소 `'none'`이다. `make studio`가 `STUDIO_ORIGIN`을 주고 web 컨테이너를 다시 만들면 그 출처 하나가 된다(`WEB_FRAME_ANCESTORS`).
- Superset의 `frame-ancestors`는 `WEB_ORIGINS`에 `STUDIO_ORIGIN`을 더한 값이 된다(frame-ancestors는 모든 조상을 본다). 임베드 허용 도메인(Referer 검사)은 웹앱 출처 그대로다.
- `make studio-down`이 둘을 원래대로 되돌린다.
- 웹앱의 `/studio/…` 경로는 nginx가 404로 막는다(구성 화면 파일은 studio 컨테이너만 서빙한다).
