"""계보와 "쓰이는 곳"(설계서 7-2, contracts/manage.md "계보").

- 데이터셋 → 테이블·열: 로더가 뷰를 만든 직후 PostgreSQL의 의존 정보로 적재 스냅샷의 `datasets.<키>.sources`에 적은 것.
- 그래프 → 데이터셋·열: 설정에서 계산한다(config.referenced_columns, 차트의 dataset, 위젯 섹션).
- 대시보드 → 탭: superset 섹션.
스냅샷은 dash_reader·dash_inspect가 읽을 수 있어 owner가 필요 없다.
"""
from __future__ import annotations

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


def used_by(lineage: Mapping[str, Any], table: str) -> dict[str, Any]:
    """테이블의 쓰이는 곳: 그 테이블을 읽는 데이터셋 → 그래프(차트·위젯 섹션) → 탭."""
    dsets = [k for k, d in lineage["datasets"].items() if d["sources"] is not None and table in d["sources"]]
    unknown = [k for k, d in lineage["datasets"].items() if d["sources"] is None]
    charts = [c for c in lineage["charts"] if c["dataset"] in dsets]
    sections = [s for s in lineage["sections"] if s["dataset"] in dsets]
    tabs = sorted({t for c in charts for t in c["tabs"]} | {s["tab"] for s in sections})
    return {"table": table, "datasets": dsets, "charts": charts, "sections": sections, "tabs": tabs,
            "unknown_datasets": unknown}


def without_column(lineage: Mapping[str, Any], table: str, column: str) -> list[str]:
    """열을 더한 뒤 그 열이 나오지 않는 sql: 데이터셋(열을 하나씩 적은 쿼리. 사람이 고쳐야 한다. 설계서 6-2)."""
    return [k for k, d in lineage["datasets"].items()
            if d["kind"] == "sql" and d["sources"] and table in d["sources"] and column not in d["sources"][table]]
