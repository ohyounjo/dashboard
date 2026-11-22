# 장시간 작업의 잠금 대기와 대용량 미리보기 안내 오케스트레이션 프롬프트

설계 PR이 병합된 뒤 `ohyounjo/dashboard` 저장소에서 새 세션을 열고 아래 블록을 붙여 넣는다. [설계서](long-running-lock-wait-design.md)

````text
# 목표

ohyounjo/dashboard의 이슈 #34(`[4+] 장시간 작업의 잠금 대기와 대용량 미리보기 안내`)를 설계서대로 구현·검증하고 PR을 올린다. 실제 확인하지 않은 결과를 성공으로 기록하지 않는다.

# 시작과 격리

- AGENTS.md, GitHub 이슈 #34의 최신 본문·댓글, reference/long-running-lock-wait-design.md, reference/data-design.md(10-3, 12, 14, 15절)를 읽는다.
- contracts/manage.md, contracts/ingest.md와 관련 코드(dashkit/manage/context.py, dashkit/manage/routes/tables.py, dashkit/manage/routes/structure.py, dashkit/ingest/operation.py, dashkit/ingest/add.py, dashkit/ingest/remove.py, dashkit/ingest/compact.py, dashkit/ingest/structure.py, web/shared/ingest.js, web/admin/menus/tables.js, web/strings.js)를 확인한다. 이슈와 계약이 다르면 계약을 기준으로 해석하고 차이를 보고한다.
- 최신 origin/main을 가져와 기준 SHA를 기록하고 그 커밋에서 새 격리 worktree와 작업 브랜치(예: feat/34-long-running-lock-wait)를 준비한다. 비밀값·토큰은 환경 변수로만 취급하고 로그·문서·커밋에 남기지 않는다. 읽기 경로는 dash_inspect 또는 기존 읽기 전용 계정만 사용한다.

# 구현

1. 잠금 대기 한도 분리 (`MANAGE_JOB_LOCK_WAIT_MS` 기본 600000ms vs `MANAGE_LOCK_WAIT_MS` 기본 5000ms):
   - `dashkit/manage/context.py`: `Limits`에 `job_lock_wait_ms` 추가.
   - `dashkit/manage/routes/tables.py`·`structure.py`: 모든 202 작업(`ingest`, `remove`, `restore`, `config`, `create`, `compact`, `restore_config`)에 `limits.job_lock_wait_ms` 전달. 즉시 응답(JSON `rows` 직접 입력·붙여넣기, 빠른 취소 `fast_only=True`)은 `limits.lock_wait_ms` 유지.
   - `delete_batch`: 잠금 전 폴더 목록과 `dash_inspect`(`_recorded()`) 기반 사전 판별로 빠른 길 비후보(이전 배치 또는 `inserted != rows` / `updated`·`skipped`·`unchanged > 0`)는 `fast_only` 5초 대기 없이 바로 202 `remove` 작업으로 진입하게 한다(없는 배치 404·합친 배치 409 즉시 응답은 유지).
   - `dashkit/ingest/structure.py` `change_config`: `locked(..., wait_ms=wait_ms)` 확보 후 `store.project_lock(project.name)`에 진입하도록 잠금 순서를 정리한다.
2. 대기 중 진행 단계 `lock` 통일:
   - `dashkit/ingest/operation.py` `locked()`에 `on_acquired` 콜백을 추가하고, `remove_batch`, `restore_batch`, `compact`, `change_config`에 `progress` 콜백을 연결한다.
   - 모든 202 작업이 잠금 대기 중 `{"step": "lock"}`("다른 쓰기가 끝나기를 기다리는 중…")을 보고하고, 잠금 획득 직후 본 작업 단계(`remove`·`restore`는 `"rebuild"`, `config`·`create`·`compact`·`restore_config`는 `"apply"`, `ingest`는 기존 계약대로 `"lock"` 유지)로 전이한다.
   - 대기 한도 초과 시 작업 결과에 `status: "failed"`, `error: {"status": 409, "detail": "apply_running", ...}`가 남는지 확인한다.
3. 50만 행 이상 대용량 미리보기 안내:
   - `web/shared/ingest.js`에 `LARGE_BATCH_ROWS = 500_000`, `previewInsertRows(batches)`, `isLargeBatchPreview(batches, threshold = LARGE_BATCH_ROWS)`를 추가한다(단일·여러 파일 합산, `total - invalid` 기준).
   - `web/strings.js`의 `S.admin.data`에 `largeBatchHint` 문구를 추가하고, `web/admin/menus/tables.js`의 `runNotes(r, { preview })`·`resultBox`·`bulkResult`에서 미리보기(`preview: true`)일 때만 표시한다.
4. 구성·계약 문서 갱신:
   - `.env.example`, `docker-compose.yml`에 `MANAGE_JOB_LOCK_WAIT_MS=600000`을 추가한다.
   - `contracts/manage.md`("한도", "데이터 더하기", "작업")와 `contracts/ingest.md`("쓰기 잠금")를 갱신한다.
5. 기존 전역 단일 advisory lock, 잠금→프로젝트 확인→복구→대조→작업→checks 순서와 단일 트랜잭션 원자성을 변경하지 않는다.

# 테스트 및 검증

1. 테스트 추가:
   - 백엔드: 선행 쓰기 잠금 중 후속 202 작업이 `step: "lock"`으로 기다렸다가 해제 후 성공하는 테스트, `MANAGE_JOB_LOCK_WAIT_MS` 초과 시 작업 결과 409 `apply_running` 테스트, 즉시 응답(직접 입력·빠른 취소)의 `MANAGE_LOCK_WAIT_MS` 초과 409 테스트, 이전 배치 되돌리기(`DELETE`)의 202 대기 성공 테스트.
   - 프런트엔드: `tests/js/ingest.test.mjs`에 499,999 / 500,000행 경계(단일 파일, 여러 파일 합산, `invalid` 제외) 테스트.
2. 실행 및 확인:
   - `make test-unit`
   - `node --test tests/js/ingest.test.mjs`
   - `pytest tests/test_admin_ui_static.py`
   - 격리된 빈 테스트 볼륨에서 `AUTH_COOKIE_SECURE=0 make test` (동시 쓰기·복구·프로젝트 전환·관리 API 테스트 포함 전체 통과 확인 후 `make test-down`)
   - `git diff --check`

# PR 및 최종 보고

- 커밋·푸시 후 `.github/pull_request_template.md` 형식에 맞춰 한국어 PR을 연다(`Closes #34` 또는 `Refs #34`).
- 리뷰 지적이나 CI 실패가 있으면 수정 후 관련 테스트를 재실행한다.
- PR 링크, 기준 SHA, 변경 요약, 단위·통합·경계 테스트 결과를 간결하게 보고한다.
````
