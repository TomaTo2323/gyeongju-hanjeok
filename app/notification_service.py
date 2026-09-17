from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from .db import NotificationRecord


def create_notification(
    db: Session,
    *,
    user_id: str,
    actor_user_id: str | None,
    type: str,
    title: str,
    message: str,
    post_id: str | None = None,
    friendship_id: str | None = None,
    shared_route_id: str | None = None,
    route_request_id: str | None = None,
) -> NotificationRecord | None:
    """Create a notification inside the caller's existing transaction."""
    if actor_user_id and actor_user_id == user_id:
        return None

    row = NotificationRecord(
        notification_id=str(uuid4()),
        user_id=user_id,
        actor_user_id=actor_user_id,
        type=type,
        title=title,
        message=message,
        post_id=post_id,
        friendship_id=friendship_id,
        shared_route_id=shared_route_id,
        route_request_id=route_request_id,
        is_read=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)
    return row
