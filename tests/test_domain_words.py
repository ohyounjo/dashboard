"""범용성: 템플릿 코드(dashkit/, web/, superset/)에 예시 도메인 단어가 없어야 한다.

금지어 = 세 예시 프로젝트(devtracker, sales-sample, chart-gallery)의 테이블명·열 이름·enum 값·데이터셋·대시보드·탭 키와 라벨(설정에서 자동 수집)
       + tests/domain_words.txt의 고정 단어.
일반 단어(id, title 등)는 tests/domain_words.txt의 [allow] 목록으로 뺀다.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from conftest import PROJECTS, ROOT

TEMPLATE_DIRS = ["dashkit", "web", "superset"]
SKIP_PARTS = {"vendor", "__pycache__"}
WORDS_FILE = Path(__file__).parent / "domain_words.txt"


def _word_lists() -> tuple[set[str], set[str]]:
    fixed, allow, section = set(), set(), "deny"
    for line in WORDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line in ("[deny]", "[allow]"):
            section = line[1:-1]
            continue
        (fixed if section == "deny" else allow).add(line.lower())
    return fixed, allow


def collect_domain_words() -> set[str]:
    words: set[str] = set()
    for proj in ("devtracker", "sales-sample", "chart-gallery"):
        schema = yaml.safe_load((PROJECTS / proj / "schema.yaml").read_text(encoding="utf-8"))
        dash = yaml.safe_load((PROJECTS / proj / "dashboard.yaml").read_text(encoding="utf-8"))
        for t, spec in schema["tables"].items():
            words.add(t)
            for c, col in spec["columns"].items():
                words.add(c)
                words.update(str(v) for v in col.get("values", []))
        words.update(dash["datasets"])
        words.update(dash["dashboards"])
        for tab in dash["tabs"]:
            words.add(tab["key"])
            words.add(tab["label"])
    return {w.lower() for w in words}


def template_files():
    for d in TEMPLATE_DIRS:
        for f in (ROOT / d).rglob("*"):
            if f.is_file() and not (SKIP_PARTS & set(f.parts)) and f.suffix not in (".pyc",):
                yield f


def test_word_list_is_meaningful():
    words = collect_domain_words()
    fixed, _ = _word_lists()
    assert {"milestone", "issue", "severity", "마일스톤"} <= fixed
    assert len(words) > 50


def test_no_domain_words_in_template_code():
    fixed, allow = _word_lists()
    words = sorted((collect_domain_words() | fixed) - allow)
    patterns = [(w, re.compile(r"(?<![a-z0-9_])" + re.escape(w) + r"(?![a-z0-9_])", re.I)) for w in words]
    hits = []
    for f in template_files():
        text = f.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            for w, pat in patterns:
                if pat.search(line):
                    hits.append(f"{f.relative_to(ROOT)}:{i}: '{w}'")
    assert hits == [], "템플릿 코드에 도메인 단어:\n" + "\n".join(hits)
