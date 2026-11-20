# 데이터 추가 적재 작업 보고

`reference/data-prompt.md`(설계서 `reference/data-design.md` 1~4단계)를 진행한 결과다. 2026-11-21 작성.

## 단계별 상태

| 단계 | 상태 | PR | Codex 리뷰 횟수 |
|---|---|---|---|
| 준비 | 할 일 없음: 설계서([ohyounjo/dashboard#22](https://github.com/ohyounjo/dashboard/pull/22))와 범위 조정([ohyounjo/dashboard#23](https://github.com/ohyounjo/dashboard/pull/23))이 시작 전에 이미 main에 병합돼 있었다 | | |
| 1. 기반 | 병합됨 | [ohyounjo/dashboard#24](https://github.com/ohyounjo/dashboard/pull/24) | 7번(6번 지적, 7번째 "지적 없음") |
| 2a. 더하기 | 병합됨 | [ohyounjo/dashboard#25](https://github.com/ohyounjo/dashboard/pull/25) | 5번 |
| 2b. 대량 | 병합됨 | [ohyounjo/dashboard#26](https://github.com/ohyounjo/dashboard/pull/26) | 3번 |
| 3. 조회 | 병합됨 | [ohyounjo/dashboard#27](https://github.com/ohyounjo/dashboard/pull/27) | 3번 |
| 4. 구조 변경 | 병합됨(이 보고서가 든 PR) | [ohyounjo/dashboard#28](https://github.com/ohyounjo/dashboard/pull/28) | 8번(7번 지적, 마지막은 아래 "4단계 리뷰") |
| 5. 선택 기능 · 6. 후순위 | 시작 안 함(범위 밖, 설계서 D7) | | |

모든 PR은 마지막 커밋에서 별도 git worktree로 `make test-unit`과 `make test`를 통과시킨 뒤, GitHub Actions(단위 테스트, 화면 캡처) 통과를 확인하고 `gh pr merge --merge`로 병합했다. 병합 뒤마다 main을 받아 개발 스택에서 `make up && make apply`(devtracker)가 되고 기존 화면이 응답하는 것을 확인했다. 1단계 병합 뒤 개발 스택에 `make data-init`을 실행했다(볼륨은 다시 만들지 않음).

## 단계별 완료 조건 확인(설계서 14절)

### 1. 기반

| 완료 조건 | 확인 |
|---|---|
| 지금 프로젝트(replace만)는 그대로 돈다 | `make test`의 예시 세 개(devtracker·sales-sample·chart-gallery) 적용·화면·번들 스냅샷이 그대로. 예시 설정·CSV·번들 스냅샷은 고치지 않음 |
| 배치 파일로 다시 만들기, 하나씩 더한 결과 = 다시 만든 결과 | `test_ingest_db.py::test_incremental_equals_rebuild_for_each_policy[error/skip/update]` 등 |
| 끊김 복구(묶음 기록 상태마다) | `test_crash_after_commit…`, `test_crash_before_commit_with_all_files_moved…`, `test_crash_with_part_of_a_group_moved…`, `test_unknown_commit_result_is_answered_by_recovery` |
| 계정 경계 | `test_reader_cannot_read_raw_and_inspect_reads_only`, `test_inspect_cannot_read_auth_tables`, `test_manage_routes.py::test_read_paths_never_open_the_owner_account` |
| 1단계 마지막 `make test`: 단위 441 passed, 1단계 621 passed·49 skipped 외 전부 통과 | |

### 2a. 더하기

한 행 폼("저장하고 계속", 키보드만으로), 붙여넣기, 파일 세 길이 같은 배치 파일을 만든다(`test_same_rows_by_form_paste_and_file_make_identical_batch_files`). 올리면 sync 없이 뷰 값이 바뀐다(`test_manage_data.py`). 실패한 것은 폴더·DB 어디에도 남지 않는다(`test_failed_batches_leave_table_and_folder_as_they_were`). 확정 도중 죽인 뒤 다시 띄우면 반영된다(`test_crash_then_restart_applies_the_batch`). 빠른 취소 뒤에도 다시 만든 결과가 같다(`test_ingest_remove.py`).

### 2b. 대량

CP949·탭·세미콜론·표시 이름 머리글을 손대지 않고 받음, 서로 참조하는 두 파일이 한 트랜잭션(부모만 옮긴 뒤 죽여도 둘 다 없음), 200MB 파일을 미리보기 뒤 다시 올리지 않고 넣기(실측 아래), 서로 다른 프로세스의 적재와 apply가 순서를 지킴(`test_ingest_and_apply_from_other_processes_keep_order`).

### 3. 조회

100만 행 테이블에서 어느 페이지든 200행 이하·수 ms(아래 실측), 필터·정렬 입력으로 SQL을 바꿀 수 없음(`test_manage_routes.py::test_filter_injection_is_rejected`, `test_paging.py`). 마지막 `make test`: 단위 444 passed·JS 28 pass·Studio 430 passed, 통합 636 passed·49 skipped 외 전부 통과.

### 4. 구조 변경

| 완료 조건 | 확인 |
|---|---|
| 운용 화면만으로 새 CSV → 테이블 → 데이터셋 → 초안 그래프까지 간다 | `test_manage_structure.py::test_new_table_from_a_file_then_history_restore`(HTTP: 추론 → 만들기(초안 그래프·탭) → 목록·데이터셋·뷰 행 수 → 이력으로 되돌리기), `test_ingest_structure.py::test_new_table_from_csv_is_saved_validated_and_applied[append/replace]`, 브라우저로 마법사 확인 |
| 열을 더하면 입력 폼과 데이터셋에 새 열이 보이고 기존 그래프는 그대로다 | `test_manage_structure.py::test_column_add_reaches_the_form_and_the_dataset`, `test_column_add_on_replace_table_changes_csv_too_and_history_restores_both`, `test_column_add_on_append_table_fills_old_batches_with_default` |
| 같은 연산을 Studio와 manager로 실행한 파일이 같다 | `test_studio_api.py::test_structure_ops_write_the_same_bytes_in_studio_and_the_manager` |
| 합친 뒤 다시 만든 결과가 합치기 전과 같다 | `test_compact_equals_rebuild_and_merged_folder_goes_after_next_load`, `test_compact_crash_is_finished_or_rolled_back[끝까지/되돌림]`, HTTP `test_compact_keeps_rows_and_leaves_one_batch` |

끊김 복구(파일을 바꾼 뒤·커밋 전·커밋 뒤), load 실패 시 전체 되돌림(지문에 들지 않는 변경 포함), 잠금 안의 재검사(전환·이력 해시·검증 사이 바뀐 파일), plan은 복구하지 않음, 다음 폴더 작업이 먼저 정리함도 `tests/test_ingest_structure.py`가 고정한다. 마지막 코드 커밋(`8dcb867`)의 `make test-unit`: 파이썬 단위 446 passed·JS 35 pass·Studio 431 passed, `make test`: 1단계 662 passed·55 skipped, Studio 13, sales-sample 15, chart-gallery 45, 초안 3, 5단계(관리 서비스 데이터·구조 경로) 15 passed — `✔ make test 통과`.

## 실측

1단계(테스트 스택, PostgreSQL 16.10 기본 설정, WSL2 8코어·8GB, ledger 누적 테이블 열 8개·`update` 정책·`checks` 하나·뷰 둘):

| 잰 것 | 결과 | 멈추는 기준 |
|---|---|---|
| 100만 행을 배치 하나로 둔 테이블의 apply | **33.2초** | 10분 |
| 1행 배치 더하기(전체 / 잠금 구간) | 0.39초 / **0.31초** | 잠금 구간 10초 |
| 1만 행 새 배치(전체 / 잠금 구간) | 1.33초 / 0.98초 | |
| 1만 행 `update` 배치(전체 / 잠금 구간) | 1.90초 / 1.74초 | |
| 같은 100만 행을 배치 200개로 나눈 apply | **93.7초** | |

- 기준을 넘지 않아 D1(배치 파일이 원본)을 유지하고, D8의 한도(파일 200MB·200만 행, 요청 500MB, 직접 입력 500행)는 고치지 않았다.
- 2b: 209.7MB·177만 행 파일 미리보기 121.5초, 넣기 125.1초, 그중 잠금 구간 **38.2초**(한도 크기의 파일은 1단계 기준 10초보다 길다. 설계서 15절과 화면 안내에 적음).
- 3단계: 100만 행 페이지 첫 5.7ms, 한가운데 8.2ms, 마지막 2.3ms, 색인 없는 열 정렬 144ms, 필터 2.8ms.

## 리뷰 지적과 처리

모든 지적을 코드·설계서에 대조했고, 아래는 전부 **고침**이다(고치지 않은 지적 없음). 각 댓글에 반영 커밋과 내용을 한국어 답글로 남겼다.

- **#24(1단계, 7번)**: [P1] 복구 트랜잭션을 실제로 커밋한 뒤 묶음 기록 삭제, [P1] 먼저 반영한 손 파일도 복구 대상에, [P1] 저장한 배치를 다시 읽도록 CSV 필드 한도, [P1] 시작 시 복구도 데이터 설정 지문 확인, [P1] 복구 커밋 결과가 불명확하면 파일 보존(503 `recovery_unknown`), [P2] skip_invalid에서 뺀 행과 치명적 오류 구분, [P2] 손 배치도 참조 순서로, [P2] plan의 기준 행 수에 추가 적재 반영, [P2] 설정을 읽기 전후 revision 일치, [P2] 충돌 정책 변경도 누적 테이블 변경으로 표시.
- **#25(2a, 5번)**: [P1] 미리보기 응답을 그 입력 스냅샷에 묶기, [P2] 업로드 검증 오류의 HTTP 상태, [P2] 미리보기 없이 넣은 업로드 임시 파일 정리, [P2] 진행률 갱신에도 요청 세대 검사, [P2] 반영 안 된 배치 삭제의 커밋 여부를 별도 표식(`loaded_at`)으로, [P2] 입력 초기화 때 진행 중 미리보기 무효화.
- **#26(2b, 3번)**: [P2] 형식 오류로 뺀 행의 원래 값 보존, [P2] 행을 뺀 최종 내용으로 중복 확인, [P2] 빠진 행 파일 폴더의 수명, [P2] 필수 열 오류 행을 뺀 뒤 total 갱신, [P2] 머리글 조회 뒤 요청 세대 확인.
- **#27(3단계, 3번)**: [P1] 큰 정수 기본 키를 정밀도 손실 없이(문자열), [P2] 다음 커서도 첫 페이지의 판 유지, [P2] `/api/data` 페이지는 값을 자르지 않음, [P2] 페이지 요청 중 중복 이동 막기.
- **#28(4단계, 8번)**: [P1] 합친 배치를 일반 되돌리기에서 제외(이름의 `-merged` 표식, 409 `merged_batch`), [P2] 겹치지 않는 이름의 접미사가 63자에서 잘려 무한 반복, [P1] append→replace 전환 검사를 잠금 안에서, [P2] 구조 변경의 커밋 판정에 작업별 표식(작업 전 `loaded_at`), [P2] enum을 text로 넓힐 때 `values` 지우기, [P1] 이력 되돌리기에도 전환 검사(새 설정과 견주기), [P1] 복구는 잠금을 잡은 뒤 기록을 다시 읽기, [P2] text로 넓힐 때 `min`·`max` 지우기, [P2] 폼을 연 때의 revision 보내기, [P1] 이력 해시 검사를 잠금 안에서(+ 파일을 바꾸기 직전 재확인), [P1] plan은 구조 변경을 복구하지 않음, [P2] 정확히 저장할 수 없는 경계 값 거절, [P2] 관리 서비스 시작·폴더 작업이 구조 변경을 먼저 정리.

## 설계서와 달라진 점

설계서 3-3 표에 "3판(리뷰)"·"4판(구현 리뷰)"·"4판(구현)" 줄로 적었다. 요점:

- 리뷰가 짚은 빈 곳을 설계서에 반영: 커서는 첫 판을 끝까지 이어 감(8-2), `/api/data`는 자르지 않음(8-1·8-5), 큰 수는 문자열(8-1).
- **설정 변경 이력은 load까지 끝난 변경만**(6-3). 설계서는 "저장 전에 사본"이었으나, 그러면 되돌려진 변경이 이력에 남는다. 사본은 저장 전에 묶음 기록에 두고, 끝난 뒤(또는 복구가) 이력으로 옮긴다.
- `aliases`·`description`을 데이터 설정 지문에서 뺐다(4-4. DB 내용과 무관. 머리글 맞춤 저장 뒤 apply를 요구하지 않으려고).
- 머리글 맞춤 저장은 좁은 경로 `POST /tables/{t}/aliases`(2b), 쓰이는 곳은 `GET /tables/{t}/used_by`(4단계)를 더했다(11절 표에 반영).
- 되돌리기는 "옮긴 뒤 다시 만들기"가 아니라 DB 작업 뒤 커밋 직전에 옮긴다(contracts/ingest.md "되돌리기와 되살리기"에 이유).

## 스스로 정한 판단

1. 쓰기 잠금은 DB 하나의 세션 advisory lock(고정 키), 관리 서비스 5초·CLI/Studio load 10초 대기.
2. 잠금 안에서 뺀 행이 있으면 정규형 파일을 다시 써서 배치 파일에도 들어간 행만 남김.
3. 미리보기는 행 오류가 있어도 끝까지(참조 대조·반영까지) 해 보고 빼고 넣을 행 수를 보임.
4. 빠진 행 파일은 정규형 머리글 + `오류` 열.
5. 키 방식 커서는 기본 키 열을 모두 내보내는 `table:` 데이터셋까지, 나머지는 건너뛰기 방식(100,000행).
6. "이 행 고치기"는 새 경로 없이 기본 키 + 바뀐 열만의 `update` 배치(update 정책 테이블에만).
7. `PATCH /config`는 구조 연산 8개만 받음(탭·차트는 Studio).
8. 배치가 있는 append 테이블을 replace로 바꾸는 것은 거절. 테이블 삭제는 설정에서만 지우고 데이터 파일은 남김.
9. Studio 저장은 replace 테이블의 열 추가를 거절(YAML만 쓰므로). 관리 서비스는 CSV와 함께 바꿈.
10. 새 테이블의 초안 그래프는 Superset 대시보드와 그 탭만(위젯 섹션 없음).
11. `sql:` 데이터셋 조회의 데이터 판을 계보(읽는 테이블)로 좁힘.
12. 테스트용 임시 DB를 지울 때 클라이언트 접속만 끊도록 테스트 도우미를 고침(autovacuum 프로세스를 끊다 실패하던 것).

## 직접 해야 할 것

1. **관리 서비스 띄우기**: 개발 스택에서 `make manage`(compose의 `manage` 프로필. 포트를 밖에 열지 않고 `http://127.0.0.1:8080/api/manage/`로만 닿음). 내리기는 `make manage-down`. 띄우지 않으면 운용 화면의 데이터 메뉴가 숨는다.
2. **운용 화면에서 확인**(`/admin/`, admin 계정): 데이터 → 누적 테이블에서 한 행 넣기("저장하고 계속"), 엑셀 표 붙여넣기, CSV 파일 올리기(미리보기 → 넣기), "방금 추가 취소"와 배치 되돌리기·되살리기, 새 테이블 마법사, 열 추가, 설정 이력. 예시 프로젝트(devtracker)는 모두 replace 테이블이라 "추가"가 없다. 누적 테이블을 시험하려면 새 테이블 마법사로 하나 만들거나 `tests/fixtures/ledger`를 복사한 프로젝트를 쓴다.
3. **프로젝트 폴더 백업 주기 정하기**(설계서 13절 D5). 배치 파일이 원본이므로 서버의 `projects/<이름>/data/`가 백업 대상이다. 운용 화면에서 바꾼 설정은 "설정 이력 → 설정 파일 내려받기"로 받아 저장소에 커밋한다(D4).
4. 개발 스택에 남은 테스트 계정: `e2e-refactor@gmail.com`(admin, 이전 화면 캡처용). 필요 없으면 운용 화면에서 지운다.
5. 다른 세션이 남긴 테스트 스택 셋(`dashkit-test-270610710`, `-4126641840`, `-669929324`, backend가 재시작을 되풀이함)이 떠 있다. 이 작업의 것이 아니라 건드리지 않았다. 필요 없으면 각 폴더에서 `make test-down`.

## 남은 것

- **5단계(선택 기능)**: 민감 열, SQL 콘솔, 내보내기, 열 통계, 신선도 표시, API 토큰, 표 차트 `page_size`. 설정 키·경로·화면을 넣지 않았다(D7). 넣을 때는 `reference/data-prompt.md` "선택 기능을 넣을 때".
- **6단계(후순위)**: 열 이름 변경·삭제(합치기 + 변환), 이어 올리기 등.
- **15절의 한계 가운데 실측으로 드러난 것**: 한도 크기의 파일(200MB)은 잠금 구간이 38초라 그동안 다른 적재가 409 `apply_running`을 받는다(나눠 올리기로 줄임). 배치 200개는 apply에 약 60초가 더 든다(합치기로 줄임). 색인 없는 열 정렬은 100만 행에서 0.1초대이고 테이블이 커지면 느려진다.
