from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import (
    NotificationRecord,
    RouteCompanionRequestRecord,
    SharedRouteMemberRecord,
    SharedRouteRecord,
    UserPublicProfileRecord,
    UserRecord,
    get_db,
)
from app.auth_service import get_current_user
from app.notification_service import create_notification

companion_router = APIRouter(tags=["shared-route-companion"])


class CompanionRequestCreate(BaseModel):
    member_code: str


class CompanionRequestResponse(BaseModel):
    request_id: str
    shared_route_id: str
    requester_user_id: str
    recipient_user_id: str
    requester_nickname: str
    status: str
    created_at: datetime


class OutgoingCompanionRequestResponse(BaseModel):
    request_id: str
    shared_route_id: str
    recipient_user_id: str
    recipient_member_code: str
    recipient_nickname: str
    status: str
    created_at: datetime


def _route_owner_id(route: SharedRouteRecord) -> str:
    return route.owner_user_id


def _response(db: Session, row: RouteCompanionRequestRecord) -> CompanionRequestResponse:
    requester = db.get(UserRecord, row.requester_user_id)
    return CompanionRequestResponse(
        request_id=row.request_id,
        shared_route_id=row.shared_route_id,
        requester_user_id=row.requester_user_id,
        recipient_user_id=row.recipient_user_id,
        requester_nickname=getattr(requester, "nickname", "경주한적 사용자"),
        status=row.status,
        created_at=row.created_at,
    )


def _restore_or_create_companion_notification(
    db: Session,
    *,
    row: RouteCompanionRequestRecord,
    requester: UserRecord,
    recipient: UserRecord,
) -> None:
    """
    이미 pending 동행 요청이 DB에 남아 있는데 알림이 누락된 상태를 복구합니다.
    기존 알림이 읽음 상태라면 다시 미읽음으로 돌려 재요청 사실을 확인할 수 있게 합니다.
    """
    notification = db.scalar(
        select(NotificationRecord)
        .where(
            NotificationRecord.user_id == recipient.user_id,
            NotificationRecord.type == "shared_route_invite",
            NotificationRecord.route_request_id == row.request_id,
        )
        .order_by(NotificationRecord.created_at.desc())
    )

    if notification is None:
        create_notification(
            db,
            user_id=recipient.user_id,
            actor_user_id=requester.user_id,
            type="shared_route_invite",
            title="새 동행 코스 요청",
            message=f"{requester.nickname}님이 함께 여행할 코스에 초대했어요.",
            shared_route_id=row.shared_route_id,
            route_request_id=row.request_id,
        )
        return

    if notification.is_read:
        notification.is_read = False
        notification.read_at = None
        notification.created_at = datetime.now(timezone.utc)
        db.add(notification)


@companion_router.post(
    "/shared-routes/{shared_route_id}/companion-requests",
    response_model=CompanionRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
def request_companion(
    shared_route_id: str,
    body: CompanionRequestCreate,
    db: Session = Depends(get_db),
    current_user: UserRecord = Depends(get_current_user),
):
    route = db.get(SharedRouteRecord, shared_route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="공유 코스를 찾을 수 없습니다.")
    if _route_owner_id(route) != current_user.user_id:
        raise HTTPException(status_code=403, detail="코스 소유자만 동행을 요청할 수 있습니다.")

    member_code = body.member_code.strip().upper()
    profile = db.scalar(
        select(UserPublicProfileRecord).where(
            UserPublicProfileRecord.member_code == member_code
        )
    )
    recipient = db.get(UserRecord, profile.user_id) if profile is not None else None
    if recipient is None:
        raise HTTPException(status_code=404, detail="해당 회원을 찾을 수 없습니다.")
    if recipient.user_id == current_user.user_id:
        raise HTTPException(status_code=400, detail="본인에게 동행 요청을 보낼 수 없습니다.")

    route_user_key = f"{shared_route_id}:{recipient.user_id}"
    membership = db.scalar(
        select(SharedRouteMemberRecord).where(
            SharedRouteMemberRecord.route_user_key == route_user_key
        )
    )
    if membership is not None:
        accepted = db.scalar(
            select(RouteCompanionRequestRecord)
            .where(
                RouteCompanionRequestRecord.shared_route_id == shared_route_id,
                RouteCompanionRequestRecord.requester_user_id == current_user.user_id,
                RouteCompanionRequestRecord.recipient_user_id == recipient.user_id,
                RouteCompanionRequestRecord.status == "accepted",
            )
            .order_by(RouteCompanionRequestRecord.created_at.desc())
        )
        if accepted is not None:
            return _response(db, accepted)

        accepted = RouteCompanionRequestRecord(
            request_id=str(uuid4()),
            shared_route_id=shared_route_id,
            requester_user_id=current_user.user_id,
            recipient_user_id=recipient.user_id,
            status="accepted",
            created_at=datetime.now(timezone.utc),
            responded_at=datetime.now(timezone.utc),
        )
        db.add(accepted)
        db.commit()
        db.refresh(accepted)
        return _response(db, accepted)

    existing = db.scalar(
        select(RouteCompanionRequestRecord).where(
            RouteCompanionRequestRecord.shared_route_id == shared_route_id,
            RouteCompanionRequestRecord.requester_user_id == current_user.user_id,
            RouteCompanionRequestRecord.recipient_user_id == recipient.user_id,
            RouteCompanionRequestRecord.status == "pending",
        )
    )
    if existing is not None:
        _restore_or_create_companion_notification(
            db,
            row=existing,
            requester=current_user,
            recipient=recipient,
        )
        db.commit()
        return _response(db, existing)

    row = RouteCompanionRequestRecord(
        request_id=str(uuid4()),
        shared_route_id=shared_route_id,
        requester_user_id=current_user.user_id,
        recipient_user_id=recipient.user_id,
        status="pending",
        created_at=datetime.now(timezone.utc),
    )
    db.add(row)

    # notifications.route_request_id가 route_companion_requests.request_id를
    # Foreign Key로 참조하므로 알림 생성 전에 요청 row를 먼저 DB에 반영합니다.
    db.flush()

    _restore_or_create_companion_notification(
        db,
        row=row,
        requester=current_user,
        recipient=recipient,
    )

    db.commit()
    db.refresh(row)
    return _response(db, row)


@companion_router.get(
    "/shared-routes/{shared_route_id}/companion-requests/outgoing",
    response_model=list[OutgoingCompanionRequestResponse],
)
def outgoing_companion_requests(
    shared_route_id: str,
    db: Session = Depends(get_db),
    current_user: UserRecord = Depends(get_current_user),
):
    route = db.get(SharedRouteRecord, shared_route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="공유 코스를 찾을 수 없습니다.")
    if _route_owner_id(route) != current_user.user_id:
        raise HTTPException(
            status_code=403,
            detail="코스 소유자만 보낸 동행 요청을 확인할 수 있습니다.",
        )

    rows = list(
        db.scalars(
            select(RouteCompanionRequestRecord)
            .where(
                RouteCompanionRequestRecord.shared_route_id == shared_route_id,
                RouteCompanionRequestRecord.requester_user_id
                == current_user.user_id,
            )
            .order_by(RouteCompanionRequestRecord.created_at.desc())
        ).all()
    )

    result: list[OutgoingCompanionRequestResponse] = []
    for row in rows:
        recipient = db.get(UserRecord, row.recipient_user_id)
        profile = db.get(UserPublicProfileRecord, row.recipient_user_id)
        result.append(
            OutgoingCompanionRequestResponse(
                request_id=row.request_id,
                shared_route_id=row.shared_route_id,
                recipient_user_id=row.recipient_user_id,
                recipient_member_code=(
                    profile.member_code if profile is not None else ""
                ),
                recipient_nickname=(
                    recipient.nickname if recipient is not None else "경주한적 사용자"
                ),
                status=row.status,
                created_at=row.created_at,
            )
        )
    return result


@companion_router.get(
    "/shared-routes/companion-requests/incoming",
    response_model=list[CompanionRequestResponse],
)
def incoming_companion_requests(
    db: Session = Depends(get_db),
    current_user: UserRecord = Depends(get_current_user),
):
    rows = list(
        db.scalars(
            select(RouteCompanionRequestRecord)
            .where(
                RouteCompanionRequestRecord.recipient_user_id == current_user.user_id,
                RouteCompanionRequestRecord.status == "pending",
            )
            .order_by(RouteCompanionRequestRecord.created_at.desc())
        ).all()
    )
    return [_response(db, row) for row in rows]


@companion_router.post(
    "/shared-routes/companion-requests/{request_id}/accept",
    response_model=CompanionRequestResponse,
)
def accept_companion_request(
    request_id: str,
    db: Session = Depends(get_db),
    current_user: UserRecord = Depends(get_current_user),
):
    row = db.get(RouteCompanionRequestRecord, request_id)
    if row is None or row.recipient_user_id != current_user.user_id:
        raise HTTPException(status_code=404, detail="동행 요청을 찾을 수 없습니다.")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="이미 처리된 동행 요청입니다.")

    route_user_key = f"{row.shared_route_id}:{current_user.user_id}"
    membership = db.scalar(
        select(SharedRouteMemberRecord).where(
            SharedRouteMemberRecord.route_user_key == route_user_key
        )
    )
    if membership is None:
        membership = SharedRouteMemberRecord(
            membership_id=str(uuid4()),
            route_user_key=route_user_key,
            shared_route_id=row.shared_route_id,
            user_id=current_user.user_id,
            role="editor",
            added_by_user_id=row.requester_user_id,
            joined_at=datetime.now(timezone.utc),
        )
        db.add(membership)

    row.status = "accepted"
    row.responded_at = datetime.now(timezone.utc)
    db.add(row)

    create_notification(
        db,
        user_id=row.requester_user_id,
        actor_user_id=current_user.user_id,
        type="shared_route_invite_accepted",
        title="동행 요청 수락",
        message=f"{current_user.nickname}님이 동행 코스 요청을 수락했어요.",
        shared_route_id=row.shared_route_id,
        route_request_id=row.request_id,
    )

    db.commit()
    db.refresh(row)
    return _response(db, row)


@companion_router.post(
    "/shared-routes/companion-requests/{request_id}/reject",
    response_model=CompanionRequestResponse,
)
def reject_companion_request(
    request_id: str,
    db: Session = Depends(get_db),
    current_user: UserRecord = Depends(get_current_user),
):
    row = db.get(RouteCompanionRequestRecord, request_id)
    if row is None or row.recipient_user_id != current_user.user_id:
        raise HTTPException(status_code=404, detail="동행 요청을 찾을 수 없습니다.")
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="이미 처리된 동행 요청입니다.")

    row.status = "rejected"
    row.responded_at = datetime.now(timezone.utc)
    db.add(row)

    create_notification(
        db,
        user_id=row.requester_user_id,
        actor_user_id=current_user.user_id,
        type="shared_route_invite_rejected",
        title="동행 요청 거절",
        message=f"{current_user.nickname}님이 동행 코스 요청을 거절했어요.",
        shared_route_id=row.shared_route_id,
        route_request_id=row.request_id,
    )

    db.commit()
    db.refresh(row)
    return _response(db, row)
