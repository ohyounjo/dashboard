"""python -m dashkit {check|plan|load|sync|migrate|api|gen|new|studio|manage|ingest|auth}

`new <이름> --from <CSV 폴더>`는 CSV에서 프로젝트 초안을 만든다(PROJECT를 쓰지 않는다).
`auth {init|create-admin}`은 인증 DB를 다룬다(dashkit/auth/cli.py, PROJECT를 쓰지 않는다).
PROJECT(필수), PROJECTS_DIR(선택) 환경변수를 쓴다. 오류는 한 줄에 하나씩 출력하고 종료 코드 1로 끝낸다.
"""
from __future__ import annotations

import argparse
import os
import sys

from .config import ConfigError, load_project


def _project(*, recover: bool = True):
    """현재 프로젝트의 설정. 읽기 전에 끝나지 않은 구조 변경(묶음 기록 config)을 먼저 정리한다(contracts/ingest.md).
    recover=False(plan)는 아무것도 고치지 않는다: owner 계정을 열지 않고 기록이 있으면 알리기만 한다."""
    from .ingest.structure import recover_before_read

    name = os.environ.get("PROJECT", "")
    if recover:
        recover_before_read(name)
    return load_project(name)


def _fail(title: str, errors: list[str]) -> int:
    print(f"✖ {title} ({len(errors)}건)", file=sys.stderr)
    for e in errors:
        print(f"  {e}", file=sys.stderr)
    return 1


def cmd_check(_args) -> int:
    from .ingest.batches import list_batches

    project = _project()
    # 누적 테이블의 배치 파일 이름 규칙(contracts/ingest.md "배치 폴더"). 어기면 load가 그 테이블을 읽지 않는다
    errors: list[str] = []
    for tname, table in project.schema["tables"].items():
        if table.get("mode") == "append":
            errors += list_batches(project.data_dir, tname)[1]
    if errors:
        return _fail("설정 오류", errors)
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

    from .ingest import pending

    project = _project(recover=False)
    unfinished = [r for _p, r in pending.list_records(project.data_dir) if r and r.get("kind") == "config"]
    if unfinished:
        print(f"! 끝나지 않은 구조 변경이 {len(unfinished)}개 있음: 설정 파일이 바뀌는 도중일 수 있다. "
              "plan은 고치지 않는다(다음 check·load·apply가 정리함)", file=sys.stderr)
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


def cmd_ingest(args) -> int:
    """CSV 파일 하나를 누적 테이블에 더한다(contracts/ingest.md "더하기"). 관리 서비스의 파일 적재와 같은 함수다."""
    import json
    from pathlib import Path

    from .ingest import IngestError, Item, ingest
    from .loader import LOCK_WAIT_MS     # CLI는 load와 같이 10초까지 쓰기 잠금을 기다린다

    name = os.environ.get("PROJECT", "")
    path = Path(args.file)
    if not path.is_file():
        return _fail("ingest 실패", [f"파일이 없음: {args.file}"])
    try:
        result = ingest(name, [Item(args.table, {"kind": "csv", "path": path, "name": path.name})],
                        dry_run=args.dry_run, force=args.force, skip_invalid=args.skip_invalid,
                        wait_ms=LOCK_WAIT_MS)
    except IngestError as exc:
        extra = [json.dumps(exc.extra, ensure_ascii=False, default=str)] if exc.extra and not exc.errors else []
        return _fail(f"ingest 실패({exc.detail}): {exc.message}", (exc.errors or []) + extra)
    b = result["batches"][0]
    rows = b["rows"]
    _audit_cli(name, b, path.name, args.dry_run)
    head = "미리보기(바꾸지 않음)" if args.dry_run else f"{args.table}/{b['file']}"
    print(f"✔ ingest: {head}  +{rows.get('inserted', 0)} ~{rows.get('updated', 0)} "
          f"건너뜀 {rows.get('skipped', 0)} 같음 {rows.get('unchanged', 0)} 오류 {rows.get('invalid', 0)}"
          f" (읽은 행 {rows['total']})")
    for e in b["errors"]:
        print(f"  {e}", file=sys.stderr)
    return 0 if result["ok"] else 1


def _audit_cli(project: str, batch: dict, source_name: str, dry_run: bool) -> None:
    """CLI 적재도 감사 로그에 남긴다(method cli, 사람 없음). 인증 DB에 닿지 못하면 건너뛴다."""
    if dry_run:
        return
    try:
        from .auth import store as auth_store

        auth_store.record_audit("data.batch.create", target_type="table", target_id=batch["table"],
                                target_label=batch["table"],
                                detail={"project": project, "table": batch["table"], "file": batch["file"],
                                        "sha256": batch["sha256"], "rows": batch["rows"], "method": "cli",
                                        "source_name": source_name})
    except Exception:
        pass


def cmd_manage(args) -> int:
    """관리 서비스(contracts/manage.md). 로그인한 운용자만 쓴다. 포트는 밖에 열지 않는다(nginx의 /api/manage/로만)."""
    import uvicorn

    uvicorn.run("dashkit.manage.app:app", host=args.host, port=args.port, proxy_headers=True)
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
    migrate.add_argument("--check", action="store_true", help="파일을 바꾸지 않고 버전 변환이 필요하면 종료 코드 1 (생략된 현재 버전 키는 통과)")
    migrate.set_defaults(fn=cmd_migrate)
    api = sub.add_parser("api", help="백엔드 API 서버")
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=8000)
    api.set_defaults(fn=cmd_api)
    studio = sub.add_parser("studio", help="구성 화면 Studio(로컬 전용, 인증 없음)")
    studio.add_argument("--host", default="0.0.0.0")
    studio.add_argument("--port", type=int, default=8090)
    studio.set_defaults(fn=cmd_studio)
    ing = sub.add_parser("ingest", help="CSV 파일 하나를 누적(append) 테이블에 더한다")
    ing.add_argument("--table", required=True)
    ing.add_argument("--file", required=True, help="CSV 파일(UTF-8 또는 CP949, 쉼표·탭·세미콜론)")
    ing.add_argument("--dry-run", action="store_true", help="미리보기: 반영해 보고 되돌린다")
    ing.add_argument("--force", action="store_true", help="같은 내용의 배치가 있어도 넣는다")
    ing.add_argument("--skip-invalid", action="store_true", help="검증 오류가 난 행을 빼고 넣는다")
    ing.set_defaults(fn=cmd_ingest)
    manage = sub.add_parser("manage", help="관리 서비스(운용자의 데이터 운용 API, /api/manage)")
    manage.add_argument("--host", default="0.0.0.0")
    manage.add_argument("--port", type=int, default=8100)
    manage.set_defaults(fn=cmd_manage)
    from .auth import cli as auth_cli

    auth_cli.register(sub)
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as exc:
        return _fail("설정 오류", exc.errors)


if __name__ == "__main__":
    sys.exit(main())
