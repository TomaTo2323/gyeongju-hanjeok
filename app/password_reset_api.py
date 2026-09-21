from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .auth_service import (
    hash_password,
    normalize_email,
    validate_email,
    validate_password,
)
from .config import Settings, get_settings
from .db import PasswordResetCodeRecord, UserRecord, get_db
from .email_service import send_password_reset_code

password_reset_router = APIRouter(
    prefix="/auth/password-reset",
    tags=["auth"],
)


class PasswordResetRequest(BaseModel):
    email: str = Field(min_length=5, max_length=255)

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        try:
            return validate_email(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class PasswordResetVerifyRequest(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    code: str = Field(min_length=6, max_length=6)

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        try:
            return validate_email(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("code")
    @classmethod
    def clean_code(cls, value: str) -> str:
        code = value.strip()
        if len(code) != 6 or not code.isdigit():
            raise ValueError("6자리 인증번호를 입력해 주세요.")
        return code


class PasswordResetConfirmRequest(BaseModel):
    reset_token: str = Field(min_length=20, max_length=512)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def clean_password(cls, value: str) -> str:
        try:
            return validate_password(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class PasswordResetMessageResponse(BaseModel):
    message: str


class PasswordResetVerifyResponse(BaseModel):
    reset_token: str


def _digest(
    value: str,
    settings: Settings,
) -> str:
    return hmac.new(
        settings.auth_secret_key.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@password_reset_router.post(
    "/request",
    response_model=PasswordResetMessageResponse,
)
def request_password_reset(
    body: PasswordResetRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    email = normalize_email(body.email)
    generic = PasswordResetMessageResponse(
        message=(
            "가입된 이메일이라면 인증번호를 전송했어요. "
            "메일함과 스팸함을 확인해 주세요."
        )
    )

    user = db.scalar(
        select(UserRecord).where(
            UserRecord.email == email
        )
    )
    if user is None or not user.is_active:
        return generic

    now = datetime.now(timezone.utc)

    latest = db.scalar(
        select(PasswordResetCodeRecord)
        .where(
            PasswordResetCodeRecord.user_id == user.user_id,
            PasswordResetCodeRecord.used_at.is_(None),
        )
        .order_by(
            PasswordResetCodeRecord.created_at.desc()
        )
        .limit(1)
    )

    if (
        latest is not None
        and latest.created_at
        > now - timedelta(
            seconds=settings.password_reset_resend_seconds
        )
    ):
        return generic

    db.execute(
        update(PasswordResetCodeRecord)
        .where(
            PasswordResetCodeRecord.user_id == user.user_id,
            PasswordResetCodeRecord.used_at.is_(None),
        )
        .values(used_at=now)
    )

    code = f"{secrets.randbelow(1_000_000):06d}"

    row = PasswordResetCodeRecord(
        user_id=user.user_id,
        email=email,
        code_hash=_digest(
            f"{email}:{code}",
            settings,
        ),
        attempts=0,
        expires_at=now + timedelta(
            minutes=settings.password_reset_code_minutes
        ),
        created_at=now,
    )
    db.add(row)
    db.flush()

    try:
        send_password_reset_code(
            settings,
            recipient_email=email,
            code=code,
        )
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="인증 이메일을 보내지 못했어요. 잠시 후 다시 시도해 주세요.",
        ) from exc

    db.commit()
    return generic


@password_reset_router.post(
    "/verify",
    response_model=PasswordResetVerifyResponse,
)
def verify_password_reset(
    body: PasswordResetVerifyRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    email = normalize_email(body.email)
    now = datetime.now(timezone.utc)

    row = db.scalar(
        select(PasswordResetCodeRecord)
        .where(
            PasswordResetCodeRecord.email == email,
            PasswordResetCodeRecord.used_at.is_(None),
        )
        .order_by(
            PasswordResetCodeRecord.created_at.desc()
        )
        .limit(1)
    )

    invalid = (
        row is None
        or row.expires_at <= now
        or row.attempts >= settings.password_reset_max_attempts
    )

    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="인증번호가 올바르지 않거나 만료되었습니다.",
        )

    expected = _digest(
        f"{email}:{body.code}",
        settings,
    )

    if not hmac.compare_digest(
        row.code_hash,
        expected,
    ):
        row.attempts += 1
        db.add(row)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="인증번호가 올바르지 않거나 만료되었습니다.",
        )

    reset_token = secrets.token_urlsafe(48)
    row.verified_at = now
    row.reset_token_hash = _digest(
        reset_token,
        settings,
    )
    row.reset_token_expires_at = now + timedelta(
        minutes=settings.password_reset_token_minutes
    )
    db.add(row)
    db.commit()

    return PasswordResetVerifyResponse(
        reset_token=reset_token
    )


@password_reset_router.post(
    "/confirm",
    response_model=PasswordResetMessageResponse,
)
def confirm_password_reset(
    body: PasswordResetConfirmRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    now = datetime.now(timezone.utc)
    token_hash = _digest(
        body.reset_token,
        settings,
    )

    row = db.scalar(
        select(PasswordResetCodeRecord).where(
            PasswordResetCodeRecord.reset_token_hash == token_hash,
            PasswordResetCodeRecord.verified_at.is_not(None),
            PasswordResetCodeRecord.used_at.is_(None),
        )
    )

    if (
        row is None
        or row.reset_token_expires_at is None
        or row.reset_token_expires_at <= now
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="비밀번호 변경 인증이 만료되었습니다. 다시 인증해 주세요.",
        )

    user = db.get(
        UserRecord,
        row.user_id,
    )
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="비밀번호를 변경할 수 없는 계정입니다.",
        )

    user.password_hash = hash_password(
        body.new_password
    )
    user.updated_at = now
    row.used_at = now

    db.add(user)
    db.add(row)
    db.commit()

    return PasswordResetMessageResponse(
        message="비밀번호가 변경되었습니다. 새 비밀번호로 로그인해 주세요."
    )
