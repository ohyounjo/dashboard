"""운용 화면에서 새 테이블 만들기(설계서 6-1, contracts/manage.md "새 테이블").

1. infer_one: 올린 CSV 하나에서 이름·유형·기본 키·참조를 추론한다(dashkit/scaffold.py의 추론. 이미 있는 테이블의 단일 열
   기본 키도 참조 후보로 본다). 파일은 아직 어디에도 쓰지 않는다.
2. build: 사람이 고친 추론으로 연산(table.add, dataset.add, 초안이면 dashboard.add·tab.add·section.add)과 첫 파일을
   만든다. 원래 머리글은 그 열의 aliases에 남긴다. 첫 파일은 방식에 따라 append는 `data/<이름>/000001-<시각>.csv.gz`
   (정규형), replace는 `data/<이름>.csv`(모든 열의 머리글)다.
3. 구조 변경(structure.change_config)이 검증 → 저장 → apply 한다.
"""
from __future__ import annotations

import csv
import gzip
import io
import re
from pathlib import Path
from typing import Any, Mapping

from .. import config, scaffold
from . import batches
from .intake import CsvParser, Intake, IntakeError
from .operation import IngestError

NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")
TYPES = ("text", "integer", "number", "boolean", "date", "timestamp", "enum")


def infer_one(project: config.Project, name: str, content: bytes) -> dict[str, Any]:
    """CSV 하나 → 테이블 추론(scaffold 형식의 표 하나). 이미 있는 이름과 겹치면 이름을 바꾼다."""
    try:
        tables = scaffold.infer_tables({name: content})
    except scaffold.ScaffoldError as exc:
        raise IngestError(422, "invalid_csv", "CSV를 읽을 수 없습니다", exc.errors) from None
    table = tables[0]
    taken = set(project.schema["tables"])
    base = table["name"]
    n = 2
    while table["name"] in taken:
        table["name"] = f"{base}_{n}"[:63]
        n += 1
    # 이미 있는 테이블의 단일 열 기본 키와 이름·유형이 맞는 열을 참조 후보로(값 대조는 apply가 한다)
    for col in table["columns"]:
        if col.get("ref") or col["name"] in table["primary_key"]:
            continue
        for tname, spec in project.schema["tables"].items():
            pk = spec["primary_key"]
            if len(pk) != 1 or spec["columns"][pk[0]]["type"] != col["type"]:
                continue
            if col["name"] == pk[0] and col["name"] != "id" or col["name"] in (f"{tname}_id", f"{tname.rstrip('s')}_id"):
                col["ref"] = f"{tname}.{pk[0]}"
                col["ref_basis"] = f"이미 있는 테이블 {tname}의 기본 키"
                break
    return table


def _check_table(project: config.Project, table: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]], list[str]]:
    name = table.get("name")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise IngestError(422, "invalid_request", "테이블 이름은 소문자로 시작하는 snake_case입니다")
    if name in project.schema["tables"]:
        raise IngestError(409, "table_exists", "같은 이름의 테이블이 이미 있습니다")
    columns = table.get("columns")
    if not isinstance(columns, list) or not columns:
        raise IngestError(422, "invalid_request", "columns가 필요합니다")
    names = []
    for c in columns:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or not NAME_RE.fullmatch(c["name"]) \
                or c.get("type") not in TYPES:
            raise IngestError(422, "invalid_request", "열마다 name(snake_case)과 type이 필요합니다")
        names.append(c["name"])
    pk = table.get("primary_key")
    if not isinstance(pk, list) or not pk or any(k not in names for k in pk):
        raise IngestError(422, "invalid_request", "primary_key는 열 이름의 목록입니다")
    return name, columns, pk


def build(project: config.Project, table: Mapping[str, Any], content: bytes, source_name: str, *,
          mode: str = "append", on_conflict: str | None = None, dataset: bool = True,
          suggest: bool = False) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """(연산 목록, {경로: 첫 파일 내용})."""
    name, columns, pk = _check_table(project, table)
    if mode not in ("append", "replace"):
        raise IngestError(422, "invalid_request", "mode는 append 또는 replace입니다")
    specs: dict[str, dict[str, Any]] = {}
    for c in columns:
        spec: dict[str, Any] = {"type": c["type"]}
        if c.get("required") and c["name"] not in pk:
            spec["required"] = True
        if c["type"] == "enum":
            spec["values"] = list(c.get("values") or [])
        for k in ("ref", "description", "min", "max", "pattern"):
            if c.get(k) is not None:
                spec[k] = c[k]
        source = c.get("source")
        if isinstance(source, str) and source and source != c["name"]:
            spec["aliases"] = [source]       # 원래 머리글: 같은 모양의 파일을 다음에 올리면 저절로 맞는다
        specs[c["name"]] = spec
    value: dict[str, Any] = {"primary_key": pk[0] if len(pk) == 1 else pk, "columns": specs}
    if mode == "append":
        value = {"mode": "append", **({"on_conflict": on_conflict} if on_conflict else {}), **value}
    ops: list[dict[str, Any]] = [{"op": "table.add", "table": name, "value": value}]
    files = {_first_path(project, name, mode): _first_file(project, name, value, columns, content, source_name, mode)}
    if dataset or suggest:
        taken = set(project.dashboard["datasets"])
        key = name if name not in taken else f"{name}_rows"
        ds: dict[str, Any] = {"table": name}
        sketch = scaffold.suggest_dashboard([{**table, "name": name, "label": table.get("label") or name,
                                             "rows": table.get("rows", 0)}]) if suggest else None
        if sketch:
            ds = {**sketch["datasets"][name], "table": name}
        ops.append({"op": "dataset.add", "key": key, "value": ds})
        if sketch:
            ops += _sketch_ops(project, sketch, name, key)
    return ops, files


def _first_path(project: config.Project, name: str, mode: str) -> str:
    if mode == "replace":
        return f"data/{name}.csv"
    return f"data/{name}/{batches.next_name(project.data_dir, name)}"


def _first_file(project: config.Project, name: str, value: Mapping[str, Any], columns: list[dict[str, Any]],
                content: bytes, source_name: str, mode: str) -> bytes:
    """올린 CSV를 새 테이블 정의로 접수해 정규형으로 쓴다(머리글 → 열은 추론의 source로 맞춘다)."""
    import tempfile

    table = config._normalize_schema({"tables": {name: dict(value)}})["tables"][name]
    overrides = {c["source"]: c["name"] for c in columns if isinstance(c.get("source"), str) and c["source"]}
    with tempfile.TemporaryDirectory(prefix="dashkit-newtable-") as tmp:
        src = Path(tmp) / "in.csv"
        src.write_bytes(content)
        parser = CsvParser(src, source_name)
        try:
            intake = Intake(parser, name, table, overrides=overrides)
        except IntakeError as exc:
            raise IngestError(422, exc.detail, exc.message, exc.errors or None, **exc.extra) from None
        out = io.StringIO()
        w = csv.writer(out, lineterminator="\n")
        header = list(table["columns"]) if mode == "replace" else intake.header
        w.writerow(header)
        pos = {c: i for i, c in enumerate(intake.header)}
        for _at, values, typed in intake.rows():
            if typed is None:
                continue        # 검증 오류는 아래에서 모두 알린다
            w.writerow([values[pos[c]] if c in pos else "" for c in header])
        parser.close()
        if intake.report.errors:
            raise IngestError(422, "invalid_rows", "첫 파일이 새 테이블의 규칙을 통과하지 못했습니다(유형·필수·기본 키를 고치세요)",
                              intake.report.errors)
    data = out.getvalue().encode("utf-8")
    if mode == "replace":
        return data
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=6) as gz:
        gz.write(data)
    return buf.getvalue()


def _sketch_ops(project: config.Project, sketch: Mapping[str, Any], name: str, dataset_key: str) -> list[dict[str, Any]]:
    """초안 그래프: Superset 대시보드 하나와 웹앱 탭 하나(그룹이 허용하기 전에는 user 등급에게 보이지 않는다)."""
    ops: list[dict[str, Any]] = []
    dashboards = project.dashboard["dashboards"]
    dkey = name if name not in dashboards else f"{name}_view"
    taken_charts = {c["key"] for _d, c in config.charts(project)}
    rename: dict[str, str] = {}
    charts = []
    for chart in sketch["dashboards"][name]["charts"]:
        key = chart["key"]
        while key in taken_charts:
            key = f"{key}_2"[:63]
        rename[chart["key"]] = key
        charts.append({k: v for k, v in {**chart, "key": key, "dataset": dataset_key}.items() if k != "note"})

    def fix(entry: Any) -> Any:
        if isinstance(entry, list):
            return [fix(e) for e in entry]
        if isinstance(entry, dict) and "chart" in entry:
            return {**entry, "chart": rename[entry["chart"]]}
        if isinstance(entry, str):
            return rename.get(entry, entry)
        return entry

    board = sketch["dashboards"][name]
    ops.append({"op": "dashboard.add", "key": dkey, "value": {"title": board["title"], "charts": charts,
                                                            "layout": fix(board["layout"])}})
    tab = sketch["tabs"][0]
    tabs = {t["key"] for t in project.dashboard["tabs"]}
    tkey = tab["key"] if tab["key"] not in tabs else f"{tab['key']}_tab"
    ops.append({"op": "tab.add", "tab": {"key": tkey, "label": tab["label"], "icon": tab["icon"]}})
    ops.append({"op": "section.add", "tab": tkey, "section": {"type": "superset", "dashboard": dkey}})
    return ops
