"""번들 스냅샷(tests/golden/<프로젝트>/). DB와 Superset 없이 번들을 만들어 비교·갱신한다.

    python -m tests.golden            스냅샷 갱신(make golden)
    python -m tests.golden --columns  현재 PROJECT의 뷰 열 정보를 DB에서 읽어 view_columns.json 갱신
                                      (스택 필요. sql: 데이터셋의 열이 바뀌었을 때만 쓴다)

view_columns.json: {데이터셋: [[열, Postgres 타입], …]} (뷰의 ordinal 순서).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "golden"
PROJECTS = ROOT / "projects"
GOLDEN_PROJECTS = ("devtracker", "sales-sample", "chart-gallery")
TIMESTAMP = datetime(2000, 1, 1, tzinfo=timezone.utc)
ENV: dict[str, str] = {}   # DATA_DB_* 기본값만 쓴다(실행 환경과 무관하게 같은 출력)


def columns_path(name: str) -> Path:
    return GOLDEN / name / "view_columns.json"


def read_columns(name: str) -> dict[str, list[tuple[str, str]]]:
    raw = json.loads(columns_path(name).read_text(encoding="utf-8"))
    return {ds: [(c, t) for c, t in cols] for ds, cols in raw.items()}


def build(name: str) -> dict[str, str]:
    from dashkit.config import load_project
    from dashkit.sync.bundle import build_bundle

    project = load_project(name, PROJECTS)
    return build_bundle(project, read_columns(name), env=ENV, timestamp=TIMESTAMP)


def read_golden(name: str) -> dict[str, str]:
    base = GOLDEN / name / "bundle"
    return {p.relative_to(base).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(base.rglob("*")) if p.is_file()}


def update(name: str) -> None:
    base = GOLDEN / name / "bundle"
    if base.exists():
        shutil.rmtree(base)
    for rel, text in build(name).items():
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    print(f"✔ golden: {name} 갱신")


def update_columns(name: str) -> None:
    from dashkit.config import load_project
    from dashkit.sync import read_view_columns

    cols = read_view_columns(load_project(name, PROJECTS))
    path = columns_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 데이터셋마다 한 줄씩 써서 diff가 읽히게 한다
    lines = [f" {json.dumps(k, ensure_ascii=False)}: {json.dumps([list(c) for c in v], ensure_ascii=False)}"
             for k, v in cols.items()]
    path.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")
    print(f"✔ golden: {name} 뷰 열 정보 갱신 ({path.relative_to(ROOT)})")


def main(argv: list[str]) -> int:
    if "--columns" in argv:
        update_columns(os.environ["PROJECT"])
        return 0
    for name in GOLDEN_PROJECTS:
        update(name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
