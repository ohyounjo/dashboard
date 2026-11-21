"""Superset의 실제 정적 경로가 등록 가능한 워커를 제공하는지 확인한다."""
from __future__ import annotations

import os
import urllib.request

import pytest


@pytest.mark.integration
def test_service_worker_is_served_as_javascript():
    base = os.environ.get("SUPERSET_URL", "http://superset:8088").rstrip("/")
    with urllib.request.urlopen(f"{base}/static/service-worker.js", timeout=30) as response:
        assert response.status == 200
        assert response.headers.get_content_type() in {"application/javascript", "text/javascript"}
        assert response.read().strip()
