"""guest token 사용자에게 줄 역할(GUEST_ROLE_NAME)을 만들고 권한을 정확히 맞춘다(멱등).

`superset init` 다음에 실행해야 한다(권한-뷰 행이 그때 생긴다). contracts/superset-notes.md 5절.
"""
import sys

from superset.app import create_app

ROLE_NAME = "EmbedGuest"
REQUIRED = [
    ("can_read", "CurrentUserRestApi"),
    ("can_read", "Dashboard"),
    ("can_read", "Chart"),
]


def main() -> int:
    app = create_app()
    with app.app_context():
        from superset import security_manager
        from superset.extensions import db

        if app.config.get("GUEST_ROLE_NAME") != ROLE_NAME:
            print(f"✖ GUEST_ROLE_NAME이 {ROLE_NAME}가 아님: {app.config.get('GUEST_ROLE_NAME')!r}", file=sys.stderr)
            return 1

        pvms, missing = [], []
        for perm, view in REQUIRED:
            pvm = security_manager.find_permission_view_menu(perm, view)
            if pvm is None:
                missing.append(f"{perm} on {view}")
            else:
                pvms.append(pvm)
        if missing:
            print("✖ 권한을 찾지 못함(superset init을 먼저 실행했는지 확인): " + ", ".join(missing), file=sys.stderr)
            return 1

        role = security_manager.find_role(ROLE_NAME) or security_manager.add_role(ROLE_NAME)
        role.permissions = pvms
        db.session.commit()

        role = security_manager.find_role(ROLE_NAME)
        got = sorted(f"{p.permission.name} on {p.view_menu.name}" for p in role.permissions)
        want = sorted(f"{p} on {v}" for p, v in REQUIRED)
        if got != want:
            print(f"✖ {ROLE_NAME} 권한 불일치: {got}", file=sys.stderr)
            return 1
        print(f"✔ {ROLE_NAME} 권한 {len(got)}개: " + ", ".join(got))
        return 0


if __name__ == "__main__":
    sys.exit(main())
