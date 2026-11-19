#!/bin/sh
# data-db 최초 초기화(initdb) 때만 실행된다.
# - 로더 계정(DATA_DB_OWNER_USER): superuser가 아닌 일반 역할. 데이터 DB의 소유자이고 CREATEDB만 가진다
#   (CREATEDB는 테스트가 임시 DB를 만들 때 쓴다).
# - 읽기 계정(DATA_DB_READER_USER): 로그인만. raw/dash 권한은 로더가 적재할 때마다 부여한다.
set -eu

: "${DATA_DB_OWNER_USER:?DATA_DB_OWNER_USER가 필요합니다}"
: "${DATA_DB_OWNER_PASSWORD:?DATA_DB_OWNER_PASSWORD가 필요합니다}"
: "${DATA_DB_READER_USER:?DATA_DB_READER_USER가 필요합니다}"
: "${DATA_DB_READER_PASSWORD:?DATA_DB_READER_PASSWORD가 필요합니다}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v owner="$DATA_DB_OWNER_USER" -v owner_pw="$DATA_DB_OWNER_PASSWORD" \
  -v reader="$DATA_DB_READER_USER" -v reader_pw="$DATA_DB_READER_PASSWORD" \
  -v db="$POSTGRES_DB" <<'SQL'
CREATE ROLE :"owner" LOGIN PASSWORD :'owner_pw' NOSUPERUSER CREATEDB NOCREATEROLE;
ALTER DATABASE :"db" OWNER TO :"owner";
ALTER SCHEMA public OWNER TO :"owner";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE ROLE :"reader" LOGIN PASSWORD :'reader_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE;
SQL
echo "01-reader.sh: 역할 $DATA_DB_OWNER_USER(일반, CREATEDB), $DATA_DB_READER_USER(읽기) 생성"
