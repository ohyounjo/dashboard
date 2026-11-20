"""Studio: 구성 화면의 서버 쪽(보강설계서 6절). 계약: contracts/studio.md.

`projects/<이름>/`의 파일을 고치고 check → load → sync를 실행하는 YAML 편집기의 API다. 전용 저장소나 DB는 없다.
tools 컨테이너와 같은 권한(owner DB 계정, Superset 관리자 계정, projects/ 쓰기)을 가지므로 backend(dashkit/api)와
분리된 프로세스로 뜬다(`python -m dashkit studio`, compose의 studio 프로필). 인증이 없다. 외부에 열지 않는다.

모듈:
- app.py       FastAPI 앱과 경로
- guard.py     요청 보호(Host·Origin·Content-Type 검사, 응답 헤더)

로직(store, ops, yamledit, roundtrip, jobs, upload)은 공용 코어 dashkit/project/에 있다(관리 서비스와 함께 쓴다).
이 폴더의 같은 이름 모듈은 기존 import 경로를 위한 다시 내보내기다.
"""
