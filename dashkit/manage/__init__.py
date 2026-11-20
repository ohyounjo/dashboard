"""관리 서비스(manager): 로그인한 운용자의 데이터 운용 API `/api/manage/…`(설계서 10절, 계약 contracts/manage.md).

`python -m dashkit manage`(compose의 manage 프로필)로 뜬다. 포트를 밖에 열지 않고 nginx의 /api/manage/로만 닿는다.
backend와 따로 두는 이유: owner DB 계정과 Superset 관리자 계정을 모든 사용자가 닿는 backend에 주지 않는다.

**라우팅과 권한만 갖는다.** 로직은 공용 코어(dashkit/project/, dashkit/ingest/)에 한 벌만 있고 Studio·CLI와 함께 쓴다.

- app.py           FastAPI 앱, 요청 보호, 오류 형식
- routes/          경로 모듈. 기능을 더하는 일은 모듈 하나와 routes/__init__.py의 목록 한 줄이다
- context.py       현재 프로젝트, 한도 값
"""
