from __future__ import annotations

import json
import logging

import firebase_admin
from firebase_admin import credentials, messaging
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import PushDeviceTokenRecord, UserConsentRecord

logger = logging.getLogger(__name__)

_FIREBASE_APP_NAME = "gyeongju-hanjeok-push"


def _firebase_app(settings: Settings):
    if not settings.firebase_service_account_json.strip():
        return None

    try:
        return firebase_admin.get_app(_FIREBASE_APP_NAME)
    except ValueError:
        pass

    try:
        service_account = json.loads(
            settings.firebase_service_account_json
        )
        credential = credentials.Certificate(service_account)

        options = {}
        project_id = (
            settings.firebase_project_id.strip()
            or str(service_account.get("project_id") or "").strip()
        )
        if project_id:
            options["projectId"] = project_id

        return firebase_admin.initialize_app(
            credential,
            options=options,
            name=_FIREBASE_APP_NAME,
        )
    except Exception:
        logger.exception("Firebase Admin 초기화 실패")
        return None


def send_push_for_user(
    db: Session,
    *,
    user_id: str,
    title: str,
    message: str,
    data: dict[str, str] | None = None,
) -> None:
    settings = get_settings()

    consent = db.scalar(
        select(UserConsentRecord).where(
            UserConsentRecord.user_id == user_id
        )
    )
    if consent is None or not consent.notification_agreed:
        return

    tokens = list(
        db.scalars(
            select(PushDeviceTokenRecord).where(
                PushDeviceTokenRecord.user_id == user_id,
                PushDeviceTokenRecord.is_active.is_(True),
            )
        ).all()
    )
    if not tokens:
        return

    app = _firebase_app(settings)
    if app is None:
        logger.warning(
            "FCM 전송 건너뜀: FIREBASE_SERVICE_ACCOUNT_JSON 미설정"
        )
        return

    payload = {
        str(key): str(value)
        for key, value in (data or {}).items()
    }
    payload["title"] = title
    payload["message"] = message

    multicast = messaging.MulticastMessage(
        tokens=[row.token for row in tokens],
        notification=messaging.Notification(
            title=title,
            body=message,
        ),
        data=payload,
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                channel_id="gyeongju_hanjeok_updates",
            ),
        ),
    )

    try:
        response = messaging.send_each_for_multicast(
            multicast,
            app=app,
        )
        if response.failure_count:
            logger.warning(
                "FCM 일부 전송 실패: user=%s success=%s failure=%s",
                user_id,
                response.success_count,
                response.failure_count,
            )
    except Exception:
        logger.exception(
            "FCM 전송 실패: user=%s",
            user_id,
        )
