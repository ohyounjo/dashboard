"""적용 전에 차이 보기(python -m dashkit plan, 보강설계서 5-2). DB·Superset·상태 파일을 바꾸지 않는다.

조회 전용이므로 DB는 **dash_reader 계정으로만** 읽는다(owner 계정을 쓰지 않는다. raw 스키마는 읽지 않는다).

- 데이터: CSV의 행 지문과 로더가 지난 적재 때 남긴 행 지문(dashkit_meta, loader/record.py)을 기본 키로 비교한다.
- 뷰: 새 설정으로 만들어질 dash 뷰의 열과 지금 뷰의 열.
- Superset: 새 번들과 지난 sync가 남긴 `.build/<프로젝트>/bundle/`을 파일 단위로 비교한다.

차이 계산은 순수 함수(diff_rows, diff_columns, table_diff, predict_view_columns, diff_views, diff_bundles)이고,
읽어 오는 부분은 dashkit/loader/current.py(DB, dash_reader)와 read_bundle_dir(파일)이다. make_plan이 둘을 잇는다.
결과는 JSON으로 바꿀 수 있는 dict이고 format_plan이 사람이 읽는 글로 바꾼다. 계약은 contracts/plan.md.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from .config import Project, table_order
from .loader.record import definition_hash, fingerprints, labels, table_columns

Columns = list[tuple[str, str]]          # [(열 이름, information_schema data_type)], 순서 있음


MAX_KEYS = 20                            # 결과에 싣는 예시 키 수(종류마다)
MINUS = "−"                         # −
BUNDLE_KINDS = (("datasets", "데이터셋"), ("charts", "차트"), ("dashboards", "대시보드"))
UNKNOWN_TEXT = "적용해야 알 수 있음"
NO_RECORD_TEXT = "지난 적재 기록 없음"
PENDING_TEXT = "끝나지 않은 폴더 작업이 있음(다음 적재·적용이 처리함): 배치를 비교하지 않음"
BATCH_TEXT = "누적 테이블은 배치 단위로 비교함"
LABEL_WIDTH = 9                          # 출력의 줄 머리 폭(화면 칸 수)


class PlanError(Exception):
    """plan을 만들 수 없음(CSV 검증 실패, DB 접속 실패). errors는 한 줄에 하나."""

    def __init__(self, title: str, errors: list[str]):
        self.title = title
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


# ---------------------------------------------------------------- 열 비교(순수)

def diff_columns(new: Sequence[Sequence[str]], old: Sequence[Sequence[str]]) -> dict[str, Any]:
    """열 목록 두 개의 차이. added·removed는 이름, retyped는 {column, from, to}, reordered는 같은 열의 순서만 바뀜."""
    new = [(c, t) for c, t in new]
    old = [(c, t) for c, t in old]
    new_types, old_types = dict(new), dict(old)
    added = [c for c, _t in new if c not in old_types]
    removed = [c for c, _t in old if c not in new_types]
    retyped = [{"column": c, "from": old_types[c], "to": t} for c, t in new if c in old_types and old_types[c] != t]
    common_new = [c for c, _t in new if c in old_types]
    common_old = [c for c, _t in old if c in new_types]
    return {"added": added, "removed": removed, "retyped": retyped, "reordered": common_new != common_old}


def _columns_changed(d: Mapping[str, Any]) -> bool:
    return bool(d["added"] or d["removed"] or d["retyped"] or d["reordered"])


def _no_column_change() -> dict[str, Any]:
    return {"added": [], "removed": [], "retyped": [], "reordered": False}


# ---------------------------------------------------------------- 행 비교(순수)

def _row_result(added: int = 0, removed: int = 0, changed: int = 0, unchanged: int = 0,
                reason: str | None = None) -> dict[str, Any]:
    return {"added": added, "removed": removed, "changed": changed, "unchanged": unchanged,
            "comparable": reason is None, "reason": reason, "keys": {"added": [], "removed": [], "changed": []}}


def incomparable(new_count: int, old_count: int, reason: str) -> dict[str, Any]:
    """행을 맞출 수 없을 때의 결과: 새 행은 전부 추가, 지난 행은 전부 삭제로 센다(comparable = False, reason에 이유)."""
    return _row_result(added=new_count, removed=old_count, reason=reason)


def diff_rows(
    new: Iterable[Sequence[str]],
    old: Iterable[Sequence[str]],
    *,
    names: Mapping[str, str] | None = None,
    max_keys: int = MAX_KEYS,
) -> dict[str, Any]:
    """행 지문 두 묶음 [(row_key, row_hash)]을 기본 키로 비교한다(지문은 loader/record.py의 fingerprints).

    - added: 새 쪽에만 있는 키, removed: 지난 쪽에만 있는 키, changed: 키는 같고 해시가 다른 행, unchanged: 나머지.
    - 어느 쪽이든 같은 키가 두 번 나오면 행을 맞출 수 없다. 조용히 덮어쓰지 않고 전부 추가·전부 삭제로 센다
      (comparable = False).
    - keys: added·changed마다 앞에서부터 max_keys개의 키(사람이 읽는 글). names는 {row_key: 글}이고 새 쪽(CSV)
      행에서 만든다(record.labels). 지난 쪽의 키는 해시뿐이라 **removed의 예시는 항상 비어 있고 개수만 센다.**
    """
    new = [(k, h) for k, h in new]
    old = [(k, h) for k, h in old]
    new_by_key, old_by_key = dict(new), dict(old)
    if len(new_by_key) != len(new) or len(old_by_key) != len(old):
        return incomparable(len(new), len(old), "기본 키 값이 같은 행이 여럿이라 행을 맞출 수 없음")
    result = _row_result()

    names = names or {}

    def note(kind: str, key: str) -> None:
        result[kind] += 1
        if kind != "removed" and key in names and len(result["keys"][kind]) < max_keys:
            result["keys"][kind].append(names[key])

    for key, digest in new:
        if key not in old_by_key:
            note("added", key)
        elif old_by_key[key] != digest:
            note("changed", key)
        else:
            result["unchanged"] += 1
    for key, _digest in old:
        if key not in new_by_key:
            note("removed", key)
    return result


def table_diff(
    name: str,
    table: Mapping[str, Any],
    new_rows: Sequence[Sequence[Any]],
    recorded: Mapping[str, Any] | None,
    old_prints: Iterable[Sequence[str]] = (),
    *,
    replaced_project: str | None = None,
) -> dict[str, Any]:
    """테이블 하나의 차이. table은 schema.yaml의 (정규화된) 테이블 정의, new_rows는 검증을 통과한 CSV 행
    (validate.TableData.rows, 열 순서는 table["columns"]), recorded는 적재 스냅샷의 그 테이블 항목
    ({"columns", "primary_key", "rows"}. 지난 적재에 없던 테이블이면 None), old_prints는 지난 적재의 행 지문이다.

    status: new(지난 적재에 없음. 전부 추가) | changed | unchanged.
    행을 맞출 수 없는 경우(comparable = False): 열 구성(이름·타입)이나 기본 키 정의가 지난 적재와 다름,
    지난 적재가 다른 프로젝트의 것(replaced_project). 이때는 +CSV 행 수, −지난 적재의 행 수다.
    """
    entry: dict[str, Any] = {"table": name, "rows": len(new_rows)}
    if recorded is None:
        rows = diff_rows(fingerprints(table, new_rows), [], names=labels(table, new_rows))
        return {**entry, "status": "new", "columns": _no_column_change(), **rows}
    old_columns = [(c, t) for c, t in recorded["columns"]]
    new_columns = table_columns(table)
    cols = diff_columns(new_columns, old_columns)
    old_count = int(recorded.get("rows", 0))
    if replaced_project is not None:
        rows = incomparable(len(new_rows), old_count, f"지난 적재는 다른 프로젝트 '{replaced_project}'의 것")
    elif sorted(new_columns) != sorted(old_columns):
        rows = incomparable(len(new_rows), old_count, "열 구성이 지난 적재와 달라 행을 맞출 수 없음")
    elif list(table["primary_key"]) != list(recorded["primary_key"]):
        rows = incomparable(len(new_rows), old_count, "기본 키가 지난 적재와 달라 행을 맞출 수 없음")
    else:
        rows = diff_rows(fingerprints(table, new_rows), old_prints, names=labels(table, new_rows))
    changed = rows["added"] or rows["removed"] or rows["changed"] or not rows["comparable"] or _columns_changed(cols)
    return {**entry, "status": "changed" if changed else "unchanged", "columns": cols, **rows}


def unrecorded_table(name: str, row_count: int) -> dict[str, Any]:
    """적재 기록이 없을 때의 항목. 비교할 수 없으므로 CSV 행 수만 싣는다(added·removed·changed는 None)."""
    return {"table": name, "rows": row_count, "status": "unrecorded", "columns": _no_column_change(),
            "added": None, "removed": None, "changed": None, "unchanged": None,
            "comparable": False, "reason": NO_RECORD_TEXT, "keys": {"added": [], "removed": [], "changed": []}}


def append_sources(project: Project, table: str) -> dict[str, Any]:
    """누적 테이블의 원본 파일 {이름: (내용 해시, 경로)}, 이름 오류, 끝나지 않은 폴더 작업이 있는지.

    plan은 아무것도 고치지 않는다: 남은 묶음 기록이 이 테이블을 가리키면 배치 비교를 하지 않고 알린다(pending).
    """
    from .ingest import batches, pending

    files, errors = batches.sources(project.data_dir, table, "append")
    busy = False
    for _path, record in pending.list_records(project.data_dir):
        items = (record or {}).get("files") or []
        if record is None or any(isinstance(i, dict) and i.get("table") == table for i in items) \
                or (record or {}).get("table") == table:
            busy = True
    return {"files": {f.name: f for f in files}, "errors": errors, "pending": busy}


def _batch_rows(project_schema: Mapping[str, Any], src: Any) -> tuple[int, list[str]]:
    """새·바뀐 배치 파일을 엄격하게 읽어 행 수와 검증 오류를 구한다(DB 없이. 참조와 키 충돌은 load가 본다)."""
    from .ingest.intake import CsvParser, Intake, IntakeError
    from .ingest.apply import source_label

    parser = CsvParser(src.path, source_label(src), strict=True)
    try:
        try:
            intake = Intake(parser, src.table, project_schema["tables"][src.table], strict=True)
        except IntakeError as exc:
            return 0, exc.errors or [exc.message]
        n = 0
        for _at, _values, typed in intake.rows():
            if typed is not None:
                n += 1
        return n, intake.report.errors
    finally:
        parser.close()


def batch_diff(name: str, table: Mapping[str, Any], folder: Mapping[str, Any], recorded: Mapping[str, Any] | None,
               old_batches: Mapping[str, tuple[str, int, int]] | None, *, replaced_project: str | None = None
               ) -> dict[str, Any]:
    """누적(append) 테이블의 차이: 배치 단위로 비교한다(contracts/plan.md "누적 테이블").

    기록에 없는 파일은 "배치 +1", 기록에 있는데 폴더에 없는 파일은 "배치 −1", 이름이 같고 해시가 다르면 "배치 ~1".
    이름과 해시가 같은 배치는 다시 읽지 않는다. 행 수는 새 배치의 행 수(+)와 없어진 배치의 기록된 행 수(−)다.
    """
    from .ingest import batches

    entry: dict[str, Any] = {"table": name, "mode": "append"}
    files = folder["files"]
    if folder["pending"]:
        return {**entry, "rows": None, "status": "pending", "columns": _no_column_change(),
                "added": None, "removed": None, "changed": None, "unchanged": None, "comparable": False,
                "reason": PENDING_TEXT, "keys": {"added": [], "removed": [], "changed": []},
                "batches": {"added": [], "removed": [], "changed": []}}
    old = dict(old_batches or {}) if replaced_project is None and recorded is not None else {}
    added: list[str] = []
    changed: list[str] = []
    new_rows = 0
    errors: list[str] = []
    for fname, src in files.items():
        prior = old.pop(fname, None)
        if prior is not None and batches.content_hash(src.path) == prior[0]:
            continue
        n, errs = _batch_rows({"tables": {name: table}}, src)
        errors += errs
        new_rows += n
        (changed if prior is not None else added).append(fname)
    if errors:
        raise PlanError("CSV 검증 실패, 차이를 계산하지 않음", errors)
    removed = sorted(old)
    # 빠지는 행: 없어지거나 바뀐 배치가 넣었던 새 키의 수(배치 기록의 inserted)
    removed_rows = sum(rec[2] for rec in old.values()) + sum(
        (old_batches or {}).get(f, ("", 0, 0))[2] for f in changed)
    batch_lists = {"added": added, "removed": removed, "changed": changed}
    cols = _no_column_change()
    if old_batches is None:                      # 적재 기록이 없다
        status = "unrecorded"
    elif recorded is None:                       # 지난 적재에 없던 테이블
        status = "new"
    elif replaced_project is not None:           # 지난 적재는 다른 프로젝트의 것(전부 새로 넣는다)
        status = "changed"
    else:
        cols = diff_columns(table_columns(table), [(c, t) for c, t in recorded["columns"]])
        status = "changed" if (added or removed or changed or _columns_changed(cols)) else "unchanged"
    # 지금 테이블의 행 수 = 배치 기록의 inserted 합(추가 적재분 포함. 스냅샷의 rows는 마지막 load 때의 값이라 쓰지 않는다)
    rows_now = (sum(rec[2] for rec in (old_batches or {}).values())
                if recorded is not None and replaced_project is None else 0)
    return {**entry, "rows": rows_now + new_rows - removed_rows if status != "unrecorded" else None,
            "status": status, "columns": cols,
            "added": new_rows, "removed": removed_rows, "changed": None, "unchanged": None,
            "comparable": False, "reason": BATCH_TEXT, "keys": {"added": [], "removed": [], "changed": []},
            "batches": batch_lists}


def removed_table(name: str, recorded: Mapping[str, Any]) -> dict[str, Any]:
    return {"table": name, "rows": 0, "status": "removed",
            "columns": {**_no_column_change(), "removed": [c for c, _t in recorded["columns"]]},
            **_row_result(removed=int(recorded.get("rows", 0)))}


# ---------------------------------------------------------------- 뷰 열(순수)

_WORD = re.compile(r"[a-z0-9_]+")


def predict_view_columns(
    project: Project,
    snapshot: Mapping[str, Any] | None,
    views_now: Mapping[str, Sequence[tuple[str, str]]],
) -> dict[str, dict[str, Any]]:
    """새 설정으로 만들어질 뷰의 열을 데이터셋마다 구한다. {키: {"columns": [(열, 타입)] 또는 None, "reason": 이유 또는 None}}.

    - `table:` 데이터셋: schema.yaml의 열 그대로다(뷰가 `SELECT * FROM raw.<테이블>`이므로).
    - `sql:` 데이터셋: 쿼리의 결과 열은 적용해 봐야 안다. 다만 **바뀐 것이 없다**는 것은 적재 스냅샷으로 알 수 있다.
      쿼리 정의의 해시가 지난 적재와 같고, 쿼리 글에 이름이 나오는 raw 테이블과 앞선 데이터셋 뷰의 구조
      (열 이름·타입·순서)가 그대로면 지금 뷰의 열이 곧 새 열이다. 하나라도 다르면 "알 수 없음"으로 둔다.
      이름은 글자 단위로만 찾으므로 실제로 읽지 않아도 걸릴 수 있다(안전한 쪽).
    - snapshot이 None이면(적재 기록이 없거나 다른 프로젝트의 것) `sql:` 데이터셋은 모두 "알 수 없음"이다.
    """
    tables = project.schema["tables"]
    datasets = project.dashboard["datasets"]
    old_tables = (snapshot or {}).get("tables") or {}
    old_datasets = (snapshot or {}).get("datasets") or {}
    # 구조가 바뀌는 raw 테이블: 새로 생김, 없어짐, 열이 달라짐
    unstable = {t for t in tables
                if t not in old_tables or [tuple(c) for c in old_tables[t]["columns"]] != table_columns(tables[t])}
    unstable |= {t for t in old_tables if t not in tables}
    out: dict[str, dict[str, Any]] = {}
    for key, ds in datasets.items():
        if "table" in ds:
            out[key] = {"columns": table_columns(tables[ds["table"]]), "reason": None}
            continue
        if snapshot is None:
            out[key] = {"columns": None, "reason": NO_RECORD_TEXT}
            continue
        if key not in old_datasets or key not in views_now:
            out[key] = {"columns": None, "reason": "새 데이터셋"}
            continue
        if old_datasets[key].get("definition") != definition_hash(ds):
            out[key] = {"columns": None, "reason": "쿼리가 바뀜"}
            continue
        words = set(_WORD.findall(ds["sql"].lower()))
        blockers = sorted(f"raw.{t}" for t in unstable if t in words)
        for other, known in out.items():   # 앞선 데이터셋만(뷰는 설정 순서로 만들어진다)
            if other in words and (known["columns"] is None or known["columns"] != list(views_now.get(other, []))):
                blockers.append(f"dash.{other}")
        blockers += sorted(f"dash.{v}" for v in views_now if v not in datasets and v in words)
        if blockers:
            out[key] = {"columns": None, "reason": f"쿼리에 나오는 {', '.join(blockers)}의 구조가 바뀜"}
        else:
            out[key] = {"columns": list(views_now[key]), "reason": None}
    return out


def diff_views(
    predicted: Mapping[str, Mapping[str, Any]],
    views_now: Mapping[str, Sequence[tuple[str, str]]],
) -> list[dict[str, Any]]:
    """뷰마다의 차이. status: new | changed | unchanged | unknown(적용해야 알 수 있음) | removed."""
    out: list[dict[str, Any]] = []
    for key, p in predicted.items():
        entry: dict[str, Any] = {"dataset": key, "exists": key in views_now, "reason": p["reason"],
                                 "columns": _no_column_change()}
        if p["columns"] is None:
            entry["status"] = "unknown"
        elif key not in views_now:
            entry["status"] = "new"
            entry["columns"]["added"] = [c for c, _t in p["columns"]]
        else:
            entry["columns"] = diff_columns(p["columns"], views_now[key])
            entry["status"] = "changed" if _columns_changed(entry["columns"]) else "unchanged"
        out.append(entry)
    for key in views_now:
        if key not in predicted:
            out.append({"dataset": key, "exists": True, "reason": None, "status": "removed",
                        "columns": {**_no_column_change(), "removed": [c for c, _t in views_now[key]]}})
    return out


# ---------------------------------------------------------------- 번들 비교(순수)

IGNORED_FILES = ("metadata.yaml",)       # 타임스탬프만 들어 있다


def read_bundle_dir(path: Path) -> dict[str, str] | None:
    """`.build/<프로젝트>/bundle/` → {상대 경로: 텍스트}. 폴더가 없으면 None(처음 적용)."""
    path = Path(path)
    if not path.is_dir():
        return None
    return {f.relative_to(path).as_posix(): f.read_text(encoding="utf-8")
            for f in sorted(path.rglob("*.yaml")) if f.is_file()}


def read_other_project(state_dir: Path, project: str) -> dict[str, Any] | None:
    """`.state/managed.json`이 다른 프로젝트의 것이면 {"project": 이름, "objects": 객체 수}. 아니면 None.

    프로젝트를 바꿔 적용하면 sync가 이 목록의 객체를 Superset에서 지운다(contracts/state-and-uuid.md). 읽기만 한다.
    """
    try:
        doc = json.loads((Path(state_dir) / "managed.json").read_text(encoding="utf-8"))
        other = doc.get("project")
        if not isinstance(other, str) or other == project:
            return None
        return {"project": other, "objects": sum(len(v or []) for v in (doc.get("objects") or {}).values())}
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def read_pending_prune(state_dir: Path, project: Any) -> int:
    """같은 프로젝트의 `.state/managed.json`에 남아 있지만 이번 설정이 관리하지 않는 객체 수.

    지난 sync에서 삭제에 실패했거나, import 뒤 prune 전에 끊긴 객체다. 다음 sync가 다시 지운다. 읽기만 한다.
    """
    from .sync.bundle import managed_objects

    try:
        doc = json.loads((Path(state_dir) / "managed.json").read_text(encoding="utf-8"))
        if doc.get("project") != project.name:
            return 0
        recorded = doc.get("objects") or {}
        current = managed_objects(project)
        return sum(len(set(map(str, recorded.get(kind) or [])) - set(uuids)) for kind, uuids in current.items())
    except (OSError, ValueError, AttributeError, TypeError):
        return 0


def _load(text: str) -> dict:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _differs(a: Mapping, b: Mapping, *path: str) -> bool:
    for p in path:
        a = a.get(p) if isinstance(a, Mapping) else None   # type: ignore[assignment]
        b = b.get(p) if isinstance(b, Mapping) else None   # type: ignore[assignment]
    return a != b


def _other_keys_differ(new: Mapping, old: Mapping, known: Iterable[str]) -> bool:
    known = set(known)
    return any(new.get(k) != old.get(k) for k in (set(new) | set(old)) - known)


def describe_change(kind: str, new_text: str, old_text: str) -> str:
    """바뀐 번들 파일 하나를 한 줄로 설명한다. kind: datasets | charts | dashboards."""
    new, old = _load(new_text), _load(old_text)
    parts: list[str] = []
    if kind == "dashboards":
        if _differs(new, old, "dashboard_title"):
            parts.append("제목 변경")
        if _differs(new, old, "position"):
            parts.append("배치 변경")
        if _differs(new, old, "metadata", "native_filter_configuration"):
            parts.append("필터 변경")
        if _differs(new, old, "metadata", "cross_filters_enabled"):
            parts.append("교차 필터 변경")
        known = ["dashboard_title", "position", "is_managed_externally"]
        nm, om = new.get("metadata") or {}, old.get("metadata") or {}
        other = _other_keys_differ(new, old, known + ["metadata"]) or _other_keys_differ(
            nm, om, ["native_filter_configuration", "cross_filters_enabled"])
    elif kind == "charts":
        if _differs(new, old, "slice_name"):
            parts.append("제목 변경")
        if _differs(new, old, "viz_type"):
            parts.append("유형 변경")
        if _differs(new, old, "dataset_uuid"):
            parts.append("데이터셋 변경")
        np_, op_ = dict(new.get("params") or {}), dict(old.get("params") or {})
        np_.pop("viz_type", None), op_.pop("viz_type", None)
        if np_ != op_:
            parts.append("옵션 변경")
        if _differs(new, old, "description"):
            parts.append("설명 변경")
        known = ["slice_name", "viz_type", "dataset_uuid", "params", "description", "is_managed_externally"]
        other = _other_keys_differ(new, old, known)
    else:
        new_cols = [c.get("column_name") for c in new.get("columns") or []]
        old_cols = [c.get("column_name") for c in old.get("columns") or []]
        added = [c for c in new_cols if c not in old_cols]
        removed = [c for c in old_cols if c not in new_cols]
        if added:
            parts.append(f"열 추가: {', '.join(added)}")
        if removed:
            parts.append(f"열 삭제: {', '.join(removed)}")
        if not added and not removed and _differs(new, old, "columns"):
            parts.append("열 속성 변경")
        if _differs(new, old, "metrics"):
            parts.append("지표 변경")
        if _differs(new, old, "main_dttm_col") and not (added or removed):
            parts.append("기본 시간 열 변경")
        known = ["columns", "metrics", "main_dttm_col", "is_managed_externally"]
        other = _other_keys_differ(new, old, known)
    if _differs(new, old, "is_managed_externally"):
        parts.append("화면 편집 잠금 변경")
    if other or not parts:
        parts.append("그 밖의 설정 변경")
    return ", ".join(parts)


def _bundle_key(path: str) -> tuple[str, str] | None:
    """번들 경로 → (종류, 키). 종류는 databases | datasets | charts | dashboards."""
    parts = path.split("/")
    if len(parts) < 2 or not parts[-1].endswith(".yaml") or parts[0] not in ("databases", "datasets", "charts", "dashboards"):
        return None
    return parts[0], parts[-1][: -len(".yaml")]


def diff_bundles(
    new: Mapping[str, str],
    old: Mapping[str, str] | None,
    unknown: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """새 번들과 지난 번들을 파일 단위로 비교한다.

    unknown: {번들 경로: 이유}. 적용해야 내용을 알 수 있는 파일은 added·changed에 넣지 않고 unknown에 싣는다.
    결과의 datasets·charts·dashboards는 {"added": [키], "changed": [{"key", "detail"}], "removed": [키],
    "unknown": [{"key", "reason"}], "unchanged": 개수}, database는 added | changed | unchanged | removed.
    first는 지난 번들이 없다는 뜻이다(처음 적용. 전부 added).
    """
    unknown = dict(unknown or {})
    first = old is None
    old = dict(old or {})
    result: dict[str, Any] = {"first": first, "database": "unchanged", "other": []}
    for kind, _label in BUNDLE_KINDS:
        result[kind] = {"added": [], "changed": [], "removed": [], "unknown": [], "unchanged": 0}
    for path in sorted(set(new) | set(old)):
        if path in IGNORED_FILES:
            continue
        where = _bundle_key(path)
        if path in new and path not in old:
            state = "added"
        elif path not in new:
            state = "removed"
        else:
            state = "unchanged" if new[path] == old[path] else "changed"
        if where is None:
            if state != "unchanged":
                result["other"].append(path)
            continue
        kind, key = where
        if kind == "databases":
            if state != "unchanged":
                result["database"] = state
            continue
        bucket = result[kind]
        if path in unknown and state != "removed":
            bucket["unknown"].append({"key": key, "reason": unknown[path]})
        elif state == "unchanged":
            bucket["unchanged"] += 1
        elif state == "changed":
            bucket["changed"].append({"key": key, "detail": describe_change(kind, new[path], old[path])})
        else:
            bucket[state].append(key)
    return result


def _bundle_changed(b: Mapping[str, Any]) -> bool:
    return bool(b["database"] != "unchanged" or b["other"]
                or any(b[k]["added"] or b[k]["changed"] or b[k]["removed"] for k, _l in BUNDLE_KINDS))


# ---------------------------------------------------------------- 묶기

def make_plan(
    project: Project,
    *,
    dbname: str | None = None,
    build_dir: Path | None = None,
    state_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """CSV·DB·지난 번들을 읽어 차이를 구한다. 아무것도 쓰지 않는다. 실패하면 PlanError.

    DB는 dash_reader 계정으로만 읽는다: 지금 뷰의 열(information_schema)과 적재 기록(dashkit_meta).
    owner 계정의 환경변수는 쓰지 않는다. Superset에는 접속하지 않는다(지난 sync가 남긴 번들 폴더와 비교한다).
    """
    from .loader import current
    from .loader.validate import validate_csv
    from .sync.bundle import APPLIED_ROOT, DATASET_DIR, build_bundle

    order = table_order(project)
    tables = project.schema["tables"]
    datasets = project.dashboard["datasets"]
    appended = [t for t in order if (tables[t].get("mode") or "replace") == "append"]
    data, errors = validate_csv(project.schema, project.data_dir, [t for t in order if t not in appended])
    folders = {t: append_sources(project, t) for t in appended}
    errors += [e for f in folders.values() for e in f["errors"]]
    if errors:
        raise PlanError("CSV 검증 실패, 차이를 계산하지 않음", errors)

    try:
        with current.reader(dbname) as conn:
            views_now = current.read_view_columns(conn)
            rec = current.read_load_record(conn)
    except current.CurrentError as exc:
        raise PlanError("지금 상태를 읽지 못함", [str(exc)]) from None

    # 데이터
    other_load = rec.project if rec is not None and rec.project != project.name else None
    if rec is None:
        table_diffs = [unrecorded_table(name, len(data[name].rows)) if name in data
                       else batch_diff(name, tables[name], folders[name], None, None) for name in order]
        load_record: dict[str, Any] = {"exists": False, "project": None}
    else:
        old_tables = rec.snapshot.get("tables") or {}
        table_diffs = [table_diff(name, tables[name], data[name].rows, old_tables.get(name),
                                  rec.fingerprints.get(name, []), replaced_project=other_load) if name in data
                       else batch_diff(name, tables[name], folders[name], old_tables.get(name),
                                       rec.batches.get(name, {}), replaced_project=other_load)
                       for name in order]
        table_diffs += [removed_table(name, old) for name, old in old_tables.items() if name not in tables]
        load_record = {"exists": True, "project": rec.project}

    # 뷰. 다른 프로젝트의 적재 기록은 이 프로젝트의 구조를 말해 주지 않는다
    snapshot = rec.snapshot if rec is not None and other_load is None else None
    predicted = predict_view_columns(project, snapshot, views_now)
    if other_load is not None:
        for p in predicted.values():
            if p["reason"] == NO_RECORD_TEXT:
                p["reason"] = f"지난 적재는 다른 프로젝트 '{other_load}'의 것"
    views = diff_views(predicted, views_now)

    # 번들: 열을 아는 데이터셋은 그 열로, 모르면 지금 뷰의 열로(바뀌지 않는다고 본다) 만든다.
    # 둘 다 없으면(또는 지금 뷰가 다른 프로젝트의 것이면) 그 데이터셋과 그 데이터셋을 쓰는 차트는 "적용해야 알 수 있음"이다.
    bundle_columns: dict[str, Columns] = {}
    unknown_files: dict[str, str] = {}
    assumed: list[str] = []
    for key in datasets:
        if predicted[key]["columns"] is not None:
            bundle_columns[key] = predicted[key]["columns"]
        elif key in views_now and other_load is None:
            bundle_columns[key] = list(views_now[key])
            assumed.append(key)
        else:
            bundle_columns[key] = []
            unknown_files[f"{DATASET_DIR}/{key}.yaml"] = predicted[key]["reason"] or UNKNOWN_TEXT
    for d in project.dashboard["dashboards"].values():
        for chart in d["charts"]:
            if not bundle_columns[chart["dataset"]]:
                unknown_files[f"charts/{chart['key']}.yaml"] = f"데이터셋 '{chart['dataset']}'의 열을 아직 모름"
    new_files = build_bundle(project, bundle_columns, env=env)
    build_root = Path(build_dir or os.environ.get("BUILD_DIR") or "/app/.build")
    # 다른 프로젝트가 적용되어 있으면 그 프로젝트의 Superset 객체는 이번 적용에서 지워지고, 이 프로젝트의 객체는
    # 전부 새로 생긴다. 이 프로젝트의 지난 번들 폴더가 남아 있어도 Superset에는 없는 것이므로 비교하지 않는다
    replaces = read_other_project(Path(state_dir or os.environ.get("STATE_DIR") or "/app/.state"), project.name)
    # 비교 기준은 마지막으로 import에 성공한 번들이다. sync가 import 전에 쓰는 bundle/은 실패한 실행의 것일 수 있다
    old_files = None if replaces else read_bundle_dir(build_root / project.name / APPLIED_ROOT)
    superset = diff_bundles(new_files, old_files, unknown_files)
    superset["assumed_columns"] = assumed   # 지금 뷰의 열을 그대로 쓴 데이터셋(적용 뒤 열이 달라질 수 있다)
    superset["replaces"] = replaces
    # 지난 sync가 지우지 못한 객체(같은 프로젝트). 번들 비교에는 안 나오지만 다음 적용에서 삭제를 다시 시도한다
    superset["pending_prune"] = 0 if replaces else read_pending_prune(
        Path(state_dir or os.environ.get("STATE_DIR") or "/app/.state"), project)

    changed = (any(t["status"] != "unchanged" for t in table_diffs)
               or any(v["status"] not in ("unchanged",) for v in views)
               or _bundle_changed(superset)
               or superset["replaces"] is not None
               or superset["pending_prune"] > 0
               or any(superset[k]["unknown"] for k, _l in BUNDLE_KINDS))
    return {
        "project": project.name,
        "changed": changed,
        "load_record": load_record,
        "data": table_diffs,
        "views": views,
        "superset": superset,
    }


# ---------------------------------------------------------------- 사람이 읽는 출력

def _columns_text(cols: Mapping[str, Any]) -> list[str]:
    parts = []
    if cols["added"]:
        parts.append(f"열 추가: {', '.join(cols['added'])}")
    if cols["removed"]:
        parts.append(f"열 삭제: {', '.join(cols['removed'])}")
    if cols["retyped"]:
        parts.append("타입 변경: " + ", ".join(f"{r['column']}({r['from']} → {r['to']})" for r in cols["retyped"]))
    if cols["reordered"] and not parts:
        parts.append("열 순서 변경")
    return parts


def _table_text(t: Mapping[str, Any]) -> str:
    if t.get("mode") == "append":
        if t["status"] == "pending":
            return PENDING_TEXT
        b = t["batches"]
        parts = [f"배치 +{len(b['added'])} {MINUS}{len(b['removed'])} ~{len(b['changed'])}"
                 f" (+{t['added']}행 {MINUS}{t['removed']}행)"]
        if t["status"] == "new":
            parts.append("(새 테이블)")
        parts += _columns_text(t["columns"])
        return "  ".join(parts)
    if t["status"] == "unrecorded":
        return f"{t['rows']}행"
    if t["status"] == "removed":
        return f"테이블 삭제 ({MINUS}{t['removed']}행)"
    counts = f"+{t['added']}행 {MINUS}{t['removed']}행 ~{t['changed']}행"
    if t["status"] == "new":
        return f"{counts}  (새 테이블)"
    parts = [counts] + _columns_text(t["columns"])
    if not t["comparable"]:
        parts.append(f"({t['reason']}: 전부 추가·전부 삭제로 셈)")
    return "  ".join(parts)



def _view_text(v: Mapping[str, Any]) -> str:
    if v["status"] == "unknown":
        return UNKNOWN_TEXT + (f" ({v['reason']})" if v["reason"] else "")
    if v["status"] == "new":
        return f"새 뷰 (열 {len(v['columns']['added'])}개)"
    if v["status"] == "removed":
        return "뷰 삭제"
    return "  ".join(_columns_text(v["columns"]))


def _bundle_text(bucket: Mapping[str, Any]) -> str:
    parts = []
    if bucket["added"]:
        parts.append(f"+{len(bucket['added'])} ({', '.join(bucket['added'])})")
    if bucket["changed"]:
        parts.append(f"~{len(bucket['changed'])} ("
                     + ", ".join(f"{c['key']}: {c['detail']}" for c in bucket["changed"]) + ")")
    if bucket["removed"]:
        parts.append(f"{MINUS}{len(bucket['removed'])} ({', '.join(bucket['removed'])})")
    if bucket["unknown"]:
        parts.append(f"?{len(bucket['unknown'])} ({', '.join(u['key'] for u in bucket['unknown'])}: {UNKNOWN_TEXT})")
    return "  ".join(parts) or "변경 없음"


def format_plan(plan: Mapping[str, Any]) -> str:
    """plan dict → 여러 줄의 글. 줄 머리는 `데이터`, `뷰`, `Superset`이고 이어지는 줄은 같은 폭만큼 들여 쓴다."""
    lines: list[str] = []

    def section(label: str, rows: list[str]) -> None:
        # 한글은 화면에서 두 칸이므로 글자 수가 아니라 칸 수로 맞춘다
        width = sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in label)
        for i, row in enumerate(rows):
            lines.append((label + " " * (LABEL_WIDTH - width) if i == 0 else " " * LABEL_WIDTH) + row)

    data = plan["data"]
    rows = []
    if any(t["status"] == "unrecorded" for t in data):
        rows.append(f"{NO_RECORD_TEXT}: 비교할 수 없음(한 번 apply하면 생김). CSV 행 수만 표시")
    rows += [f"{t['table']}  {_table_text(t)}" for t in data if t["status"] != "unchanged"]
    same = [t["table"] for t in data if t["status"] == "unchanged"]
    if same:
        rows.append(f"변경 없음: {', '.join(same)}" if rows else f"변경 없음 (테이블 {len(same)}개)")
    section("데이터", rows or ["테이블 없음"])

    views = plan["views"]
    rows = [f"dash.{v['dataset']}  {_view_text(v)}" for v in views if v["status"] != "unchanged"]
    same_views = [v["dataset"] for v in views if v["status"] == "unchanged"]
    if same_views:
        rows.append(f"변경 없음: {', '.join('dash.' + k for k in same_views)}" if rows
                    else f"변경 없음 (뷰 {len(same_views)}개)")
    section("뷰", rows or ["뷰 없음"])

    b = plan["superset"]
    rows = []
    if b["first"]:
        rows.append("처음 적용 (지난 번들이 없음)" if not b.get("replaces") else "프로젝트 전환 (전부 새로 만듦)")
    if b["database"] != "unchanged" and not b["first"]:
        rows.append({"added": "데이터베이스 연결 추가", "changed": "데이터베이스 연결 변경",
                     "removed": "데이터베이스 연결 삭제"}[b["database"]])
    quiet = []
    for kind, label in BUNDLE_KINDS:
        text = _bundle_text(b[kind])
        if text == "변경 없음":
            quiet.append(label)
        else:
            rows.append(f"{label} {text}")
    if b.get("pending_prune"):
        rows.append(f"지난 적용에서 지우지 못한 객체 {b['pending_prune']}개 삭제 재시도")
    if b.get("replaces"):
        rows.append(f"지금 적용된 프로젝트 '{b['replaces']['project']}'의 객체 {b['replaces']['objects']}개 삭제")
    if b["other"]:
        rows.append(f"그 밖의 파일 변경: {', '.join(b['other'])}")
    if b.get("assumed_columns"):
        rows.append(f"(데이터셋 {', '.join(b['assumed_columns'])}의 열은 지금 뷰 기준. 적용 뒤 달라질 수 있음)")
    if quiet and rows:
        rows.append(f"변경 없음: {', '.join(quiet)}")
    section("Superset", rows or ["변경 없음"])

    lines.append("")
    lines.append("→ 바뀌는 것이 있음. 적용: make apply" if plan["changed"] else "→ 바뀌는 것이 없음")
    return "\n".join(lines)
