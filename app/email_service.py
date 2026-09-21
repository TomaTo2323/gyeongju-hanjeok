from __future__ import annotations

import smtplib
from email.message import EmailMessage

from .config import Settings


def send_password_reset_code(
    settings: Settings,
    *,
    recipient_email: str,
    code: str,
) -> None:
    username = settings.smtp_username.strip()
    password = settings.smtp_password.strip()
    from_email = (
        settings.smtp_from_email.strip()
        or username
    )

    if (
        not settings.smtp_host.strip()
        or not username
        or not password
        or not from_email
    ):
        raise RuntimeError(
            "SMTP 환경변수가 설정되지 않았습니다."
        )

    message = EmailMessage()
    message["Subject"] = "[경주한적] 비밀번호 변경 인증번호"
    message["From"] = (
        f"{settings.smtp_from_name} <{from_email}>"
    )
    message["To"] = recipient_email
    message.set_content(
        "경주한적 비밀번호 변경 인증번호입니다.\n\n"
        f"인증번호: {code}\n\n"
        f"{settings.password_reset_code_minutes}분 안에 "
        "앱의 비밀번호 찾기 화면에 입력해 주세요.\n"
        "본인이 요청하지 않았다면 이 이메일을 무시해 주세요."
    )

    with smtplib.SMTP(
        settings.smtp_host,
        settings.smtp_port,
        timeout=15,
    ) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(username, password)
        smtp.send_message(message)
