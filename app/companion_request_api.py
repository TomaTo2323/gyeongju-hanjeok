from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth_service import get_current_user
from .db import (
    RouteCompanionRequestRecord,
    SharedRouteMemberRecord,
    SharedRouteRecord,
    UserPublicProfileRecord,
    UserRecord,
    get_db,
)
from .member_service import normalize_member_code
from .notification_service import create_notification

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


def _response(db: Session, row: RouteCompanionRequestRecord) -> CompanionRequestResponse:
    requester = db.get(UserRecord, row.requester_user_id)
    return CompanionRequestResponse(
        request_id=row.request_id,
        shared_route_id=row.shared_route_id,
        requester_user_id=row.requester_user_id,
        recipient_user_id=row.recipient_user_id,
        requester_nickname=requester.nickname if requester is not None else "경주한적 사용자",
        status=row.status,
        created_at=row.created_at,
    )


def _route_user_key(shared_route_id: str, user_id: str) -> str:
    return f"{shared_route_id}:{user_id}"


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
    if route.owner_user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="코스를 만든 방장만 동행을 요청할 수 있습니다.")

    member_code = normalize_member_code(body.member_code)
    profile = db.scalar(
        select(UserPublicProfileRecord).where(
            UserPublicProfileRecord.member_code == member_code
        )
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="해당 회원코드를 찾을 수 없습니다.")
    recipient = db.get(UserRecord, profile.user_id)
    if recipient is None:
        raise HTTPException(status_code=404, detail="해당 회원을 찾을 수 없습니다.")
    if recipient.user_id == current_user.user_id:
        raise HTTPException(status_code=400, detail="본인에게 동행 요청을 보낼 수 없습니다.")

    existing = db.scalar(
        select(RouteCompanionRequestRecord).where(
            RouteCompanionRequestRecord.shared_route_id == shared_route_id,
            RouteCompanionRequestRecord.requester_user_id == current_user.user_id,
            RouteCompanionRequestRecord.recipient_user_id == recipient.user_id,
            RouteCompanionRequestRecord.status == "pending",
        )
    )
    if existing is not None:
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

    create_notification(
        db,
        user_id=recipient.user_id,
        actor_user_id=current_user.user_id,
        type="shared_route_invite",
        title="동행 코스 요청",
        message=f"{current_user.nickname}님이 함께 여행할 코스로 초대했어요.",
        shared_route_id=shared_route_id,
        route_request_id=row.request_id,
    )
    db.commit()
    db.refresh(row)
    return _response(db, row)


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

    route = db.get(SharedRouteRecord, row.shared_route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="공유 코스를 찾을 수 없습니다.")

    route_user_key = _route_user_key(row.shared_route_id, current_user.user_id)
    membership = db.scalar(
        select(SharedRouteMemberRecord).where(
            SharedRouteMemberRecord.route_user_key == route_user_key
        )
    )
    if membership is None:
        db.add(
            SharedRouteMemberRecord(
                route_user_key=route_user_key,
                shared_route_id=row.shared_route_id,
                user_id=current_user.user_id,
                role="editor",
                added_by_user_id=row.requester_user_id,
                joined_at=datetime.now(timezone.utc),
            )
        )

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
    db.commit()
    db.refresh(row)
    return _response(db, row)
