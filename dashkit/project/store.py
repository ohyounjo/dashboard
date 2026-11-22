"""프로젝트 파일 읽기와 저장. 계약: contracts/studio.md "저장 방식".

저장은 항상 같은 순서다.
1. 임시 폴더에 프로젝트 사본을 만들고 변경을 적용한다.
2. 사본을 `config.load_project`로 검증한다(CSV를 바꿨으면 `validate_csv`도).
3. 통과하면 바뀐 파일만 원래 자리에 원자적으로 바꿔 넣는다(같은 폴더의 임시 파일 → `os.replace`).
   실패하면 원래 파일을 건드리지 않는다.

경로의 `{p}`, `{table}`은 실제 목록(프로젝트 폴더 이름, 그 프로젝트 schema.yaml의 테이블)과 대조한 뒤
**목록 쪽 문자열**로만 파일 경로를 만든다.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping

import yaml

from .. import config, scaffold
from ..loader.validate import TableData, validate_csv
from . import yamledit

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")          # config.load_project와 같은 형식
TABLE_RE = re.compile(r"[a-z][a-z0-9_]{0,62}")           # 스키마의 테이블 이름 형식(파일 이름 data/<테이블>.csv에 쓴다)
CONFIG_FILES = (config.SCHEMA_FILE, config.DASHBOARD_FILE)
UPLOAD_TTL = 3600          # 추론에 올린 CSV를 기억하는 시간(초)
UPLOAD_KEEP = 3            # 기억하는 업로드 수


class StoreError(Exception):
    """API 오류로 그대로 나가는 실패. errors는 `<파일>:<위치>: <이유>` 줄들."""

    def __init__(self, status: int, detail: str, message: str, errors: list[str] | None = None):
        self.status = status
        self.detail = detail
        self.message = message
        self.errors = list(errors) if errors is not None else None
        super().__init__(message)


def projects_dir() -> Path:
    return config.default_projects_dir()


def state_dir() -> Path:
    return Path(os.environ.get("STATE_DIR") or "/app/.state")


def _dates_to_str(obj: Any) -> Any:
    """YAML이 날짜로 읽은 값을 문자열로(config.load_project와 같은 처리). JSON으로 내보내고 비교할 때 쓴다."""
    if isinstance(obj, dict):
        return {k: _dates_to_str(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_dates_to_str(v) for v in obj]
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    return obj


# ---------------------------------------------------------------- 프로젝트 목록과 경로 값

def project_names() -> list[str]:
    """projects/ 바로 아래의 프로젝트 폴더 이름(이름순). 형식이 맞지 않는 이름과 심볼릭 링크는 뺀다."""
    root = projects_dir()
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    return [p.name for p in entries if NAME_RE.fullmatch(p.name) and p.is_dir() and not p.is_symlink()]


def resolve_project(name: str) -> str:
    """경로의 {p} → 목록에 있는 이름. 없으면 404."""
    for known in project_names():
        if known == name:
            return known
    raise StoreError(404, "unknown_project", "없는 프로젝트입니다")


def project_root(name: str) -> Path:
    return projects_dir() / resolve_project(name)


def load(name: str) -> config.Project:
    """검증을 통과한 설정. 실패하면 422 invalid_config."""
    try:
        return config.load_project(resolve_project(name), projects_dir())
    except config.ConfigError as exc:
        raise StoreError(422, "invalid_config", "설정 검사를 통과하지 못했습니다", exc.errors) from None


def resolve_table(project: config.Project, table: str) -> str:
    """경로의 {table} → 그 프로젝트 schema.yaml의 테이블 이름. 없으면 404."""
    for known in project.schema["tables"]:
        if known == table and TABLE_RE.fullmatch(known):
            return known
    raise StoreError(404, "unknown_table", "schema.yaml에 없는 테이블입니다")


def resolve_replace_table(project: config.Project, table: str) -> str:
    """CSV를 통째로 바꾸는 경로의 {table}. 누적(append) 테이블이면 409 append_table(배치를 더한다)."""
    name = resolve_table(project, table)
    if (project.schema["tables"][name].get("mode") or "replace") == "append":
        raise StoreError(409, "append_table", "누적(append) 테이블은 파일을 통째로 바꾸지 않고 배치를 더합니다"
                         "(운용 화면의 데이터 메뉴 또는 make ingest)")
    return name


def current_project() -> str | None:
    """지금 웹앱에 적용된 프로젝트(.state/embed.json의 project). 없으면 None."""
    try:
        with open(state_dir() / "embed.json", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    name = data.get("project") if isinstance(data, dict) else None
    return name if isinstance(name, str) and NAME_RE.fullmatch(name) else None


def read_raw(root: Path, file: str) -> Any:
    """설정 파일을 읽은 그대로(기본값을 채우지 않은 것). 읽을 수 없으면 None."""
    try:
        return _dates_to_str(yaml.safe_load((root / file).read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def listing_row(name: str) -> dict[str, Any]:
    """목록의 한 줄: {name, title, valid, errors}."""
    root = projects_dir() / name
    try:
        project = config.load_project(name, projects_dir())
        return {"name": name, "title": project.dashboard["app"]["title"], "valid": True, "errors": 0}
    except config.ConfigError as exc:
        raw = read_raw(root, config.DASHBOARD_FILE)
        title = raw.get("app", {}).get("title") if isinstance(raw, dict) and isinstance(raw.get("app"), dict) else None
        return {"name": name, "title": title if isinstance(title, str) else None, "valid": False,
                "errors": len(exc.errors)}


def revision(root: Path) -> str:
    """두 설정 파일 내용의 지문. `PATCH …/config`의 `revision`과 비교한다."""
    h = hashlib.sha256()
    for file in CONFIG_FILES:
        try:
            h.update((root / file).read_bytes())
        except OSError:
            h.update(b"\0missing")
        h.update(b"\0")
    return h.hexdigest()[:16]


def csv_row_count(path: Path) -> int | None:
    """CSV의 데이터 행 수(머리글과 빈 줄 제외). 읽을 수 없으면 None."""
    try:
        text = path.read_text(encoding="utf-8-sig")
        rows = csv.reader(io.StringIO(text, newline=""))
        next(rows, None)
        return sum(1 for r in rows if r and not (len(r) == 1 and r[0].strip() == ""))
    except (OSError, UnicodeDecodeError, csv.Error):
        return None


# ---------------------------------------------------------------- 바뀐 파일 목록

class Changes:
    """이 프로세스가 쓴 파일(저장소 기준 경로 → 마지막으로 쓴 시각). 되돌리기는 git에 맡긴다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._files: "OrderedDict[str, str]" = OrderedDict()

    def add(self, project: str, files: list[str]) -> None:
        at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            for file in files:
                path = f"projects/{project}/{file}"
                self._files.pop(path, None)
                self._files[path] = at

    def list(self) -> list[dict[str, str]]:
        with self._lock:
            return [{"path": p, "at": at} for p, at in self._files.items()]


changes = Changes()


# ---------------------------------------------------------------- 저장

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def project_lock(name: str) -> threading.Lock:
    """프로젝트마다 하나의 잠금. 읽고-고치고-쓰기와 apply가 겹치지 않게 한다."""
    with _locks_guard:
        return _locks.setdefault(name, threading.Lock())


def _replace_atomic(path: Path, content: bytes) -> None:
    """같은 폴더의 임시 파일에 쓰고 os.replace로 바꿔 넣는다(읽는 쪽은 옛 파일 아니면 새 파일만 본다)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".studio-tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except OSError:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class Staged:
    """검증을 통과한 사본. project는 사본에서 읽은 설정, data는 CSV를 검증했을 때의 표 내용."""
    project: config.Project
    data: dict[str, TableData] | None


def prepare(name: str, files: Mapping[str, bytes], *, with_csv: bool) -> Staged:
    """임시 폴더의 사본에 files(프로젝트 기준 경로 → 내용)를 덮어쓰고 검증한다. 원래 파일은 건드리지 않는다.

    with_csv면 data/도 복사해 CSV 검증(validate_csv)까지 한다. 실패하면 StoreError(422).
    """
    root = project_root(name)
    with tempfile.TemporaryDirectory(prefix="dashkit-studio-") as tmp:
        copy_root = Path(tmp) / root.name
        copy_root.mkdir()
        for file in CONFIG_FILES:
            if (root / file).is_file():
                shutil.copyfile(root / file, copy_root / file)
        if with_csv and (root / "data").is_dir():
            (copy_root / "data").mkdir()
            for p in (root / "data").iterdir():
                if p.is_file() and p.suffix == ".csv" and f"data/{p.name}" not in files:
                    shutil.copyfile(p, copy_root / "data" / p.name)
        for rel, content in files.items():
            target = copy_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        try:
            project = config.load_project(root.name, Path(tmp))
        except config.ConfigError as exc:
            raise StoreError(422, "invalid_config", "설정 검사를 통과하지 못했습니다(파일은 그대로입니다)",
                             exc.errors) from None
        data = None
        if with_csv:
            # 누적(append) 테이블은 배치 폴더가 원본이라 여기서 보지 않는다(contracts/ingest.md)
            order = [t for t in config.table_order(project)
                     if (project.schema["tables"][t].get("mode") or "replace") != "append"]
            data, errors = validate_csv(project.schema, project.data_dir, order)
            if errors:
                raise StoreError(422, "invalid_csv", "CSV 검증을 통과하지 못했습니다(파일은 그대로입니다)", errors)
        # 사본 폴더는 곧 지워지므로 원래 폴더를 가리키는 설정으로 돌려준다
        project = config.Project(name=project.name, root=root, schema=project.schema, dashboard=project.dashboard)
    return Staged(project, data)


def commit(name: str, files: Mapping[str, bytes]) -> list[str]:
    """검증을 마친 files 가운데 내용이 달라진 것만 원래 자리에 바꿔 넣는다. 바뀐 파일 목록을 돌려준다."""
    root = project_root(name)
    changed: list[str] = []
    for rel, content in files.items():
        path = root / rel
        try:
            if path.read_bytes() == content:
                continue
        except OSError:
            pass
        _replace_atomic(path, content)
        changed.append(rel)
    if changed:
        changes.add(root.name, changed)
    return changed


# ---------------------------------------------------------------- 설정 변경 연산 저장(Studio·관리 서비스가 함께 쓴다)

def save_ops(name: str, op_list: list[Mapping[str, Any]], base_revision: str | None) -> dict[str, Any]:
    """연산을 적용해 저장한다(고친 곳만 다시 쓰기 → 사본 검증 → 원자적 교체). 부른 쪽이 프로젝트 잠금을 잡는다.

    base_revision이 있으면 지금 파일의 revision과 같아야 한다(아니면 409 stale_config). 관리 서비스는 반드시 보낸다.
    결과: {"changed", "dashboard", "schema", "revision", "written"}
    """
    from . import ops

    root = project_root(name)
    if base_revision is not None and base_revision != revision(root):
        raise StoreError(409, "stale_config", "설정 파일이 그 사이에 바뀌었습니다. 다시 불러온 뒤 시도하세요")
    _no_replace_column_add(root, op_list)
    files: dict[str, bytes] = {}
    how: dict[str, str] = {}
    for file in ops.files_of(op_list):
        try:
            text = (root / file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise StoreError(422, "invalid_config", "설정 파일을 읽을 수 없습니다",
                             [f"{file}:(최상위): 파일을 읽을 수 없음"]) from None
        new_text, how[file] = edit_yaml(text, lambda doc, file=file: ops.apply_ops(doc, op_list, file))
        files[file] = new_text.encode("utf-8")
    prepare(name, files, with_csv=False)
    changed = commit(name, files)
    return {"changed": changed, "dashboard": read_raw(root, config.DASHBOARD_FILE),
            "schema": read_raw(root, config.SCHEMA_FILE), "revision": revision(root), "written": how}


def _no_replace_column_add(root: Path, op_list: list[Mapping[str, Any]]) -> None:
    """데이터 파일 변환·합치기가 필요한 연산은 YAML만 쓰는 Studio 저장에서 거절한다.
    관리 서비스의 구조 변경(dashkit/ingest/structure.py)이 설정과 데이터 파일을 한 번에 바꾼다."""
    from . import ops

    tables = (read_raw(root, config.SCHEMA_FILE) or {}).get("tables") or {}
    for i, o in enumerate(op_list):
        op_name = o.get("op")
        if op_name == "column.add":
            spec = tables.get(o.get("table"))
            if isinstance(spec, dict) and (spec.get("mode") or "replace") == "replace":
                message = f"ops[{i}] column.add: replace 테이블은 CSV에도 열을 더해야 함(운용 화면의 열 추가를 쓰거나 두 파일을 함께 고친다)"
                raise StoreError(400, "invalid_op", message, [message])
        elif op_name in ("column.rename", "column.remove"):
            message = f"ops[{i}] {op_name}: 데이터 파일을 함께 변환해야 하는 연산입니다. 운용 화면에서 실행하세요"
            raise StoreError(400, "invalid_op", message, [message])
        elif op_name == "column.update":
            spec = tables.get(o.get("table"))
            col_spec = (spec.get("columns") or {}).get(o.get("column")) if isinstance(spec, dict) else None
            old_type = col_spec.get("type") if isinstance(col_spec, dict) else None
            new_type = (o.get("set") or {}).get("type") if isinstance(o.get("set"), Mapping) else None
            if old_type and new_type and (old_type, new_type) in ops._NARROWER:
                message = f"ops[{i}] column.update: 유형 좁히기는 데이터 파일을 함께 변환해야 하는 연산입니다. 운용 화면에서 실행하세요"
                raise StoreError(400, "invalid_op", message, [message])


# ---------------------------------------------------------------- YAML 고쳐 쓰기

def edit_yaml(text: str, mutate: Callable[[MutableMapping], None]) -> tuple[str, str]:
    """text를 mutate로 고친 글과 쓴 방식("exact": 고친 곳만 다시 씀, "roundtrip": ruamel로 전체를 다시 씀).

    어느 방식이든 결과를 PyYAML로 다시 읽어 "보통 dict에 mutate를 적용한 값"과 같은지 확인한다.
    같지 않으면 저장하지 않는다(StoreError 500).
    """
    try:
        expected = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise StoreError(422, "invalid_config", "설정 파일을 읽을 수 없습니다", [f"YAML 문법 오류: {exc}"]) from None
    if not isinstance(expected, dict):
        raise StoreError(422, "invalid_config", "설정 파일을 읽을 수 없습니다", ["최상위가 매핑이 아님"])
    mutate(expected)                      # 연산 오류(OpError)는 여기서 먼저 난다
    expected = _dates_to_str(expected)

    def matches(new_text: str) -> bool:
        try:
            return yamledit.strict_equal(_dates_to_str(yaml.safe_load(new_text)), copy.deepcopy(expected))
        except yaml.YAMLError:
            return False

    try:
        doc = yamledit.Document(text)
        mutate(doc.root)
        new_text = doc.render()
        if matches(new_text):
            return new_text, "exact"
    except yamledit.Unsupported:
        pass
    from . import roundtrip

    try:
        new_text = roundtrip.edit(text, mutate)
    except roundtrip.RoundTripError as exc:
        raise StoreError(500, "cannot_write", f"설정 파일을 다시 쓸 수 없습니다: {exc}") from None
    if not matches(new_text):
        raise StoreError(500, "cannot_write", "다시 쓴 설정이 기대한 내용과 달라 저장하지 않았습니다")
    return new_text, "roundtrip"


# ---------------------------------------------------------------- 추론에 올린 CSV

class Uploads:
    """`POST …/projects/infer`에 올린 CSV를 프로젝트를 만들 때까지 메모리에 둔다(projects/에 쓰지 않는다)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: "OrderedDict[str, tuple[float, dict[str, bytes]]]" = OrderedDict()

    def put(self, sources: dict[str, bytes]) -> str:
        upload_id = secrets.token_urlsafe(16)
        now = time.monotonic()
        with self._lock:
            self._items[upload_id] = (now, sources)
            for key in [k for k, (at, _s) in self._items.items() if now - at > UPLOAD_TTL]:
                del self._items[key]
            while len(self._items) > UPLOAD_KEEP:
                self._items.popitem(last=False)
        return upload_id

    def get(self, upload_id: Any) -> dict[str, bytes]:
        with self._lock:
            item = self._items.get(upload_id) if isinstance(upload_id, str) else None
            if item is None or time.monotonic() - item[0] > UPLOAD_TTL:
                raise StoreError(410, "upload_expired", "올린 CSV를 찾을 수 없습니다. 파일을 다시 올려 주세요")
            return item[1]

    def drop(self, upload_id: str) -> None:
        with self._lock:
            self._items.pop(upload_id, None)


uploads = Uploads()


def create_project(name: Any, inference: Any, sources: Mapping[str, bytes], *, resuggest: bool) -> str:
    """추론 결과로 프로젝트를 만든다(scaffold.render → write_project). 만든 이름을 돌려준다."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise StoreError(422, "invalid_name", "프로젝트 이름은 소문자·숫자로 시작하고 소문자·숫자·_·-만 씁니다")
    if not isinstance(inference, dict) or not isinstance(inference.get("tables"), list):
        raise StoreError(422, "invalid_request", "inference 형식이 올바르지 않습니다")
    if (projects_dir() / name).exists():
        raise StoreError(409, "project_exists", "같은 이름의 프로젝트가 이미 있습니다")
    try:
        if resuggest:
            # 표(이름, 유형, 키)를 고쳤으면 초안 차트를 그 표에서 다시 고른다. app은 보낸 것을 유지한다
            tables = inference["tables"]
            dashboard = scaffold.suggest_dashboard(tables)
            sent = inference.get("dashboard")
            if isinstance(sent, dict) and isinstance(sent.get("app"), dict):
                dashboard = {"app": sent["app"], **dashboard}
            inference = {**inference, "dashboard": dashboard}
        files = scaffold.render(name, inference, sources)
        root = scaffold.write_project(name, files, projects_dir())
    except scaffold.ScaffoldError as exc:
        if (projects_dir() / name).exists() and any("이미 있음" in e for e in exc.errors):
            raise StoreError(409, "project_exists", "같은 이름의 프로젝트가 이미 있습니다") from None
        raise StoreError(422, "invalid_inference", "이 추론 결과로는 프로젝트를 만들 수 없습니다", exc.errors) from None
    except (KeyError, TypeError, AttributeError, IndexError, ValueError) as exc:
        raise StoreError(422, "invalid_inference", "이 추론 결과로는 프로젝트를 만들 수 없습니다",
                         [f"추론 결과의 형식이 잘못됨({type(exc).__name__}: {exc})"]) from None
    changes.add(name, sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()))
    return name
