#!/bin/sh
# 운용자 조회용 읽기 계정(DATA_DB_INSPECT_USER, 기본 dash_inspect). 계약: contracts/ingest.md "DB 계정", contracts/auth.md "DB 계정".
#
# - data-db 최초 초기화(initdb) 때 자동으로 실행된다(02-auth.sh 다음).
# - 이미 있는 볼륨에는 `make data-init`이 data-db 컨테이너 안에서 같은 스크립트를 실행한다. 여러 번 실행해도 된다.
# - 역할 생성에 superuser가 필요하므로 컨테이너 안의 postgres 계정(로컬 소켓)으로 돈다.
#
# 하는 일: 역할이 없으면 만들고 비밀번호를 환경변수 값으로 맞춘다(로그인만, 쓰기·역할·DB 생성 권한 없음).
# raw·dash·dashkit_meta의 SELECT는 로더가 적재할 때마다 준다(dash_reader에게 dash 권한을 주는 것과 같은 방식).
# dashkit_auth에는 어떤 권한도 주지 않는다(02-auth.sh가 PUBLIC의 권한을 모두 거둔다).
set -eu

: "${DATA_DB_INSPECT_USER:=dash_inspect}"
: "${DATA_DB_INSPECT_PASSWORD:?DATA_DB_INSPECT_PASSWORD가 필요합니다}"
: "${DATA_DB_OWNER_USER:?DATA_DB_OWNER_USER가 필요합니다}"
: "${DATA_DB_READER_USER:?DATA_DB_READER_USER가 필요합니다}"
INSPECT_DB=${POSTGRES_DB:?POSTGRES_DB가 필요합니다}
INSPECT_SU=${POSTGRES_USER:-postgres}

if [ "$DATA_DB_INSPECT_PASSWORD" = "__GENERATE__" ]; then
  echo "03-inspect.sh: DATA_DB_INSPECT_PASSWORD가 아직 생성되지 않음(.env). make data-init이 채운다" >&2
  exit 1
fi
for other in "$DATA_DB_OWNER_USER" "$DATA_DB_READER_USER" "${DATA_DB_AUTH_USER:-dash_auth}" "$INSPECT_SU"; do
  if [ "$DATA_DB_INSPECT_USER" = "$other" ]; then
    echo "03-inspect.sh: DATA_DB_INSPECT_USER는 owner·reader·auth·superuser와 다른 이름이어야 합니다" >&2
    exit 1
  fi
done

psql -X -q -v ON_ERROR_STOP=1 --username "$INSPECT_SU" --dbname "$INSPECT_DB" -v inspect="$DATA_DB_INSPECT_USER" <<'SQL'
\getenv inspect_pw DATA_DB_INSPECT_PASSWORD
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', :'inspect')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'inspect') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'inspect', :'inspect_pw') \gexec
-- 조회 계정은 기본으로 읽기 전용 트랜잭션이다(권한과 함께 한 번 더 막는다)
SELECT format('ALTER ROLE %I SET default_transaction_read_only = on', :'inspect') \gexec
SQL
echo "03-inspect.sh: 역할 $DATA_DB_INSPECT_USER(읽기 전용 조회)"
