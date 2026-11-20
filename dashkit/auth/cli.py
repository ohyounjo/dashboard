"""python -m dashkit auth {init|create-admin|outbox} (contracts/auth.md "CLI").

tools 컨테이너에서 dash_auth 계정으로 돈다.

- init: 인증 스키마가 이 코드의 최신 버전인지 확인한다. 스키마·계정을 만드는 일은 `make auth-init`이 data-db 컨테이너
  안에서(superuser) data-db/init/02-auth.sh로 먼저 하고, 이 명령은 그 결과를 dash_auth로 접속해 확인한다.
- create-admin: 첫 admin을 만든다. 비밀번호는 프롬프트로 두 번 받는다(--password-stdin이면 표준 입력의 첫 줄).
  코드와 .env에 넣지 않는다. 같은 이메일의 계정이 이미 있으면 아무것도 바꾸지 않고 실패한다.
- outbox: 테스트 메일함의 최근 메일과 링크를 출력한다. 등급 제한 없이 전부 나온다(서버에 들어갈 수 있는 사람만 쓴다).
  admin 자신이 비밀번호를 잊었을 때, operator 계정의 링크가 필요한데 admin이 없을 때 쓴다.
"""
from __future__ import annotations

import getpass
import re
import sys
from pathlib import Path

from . import emails, passwords, store
from . import settings as auth_settings

SQL_DIR = Path(__file__).resolve().parent / "sql"

POLICY_MESSAGES = {
    "too_short": f"비밀번호는 {passwords.MIN_LENGTH}자 이상이어야 합니다",
    "too_long": f"비밀번호는 {passwords.MAX_LENGTH}자 이하여야 합니다",
    "same_as_email": "비밀번호가 이메일의 @ 앞부분과 같으면 안 됩니다",
    "too_common": "흔히 쓰이는 비밀번호입니다. 다른 비밀번호를 쓰세요",
}


def latest_version() -> int:
    versions = [int(m.group(1)) for f in SQL_DIR.glob("*.sql") if (m := re.match(r"(\d{3})_", f.name))]
    return max(versions, default=0)


def _fail(message: str) -> int:
    print(f"✖ {message}", file=sys.stderr)
    return 1


def cmd_init(_args) -> int:
    want = latest_version()
    try:
        have = store.schema_version()
    except store.Unavailable as exc:
        return _fail(f"auth init: {exc}. data-db의 02-auth.sh 결과와 .env의 DATA_DB_AUTH_PASSWORD를 확인하세요")
    if have != want:
        return _fail(f"auth init: 인증 스키마 버전 {have}, 코드는 {want}. make auth-init을 다시 실행하세요")
    print(f"✔ auth init: dashkit_auth 스키마 버전 {have}, dash_auth 계정으로 접속 확인")
    return 0


def _read_password(from_stdin: bool) -> str | None:
    if from_stdin:
        line = sys.stdin.readline()
        return line.rstrip("\r\n") if line else None
    first = getpass.getpass("비밀번호: ")
    second = getpass.getpass("비밀번호 확인: ")
    if first != second:
        print("✖ 두 비밀번호가 다릅니다", file=sys.stderr)
        return None
    return first


def cmd_create_admin(args) -> int:
    try:
        cfg = auth_settings.load()
    except auth_settings.AuthConfigError as exc:
        return _fail(f"create-admin: {exc}")
    email = emails.normalize(args.email or "")
    if not emails.valid(email):
        return _fail("create-admin: 이메일 형식이 올바르지 않습니다. 사용법: make admin EMAIL=<주소>")
    if not cfg.domain_allowed(email):
        return _fail(f"create-admin: 허용하지 않는 도메인입니다(AUTH_EMAIL_DOMAINS: {', '.join(cfg.email_domains)})")
    try:
        if store.user_by_email(email) is not None:
            return _fail("create-admin: 같은 이메일의 계정이 이미 있습니다. 아무것도 바꾸지 않았습니다")
    except store.Unavailable as exc:
        return _fail(f"create-admin: {exc}")
    password = _read_password(args.password_stdin)
    if password is None:
        return _fail("create-admin: 비밀번호를 받지 못했습니다")
    reason = passwords.policy_violation(password, email)
    if reason:
        return _fail(f"create-admin: {POLICY_MESSAGES.get(reason, reason)}")
    try:
        store.create_user(email, role="admin", status="active", source="invite",
                          password_hash=passwords.hash_password(password), email_verified=True)
    except store.Conflict:
        return _fail("create-admin: 같은 이메일의 계정이 이미 있습니다")
    except store.Unavailable as exc:
        return _fail(f"create-admin: {exc}")
    print(f"✔ create-admin: admin 계정 {email}을 만들었습니다. 웹앱에서 로그인하세요")
    return 0


def cmd_outbox(args) -> int:
    try:
        rows = store.outbox_list(None, limit=args.limit)
    except store.Unavailable as exc:
        return _fail(f"outbox: {exc}")
    if not rows:
        print("테스트 메일함이 비어 있습니다(AUTH_MAIL_MODE=test에서 보낸 메일만 쌓인다, 7일 보관)")
        return 0
    for r in rows:
        when = r["created_at"].astimezone().strftime("%Y-%m-%d %H:%M")
        print(f"{when}  {r['kind']:<18} {r['to_email']} ({r['role']})")
        if r["link"]:
            print(f"    {r['link']}")
    return 0


def register(sub) -> None:
    auth = sub.add_parser("auth", help="로그인·사용자 관리(인증 DB)")
    asub = auth.add_subparsers(dest="auth_cmd", required=True)
    asub.add_parser("init", help="인증 스키마 버전 확인(make auth-init이 부른다)").set_defaults(fn=cmd_init)
    ca = asub.add_parser("create-admin", help="첫 admin 계정 만들기(비밀번호는 프롬프트)")
    ca.add_argument("--email", required=True)
    ca.add_argument("--password-stdin", action="store_true", help="표준 입력의 첫 줄을 비밀번호로 읽는다(자동화용)")
    ca.set_defaults(fn=cmd_create_admin)
    ob = asub.add_parser("outbox", help="테스트 메일함의 최근 메일과 링크(등급 제한 없음)")
    ob.add_argument("--limit", type=int, default=20)
    ob.set_defaults(fn=cmd_outbox)


def run(argv: list[str]) -> int:
    """`python -m dashkit auth <argv>`와 같다(테스트용 진입점)."""
    import argparse

    parser = argparse.ArgumentParser(prog="dashkit")
    register(parser.add_subparsers(dest="cmd", required=True))
    args = parser.parse_args(["auth", *argv])
    return args.fn(args)
