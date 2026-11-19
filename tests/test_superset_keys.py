"""Superset 버전 관리(보강설계서 3-4). DB 없이 돈다.

- 버전은 .env.example의 SUPERSET_VERSION 한 곳에만 있다.
- 키 대조: 우리가 만드는 번들 YAML의 키가 그 버전의 export(contracts/superset-<버전>-export/)에 있는 키인지 본다.
  업그레이드 뒤 export를 다시 받으면, Superset에서 사라진 키가 이 테스트로 드러난다(contracts/superset-upgrade.md).

- import 전용 키: export에는 나오지 않지만 import가 받는 키(표식 is_managed_externally, external_url)는
  export 폴더의 import-only-keys.yaml에 근거와 함께 적어 둔 것만 허용한다. 실제로 저장되는지는 통합 테스트
  (tests/test_sync.py의 표식 테스트)가 본다.

대조 범위: 종류별 최상위 키, 데이터셋의 metrics[]·columns[] 항목 키, 데이터베이스 extra 키.
차트 params와 대시보드 position·metadata 안쪽은 Superset이 JSON으로 그대로 보관하는 값이라 export로는 검증되지 않는다.
그쪽은 갤러리 프로젝트의 렌더링 확인이 맡는다.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

import golden
from conftest import ROOT

KIND_DIRS = {"databases": "database", "datasets": "dataset", "charts": "chart", "dashboards": "dashboard"}


def superset_version() -> str:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    found = re.findall(r"^SUPERSET_VERSION=(\S+)$", text, flags=re.M)
    assert len(found) == 1, ".env.example에 SUPERSET_VERSION이 정확히 한 번 있어야 함"
    return found[0]


def export_dir() -> Path:
    return ROOT / "contracts" / f"superset-{superset_version()}-export"


def _kind(rel: str) -> str | None:
    return KIND_DIRS.get(rel.split("/", 1)[0])


def _keys(doc: dict, kind: str) -> dict[str, set[str]]:
    """{위치: 키 집합}. 위치는 '<종류>', '<종류>.metrics[]' 같은 이름."""
    out = {kind: set(doc)}
    if kind == "dataset":
        for part in ("metrics", "columns"):
            out[f"{kind}.{part}[]"] = {k for item in doc.get(part) or [] for k in item}
    if kind == "database" and isinstance(doc.get("extra"), dict):
        out[f"{kind}.extra"] = set(doc["extra"])
    return out


def exported_keys() -> dict[str, set[str]]:
    base = export_dir()
    merged: dict[str, set[str]] = {}
    for path in sorted(base.rglob("*.yaml")):
        # 종류 폴더는 export 최상위 또는 한 단계 아래(여러 번 받은 export를 폴더로 나눠 둘 수 있다)
        parts = path.relative_to(base).parts
        kind = next((KIND_DIRS[p] for p in parts[:2] if p in KIND_DIRS), None)
        if kind is None:
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for where, keys in _keys(doc, kind).items():
            merged.setdefault(where, set()).update(keys)
    return merged


def import_only_keys() -> dict[str, set[str]]:
    """export에는 없고 import만 받는 최상위 키(종류 → 키). export 폴더 아래 import-only-keys.yaml들을 합친다."""
    merged: dict[str, set[str]] = {}
    for path in sorted(export_dir().rglob("import-only-keys.yaml")):
        for kind, keys in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).items():
            assert kind in KIND_DIRS.values(), f"{path.name}: 알 수 없는 종류 {kind}"
            merged.setdefault(kind, set()).update(keys)
    return merged


def test_import_only_keys_are_not_exported():
    """import 전용 목록은 export에 없는 키만 담는다(export에 나오기 시작하면 목록에서 빼야 한다)."""
    exported = exported_keys()
    stale = {kind: sorted(keys & exported.get(kind, set())) for kind, keys in import_only_keys().items()}
    assert not any(stale.values()), f"export에 있는 키가 import-only-keys.yaml에 남아 있음: {stale}"


def test_version_is_defined_in_one_place():
    version = superset_version()
    assert export_dir().is_dir(), f"{export_dir().relative_to(ROOT)}가 없음(contracts/superset-upgrade.md)"
    for rel in ("docker-compose.yml", "superset/Dockerfile", "Makefile"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert version not in text, f"{rel}에 Superset 버전 {version}이 직접 쓰여 있음(.env.example의 SUPERSET_VERSION만 쓴다)"
    assert "FROM apache/superset:${SUPERSET_VERSION}" in (ROOT / "superset/Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "dashkit-superset:${SUPERSET_VERSION" in compose and "SUPERSET_VERSION: ${SUPERSET_VERSION" in compose


def test_export_covers_every_kind():
    keys = exported_keys()
    for where in ("database", "database.extra", "dataset", "dataset.metrics[]", "dataset.columns[]", "chart", "dashboard"):
        assert keys.get(where), f"export에 {where} 키가 없음"


@pytest.mark.parametrize("name", golden.GOLDEN_PROJECTS)
def test_bundle_keys_exist_in_export(name):
    allowed = exported_keys()
    for kind, keys in import_only_keys().items():
        allowed[kind] = allowed.get(kind, set()) | keys
    unknown = []
    for rel, text in golden.build(name).items():
        kind = _kind(rel)
        if kind is None:
            continue
        for where, keys in _keys(yaml.safe_load(text), kind).items():
            extra = sorted(keys - allowed.get(where, set()))
            if extra:
                unknown.append(f"{rel}: {where}에 export에 없는 키 {extra}")
    assert unknown == [], "\n".join(unknown)
