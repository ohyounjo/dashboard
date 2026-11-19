"""Studio: 구성 화면의 서버 쪽(보강설계서 6절). 계약: contracts/studio.md.

`projects/<이름>/`의 파일을 고치고 check → load → sync를 실행하는 YAML 편집기의 API다. 전용 저장소나 DB는 없다.
tools 컨테이너와 같은 권한(owner DB 계정, Superset 관리자 계정, projects/ 쓰기)을 가지므로 backend(dashkit/api)와
분리된 프로세스로 뜬다(`python -m dashkit studio`, compose의 studio 프로필). 인증이 없다. 외부에 열지 않는다.

모듈:
- app.py       FastAPI 앱과 경로
- guard.py     요청 보호(Host·Origin·Content-Type 검사, 응답 헤더)
- upload.py    multipart 업로드 읽기(크기 한도, 파일 이름 검사)
- store.py     프로젝트 목록, 저장(임시 사본 → 검증 → 원자적 교체), 바뀐 파일 목록
- ops.py       설정 변경 연산(등록 표)
- yamledit.py  YAML을 고친 곳만 다시 쓰기
- roundtrip.py yamledit이 다룰 수 없는 파일을 ruamel.yaml round-trip으로 쓰기(이 모듈만 ruamel을 import한다)
- jobs.py      apply(check → load → sync) 실행과 진행 로그
"""
