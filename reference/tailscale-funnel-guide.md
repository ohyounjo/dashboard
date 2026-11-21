# [참고용] WSL 대시보드를 Tailscale Funnel로 접속하기

2026-11-21에 PC1의 WSL에서 실행한 대시보드를 PC2의 Windows 브라우저에서 확인한 설정이다. PC1과 PC2의 Windows에는 Tailscale을 설치하지 않았고, PC1의 WSL에만 설치했다. `{도메인 주소}`는 PC1의 Tailscale 도메인으로 바꿔 입력한다. 포트는 이 환경의 예시다.

> Funnel은 서비스와 Superset 접속 경로를 인터넷에 공개한다. HTTPS는 전송 구간을 보호하지만 접속자를 로컬 네트워크나 tailnet으로 제한하지는 않는다. 관리 화면과 Superset의 로그인 계정을 안전하게 관리하고, 공개가 필요할 때만 켠다. tailnet 구성원에게만 공개하려면 Funnel 대신 Tailscale Serve를 사용하고 접속 PC에도 Tailscale이 필요하다.

## 구성

| 공개 주소 | PC1 WSL의 연결 대상 | 용도 |
|---|---|---|
| `https://{도메인 주소}:8443` | `http://127.0.0.1:8080` | 웹앱과 운용 화면 |
| `https://{도메인 주소}:10000` | `http://127.0.0.1:8088` | Superset 그래프 임베드 |

기존 `https://{도메인 주소}`의 443 포트는 다른 서비스(`127.0.0.1:8644`)에서 사용 중이므로 그대로 둔다. Docker의 웹앱·Superset 포트는 `127.0.0.1`에만 열어 두고 Funnel이 HTTPS를 처리한다. 관리 서비스는 웹앱의 `/api/manage/`를 통해 접근한다.

## 설정 방법

1. PC1의 WSL에서 Tailscale에 로그인하고 Funnel을 사용할 수 있는지 확인한다. 이 환경에서는 WSL 네트워크가 `mirrored` 모드다. Tailscale 설정 변경에 관리자 권한이 필요하면 WSL 터미널에서 다음 명령을 한 번 실행한다. 사용자 이름은 실제 WSL 계정에 맞춘다.

   ```bash
   sudo tailscale set --operator=ohyounjo
   ```

2. 프로젝트의 `.env`에 아래 값을 설정한다. 다른 기기의 브라우저가 사용하는 주소로 바꿔야 하므로 `SUPERSET_PUBLIC_URL`에 `localhost`를 쓰지 않는다. 기존 `.env`의 비밀번호와 토큰은 유지하며 저장소에 커밋하지 않는다.

   ```dotenv
   WEB_ORIGINS=http://localhost:8080,http://127.0.0.1:8080,https://{도메인 주소}:8443
   WEB_PUBLIC_URL=https://{도메인 주소}:8443
   SUPERSET_PUBLIC_URL=https://{도메인 주소}:10000
   AUTH_COOKIE_SECURE=1
   ```

   `WEB_ORIGINS`는 요청과 그래프 임베드의 허용 출처이고, `WEB_PUBLIC_URL`은 메일 링크 주소다. `AUTH_COOKIE_SECURE=1`은 세션 쿠키를 HTTPS에서만 보내도록 한다. 평소 PC1에서도 HTTPS 주소로 접속한다.

3. 프로젝트 디렉터리에서 설정을 반영한다. `make sync`는 현재 프로젝트의 Superset 객체와 임베드 허용 출처를 다시 반영한다. 관리 서비스에도 변경된 환경을 적용하기 위해 `make manage`를 실행한다.

   ```bash
   make up
   make sync
   make manage
   ```

4. 기존 Funnel을 확인한 뒤 비어 있는 포트에 각각 등록한다. 아래 명령은 백그라운드에서 유지된다.

   ```bash
   tailscale funnel status
   tailscale funnel --bg --https=8443 http://127.0.0.1:8080
   tailscale funnel --bg --https=10000 http://127.0.0.1:8088
   ```

5. PC2에서 `https://{도메인 주소}:8443/admin/`을 열어 admin으로 로그인하고 데이터 메뉴와 대시보드 그래프를 확인한다. PC1의 WSL, Docker 서비스와 Tailscale이 실행 중이어야 한다.

## 확인과 종료

PC1에서 현재 공개 설정과 서비스 응답을 확인한다.

```bash
tailscale funnel status
curl -I https://{도메인 주소}:8443/admin/
curl -i https://{도메인 주소}:10000/health
```

PC2에서 접속할 수 없으면 PC1의 WSL과 Tailscale, Docker 서비스 상태를 먼저 확인한다. 로그인은 되지만 그래프가 보이지 않으면 `SUPERSET_PUBLIC_URL`, `WEB_ORIGINS`, Superset Funnel과 `make sync` 결과를 확인한다. 네트워크에서 8443 또는 10000 포트를 차단하지 않는지도 확인한다.

두 공개 경로만 끄려면 다음을 실행한다. 다른 서비스의 443 포트에는 영향을 주지 않는다.

```bash
tailscale funnel --https=8443 off
tailscale funnel --https=10000 off
```

다시 켤 때는 위 4단계의 두 등록 명령을 실행한다. 모든 Funnel 설정을 지우는 `tailscale funnel reset`은 기존 서비스까지 중단할 수 있으므로 사용하지 않는다.
