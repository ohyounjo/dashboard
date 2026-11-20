"""python -m dashkit {check|plan|load|sync|migrate|api|gen|new|studio|auth}

`new <이름> --from <CSV 폴더>`는 CSV에서 프로젝트 초안을 만든다(PROJECT를 쓰지 않는다).
`auth {init|create-admin}`은 인증 DB를 다룬다(dashkit/auth/cli.py, PROJECT를 쓰지 않는다).
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


def cmd_new(args) -> int:
    """CSV 폴더에서 프로젝트 초안을 만든다(PROJECT 환경변수는 쓰지 않는다)."""
    import json

    from . import scaffold

    try:
        if args.dry_run:
            scaffold.check_name(args.name)
            print(json.dumps(scaffold.infer_folder(args.source), ensure_ascii=False, indent=2))
            return 0
        root, inference = scaffold.create_project(args.name, scaffold.read_folder(args.source))
    except scaffold.ScaffoldError as exc:
        return _fail("new 실패, 만든 파일 없음", exc.errors)
    n = scaffold.overview(inference)
    print(f"✔ new: {root}에 초안을 만듦 (테이블 {n['tables']}, 열 {n['columns']}(이름 바꿈 {n['renamed']}), "
          f"enum {n['enums']}, ref {n['refs']}, 차트 {n['charts']}, 탭 {n['tabs']}, 위젯 섹션 {n['widgets']})")
    print(f"  다음: schema.yaml과 dashboard.yaml에서 \"{scaffold.MARK}\"를 찾아 확인한 뒤 "
          f"PROJECT={args.name} make check, PROJECT={args.name} make apply")
    return 0


def cmd_plan(args) -> int:
    """적용 전에 차이를 보여 준다. DB·Superset·상태 파일을 바꾸지 않는다."""
    import json

    from . import plan

    project = _project()
    try:
        result = plan.make_plan(project)
    except plan.PlanError as exc:
        return _fail(f"plan: {exc.title}", exc.errors)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else plan.format_plan(result))
    return 0


def cmd_migrate(args) -> int:
    """설정 파일을 최신 형식 버전으로 고친다(--check는 확인만)."""
    import re

    from . import migrate
    from .config import default_projects_dir

    name = os.environ.get("PROJECT", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        raise ConfigError([f"PROJECT:(최상위): 프로젝트 이름 형식 오류 '{name}' (소문자, 숫자, _, -)"])
    results = migrate.migrate_project(default_projects_dir() / name, check=args.check)
    pending = [r for r in results if r.changed]
    for r in pending:
        print(f"  {r.file}: {'; '.join(r.steps)}")
    if not pending:
        print(f"✔ migrate: {name} 설정이 이미 최신 형식임(버전 {migrate.CONFIG_VERSION})")
        return 0
    if args.check:
        print(f"✖ migrate --check: {name}에 고칠 파일 {len(pending)}개(파일은 바꾸지 않음). 고치기: make migrate",
              file=sys.stderr)
        return 1
    print(f"✔ migrate: {name} 파일 {len(pending)}개를 버전 {migrate.CONFIG_VERSION} 형식으로 고침")
    return 0


def cmd_api(args) -> int:
    import uvicorn

    uvicorn.run("dashkit.api.app:app", host=args.host, port=args.port, proxy_headers=True)
    return 0


def cmd_studio(args) -> int:
    """구성 화면 Studio(contracts/studio.md). 인증이 없다. 외부에 열지 않는다(compose가 127.0.0.1에만 연다)."""
    import uvicorn

    os.environ.setdefault("STUDIO_LISTEN_PORT", str(args.port))
    uvicorn.run("dashkit.studio.app:app", host=args.host, port=args.port)
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
    new = sub.add_parser("new", help="CSV 폴더에서 프로젝트 초안을 만든다(DB 불필요)")
    new.add_argument("name", help="프로젝트 이름(소문자, 숫자, _, -)")
    new.add_argument("--from", dest="source", required=True, metavar="CSV_폴더", help="*.csv가 있는 폴더")
    new.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 추론 결과(JSON)만 출력")
    new.set_defaults(fn=cmd_new)
    plan = sub.add_parser("plan", help="적용 전에 차이 보기(아무것도 바꾸지 않음)")
    plan.add_argument("--json", action="store_true", help="구조화된 결과를 JSON으로 출력")
    plan.set_defaults(fn=cmd_plan)
    migrate = sub.add_parser("migrate", help="설정 파일을 최신 형식 버전으로 고친다")
    migrate.add_argument("--check", action="store_true", help="파일을 바꾸지 않고, 고칠 것이 있으면 종료 코드 1")
    migrate.set_defaults(fn=cmd_migrate)
    api = sub.add_parser("api", help="백엔드 API 서버")
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=8000)
    api.set_defaults(fn=cmd_api)
    studio = sub.add_parser("studio", help="구성 화면 Studio(로컬 전용, 인증 없음)")
    studio.add_argument("--host", default="0.0.0.0")
    studio.add_argument("--port", type=int, default=8090)
    studio.set_defaults(fn=cmd_studio)
    from .auth import cli as auth_cli

    auth_cli.register(sub)
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as exc:
        return _fail("설정 오류", exc.errors)


if __name__ == "__main__":
    sys.exit(main())
