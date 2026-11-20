#!/bin/sh
# 인증 DB: dashkit_auth 스키마와 인증 전용 계정(DATA_DB_AUTH_USER). 계약: contracts/auth.md "DB 계정".
#
# - data-db 최초 초기화(initdb) 때 자동으로 실행된다(01-reader.sh 다음).
# - 이미 있는 볼륨에는 `make auth-init`이 data-db 컨테이너 안에서 같은 스크립트를 실행한다. 여러 번 실행해도 된다.
# - 역할 생성에 superuser가 필요하므로 컨테이너 안의 postgres 계정(로컬 소켓)으로 돈다.
#
# 하는 일
#   1. 역할이 없으면 만든다. 비밀번호는 매번 환경변수 값으로 맞춘다(.env를 바꾼 뒤 다시 실행하면 반영).
#   2. 스키마와 schema_version 표가 없으면 만든다. 스키마 소유자는 superuser다
#      (로더 계정이 DB 소유자여도 이 스키마는 지우지 못한다).
#   3. AUTH_SQL_DIR의 번호 붙은 SQL(001_init.sql …) 중 schema_version에 없는 것을 번호 순서대로,
#      파일 하나를 한 트랜잭션으로 적용하고 기록한다.
#   4. 권한을 다시 준다: 인증 계정은 dashkit_auth 표의 SELECT·INSERT·UPDATE·DELETE만(schema_version은 SELECT만).
#      다른 계정(PUBLIC)에는 이 스키마의 어떤 권한도 없다.
set -eu

: "${DATA_DB_AUTH_USER:?DATA_DB_AUTH_USER가 필요합니다}"
: "${DATA_DB_AUTH_PASSWORD:?DATA_DB_AUTH_PASSWORD가 필요합니다}"
: "${DATA_DB_OWNER_USER:?DATA_DB_OWNER_USER가 필요합니다}"
: "${DATA_DB_READER_USER:?DATA_DB_READER_USER가 필요합니다}"
AUTH_SQL_DIR=${AUTH_SQL_DIR:-/dashkit-auth-sql}
AUTH_DB=${POSTGRES_DB:?POSTGRES_DB가 필요합니다}
AUTH_SU=${POSTGRES_USER:-postgres}

if [ "$DATA_DB_AUTH_PASSWORD" = "__GENERATE__" ]; then
  echo "02-auth.sh: DATA_DB_AUTH_PASSWORD가 아직 생성되지 않음(.env). make auth-init이 채운다" >&2
  exit 1
fi
if [ "$DATA_DB_AUTH_USER" = "$DATA_DB_OWNER_USER" ] || [ "$DATA_DB_AUTH_USER" = "$DATA_DB_READER_USER" ] \
   || [ "$DATA_DB_AUTH_USER" = "$AUTH_SU" ]; then
  echo "02-auth.sh: DATA_DB_AUTH_USER는 owner·reader·superuser와 다른 이름이어야 합니다" >&2
  exit 1
fi
if ! ls "$AUTH_SQL_DIR"/[0-9][0-9][0-9]_*.sql > /dev/null 2>&1; then
  echo "02-auth.sh: SQL 파일이 없음($AUTH_SQL_DIR). compose의 data-db 마운트를 확인하세요" >&2
  exit 1
fi

dashkit_auth_psql() {
  psql -X -q -v ON_ERROR_STOP=1 --username "$AUTH_SU" --dbname "$AUTH_DB" "$@"
}

# 1~2. 역할, 스키마, 버전 표. 비밀번호는 명령줄이 아니라 환경변수에서 읽는다(\getenv)
dashkit_auth_psql -v auth="$DATA_DB_AUTH_USER" <<'SQL'
\getenv auth_pw DATA_DB_AUTH_PASSWORD
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION', :'auth')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'auth') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'auth', :'auth_pw') \gexec
CREATE SCHEMA IF NOT EXISTS dashkit_auth;
CREATE TABLE IF NOT EXISTS dashkit_auth.schema_version (
    version    integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
SQL

# 3. 번호 순서대로 적용
applied=0
for f in "$AUTH_SQL_DIR"/[0-9][0-9][0-9]_*.sql; do
  v=$(basename "$f" | cut -c1-3 | sed 's/^0*//')
  [ -n "$v" ] || { echo "02-auth.sh: 버전 번호는 001부터: $f" >&2; exit 1; }
  done_v=$(dashkit_auth_psql -tA -c "SELECT 1 FROM dashkit_auth.schema_version WHERE version = $v")
  [ "$done_v" = 1 ] && continue
  { cat "$f"; printf '\nINSERT INTO dashkit_auth.schema_version (version) VALUES (%s);\n' "$v"; } \
    | dashkit_auth_psql --single-transaction
  echo "02-auth.sh: $(basename "$f") 적용"
  applied=$((applied + 1))
done

# 4. 권한(매번)
dashkit_auth_psql -v auth="$DATA_DB_AUTH_USER" <<'SQL'
REVOKE ALL ON SCHEMA dashkit_auth FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA dashkit_auth FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA dashkit_auth FROM PUBLIC;
GRANT USAGE ON SCHEMA dashkit_auth TO :"auth";
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA dashkit_auth TO :"auth";
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON dashkit_auth.schema_version FROM :"auth";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA dashkit_auth TO :"auth";
SQL

latest=$(dashkit_auth_psql -tA -c "SELECT coalesce(max(version), 0) FROM dashkit_auth.schema_version")
echo "02-auth.sh: 역할 $DATA_DB_AUTH_USER, 스키마 dashkit_auth 버전 $latest (이번에 적용 $applied개)"
