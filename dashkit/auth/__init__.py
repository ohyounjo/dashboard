"""웹앱 로그인·사용자 관리·접근 제어. 계약: contracts/auth.md.

인증 수단(비밀번호, 세션, 일회용 토큰, 메일)은 이 패키지 안에 가둔다. 나중에 사내 SSO를 붙일 때 바꾸는 곳은 여기뿐이다.

- settings.py   환경변수(AUTH_*, WEB_ORIGINS, WEB_PUBLIC_URL)
- passwords.py  Argon2id 해시와 비밀번호 정책
- sessions.py   세션 토큰, 쿠키, 만료 계산
- store.py      dashkit_auth 스키마 읽기·쓰기(dash_auth 계정만)
- guard.py      상태를 바꾸는 요청의 출처·본문 형식 검사(CSRF)
- routes.py     /api/auth/*, 로그인 확인 의존성(require_user 등)
- cli.py        python -m dashkit auth {init|create-admin}
"""
