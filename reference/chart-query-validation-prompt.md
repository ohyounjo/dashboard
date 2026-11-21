# 차트 쿼리 라벨 검사 보완 오케스트레이션 프롬프트

설계 PR이 병합된 뒤 `ohyounjo/dashboard` 저장소에서 새 Codex 채팅을 열고 아래 블록을 붙여 넣는다. [설계서](chart-query-validation-design.md)

````text
# 목표

ohyounjo/dashboard의 이슈 #55를 구현한다. Superset이 서로 다른 지표를 조용히 제거하는 설정을 적용 전에 찾아내고 원본 차트별 검사 범위를 계약으로 남긴다. 구현과 실측 결과를 검증한 뒤 구현 PR을 리뷰 가능한 상태로 올린다.

# 시작

- AGENTS.md, GitHub 이슈 #55의 본문과 댓글, reference/chart-query-validation-design.md를 읽는다.
- contracts/chart-shorthand.md의 중복 라벨 검사, contracts/README.md의 참조 검사 표, contracts/superset-notes.md 10절과 14절, reference/chart-catalog.md의 원본 유형 안내를 확인한다. 차이가 있으면 구현 전 상태는 계약을 기준으로 해석하고 설계로 바꾸는 동작을 구현 PR에 적는다.
- 최신 main 기준으로 codex/55-chart-query-validation 작업 브랜치를 준비한다. 기존 작업과 충돌하면 작업 공간을 분리한다.

# 작업 순서

1. Superset 6.1.0의 격리된 테스트 스택에서 먼저 실측한다. 갤러리 스키마와 데이터를 복사해 브라우저 관리 화면이 보낸 쿼리를 캡처한다. 쿼리별 columns·metrics와 순서, 응답 상태, 제거된 지표를 기록한다. 요청을 직접 조립하는 API 테스트는 서버 검증 대조용으로 쓴다.
2. 같은 adhoc 라벨의 동일 SQL과 다른 SQL, 저장 지표 이름과 adhoc 라벨이 같은 두 순서, 저장 지표 표시 이름 충돌을 재현한다. mixed의 A/B 쿼리도 각각 확인한다. 원본 table은 행·집계·모드 생략, 덮어쓰기, percent_metrics 및 정렬에 따른 실제 쿼리의 열·지표을 확인한다. 갤러리의 graph_chart, tree_chart, gantt_chart와 카탈로그의 chord, world_map, country_map은 설계서의 표를 따라 요청 구조와 지원 한계를 기록한다.
3. 실측으로 서로 다른 지표가 제거되는 경우는 기존 ConfigError 경로의 검사 오류로 처리한다. 같은 정의의 반복과 서로 다른 쿼리 간 같은 라벨은 실측 범위에서 허용한다. 쿼리 전송 전에 사라지는 지표와 서버가 거부하는 라벨을 구분해 메시지를 작성한다. 현재 증거와 설계의 예상이 다르면 관측 결과를 남기고 검사 지원 범위를 좁혀 근거와 함께 계약에 기록한다. 오류 정책이나 완료 조건 자체가 달라지면 관련 이슈에 합의가 필요한 지점을 남긴다.
4. 기존 레지스트리, 파라미터 변환, 라벨 검사와 오류 수집을 재사용한다. params의 얕은 덮어쓰기와 실제 viz_type을 번들 생성과 맞추고, 문제 값을 제공한 dashboard.yaml 경로와 충돌한 자리를 오류에 표시한다. 미지원 원본 구성의 호환성을 유지한다. AGENTS.md의 계정 구분과 비밀값 규칙을 지키고 실측은 테스트 스택의 데이터 및 볼륨에서 수행한다.
5. 회귀 테스트를 추가한다. mixed의 A/B 내부·사이, params로 충돌이 추가·제거되는 경우, 표시 이름, 실제 YAML 오류 경로, 원본 table 모드와 원본 미지원 입력, 기존 성공 사례를 포함한다. 가능하면 변경 전 실패와 변경 후 통과를 확인한다. 예시 프로젝트 세 개의 설정과 번들 golden을 검증한다.
6. contracts/superset-notes.md 14절에 브라우저 실측 결과를, contracts/chart-shorthand.md 및 contracts/README.md에 확정 계약을, reference/chart-catalog.md에 원본 지원 범위를 반영한다. 차트 레지스트리에서 생성한 표가 달라지면 make gen을 실행한다.
7. make test-unit, 격리된 make test, git diff --check를 실행한다. 실측 또는 테스트가 막히면 원인을 확인해 가능한 복구를 시도하고, 수행한 검증과 미확인 항목을 결과에 적는다. 완료 여부는 설계서의 체크리스트로 확인한다.

# PR 및 보고

- 검증 결과와 실측 근거를 확인하고 구현 브랜치에 커밋·푸시한 뒤 한국어로 PR을 연다. .github/pull_request_template.md를 따르고 Refs #55로 연결한다.
- PR 본문에 오류 정책, 원본 유형별 검사 범위, 실측 증거와 실행한 테스트를 남긴다. 미완료 조건이 있으면 draft로 올리고 확인된 결과와 남은 조건을 명시한다.
- 리뷰가 올라오면 최신 커밋을 기준으로 검사 및 지적을 대조한다. 타당한 지적은 수정하고 관련 검증을 다시 실행한다. 구현 PR 병합은 이 작업에서 별도로 받은 사용자 지시를 따른다.
- 최종 보고에 PR 링크, 검증 결과, 실측에 따라 조정한 설계와 남은 범위를 적는다.
```
