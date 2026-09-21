from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth_service import get_current_user
from .db import (
    PushDeviceTokenRecord,
    UserConsentRecord,
    UserRecord,
    get_db,
)

push_router = APIRouter(
    prefix="/notifications/device-tokens",
    tags=["notifications"],
)


class PushTokenRequest(BaseModel):
    token: str = Field(min_length=20, max_length=512)
    platform: str = Field(default="android", max_length=20)


class PushTokenUnregisterRequest(BaseModel):
    token: str = Field(min_length=20, max_length=512)


class PushTokenResponse(BaseModel):
    registered: bool


def _notification_consent(
    db: Session,
    user_id: str,
) -> UserConsentRecord | None:
    return db.scalar(
        select(UserConsentRecord).where(
            UserConsentRecord.user_id == user_id
        )
    )


@push_router.post("", response_model=PushTokenResponse)
def register_push_token(
    body: PushTokenRequest,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    consent = _notification_consent(
        db,
        current_user.user_id,
    )

    if consent is None or not consent.notification_agreed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="새 알림 받기에 동의한 사용자만 기기 알림을 등록할 수 있습니다.",
        )

    token = body.token.strip()
    existing = db.scalar(
        select(PushDeviceTokenRecord).where(
            PushDeviceTokenRecord.token == token
        )
    )

    now = datetime.now(timezone.utc)

    if existing is None:
        existing = PushDeviceTokenRecord(
            user_id=current_user.user_id,
            token=token,
            platform=body.platform.strip().lower() or "android",
            is_active=True,
            created_at=now,
            updated_at=now,
        )
    else:
        existing.user_id = current_user.user_id
        existing.platform = body.platform.strip().lower() or "android"
        existing.is_active = True
        existing.updated_at = now

    db.add(existing)
    db.commit()
    return PushTokenResponse(registered=True)


@push_router.post(
    "/unregister",
    response_model=PushTokenResponse,
)
def unregister_push_token(
    body: PushTokenUnregisterRequest,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.scalar(
        select(PushDeviceTokenRecord).where(
            PushDeviceTokenRecord.token == body.token.strip(),
            PushDeviceTokenRecord.user_id == current_user.user_id,
        )
    )

    if row is not None:
        row.is_active = False
        row.updated_at = datetime.now(timezone.utc)
        db.add(row)
        db.commit()

    return PushTokenResponse(registered=False)
