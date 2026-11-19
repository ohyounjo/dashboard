"""Superset 설정. 값은 모두 환경변수에서 읽는다(.env → docker-compose.yml).

근거: contracts/superset-notes.md 4~6절.
"""
import copy
import os
from urllib.parse import quote

from superset.config import TALISMAN_CONFIG as _DEFAULT_TALISMAN_CONFIG


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"환경변수 {name}가 필요합니다")
    return value


# ---------------------------------------------------------------- 기본
SECRET_KEY = _require("SUPERSET_SECRET_KEY")

SQLALCHEMY_DATABASE_URI = "postgresql+psycopg2://{user}:{pw}@{host}:{port}/{db}".format(
    user=quote(os.environ.get("SUPERSET_DB_USER", "superset"), safe=""),
    pw=quote(_require("SUPERSET_DB_PASSWORD"), safe=""),
    host=os.environ.get("SUPERSET_DB_HOST", "superset-db"),
    port=os.environ.get("SUPERSET_DB_PORT", "5432"),
    db=os.environ.get("SUPERSET_DB_NAME", "superset"),
)

# 예제 데이터는 쓰지 않는다(superset load-examples를 실행하지 않는다)
SUPERSET_LOAD_EXAMPLES = False

# ---------------------------------------------------------------- 임베드
FEATURE_FLAGS = {"EMBEDDED_SUPERSET": True}

GUEST_ROLE_NAME = "EmbedGuest"  # superset/init_guest_role.py가 만든다
GUEST_TOKEN_JWT_SECRET = _require("GUEST_TOKEN_JWT_SECRET")
GUEST_TOKEN_JWT_ALGO = "HS256"
GUEST_TOKEN_JWT_AUDIENCE = _require("GUEST_TOKEN_JWT_AUDIENCE")
GUEST_TOKEN_JWT_EXP_SECONDS = 300

# 웹앱 출처만 iframe으로 띄울 수 있게 한다. CORS는 끈다(SDK는 iframe + postMessage).
# 6.1.0 기본값은 ENABLE_CORS = True(지도 타일 출처 허용)이므로 명시적으로 끈다.
ENABLE_CORS = False
WEB_ORIGINS = [o.strip() for o in os.environ.get("WEB_ORIGINS", "").split(",") if o.strip()]
if not WEB_ORIGINS:
    raise RuntimeError("환경변수 WEB_ORIGINS가 필요합니다")

TALISMAN_ENABLED = True
TALISMAN_CONFIG = copy.deepcopy(_DEFAULT_TALISMAN_CONFIG)
# Studio(구성 화면)의 미리보기는 Studio → 웹앱 → Superset으로 iframe이 겹친다. frame-ancestors는 모든 조상을 보므로
# STUDIO_ORIGIN이 있을 때만(make studio) 그 출처를 정확히 더한다. 임베드 허용 도메인(Referer 검사)은 웹앱 출처 그대로다.
STUDIO_ORIGIN = os.environ.get("STUDIO_ORIGIN", "").strip()
TALISMAN_CONFIG["content_security_policy"]["frame-ancestors"] = list(WEB_ORIGINS) + ([STUDIO_ORIGIN] if STUDIO_ORIGIN else [])
TALISMAN_CONFIG["frame_options"] = None  # X-Frame-Options 대신 frame-ancestors로 제어

# ---------------------------------------------------------------- 캐시
# 차트 데이터 캐시를 끈다(적재 직후 바로 반영, Redis 없음). 필터 상태 등은 메타데이터 DB를 쓴다.
CACHE_CONFIG = {"CACHE_TYPE": "NullCache"}
DATA_CACHE_CONFIG = {"CACHE_TYPE": "NullCache"}
