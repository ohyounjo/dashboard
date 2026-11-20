"""메일 발송(contracts/auth.md "메일과 테스트 모드"). 발송은 Mailer 인터페이스 하나 뒤에 둔다.

| AUTH_MAIL_MODE | 구현 |
|---|---|
| test(기본) | OutboxMailer: 실제로 보내지 않고 dashkit_auth.outbox에 쌓는다. 운용 화면의 테스트 메일함과 `make outbox`로 본다 |
| smtp | SmtpMailer: 회사 메일 시스템. 이 저장소에는 구현이 없다(MailUnavailable). 회사에서 send를 구현하고 접속 정보를 환경변수로 넣는다 |

본문 문구는 여기(서버)의 TEMPLATES에 둔다. 링크 주소는 WEB_PUBLIC_URL로 만든다(links). 링크의 토큰은 URL 조각(#) 안에 있어
서버 접근 로그와 Referer에 남지 않는다. 이 모듈은 링크(토큰)를 로그에 남기지 않는다.
"""
from __future__ import annotations

import logging
from typing import Literal, Protocol

from . import settings as auth_settings
from . import store

log = logging.getLogger("dashkit.auth")

MailKind = Literal["verify_email", "reset_password", "invite", "already_registered"]
KINDS: tuple[str, ...] = ("verify_email", "reset_password", "invite", "already_registered")

# 제목, 본문({link}는 링크로 바뀐다)
TEMPLATES: dict[str, tuple[str, str]] = {
    "verify_email": ("가입 메일 인증",
                     "대시보드 가입을 마치려면 아래 링크를 여세요. 링크는 24시간 동안 한 번만 쓸 수 있습니다.\n\n{link}\n\n"
                     "직접 가입하지 않았다면 이 메일을 무시하세요."),
    "reset_password": ("비밀번호 재설정",
                       "아래 링크에서 새 비밀번호를 정하세요. 링크는 30분 동안 한 번만 쓸 수 있습니다.\n\n{link}\n\n"
                       "요청하지 않았다면 이 메일을 무시하세요. 비밀번호는 바뀌지 않습니다."),
    "invite": ("대시보드 초대",
               "대시보드 계정이 만들어졌습니다. 아래 링크에서 비밀번호를 정하면 로그인할 수 있습니다. "
               "링크는 72시간 동안 한 번만 쓸 수 있습니다.\n\n{link}"),
    "already_registered": ("이미 가입된 주소",
                           "이 주소로 가입하려는 요청이 있었지만 이미 가입된 주소입니다. "
                           "비밀번호를 잊었다면 로그인 화면의 '비밀번호를 잊으셨나요?'를 쓰세요.\n\n"
                           "직접 요청하지 않았다면 이 메일을 무시하세요."),
}


class MailUnavailable(Exception):
    """메일을 보낼 수 없다(smtp 미구현 등). 메시지에 받는 주소·링크를 넣지 않는다."""


class Mailer(Protocol):
    def send(self, to: str, kind: MailKind, link: str | None) -> None: ...


def render(kind: str, link: str | None) -> tuple[str, str]:
    subject, body = TEMPLATES[kind]
    return subject, body.format(link=link or "")


class OutboxMailer:
    """테스트 모드: outbox 표에 쌓는다. 받는 계정이 없으면 쌓지 않는다."""

    def send(self, to: str, kind: MailKind, link: str | None) -> None:
        if kind not in KINDS:
            raise ValueError(kind)
        store.outbox_add(to, kind, link)


class SmtpMailer:
    """회사 메일 시스템. 이 설계의 구현 범위 밖이다."""

    def send(self, to: str, kind: MailKind, link: str | None) -> None:
        raise MailUnavailable("AUTH_MAIL_MODE=smtp의 발송은 구현되지 않았습니다(dashkit/auth/mailer.py의 SmtpMailer)")


def ensure_available() -> None:
    """운용자 경로(초대 등)는 보내기 전에 확인한다. 보낼 수 없으면 MailUnavailable(계정을 만들기 전에 알린다)."""
    if auth_settings.get().mail_mode != "test":
        raise MailUnavailable("AUTH_MAIL_MODE=smtp의 발송은 구현되지 않았습니다")


def get() -> Mailer:
    return OutboxMailer() if auth_settings.get().mail_mode == "test" else SmtpMailer()


def link(page: str, token: str) -> str:
    """메일 링크. page는 verify.html 또는 reset.html."""
    return f"{auth_settings.get().public_url}/{page}#token={token}"


def send_quietly(to: str, kind: MailKind, mail_link: str | None) -> bool:
    """로그인 전 경로(가입·찾기)용: 실패해도 응답을 바꾸지 않는다(계정 존재를 드러내지 않게). 실패는 로그에 남긴다."""
    try:
        get().send(to, kind, mail_link)
        return True
    except MailUnavailable as exc:
        log.error("메일 %s를 보내지 못함: %s", kind, exc)
    except store.Unavailable:
        log.error("메일 %s를 테스트 메일함에 쌓지 못함(인증 DB)", kind)
    return False
