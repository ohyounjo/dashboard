#!/usr/bin/env bash
# superset-init 컨테이너가 실행한다. 몇 번을 다시 실행해도 같은 결과로 성공해야 한다.
set -euo pipefail

: "${SUPERSET_ADMIN_USER:?SUPERSET_ADMIN_USER가 필요합니다}"
: "${SUPERSET_ADMIN_PASSWORD:?SUPERSET_ADMIN_PASSWORD가 필요합니다}"

echo "[superset-init] 1/4 db upgrade"
superset db upgrade

echo "[superset-init] 2/4 관리자 계정(${SUPERSET_ADMIN_USER})"
if superset fab list-users 2>/dev/null | grep -Eq "username:${SUPERSET_ADMIN_USER}( |\||$)"; then
  # 이미 있으면 .env의 비밀번호로 맞춘다(재실행 시에도 .env가 기준)
  superset fab reset-password --username "${SUPERSET_ADMIN_USER}" --password "${SUPERSET_ADMIN_PASSWORD}"
  echo "[superset-init] 관리자 계정이 이미 있음 → 비밀번호만 .env 값으로 맞춤"
else
  superset fab create-admin \
    --username "${SUPERSET_ADMIN_USER}" \
    --firstname Admin --lastname User \
    --email "${SUPERSET_ADMIN_USER}@localhost.invalid" \
    --password "${SUPERSET_ADMIN_PASSWORD}"
fi

echo "[superset-init] 3/4 superset init"
superset init

echo "[superset-init] 4/4 guest 역할"
python /app/superset_init/init_guest_role.py

echo "[superset-init] 완료"
