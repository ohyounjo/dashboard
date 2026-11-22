"""데이터 신선도 계산 및 기한 초과 판정(MANAGE_FEATURES=freshness).

모든 시각 계산은 UTC aware datetime으로 정규화해 수행하며, API 출력은 항상
`YYYY-MM-DDTHH:MM:SSZ` 형식의 UTC ISO 문자열로 직렬화한다.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

FRESHNESS_RE = re.compile(r"^([1-9][0-9]*)([mhd])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def parse_freshness(value: str) -> timedelta:
    """`30m`, `12h`, `7d` 같은 갱신 기한 문자열을 양의 timedelta로 변환한다."""
    if not isinstance(value, str):
        raise ValueError("freshness는 문자열이어야 합니다")
    match = FRESHNESS_RE.fullmatch(value)
    if not match:
        raise ValueError(f"freshness 형식이 올바르지 않습니다: {value!r} (예: 30m, 12h, 7d)")
    amount = int(match.group(1))
    unit = _UNITS[match.group(2)]
    return timedelta(**{unit: amount})


def to_utc(dt: datetime) -> datetime:
    """naive datetime은 UTC로 간주하고, aware datetime은 UTC로 변환한다."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_utc_iso(dt: datetime) -> str:
    """UTC ISO 문자열(`YYYY-MM-DDTHH:MM:SSZ`)로 직렬화한다."""
    return to_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def table_last_added_at(recorded: Mapping[str, Mapping[str, Any]], table: str) -> datetime | None:
    """`dashkit_meta.batch` 기록에서 `table`의 `added_at` 최댓값(UTC)을 반환한다."""
    rec = recorded.get(table) or {}
    times = [to_utc(r.added_at) for r in rec.values() if getattr(r, "added_at", None) is not None]
    return max(times) if times else None


def _tab_datasets(project: Any, tab: Mapping[str, Any]) -> set[str]:
    dashboards = (project.dashboard or {}).get("dashboards") or {}
    datasets: set[str] = set()
    for sec in tab.get("sections") or []:
        if sec.get("type") == "superset":
            dash = dashboards.get(sec.get("dashboard")) or {}
            for chart in dash.get("charts") or []:
                if isinstance(chart, Mapping) and chart.get("dataset"):
                    datasets.add(chart["dataset"])
        elif sec.get("dataset"):
            datasets.add(sec["dataset"])
    return datasets


def tab_data_as_of(
    project: Any,
    tab: Mapping[str, Any],
    graph: Mapping[str, Any],
    recorded: Mapping[str, Mapping[str, Any]],
    *,
    loaded_project: str | None = None,
) -> str | None:
    """탭이 참조하는 모든 테이블의 마지막 배치 시각 중 가장 오래된 시각을 UTC ISO 문자열로 반환한다."""
    if loaded_project is not None and loaded_project != project.name:
        return None
    datasets = _tab_datasets(project, tab)
    if not datasets:
        return None
    ds_graph = graph.get("datasets") or {}
    tables: set[str] = set()
    for ds in datasets:
        info = ds_graph.get(ds)
        if not info or info.get("sources") is None:
            return None
        tables.update(info["sources"])
    if not tables:
        return None
    times: list[datetime] = []
    for table in tables:
        added_at = table_last_added_at(recorded, table)
        if added_at is None:
            return None
        times.append(added_at)
    return format_utc_iso(min(times))


def is_stale(
    last_added_at: datetime | None,
    freshness: str | None,
    now_utc: datetime | None = None,
) -> bool:
    """`now_utc - last_added_at > parse_freshness(freshness)`일 때만 True(경계 == delta는 False)."""
    if not freshness or last_added_at is None:
        return False
    delta = parse_freshness(freshness)
    now = to_utc(now_utc) if now_utc is not None else datetime.now(timezone.utc)
    return (now - to_utc(last_added_at)) > delta
