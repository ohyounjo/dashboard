"""적용 전에 차이 보기(python -m dashkit plan, 보강설계서 5-2). DB·Superset·상태 파일을 바꾸지 않는다.

세 가지를 비교한다.

- 데이터: CSV와 지금의 raw 테이블을 기본 키로 비교한다(+추가 −삭제 ~변경 행 수).
- 뷰: 새 설정으로 만들어질 dash 뷰의 열과 지금 뷰의 열.
- Superset: 새 번들과 지난 sync가 남긴 `.build/<프로젝트>/bundle/`을 파일 단위로 비교한다.

차이 계산은 순수 함수(diff_rows, diff_columns, table_diff, predict_view_columns, diff_views, diff_bundles)이고,
읽어 오는 부분은 dashkit/loader/current.py(DB)와 read_bundle_dir(파일)이다. make_plan이 둘을 잇는다.
결과는 JSON으로 바꿀 수 있는 dict이고 format_plan이 사람이 읽는 글로 바꾼다.
Studio의 데이터 화면은 CSV 하나를 검증한 TableData와 current.read_raw_table의 결과를 table_diff에 넘기면 된다.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import yaml

from .config import Project, table_order

Columns = list[tuple[str, str]]          # [(열 이름, information_schema data_type)], 순서 있음

# schema.yaml의 type → 적재 뒤 information_schema.columns의 data_type(로더의 CREATE TABLE과 짝이다)
INFO_TYPES = {
    "text": "text",
    "enum": "text",
    "integer": "bigint",
    "number": "numeric",
    "boolean": "boolean",
    "date": "date",
    "timestamp": "timestamp with time zone",
}

MAX_KEYS = 20                            # 결과에 싣는 예시 키 수(종류마다)
MINUS = "−"                         # −
BUNDLE_KINDS = (("datasets", "데이터셋"), ("charts", "차트"), ("dashboards", "대시보드"))
UNKNOWN_TEXT = "적용해야 알 수 있음"
LABEL_WIDTH = 9                          # 출력의 줄 머리 폭(화면 칸 수)


class PlanError(Exception):
    """plan을 만들 수 없음(CSV 검증 실패, DB 접속 실패). errors는 한 줄에 하나."""

    def __init__(self, title: str, errors: list[str]):
        self.title = title
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


# ---------------------------------------------------------------- 열 비교(순수)

def table_columns(table: Mapping[str, Any]) -> Columns:
    """schema.yaml의 테이블 정의 → 적재 뒤 raw 테이블의 열(= `table:` 데이터셋 뷰의 열)."""
    return [(name, INFO_TYPES[spec["type"]]) for name, spec in table["columns"].items()]


def diff_columns(new: Sequence[tuple[str, str]], old: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """열 목록 두 개의 차이. added·removed는 이름, retyped는 {column, from, to}, reordered는 같은 열의 순서만 바뀜."""
    new_types, old_types = dict(new), dict(old)
    added = [c for c, _t in new if c not in old_types]
    removed = [c for c, _t in old if c not in new_types]
    retyped = [{"column": c, "from": old_types[c], "to": t} for c, t in new if c in old_types and old_types[c] != t]
    common_new = [c for c, _t in new if c in old_types]
    common_old = [c for c, _t in old if c in new_types]
    return {"added": added, "removed": removed, "retyped": retyped, "reordered": common_new != common_old}


def _columns_changed(d: Mapping[str, Any]) -> bool:
    return bool(d["added"] or d["removed"] or d["retyped"] or d["reordered"])


# ---------------------------------------------------------------- 행 비교(순수)

def _norm(value: Any, tz: tzinfo | None) -> Any:
    """비교용 값. 시각은 UTC로 맞춘다(시간대 없는 시각은 tz 기준으로 본다. 적재할 때 Postgres가 하는 해석과 같다)."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            if tz is None:
                return value
            value = value.replace(tzinfo=tz)
        return value.astimezone(timezone.utc)
    return value


def _key_text(key: tuple) -> str:
    return ", ".join("" if k is None else str(k) for k in key)


def diff_rows(
    columns: Sequence[str],
    primary_key: Sequence[str],
    new_rows: Iterable[Sequence[Any]],
    old_columns: Sequence[str],
    old_rows: Iterable[Sequence[Any]],
    *,
    tz: tzinfo | None = None,
    max_keys: int = MAX_KEYS,
) -> dict[str, Any]:
    """새 행(columns 순서)과 지금 행(old_columns 순서)을 기본 키로 비교한다.

    - added: 새 행에만 있는 키, removed: 지금 행에만 있는 키, changed: 키는 같고 값이 다른 행, unchanged: 나머지.
    - 값은 **양쪽에 다 있는 열**만 비교한다(compared_columns). 열이 추가·삭제된 것은 diff_columns가 따로 알려 준다.
    - 기본 키 열이 지금 테이블에 없으면 행을 맞출 수 없으므로 전부 추가·전부 삭제로 센다(comparable = False).
    - keys: 종류마다 앞에서부터 max_keys개의 키(문자열. 복합 키는 ", "로 잇는다).
    """
    columns, old_columns, primary_key = list(columns), list(old_columns), list(primary_key)
    new_rows, old_rows = list(new_rows), list(old_rows)
    result: dict[str, Any] = {
        "added": 0, "removed": 0, "changed": 0, "unchanged": 0,
        "comparable": True, "compared_columns": [c for c in columns if c in old_columns],
        "keys": {"added": [], "removed": [], "changed": []},
    }

    def note(kind: str, key: tuple) -> None:
        result[kind] += 1
        if len(result["keys"][kind]) < max_keys:
            result["keys"][kind].append(_key_text(key))

    new_pk = [columns.index(c) for c in primary_key]
    if any(c not in old_columns for c in primary_key):
        result["comparable"] = False
        result["compared_columns"] = []
        for row in new_rows:
            note("added", tuple(row[i] for i in new_pk))
        result["removed"] = len(old_rows)
        return result

    old_pk = [old_columns.index(c) for c in primary_key]
    new_idx = [columns.index(c) for c in result["compared_columns"]]
    old_idx = [old_columns.index(c) for c in result["compared_columns"]]

    old_by_key: dict[tuple, tuple] = {}
    for row in old_rows:
        key = tuple(_norm(row[i], tz) for i in old_pk)
        old_by_key[key] = tuple(_norm(row[i], tz) for i in old_idx)
    seen: set[tuple] = set()
    for row in new_rows:
        key = tuple(_norm(row[i], tz) for i in new_pk)
        seen.add(key)
        if key not in old_by_key:
            note("added", key)
        elif old_by_key[key] != tuple(_norm(row[i], tz) for i in new_idx):
            note("changed", key)
        else:
            result["unchanged"] += 1
    for key in old_by_key:
        if key not in seen:
            note("removed", key)
    return result


def table_diff(
    name: str,
    table: Mapping[str, Any],
    new_rows: Sequence[Sequence[Any]],
    current: Any | None,
    *,
    tz: tzinfo | None = None,
) -> dict[str, Any]:
    """테이블 하나의 차이. table은 schema.yaml의 (정규화된) 테이블 정의, new_rows는 검증을 통과한 CSV 행
    (validate.TableData.rows, 열 순서는 table["columns"]), current는 지금의 raw 테이블(loader.current.RawTable.
    `columns`, `row_columns`, `rows`만 쓴다)이고 테이블이 아직 없으면 None이다.

    status: new(테이블이 없음. 전부 추가) | changed | unchanged.
    """
    columns = list(table["columns"])
    if current is None:
        rows = diff_rows(columns, table["primary_key"], new_rows, [], [], tz=tz)
        rows.update(comparable=True)
        cols = {"added": [], "removed": [], "retyped": [], "reordered": False}
        status = "new"
    else:
        rows = diff_rows(columns, table["primary_key"], new_rows, current.row_columns, current.rows, tz=tz)
        cols = diff_columns(table_columns(table), current.columns)
        changed = rows["added"] or rows["removed"] or rows["changed"] or _columns_changed(cols)
        status = "changed" if changed else "unchanged"
    return {"table": name, "status": status, "rows": len(new_rows), "columns": cols, **rows}


# ---------------------------------------------------------------- 뷰 열(순수)

_WORD = re.compile(r"[a-z0-9_]+")


def predict_view_columns(
    project: Project,
    raw_now: Mapping[str, Sequence[tuple[str, str]]],
    views_now: Mapping[str, Sequence[tuple[str, str]]],
    describe: Callable[[str], Columns] | None,
) -> dict[str, dict[str, Any]]:
    """새 설정으로 만들어질 뷰의 열을 데이터셋마다 구한다. {키: {"columns": [(열, 타입)] 또는 None, "reason": 이유 또는 None}}.

    - `table:` 데이터셋: schema.yaml의 열 그대로다(뷰가 `SELECT * FROM raw.<테이블>`이므로).
    - `sql:` 데이터셋: describe(쿼리)가 **지금 DB**에서 결과 열을 알려 준다(실행하지 않는 Parse/Describe).
      그 답이 적용 뒤에도 맞으려면 쿼리가 읽는 것의 구조가 그대로여야 한다. 그래서 쿼리 글에 이름이 나오는
      raw 테이블이나 앞선 데이터셋 뷰 가운데 구조(열 이름·타입·순서)가 바뀌는 것이 하나라도 있으면 묻지 않고
      "알 수 없음"으로 둔다. 이름은 글자 단위로만 찾으므로 실제로 읽지 않아도 걸릴 수 있다(안전한 쪽).
    - describe가 None이거나 실패해도 "알 수 없음"이다(이유를 싣는다).
    """
    tables = project.schema["tables"]
    datasets = project.dashboard["datasets"]
    # 구조가 바뀌는 raw 테이블: 새로 생김, 없어짐, 열이 달라짐
    unstable = {t for t in tables if list(raw_now.get(t, [])) != table_columns(tables[t])}
    unstable |= {t for t in raw_now if t not in tables}
    out: dict[str, dict[str, Any]] = {}
    for key, ds in datasets.items():
        if "table" in ds:
            out[key] = {"columns": table_columns(tables[ds["table"]]), "reason": None}
            continue
        if describe is None:
            out[key] = {"columns": None, "reason": "아직 적재된 적이 없음"}
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
            try:
                out[key] = {"columns": list(describe(ds["sql"])), "reason": None}
            except Exception as exc:  # loader.current.DescribeError(순수 함수라 형을 가리지 않는다)
                out[key] = {"columns": None, "reason": f"지금 DB에서 열을 알 수 없음: {exc}"}
    return out


def diff_views(
    predicted: Mapping[str, Mapping[str, Any]],
    views_now: Mapping[str, Sequence[tuple[str, str]]],
) -> list[dict[str, Any]]:
    """뷰마다의 차이. status: new | changed | unchanged | unknown(적용해야 알 수 있음) | removed."""
    out: list[dict[str, Any]] = []
    for key, p in predicted.items():
        entry: dict[str, Any] = {"dataset": key, "exists": key in views_now, "reason": p["reason"],
                                 "columns": {"added": [], "removed": [], "retyped": [], "reordered": False}}
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
                        "columns": {"added": [], "removed": [c for c, _t in views_now[key]], "retyped": [],
                                    "reordered": False}})
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

    DB 접근: raw 테이블과 `sql:` 데이터셋의 열은 owner 계정의 읽기 전용 트랜잭션, 지금 뷰의 열은 dash_reader.
    Superset에는 접속하지 않는다(지난 sync가 남긴 번들 폴더와 비교한다).
    """
    from .loader import current
    from .loader.validate import validate_csv
    from .sync.bundle import BUNDLE_ROOT, DATASET_DIR, build_bundle

    order = table_order(project)
    tables = project.schema["tables"]
    datasets = project.dashboard["datasets"]
    data, errors = validate_csv(project.schema, project.data_dir, order)
    if errors:
        raise PlanError("CSV 검증 실패, 차이를 계산하지 않음", errors)

    table_diffs: list[dict[str, Any]] = []
    try:
        views_now = current.reader_view_columns(dbname)
        with current.readonly_owner(dbname) as conn:
            tz = current.session_timezone(conn)
            raw_now = current.read_columns(conn, current.RAW)
            for name in order:
                now = None
                if name in raw_now:
                    now = current.read_raw_table(conn, name, raw_now[name], list(tables[name]["columns"]))
                table_diffs.append(table_diff(name, tables[name], data[name].rows, now, tz=tz))
            for name in raw_now:
                if name not in tables:
                    gone = current.read_raw_table(conn, name, raw_now[name], [raw_now[name][0][0]])
                    table_diffs.append({
                        "table": name, "status": "removed", "rows": 0, "added": 0, "removed": len(gone.rows),
                        "changed": 0, "unchanged": 0, "comparable": True, "compared_columns": [],
                        "keys": {"added": [], "removed": [], "changed": []},
                        "columns": {"added": [], "removed": [c for c, _t in raw_now[name]], "retyped": [], "reordered": False},
                    })
            loaded_before = bool(raw_now)
            predicted = predict_view_columns(
                project, raw_now, views_now,
                (lambda q: current.describe_query(conn, q)) if loaded_before else None)
    except current.CurrentError as exc:
        raise PlanError("지금 상태를 읽지 못함", [str(exc)]) from None

    views = diff_views(predicted, views_now)

    # 번들: 열을 아는 데이터셋은 그 열로, 모르면 지금 뷰의 열로(바뀌지 않는다고 본다) 만든다.
    # 둘 다 없으면 그 데이터셋과 그 데이터셋을 쓰는 차트는 "적용해야 알 수 있음"이다.
    bundle_columns: dict[str, Columns] = {}
    unknown_files: dict[str, str] = {}
    assumed: list[str] = []
    for key in datasets:
        if predicted[key]["columns"] is not None:
            bundle_columns[key] = predicted[key]["columns"]
        elif key in views_now:
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
    old_files = None if replaces else read_bundle_dir(build_root / project.name / BUNDLE_ROOT)
    superset = diff_bundles(new_files, old_files, unknown_files)
    superset["assumed_columns"] = assumed   # 지금 뷰의 열을 그대로 쓴 데이터셋(적용 뒤 열이 달라질 수 있다)
    superset["replaces"] = replaces

    changed = (any(t["status"] != "unchanged" for t in table_diffs)
               or any(v["status"] not in ("unchanged",) for v in views)
               or _bundle_changed(superset)
               or superset["replaces"] is not None
               or any(superset[k]["unknown"] for k, _l in BUNDLE_KINDS))
    return {
        "project": project.name,
        "changed": changed,
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
    if t["status"] == "removed":
        return f"테이블 삭제 ({MINUS}{t['removed']}행)"
    counts = f"+{t['added']}행 {MINUS}{t['removed']}행 ~{t['changed']}행"
    if t["status"] == "new":
        return f"{counts}  (새 테이블)"
    parts = [counts] + _columns_text(t["columns"])
    if not t["comparable"]:
        parts.append("기본 키 열이 지금 테이블에 없어 행을 맞출 수 없음")
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
    rows = [f"{t['table']}  {_table_text(t)}" for t in data if t["status"] != "unchanged"]
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
