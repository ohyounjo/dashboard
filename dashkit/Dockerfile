# 빌드 단계
#   base   공통(파이썬 의존성, dashkit/, contracts/, 위젯 매니페스트)
#   studio base + ruamel.yaml. compose의 studio 프로필만 쓴다(구성 화면. 운영 배포에 넣지 않는다)
#   app    backend(API)와 tools(check/load/sync/테스트)가 함께 쓰는 이미지. 마지막 단계라서 target을 주지 않으면 이것이 빌드된다
# 빌드 컨텍스트는 저장소 루트다(contracts/와 web/widgets/*/widget.json을 함께 복사).
FROM python:3.12.16-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp

COPY dashkit/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

RUN groupadd --gid 10001 dashkit && useradd --uid 10001 --gid dashkit --no-create-home --shell /usr/sbin/nologin dashkit

WORKDIR /app
COPY --chown=root:root dashkit/ /app/dashkit/
COPY --chown=root:root contracts/ /app/contracts/
# 위젯 매니페스트(widget.json)만 들어간다(.dockerignore). 위젯 코드(widget.js)는 nginx가 web/에서 서빙한다
COPY --chown=root:root web/widgets/ /app/web/widgets/
RUN find /app -name __pycache__ -type d -prune -exec rm -rf {} +

# ---------------------------------------------------------------- studio
FROM base AS studio
COPY dashkit/requirements-studio.txt /tmp/requirements-studio.txt
RUN pip install --no-cache-dir -r /tmp/requirements-studio.txt
USER dashkit
EXPOSE 8090
CMD ["python", "-m", "dashkit", "studio"]

# ---------------------------------------------------------------- app (기본)
FROM base AS app
USER dashkit
EXPOSE 8000
CMD ["python", "-m", "dashkit", "api"]
