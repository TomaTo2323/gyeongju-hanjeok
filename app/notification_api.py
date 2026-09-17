from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .auth_service import get_current_user
from .db import NotificationRecord, UserPublicProfileRecord, UserRecord, get_db

notification_router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationActorResponse(BaseModel):
    user_id: str
    member_code: str | None = None
    nickname: str


class NotificationResponse(BaseModel):
    notification_id: str
    type: str
    title: str
    message: str
    is_read: bool
    created_at: datetime
    actor: NotificationActorResponse | None = None
    post_id: str | None = None
    friendship_id: str | None = None
    shared_route_id: str | None = None
    route_request_id: str | None = None


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    unread_count: int


class UnreadCountResponse(BaseModel):
    unread_count: int


def _actor(db: Session, actor_user_id: str | None) -> NotificationActorResponse | None:
    if not actor_user_id:
        return None
    user = db.get(UserRecord, actor_user_id)
    if user is None:
        return None
    profile = db.get(UserPublicProfileRecord, actor_user_id)
    return NotificationActorResponse(
        user_id=user.user_id,
        member_code=profile.member_code if profile is not None else None,
        nickname=user.nickname,
    )


def _response(db: Session, row: NotificationRecord) -> NotificationResponse:
    return NotificationResponse(
        notification_id=row.notification_id,
        type=row.type,
        title=row.title,
        message=row.message,
        is_read=row.is_read,
        created_at=row.created_at,
        actor=_actor(db, row.actor_user_id),
        post_id=row.post_id,
        friendship_id=row.friendship_id,
        shared_route_id=row.shared_route_id,
        route_request_id=row.route_request_id,
    )


@notification_router.get("", response_model=NotificationListResponse)
def list_notifications(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: UserRecord = Depends(get_current_user),
):
    uid = current_user.user_id
    stmt = select(NotificationRecord).where(NotificationRecord.user_id == uid)
    if unread_only:
        stmt = stmt.where(NotificationRecord.is_read.is_(False))
    stmt = stmt.order_by(NotificationRecord.created_at.desc()).limit(limit)
    rows = list(db.scalars(stmt).all())

    unread_count = int(
        db.scalar(
            select(func.count())
            .select_from(NotificationRecord)
            .where(
                NotificationRecord.user_id == uid,
                NotificationRecord.is_read.is_(False),
            )
        ) or 0
    )
    return NotificationListResponse(
        items=[_response(db, row) for row in rows],
        unread_count=unread_count,
    )


@notification_router.get("/unread-count", response_model=UnreadCountResponse)
def unread_count(
    db: Session = Depends(get_db),
    current_user: UserRecord = Depends(get_current_user),
):
    uid = current_user.user_id
    count = int(
        db.scalar(
            select(func.count())
            .select_from(NotificationRecord)
            .where(
                NotificationRecord.user_id == uid,
                NotificationRecord.is_read.is_(False),
            )
        ) or 0
    )
    return UnreadCountResponse(unread_count=count)


@notification_router.patch("/{notification_id}/read", response_model=NotificationResponse)
def mark_read(
    notification_id: str,
    db: Session = Depends(get_db),
    current_user: UserRecord = Depends(get_current_user),
):
    uid = current_user.user_id
    row = db.get(NotificationRecord, notification_id)
    if row is None or row.user_id != uid:
        raise HTTPException(status_code=404, detail="알림을 찾을 수 없습니다.")

    if not row.is_read:
        row.is_read = True
        row.read_at = datetime.now(timezone.utc)
        db.add(row)
        db.commit()
        db.refresh(row)

    return _response(db, row)


@notification_router.patch("/read-all", response_model=UnreadCountResponse)
def mark_all_read(
    db: Session = Depends(get_db),
    current_user: UserRecord = Depends(get_current_user),
):
    uid = current_user.user_id
    db.execute(
        update(NotificationRecord)
        .where(
            NotificationRecord.user_id == uid,
            NotificationRecord.is_read.is_(False),
        )
        .values(is_read=True, read_at=datetime.now(timezone.utc))
    )
    db.commit()
    return UnreadCountResponse(unread_count=0)
