"""계보와 "쓰이는 곳"(설계서 7-2, contracts/manage.md "계보").

- 데이터셋 → 테이블·열: 로더가 뷰를 만든 직후 PostgreSQL의 의존 정보로 적재 스냅샷의 `datasets.<키>.sources`에 적은 것.
- 그래프 → 데이터셋·열: 설정에서 계산한다(config.referenced_columns, 차트의 dataset, 위젯 섹션).
- 대시보드 → 탭: superset 섹션.
스냅샷은 dash_reader·dash_inspect가 읽을 수 있어 owner가 필요 없다.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from .. import config, widgets


def build(project: config.Project, snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """{tables, datasets, charts, sections, dashboards}. 스냅샷이 이 프로젝트의 것이 아니면 데이터셋의 sources는
    table: 데이터셋만 설정에서 채운다(sql: 데이터셋은 적용해야 안다: sources null)."""
    recorded = (snapshot or {}).get("datasets") or {} if (snapshot or {}).get("project") == project.name else {}
    datasets: dict[str, Any] = {}
    for key, ds in project.dashboard["datasets"].items():
        sources = (recorded.get(key) or {}).get("sources")
        if sources is None and "table" in ds:
            sources = {ds["table"]: list(project.schema["tables"][ds["table"]]["columns"])}
        datasets[key] = {"kind": "table" if "table" in ds else "sql", "table": ds.get("table"), "sources": sources}
    refs = config.referenced_columns(project)
    charts = []
    tab_of: dict[str, list[str]] = {}
    for tab in project.dashboard["tabs"]:
        for sec in tab["sections"]:
            if sec["type"] == "superset":
                tab_of.setdefault(sec["dashboard"], []).append(tab["key"])
    for dkey, chart in config.charts(project):
        charts.append({"key": chart["key"], "title": chart.get("title"), "dashboard": dkey, "dataset": chart["dataset"],
                       "tabs": tab_of.get(dkey, [])})
    sections = []
    registry = widgets.registry()
    for tab in project.dashboard["tabs"]:
        for i, sec in enumerate(tab["sections"]):
            if sec["type"] != "superset" and sec["type"] in registry and registry[sec["type"]].has_dataset:
                sections.append({"tab": tab["key"], "index": i, "type": sec["type"], "dataset": sec["dataset"]})
    return {
        "tables": {t: {"mode": spec.get("mode") or "replace", "columns": list(spec["columns"])}
                   for t, spec in project.schema["tables"].items()},
        "datasets": datasets,
        "charts": charts,
        "sections": sections,
        "dashboards": {k: {"title": d["title"], "tabs": tab_of.get(k, [])} for k, d in project.dashboard["dashboards"].items()},
        "columns_used": {ds: sorted(cols) for ds, cols in refs.items()},
    }


def column_usage(project: config.Project, lineage: Mapping[str, Any], table: str) -> dict[str, dict[str, Any]]:
    """테이블의 각 열을 참조하는 데이터셋·차트·필터·위젯 섹션·labels 매핑을 반환한다."""
    table_spec = project.schema["tables"].get(table)
    if table_spec is None:
        return {}
    dash = project.dashboard
    datasets_cfg = dash.get("datasets") or {}
    lin_datasets = lineage.get("datasets") or {}
    tab_of: dict[str, list[str]] = {}
    for tab in dash.get("tabs") or []:
        for sec in tab.get("sections") or []:
            if sec.get("type") == config.EMBED_SECTION:
                tab_of.setdefault(sec["dashboard"], []).append(tab["key"])
    widget_reg = widgets.registry()

    out: dict[str, dict[str, Any]] = {}
    for col in table_spec["columns"]:
        col_pat = re.compile(rf"\b{re.escape(col)}\b")
        col_datasets: list[str] = []
        col_labels: list[str] = []
        col_metrics: list[dict[str, str]] = []
        for dskey, ds in datasets_cfg.items():
            if ds.get("table") == table:
                col_datasets.append(dskey)
                if col in (ds.get("labels") or {}) and col not in (ds.get("metrics") or {}):
                    col_labels.append(dskey)
                for mkey, mexpr in (ds.get("metrics") or {}).items():
                    if col_pat.search(str(mexpr)):
                        col_metrics.append({"dataset": dskey, "metric": mkey})
            else:
                sources = (lin_datasets.get(dskey) or {}).get("sources")
                if sources is not None and col in (sources.get(table) or []):
                    col_datasets.append(dskey)
        col_ds_set = set(col_datasets)

        col_charts: list[dict[str, Any]] = []
        col_filters: list[dict[str, Any]] = []
        for dkey, d in (dash.get("dashboards") or {}).items():
            for chart in d.get("charts") or []:
                dskey = chart.get("dataset")
                if dskey not in col_ds_set:
                    continue
                direct = "viz" in chart and any(c == col for _, c in config.chart_registry.column_refs(chart))
                is_sql = (lin_datasets.get(dskey) or {}).get("kind") == "sql"
                if direct or is_sql:
                    col_charts.append({
                        "key": chart["key"],
                        "title": chart.get("title"),
                        "dashboard": dkey,
                        "dataset": dskey,
                        "tabs": tab_of.get(dkey, []),
                        "direct": direct,
                    })
            for flt in d.get("filters") or []:
                dskey = flt.get("dataset")
                if dskey in col_ds_set and flt.get("column") == col:
                    col_filters.append({
                        "dashboard": dkey,
                        "key": flt["key"],
                        "label": flt.get("label"),
                        "dataset": dskey,
                    })

        col_sections: list[dict[str, Any]] = []
        for tab in dash.get("tabs") or []:
            for i, sec in enumerate(tab.get("sections") or []):
                stype = sec.get("type")
                if stype == config.EMBED_SECTION or stype not in widget_reg or not widget_reg[stype].has_dataset:
                    continue
                dskey = sec.get("dataset")
                if dskey not in col_ds_set:
                    continue
                direct = any(c == col for _, c in widget_reg[stype].column_refs(sec))
                is_sql = (lin_datasets.get(dskey) or {}).get("kind") == "sql"
                if direct or is_sql:
                    col_sections.append({
                        "tab": tab["key"],
                        "index": i,
                        "type": stype,
                        "dataset": dskey,
                        "direct": direct,
                    })

        out[col] = {
            "datasets": col_datasets,
            "charts": col_charts,
            "filters": col_filters,
            "sections": col_sections,
            "labels": col_labels,
            "metrics": col_metrics,
        }
    return out


def used_by(lineage: Mapping[str, Any], table: str, project: config.Project | None = None) -> dict[str, Any]:
    """테이블의 쓰이는 곳: 그 테이블을 읽는 데이터셋 → 그래프(차트·위젯 섹션) → 탭."""
    dsets = [k for k, d in lineage["datasets"].items() if d["sources"] is not None and table in d["sources"]]
    unknown = [k for k, d in lineage["datasets"].items() if d["sources"] is None]
    charts = [c for c in lineage["charts"] if c["dataset"] in dsets]
    sections = [s for s in lineage["sections"] if s["dataset"] in dsets]
    tabs = sorted({t for c in charts for t in c["tabs"]} | {s["tab"] for s in sections})
    out: dict[str, Any] = {
        "table": table, "datasets": dsets, "charts": charts, "sections": sections, "tabs": tabs,
        "unknown_datasets": unknown,
    }
    if project is not None:
        out["columns"] = column_usage(project, lineage, table)
    return out


def without_column(lineage: Mapping[str, Any], table: str, column: str) -> list[str]:
    """열을 더한 뒤 그 열이 나오지 않는 sql: 데이터셋(열을 하나씩 적은 쿼리. 사람이 고쳐야 한다. 설계서 6-2)."""
    return [k for k, d in lineage["datasets"].items()
            if d["kind"] == "sql" and d["sources"] and table in d["sources"] and column not in d["sources"][table]]
