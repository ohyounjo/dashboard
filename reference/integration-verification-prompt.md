# 이슈 #53 통합 검증 오케스트레이션 프롬프트

설계 PR 병합 후 새 작업에서 아래를 사용한다. [설계서](integration-verification-design.md)

```text
# 목표
ohyounjo/dashboard 이슈 #53의 병합 조합을 검증하고 보강 보고서를 갱신한다. 결과를 검토 가능한 PR로 올린다. 실제 검증 전에는 성공을 주장하지 않는다.

# 시작과 격리
- AGENTS.md, 이슈 #53의 최신 본문·댓글, reference/integration-verification-design.md, contracts/studio.md, contracts/auth.md, contracts/chart-shorthand.md, contracts/superset-notes.md 13·14절을 읽는다. PR #29·#30·#31과 이슈 #55의 현재 상태를 확인한다. 계약과 이슈가 다르면 계약을 기준으로 해석하고 차이를 보고한다.
- 최신 origin/main을 가져와 SHA를 기록하고 그 커밋의 새 격리 worktree 및 작업 브랜치를 준비한다. 사용자의 다른 변경과 개발 스택·다른 세션의 데이터는 건드리지 않는다. 비밀값은 환경 변수로만 받고 로그나 커밋에 남기지 않는다.
- Makefile의 테스트 스택 이름, 경로, 이미지 접미사, 포트, 정리 대상을 확인한다. projects/zz-newtest와 projects/zz-ingest가 격리 worktree에 없는지 확인한다. 기존 테스트 스택이나 포트가 겹치면 다른 실행에 영향을 주지 않는 구간을 고른다. 자신이 만든 자원만 정리한다.

# 실행 순서
1. 같은 기준 SHA에서 make test-unit과 make test를 실행한다. 명령별 종료 코드, 단계별 결과, pass/fail/skip, 실패 원인과 환경을 기록한다. 브라우저 확인을 위해 KEEP=1을 쓰면 작업 종료 시 같은 격리 worktree에서 자신의 스택만 make test-down으로 정리한다. 실패 시 안전한 범위에서 원인을 조사하고 로그의 비밀값을 제거한다.
2. 실제 브라우저에서 허용된 Studio 출처의 미리보기 안에서 로그인하고 적용된 프로젝트의 Superset 차트를 확인한다. 다른 출처에서는 올바른 주소 안내와 링크가 나오는지 확인한다. 테스트 스택의 STUDIO_ORIGIN과 포트를 맞춘다. 단위 테스트나 API 성공을 화면 로그인·렌더링 성공으로 대신하지 않는다.
3. devtracker, sales-sample, chart-gallery 예시의 check·apply와 갤러리 조회 결과를 확인한다. 격리된 임시 사본에서 bar의 x/groupby 중복은 정확한 dashboard.yaml 경로의 오류로 거부되는지, 유효 예시는 통과하는지 확인한다. 화면·네트워크 근거와 미확인 범위를 남긴다.
4. reference/enhancement-report.md의 오래된 설명을 PR #29·#31과 계약에 맞게 정정한다. can_time_range on Api의 정확한 이름과 두 권한의 기존 실측·권고, 현재 권한과 남은 제한을 구분한다. 해결된 x/groupby 중복 오류와 #55의 미해결 자동 지표 제거·원본 유형 검사 범위를 혼동하지 않는다. 권한을 임의로 넓히지 않는다.
5. 발견된 병합 회귀는 재현 후 고치고 필요한 회귀 테스트와 계약을 갱신한다. 수정 후 관련 검사와 make test-unit, make test를 다시 실행한다. 완료 조건이나 범위가 실제로 바뀌면 이슈를 갱신한다.
6. 테스트와 브라우저 결과, 실패 원인·미확인 사항, 실행 SHA, 기존 제한을 정리하고 git diff --check를 실행한다. 검증하지 못한 항목은 미완료로 명시한다.

# 결과 제출
- 변경을 커밋·푸시하고 저장소 PR 템플릿을 따라 한국어 PR을 만든다. 검증이 미완료라면 draft로 열고 남은 조건을 적는다. 이슈 #53을 연결하되 완료 조건을 모두 충족했을 때만 종료 키워드를 쓴다. 병합은 별도 사용자 지시를 따른다.
- 최종 보고에 PR 링크, 기준 SHA, 명령·브라우저 검증 결과, 발견한 회귀와 수정, 수행하지 못한 항목 및 정리 결과를 적는다.
```
