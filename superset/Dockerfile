# Superset 6.1.0 + PostgreSQL 드라이버(공식 이미지에는 없다. contracts/superset-notes.md 1절)
FROM apache/superset:6.1.0

USER root
RUN uv pip install --python /app/.venv/bin/python --no-cache psycopg2-binary==2.9.9

# PYTHONPATH=/app/pythonpath(이미지 기본값)에 있는 superset_config.py를 Superset이 읽는다
COPY --chown=superset:superset superset_config.py /app/pythonpath/superset_config.py
COPY --chown=superset:superset init.sh init_guest_role.py /app/superset_init/
RUN chmod 0755 /app/superset_init/init.sh

USER superset
