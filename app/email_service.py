from __future__ import annotations

import base64
from email.message import EmailMessage

import httpx

from .config import Settings


def _get_gmail_access_token(settings: Settings) -> str:
    client_id = settings.gmail_client_id.strip()
    client_secret = settings.gmail_client_secret.strip()
    refresh_token = settings.gmail_refresh_token.strip()

    if not client_id:
        raise RuntimeError("GMAIL_CLIENT_ID가 설정되지 않았습니다.")
    if not client_secret:
        raise RuntimeError("GMAIL_CLIENT_SECRET이 설정되지 않았습니다.")
    if not refresh_token:
        raise RuntimeError("GMAIL_REFRESH_TOKEN이 설정되지 않았습니다.")

    with httpx.Client(timeout=15.0) as client:
        response = client.post(
            settings.google_oauth_token_url,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )

    if response.status_code != 200:
        detail = response.text.strip()
        if len(detail) > 1000:
            detail = detail[:1000] + "..."
        raise RuntimeError(
            "Google OAuth access token 발급 실패 "
            f"(status={response.status_code}, body={detail})"
        )

    data = response.json()
    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("Google OAuth 응답에 access_token이 없습니다.")

    return access_token


def send_password_reset_code(
    settings: Settings,
    *,
    recipient_email: str,
    code: str,
) -> None:
    from_email = settings.email_from_email.strip()
    from_name = settings.email_from_name.strip() or "경주한적"

    if not from_email:
        raise RuntimeError("EMAIL_FROM_EMAIL이 설정되지 않았습니다.")

    access_token = _get_gmail_access_token(settings)

    message = EmailMessage()
    message["Subject"] = "[경주한적] 비밀번호 변경 인증번호"
    message["From"] = f"{from_name} <{from_email}>"
    message["To"] = recipient_email
    message.set_content(
        "경주한적 비밀번호 변경 인증번호입니다.\n\n"
        f"인증번호: {code}\n\n"
        f"{settings.password_reset_code_minutes}분 안에 "
        "앱의 비밀번호 찾기 화면에 입력해 주세요.\n"
        "본인이 요청하지 않았다면 이 이메일을 무시해 주세요."
    )

    raw = base64.urlsafe_b64encode(
        message.as_bytes()
    ).decode("ascii")

    url = (
        settings.gmail_api_base_url.strip().rstrip("/")
        + "/users/me/messages/send"
    )

    with httpx.Client(timeout=15.0) as client:
        response = client.post(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            json={"raw": raw},
        )

    if response.status_code not in {200, 201}:
        detail = response.text.strip()
        if len(detail) > 1000:
            detail = detail[:1000] + "..."
        raise RuntimeError(
            "Gmail API 메일 전송 실패 "
            f"(status={response.status_code}, body={detail})"
        )
