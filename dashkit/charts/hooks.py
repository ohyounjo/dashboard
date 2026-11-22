"""단순 대응(필드 하나 → 파라미터 하나)이 안 되는 유형의 params 생성 함수.

유형 파일의 `hook: dashkit.charts.hooks:<이름>`으로 지정한다. 함수는 차트 설정을 받아
viz_type, defaults, d3format, where를 뺀 params를 돌려준다(쓰는 순서 = dict 순서).
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from . import metric_label, metric_param


def _order_by_col(col: str, direction: str) -> str:
    # Superset은 JSON 문자열 배열을 쓴다: '["c", false]' (asc=true)
    return json.dumps([col, direction == "asc"])


def table(chart: Mapping[str, Any]) -> dict:
    """columns가 있으면 원본 행 모드, 없으면 집계 모드. order_by는 지표면 정렬 지표, 열이면 order_by_cols."""
    p: dict[str, Any] = {}
    order_by = chart.get("order_by") or []
    if "columns" in chart:
        p["query_mode"] = "raw"
        p["all_columns"] = list(chart["columns"])
        if order_by:
            p["order_by_cols"] = [_order_by_col(c, d) for c, d in order_by]
        if chart.get("page_size") is not None:
            p["server_pagination"] = True
            p["server_page_length"] = chart["page_size"]
    else:
        p["query_mode"] = "aggregate"
        p["groupby"] = list(chart.get("groupby") or [])
        metrics = list(chart.get("metrics") or [])
        p["metrics"] = [metric_param(m) for m in metrics]
        by_label = {metric_label(m): m for m in metrics}
        col_order = []
        for c, d in order_by:
            if c in by_label:
                # 지표 정렬은 하나만 쓸 수 있다: 첫 지표 항목만 반영
                if "timeseries_limit_metric" not in p:
                    p["timeseries_limit_metric"] = metric_param(by_label[c])
                    p["order_desc"] = d == "desc"
            else:
                col_order.append(_order_by_col(c, d))
        if col_order:
            p["order_by_cols"] = col_order
    if chart.get("row_limit") is not None:
        p["row_limit"] = chart["row_limit"]
    p["table_timestamp_format"] = "%Y-%m-%d"
    if chart.get("search") is not None:
        p["include_search"] = bool(chart["search"])
    return p
