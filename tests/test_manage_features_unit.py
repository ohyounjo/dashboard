"""선택 기능 공통 기반(MANAGE_FEATURES), 한도(EXPORT_MAX_ROWS), 신선도(freshness) 단위 테스트."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from dashkit import config, freshness
from dashkit.features import KNOWN_MANAGE_FEATURES, has_manage_feature, manage_features
from dashkit.loader.record import data_revision, db_relevant
from dashkit.manage import context
from dashkit.project import ops


def test_manage_features_parsing(monkeypatch):
    monkeypatch.delenv("MANAGE_FEATURES", raising=False)
    assert manage_features() == frozenset()
    assert not has_manage_feature("profile")

    monkeypatch.setenv("MANAGE_FEATURES", " Profile , EXPORT , unknown_feat , freshness , ")
    assert manage_features() == frozenset({"profile", "export", "freshness"})
    assert manage_features() <= KNOWN_MANAGE_FEATURES
    assert has_manage_feature("profile")
    assert has_manage_feature("export")
    assert has_manage_feature("freshness")
    assert not has_manage_feature("unknown_feat")


def test_export_max_rows_limit(monkeypatch):
    monkeypatch.delenv("EXPORT_MAX_ROWS", raising=False)
    assert context.limits().export_max_rows == 1_000_000

    monkeypatch.setenv("EXPORT_MAX_ROWS", "500")
    assert context.limits().export_max_rows == 500

    monkeypatch.setenv("EXPORT_MAX_ROWS", "0")
    assert context.limits().export_max_rows == 1


def test_parse_freshness_and_is_stale():
    assert freshness.parse_freshness("30m") == timedelta(minutes=30)
    assert freshness.parse_freshness("12h") == timedelta(hours=12)
    assert freshness.parse_freshness("7d") == timedelta(days=7)
    for bad in ("", "0d", "-1h", "7w", "abc", "1.5d"):
        with pytest.raises(ValueError):
            freshness.parse_freshness(bad)

    now = datetime(2026, 11, 22, 12, 0, 0, tzinfo=timezone.utc)
    # 경계 시각: == delta는 정상(False), 1초라도 넘으면 초과(True)
    exact = now - timedelta(hours=12)
    over = now - timedelta(hours=12, seconds=1)
    assert freshness.is_stale(exact, "12h", now) is False
    assert freshness.is_stale(over, "12h", now) is True
    assert freshness.is_stale(None, "12h", now) is False
    assert freshness.is_stale(over, None, now) is False

    # naive datetime과 non-UTC aware datetime 모두 UTC로 정규화
    kst = timezone(timedelta(hours=9))
    exact_kst = exact.astimezone(kst)
    exact_naive = exact.replace(tzinfo=None)
    assert freshness.is_stale(exact_kst, "12h", now) is False
    assert freshness.is_stale(exact_naive, "12h", now) is False
    assert freshness.format_utc_iso(exact_kst) == "2026-11-22T00:00:00Z"


def test_tab_data_as_of_edge_cases():
    project = SimpleNamespace(
        name="proj",
        dashboard={
            "dashboards": {
                "d1": {"charts": [{"dataset": "ds_a"}, {"dataset": "ds_b"}]},
                "d2": {"charts": [{"dataset": "ds_missing_lineage"}]},
                "d3": {"charts": [{"dataset": "ds_no_batch"}]},
            }
        },
    )
    graph = {
        "datasets": {
            "ds_a": {"sources": ["t1"]},
            "ds_b": {"sources": ["t2"]},
            "ds_missing_lineage": {"sources": None},
            "ds_no_batch": {"sources": ["t3"]},
        }
    }
    t1_old = datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)
    t1_new = datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)
    t2_older = datetime(2026, 11, 20, 8, 0, 0, tzinfo=timezone.utc)
    recorded = {
        "t1": {
            "001.csv": SimpleNamespace(added_at=t1_old),
            "002.csv": SimpleNamespace(added_at=t1_new),
        },
        "t2": {
            "001.csv": SimpleNamespace(added_at=t2_older),
        },
        "t3": {
            "001.csv": SimpleNamespace(added_at=None),
        },
    }

    # 여러 테이블/그래프 중 가장 오래된 최신 배치 시각 선택 (t1=11-21 10:00, t2=11-20 08:00 -> 11-20 08:00)
    tab_multi = {"sections": [{"type": "superset", "dashboard": "d1"}, {"type": "gantt", "dataset": "ds_a"}]}
    assert freshness.tab_data_as_of(project, tab_multi, graph, recorded, loaded_project="proj") == "2026-11-20T08:00:00Z"

    # 데이터셋 없음
    assert freshness.tab_data_as_of(project, {"sections": []}, graph, recorded, loaded_project="proj") is None
    # 계보 누락
    assert freshness.tab_data_as_of(project, {"sections": [{"type": "superset", "dashboard": "d2"}]}, graph, recorded, loaded_project="proj") is None
    # 배치 시각 누락
    assert freshness.tab_data_as_of(project, {"sections": [{"type": "superset", "dashboard": "d3"}]}, graph, recorded, loaded_project="proj") is None
    # 타 프로젝트 스냅샷
    assert freshness.tab_data_as_of(project, tab_multi, graph, recorded, loaded_project="other") is None


def test_freshness_not_in_data_revision_and_ops_support():
    schema_a = {
        "tables": {
            "items": {
                "primary_key": ["id"],
                "columns": {"id": {"type": "text"}},
            }
        }
    }
    schema_b = {
        "tables": {
            "items": {
                "primary_key": ["id"],
                "freshness": "7d",
                "columns": {"id": {"type": "text"}},
            }
        }
    }
    dash = {"datasets": {}}
    assert "freshness" not in db_relevant(schema_b["tables"]["items"])
    assert data_revision(schema_a, dash) == data_revision(schema_b, dash)

    doc = {"tables": {"items": {"primary_key": "id", "columns": {"id": {"type": "text"}}}}}
    ops.table_update(doc, {"op": "table.update", "table": "items", "set": {"freshness": "12h"}})
    assert doc["tables"]["items"]["freshness"] == "12h"
    ops.table_update(doc, {"op": "table.update", "table": "items", "set": {"freshness": None}})
    assert "freshness" not in doc["tables"]["items"]
    ops.table_add(doc, {
        "op": "table.add",
        "table": "events",
        "value": {"primary_key": "id", "freshness": "30m", "columns": {"id": {"type": "text"}}},
    })
    assert doc["tables"]["events"]["freshness"] == "30m"


def test_load_project_accepts_freshness_and_sensitive(tmp_path):
    proj = tmp_path / "demo"
    (proj / "data").mkdir(parents=True)
    (proj / "data" / "items.csv").write_text("id,secret\n1,abc\n", encoding="utf-8")
    (proj / "schema.yaml").write_text(
        "tables:\n"
        "  items:\n"
        "    primary_key: id\n"
        "    freshness: 7d\n"
        "    columns:\n"
        "      id: {type: text}\n"
        "      secret: {type: text, sensitive: true}\n",
        encoding="utf-8",
    )
    (proj / "dashboard.yaml").write_text(
        "app: {title: Demo, as_of: '2026-11-22'}\n"
        "datasets:\n"
        "  items: {table: items}\n"
        "dashboards:\n"
        "  main:\n"
        "    title: Main\n"
        "    charts: [{key: c1, title: C1, viz: table, dataset: items, columns: [id]}]\n"
        "    layout: [[c1]]\n"
        "tabs:\n"
        "  - {key: main, label: Main, icon: grid, sections: [{type: superset, dashboard: main}]}\n",
        encoding="utf-8",
    )
    loaded = config.load_project("demo", tmp_path)
    assert loaded.schema["tables"]["items"]["freshness"] == "7d"
    assert loaded.schema["tables"]["items"]["columns"]["secret"]["sensitive"] is True
