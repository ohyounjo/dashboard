"""레지스트리에서 계약 파일과 차트 카탈로그의 생성 구간을 만든다(python -m dashkit gen, make gen).

파일 안의 `<!-- 생성 시작: <이름> -->` ~ `<!-- 생성 끝: <이름> -->` 사이만 바꾼다. 손으로 고치지 않는다.
tests/test_charts_registry.py가 최신인지 확인한다.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import yaml

from . import charts

# 유형별 예시를 읽어 오는 프로젝트(보강설계서 4-5). 없으면 예시 없이 표만 만든다.
GALLERY_PROJECT = "chart-gallery"


def _repo_root() -> Path:
    return Path(os.environ.get("REPO_ROOT") or Path(__file__).resolve().parent.parent)


def gallery_examples() -> dict[str, dict]:
    """{viz: 갤러리 dashboard.yaml에서 그 유형을 쓴 첫 차트}. 갤러리가 없으면 빈 dict."""
    path = _repo_root() / "projects" / GALLERY_PROJECT / "dashboard.yaml"
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    out: dict[str, dict] = {}
    for dashboard in (doc.get("dashboards") or {}).values():
        for chart in dashboard.get("charts") or []:
            if "viz" in chart:
                out.setdefault(chart["viz"], chart)
    return out


def _field_names(t: charts.ChartType, required: bool) -> str:
    return ", ".join(f"`{f.name}`" for f in t.fields if f.required == required) or "—"


def catalog_shorthand() -> str:
    """reference/chart-catalog.md의 축약 절: 유형 표(레지스트리) + 유형별 예시(갤러리의 첫 차트)."""
    types = charts.registry()
    examples = gallery_examples()
    lines = [
        f"축약 유형은 {len(types)}종이다. 유형 하나가 `dashkit/charts/<viz>.yaml` 파일 하나이고, 아래 표와 예시는 그 파일들과 "
        f"`projects/{GALLERY_PROJECT}/dashboard.yaml`에서 생성한다(`make gen`). 손으로 고치지 않는다.",
        "",
        "| 축약 `viz` | 이름 | Superset `viz_type` | 필수 필드 | 선택 필드 |",
        "|---|---|---|---|---|",
    ]
    for viz, t in types.items():
        lines.append(f"| `{viz}` | {t.label} | `{t.viz_type}` | {_field_names(t, True)} | {_field_names(t, False)} |")
    if examples:
        lines += [
            "",
            f"유형별 예시는 `projects/{GALLERY_PROJECT}/dashboard.yaml`에서 그 유형을 쓴 첫 차트다. "
            f"`PROJECT={GALLERY_PROJECT} make apply`로 띄우면 화면에서 볼 수 있다. "
            "`dataset`, 열, 지표 이름은 자기 설정의 것으로 바꿔 쓴다.",
        ]
        for viz, t in types.items():
            if viz not in examples:
                continue
            snippet = yaml.safe_dump([examples[viz]], sort_keys=False, allow_unicode=True, default_flow_style=None, width=110)
            lines += ["", f"**`{viz}`** {t.label}", "", "```yaml", snippet.rstrip("\n"), "```"]
    return "\n".join(lines) + "\n"


# (저장소 기준 경로, 구간 이름, 내용 생성 함수)
SECTIONS: list[tuple[str, str, Callable[[], str]]] = [
    ("contracts/chart-shorthand.md", "chart-types", charts.shorthand_table),
    ("reference/chart-catalog.md", "shorthand-types", catalog_shorthand),
]


def _replace(text: str, name: str, body: str, path: str) -> str:
    begin, end = f"<!-- 생성 시작: {name} -->", f"<!-- 생성 끝: {name} -->"
    try:
        a = text.index(begin) + len(begin)
        b = text.index(end, a)
    except ValueError as exc:
        raise RuntimeError(f"{path}: 생성 구간 표시 '{name}'이 없음") from exc
    return text[:a] + "\n" + body + text[b:]


def render() -> dict[str, tuple[str, str]]:
    """{경로: (현재 내용, 생성 결과)}"""
    out: dict[str, tuple[str, str]] = {}
    root = _repo_root()
    for rel, name, fn in SECTIONS:
        current = out[rel][1] if rel in out else (root / rel).read_text(encoding="utf-8")
        original = out[rel][0] if rel in out else current
        out[rel] = (original, _replace(current, name, fn(), rel))
    return out


def check() -> list[str]:
    return [rel for rel, (old, new) in render().items() if old != new]


def write() -> list[str]:
    changed = []
    for rel, (old, new) in render().items():
        if old != new:
            (_repo_root() / rel).write_text(new, encoding="utf-8")
            changed.append(rel)
    return changed
