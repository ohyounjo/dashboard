"""python -m dashkit {check|load|sync|api|gen}

PROJECT(필수), PROJECTS_DIR(선택) 환경변수를 쓴다. 오류는 한 줄에 하나씩 출력하고 종료 코드 1로 끝낸다.
"""
from __future__ import annotations

import argparse
import os
import sys

from .config import ConfigError, load_project


def _project():
    name = os.environ.get("PROJECT", "")
    return load_project(name)


def _fail(title: str, errors: list[str]) -> int:
    print(f"✖ {title} ({len(errors)}건)", file=sys.stderr)
    for e in errors:
        print(f"  {e}", file=sys.stderr)
    return 1


def cmd_check(_args) -> int:
    project = _project()
    print(f"✔ check: {project.name} 설정이 유효함")
    return 0


def cmd_load(_args) -> int:
    from .loader import LoadError, load

    project = _project()
    try:
        counts = load(project)
    except LoadError as exc:
        return _fail("load 실패, DB는 이전 상태 그대로", exc.errors)
    total = sum(counts.values())
    detail = ", ".join(f"{t} {n}" for t, n in counts.items())
    print(f"✔ load: {project.name} {total}행 적재 ({detail})")
    return 0


def cmd_sync(_args) -> int:
    from .sync import SyncError, sync

    project = _project()
    try:
        result = sync(project)
    except SyncError as exc:
        return _fail("sync 실패", [str(exc)])
    detail = ", ".join(f"{k} {len(v)}" for k, v in result.items())
    print(f"✔ sync: {project.name} Superset 반영 완료 ({detail})")
    return 0


def cmd_gen(args) -> int:
    from . import generated

    stale = generated.check() if args.check else generated.write()
    if args.check and stale:
        return _fail("생성 구간이 최신이 아님(make gen)", stale)
    print("✔ gen: 생성 구간이 최신임" if args.check else f"✔ gen: {len(stale)}개 파일 갱신")
    return 0


def cmd_api(args) -> int:
    import uvicorn

    uvicorn.run("dashkit.api.app:app", host=args.host, port=args.port, proxy_headers=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dashkit")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="설정 검사(DB 불필요)").set_defaults(fn=cmd_check)
    sub.add_parser("load", help="CSV 검증 후 raw·dash 스키마 재생성").set_defaults(fn=cmd_load)
    sub.add_parser("sync", help="asset 번들 생성·가져오기, 임베드, prune").set_defaults(fn=cmd_sync)
    gen = sub.add_parser("gen", help="레지스트리에서 계약 파일의 생성 구간을 다시 쓴다")
    gen.add_argument("--check", action="store_true", help="쓰지 않고 최신인지만 확인")
    gen.set_defaults(fn=cmd_gen)
    api = sub.add_parser("api", help="백엔드 API 서버")
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=8000)
    api.set_defaults(fn=cmd_api)
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as exc:
        return _fail("설정 오류", exc.errors)


if __name__ == "__main__":
    sys.exit(main())
