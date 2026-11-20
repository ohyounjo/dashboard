"""프로젝트 폴더를 다루는 공용 코어(설계서 reference/data-design.md 10-2).

Studio(dashkit/studio)와 관리 서비스(dashkit/manage), CLI가 같은 로직을 쓴다. 두 앱은 라우팅과 권한만 갖는다.
한쪽 앱에만 있는 로직은 결함으로 본다.

모듈:
- store.py     프로젝트 목록, 저장(임시 사본 → 검증 → 원자적 교체), 바뀐 파일 목록
- ops.py       설정 변경 연산(등록 표)
- yamledit.py  YAML을 고친 곳만 다시 쓰기
- roundtrip.py yamledit이 다룰 수 없는 파일을 ruamel.yaml round-trip으로 쓰기(이 모듈만 ruamel을 import한다)
- jobs.py      apply(check → load → sync) 실행과 진행 로그
- runs.py      관리 서비스의 작업(여러 개, 진행률·로그·결과)
- upload.py    multipart 업로드 읽기(크기 한도, 파일 이름 검사)
- locks.py     데이터 DB의 쓰기 잠금(세션 단위 advisory lock, 설계서 10-3)
"""
