# dashkit 이미지 하나를 backend(API)와 tools(check/load/sync/테스트)가 함께 쓴다.
# 빌드 컨텍스트는 저장소 루트다(contracts/를 함께 복사).
FROM python:3.12.16-slim-bookworm

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
RUN find /app -name __pycache__ -type d -prune -exec rm -rf {} +

USER dashkit
EXPOSE 8000
CMD ["python", "-m", "dashkit", "api"]
