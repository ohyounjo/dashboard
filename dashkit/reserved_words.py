"""테이블·열·데이터셋 이름에 쓸 수 없는 SQL 예약어.

PostgreSQL 16 매뉴얼 부록 C(SQL Key Words)에서 PostgreSQL 열이 "reserved"인 단어와
"reserved (can be function or type)"인 단어, 그리고 SQL:2016 표준 예약어 중 열 이름으로
자주 쓰이는 단어를 합쳤다. 표준 예약어를 포함하는 이유는 `start`, `end`, `value`처럼
PostgreSQL에서는 통과하지만 다른 도구(Superset SQL 생성, 다른 DB)에서 따옴표를 요구하는
이름을 처음부터 막기 위해서다.
"""

POSTGRES_RESERVED = frozenset("""
all analyse analyze and any array as asc asymmetric authorization binary both case cast check
collate collation column concurrently constraint create cross current_catalog current_date
current_role current_schema current_time current_timestamp current_user default deferrable desc
distinct do else end except false fetch for foreign freeze from full grant group having ilike in
initially inner intersect into is isnull join lateral leading left like limit localtime
localtimestamp natural not notnull null offset on only or order outer overlaps placing primary
references returning right select session_user similar some symmetric system_user table
tablesample then to trailing true union unique user using variadic verbose when where window with
""".split())

SQL_STANDARD_RESERVED = frozenset("""
abs absolute add alter at avg begin between bigint blob boolean by call called cascaded char
character clob close commit condition connect count cube current cursor cycle date day deallocate
dec decimal declare delete describe double drop dynamic each element escape every exec execute
exists external filter float free function get global hold hour identity in indicator inout input
insensitive insert int integer interval language large local lower match max member merge method
min minute mod modifies module month national nchar nclob new no none numeric of old open out over
overlay parameter partition percent period position precision prepare procedure range rank reads
real recursive release result return returns revoke rollback row rows savepoint scope scroll search
second sensitive set smallint specific sql sqlexception sqlstate sqlwarning start static
submultiset sum system time timestamp translate treat trigger trim uescape unknown unnest update
upper value values varchar varying versioning whenever within without year
""".split())

RESERVED_WORDS = POSTGRES_RESERVED | SQL_STANDARD_RESERVED


def is_reserved(name: str) -> bool:
    return name.lower() in RESERVED_WORDS
