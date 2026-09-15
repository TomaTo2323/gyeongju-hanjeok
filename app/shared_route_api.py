from __future__ import annotations

import html
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth_service import get_current_user
from .config import Settings, get_settings
from .db import (
    FriendshipRecord,
    SharedRouteInviteRecord,
    SharedRouteMemberRecord,
    SharedRouteRecord,
    UserPublicProfileRecord,
    UserRecord,
    get_db,
)
from .member_service import ensure_public_profile, normalize_member_code, pair_key

shared_route_router = APIRouter(
    prefix="/shared-routes",
    tags=["shared-routes"],
)
shared_route_landing_router = APIRouter(tags=["shared-route-invite"])


class CreateSharedRouteRequest(BaseModel):
    source_route_id: str = Field(min_length=1, max_length=128)
    route: dict[str, Any]


class UpdateSharedRouteRequest(BaseModel):
    route: dict[str, Any]
    expected_version: int | None = None


class AddRouteMemberRequest(BaseModel):
    member_code: str = Field(min_length=4, max_length=20)
    role: Literal["editor", "viewer"] = "editor"


class SharedRouteMemberResponse(BaseModel):
    membership_id: str
    user_id: str
    member_code: str
    nickname: str
    role: Literal["owner", "editor", "viewer"]
    joined_at: datetime


class SharedRouteResponse(BaseModel):
    shared_route_id: str
    source_route_id: str
    owner_user_id: str
    version: int
    route: dict[str, Any]
    members: list[SharedRouteMemberResponse]
    updated_at: datetime


class SharedRouteInviteResponse(BaseModel):
    invite_token: str
    shared_route_id: str
    invite_url: str
    deep_link: str
    expires_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _route_user_key(shared_route_id: str, user_id: str) -> str:
    return f"{shared_route_id}:{user_id}"


def _load_route(db: Session, shared_route_id: str) -> SharedRouteRecord:
    route = db.get(SharedRouteRecord, shared_route_id)
    if route is None:
        raise HTTPException(status_code=404, detail="공유 코스를 찾을 수 없습니다.")
    return route


def _membership(
    db: Session,
    shared_route_id: str,
    user_id: str,
) -> SharedRouteMemberRecord | None:
    return db.scalar(
        select(SharedRouteMemberRecord).where(
            SharedRouteMemberRecord.route_user_key
            == _route_user_key(shared_route_id, user_id)
        )
    )


def _require_member(
    db: Session,
    route: SharedRouteRecord,
    user_id: str,
) -> SharedRouteMemberRecord:
    membership = _membership(db, route.shared_route_id, user_id)
    if membership is None:
        raise HTTPException(
            status_code=403,
            detail="이 공유 코스를 볼 권한이 없습니다.",
        )
    return membership


def _require_editor(
    db: Session,
    route: SharedRouteRecord,
    user_id: str,
) -> SharedRouteMemberRecord:
    membership = _require_member(db, route, user_id)
    if membership.role not in {"owner", "editor"}:
        raise HTTPException(
            status_code=403,
            detail="이 코스를 수정할 권한이 없습니다.",
        )
    return membership


def _member_response(
    db: Session,
    membership: SharedRouteMemberRecord,
) -> SharedRouteMemberResponse:
    user = db.get(UserRecord, membership.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="동행 사용자 정보를 찾을 수 없습니다.")
    profile = ensure_public_profile(db, user)

    return SharedRouteMemberResponse(
        membership_id=membership.membership_id,
        user_id=user.user_id,
        member_code=profile.member_code,
        nickname=user.nickname,
        role=membership.role,  # type: ignore[arg-type]
        joined_at=membership.joined_at,
    )


def _response(
    db: Session,
    route: SharedRouteRecord,
) -> SharedRouteResponse:
    memberships = db.scalars(
        select(SharedRouteMemberRecord)
        .where(SharedRouteMemberRecord.shared_route_id == route.shared_route_id)
        .order_by(SharedRouteMemberRecord.joined_at.asc())
    ).all()

    return SharedRouteResponse(
        shared_route_id=route.shared_route_id,
        source_route_id=route.source_route_id,
        owner_user_id=route.owner_user_id,
        version=route.version,
        route=route.route_data,
        members=[
            _member_response(db, membership)
            for membership in memberships
        ],
        updated_at=route.updated_at,
    )


def _accepted_friends(
    db: Session,
    user_id_a: str,
    user_id_b: str,
) -> bool:
    friendship = db.scalar(
        select(FriendshipRecord).where(
            FriendshipRecord.pair_key == pair_key(user_id_a, user_id_b)
        )
    )
    return friendship is not None and friendship.status == "accepted"



@shared_route_router.get("", response_model=list[SharedRouteResponse])
def list_shared_routes(
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    memberships = db.scalars(
        select(SharedRouteMemberRecord).where(
            SharedRouteMemberRecord.user_id == current_user.user_id
        )
    ).all()

    route_ids = list(
        dict.fromkeys(
            membership.shared_route_id
            for membership in memberships
        )
    )

    if not route_ids:
        return []

    routes = db.scalars(
        select(SharedRouteRecord)
        .where(SharedRouteRecord.shared_route_id.in_(route_ids))
        .order_by(SharedRouteRecord.updated_at.desc())
    ).all()

    return [
        _response(db, route)
        for route in routes
    ]


@shared_route_router.post("", response_model=SharedRouteResponse)
def create_or_sync_shared_route(
    body: CreateSharedRouteRequest,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    route = db.scalar(
        select(SharedRouteRecord).where(
            SharedRouteRecord.owner_user_id == current_user.user_id,
            SharedRouteRecord.source_route_id == body.source_route_id,
        )
    )

    if route is None:
        route = SharedRouteRecord(
            source_route_id=body.source_route_id,
            owner_user_id=current_user.user_id,
            route_data=body.route,
            version=1,
        )
        db.add(route)
        db.flush()

        db.add(
            SharedRouteMemberRecord(
                route_user_key=_route_user_key(
                    route.shared_route_id,
                    current_user.user_id,
                ),
                shared_route_id=route.shared_route_id,
                user_id=current_user.user_id,
                role="owner",
                added_by_user_id=current_user.user_id,
            )
        )
    else:
        route.route_data = body.route
        route.version += 1
        route.updated_at = _now()

    db.commit()
    db.refresh(route)
    return _response(db, route)


@shared_route_router.get("/{shared_route_id}", response_model=SharedRouteResponse)
def get_shared_route(
    shared_route_id: str,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    route = _load_route(db, shared_route_id)
    _require_member(db, route, current_user.user_id)
    return _response(db, route)


@shared_route_router.put("/{shared_route_id}", response_model=SharedRouteResponse)
def update_shared_route(
    shared_route_id: str,
    body: UpdateSharedRouteRequest,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    route = _load_route(db, shared_route_id)
    _require_editor(db, route, current_user.user_id)

    if (
        body.expected_version is not None
        and body.expected_version != route.version
    ):
        raise HTTPException(
            status_code=409,
            detail="다른 동행이 코스를 먼저 수정했어요. 최신 코스를 다시 불러와 주세요.",
        )

    route.route_data = body.route
    route.version += 1
    route.updated_at = _now()
    db.commit()
    db.refresh(route)
    return _response(db, route)


@shared_route_router.post(
    "/{shared_route_id}/members/by-code",
    response_model=SharedRouteResponse,
)
def add_route_member_by_code(
    shared_route_id: str,
    body: AddRouteMemberRequest,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    route = _load_route(db, shared_route_id)
    membership = _require_member(db, route, current_user.user_id)

    if membership.role != "owner":
        raise HTTPException(
            status_code=403,
            detail="동행 추가는 코스를 만든 방장만 할 수 있습니다.",
        )

    member_code = normalize_member_code(body.member_code)
    profile = db.scalar(
        select(UserPublicProfileRecord).where(
            UserPublicProfileRecord.member_code == member_code
        )
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="해당 회원코드를 찾을 수 없습니다.")
    if profile.user_id == current_user.user_id:
        raise HTTPException(status_code=422, detail="나 자신은 동행으로 추가할 수 없습니다.")

    if not _accepted_friends(db, current_user.user_id, profile.user_id):
        raise HTTPException(
            status_code=409,
            detail="먼저 친구로 연결한 뒤 동행으로 추가해 주세요.",
        )

    existing = _membership(db, route.shared_route_id, profile.user_id)
    if existing is None:
        db.add(
            SharedRouteMemberRecord(
                route_user_key=_route_user_key(route.shared_route_id, profile.user_id),
                shared_route_id=route.shared_route_id,
                user_id=profile.user_id,
                role=body.role,
                added_by_user_id=current_user.user_id,
            )
        )
        db.commit()

    db.refresh(route)
    return _response(db, route)


@shared_route_router.delete(
    "/{shared_route_id}/members/{membership_id}",
    response_model=SharedRouteResponse,
)
def remove_route_member(
    shared_route_id: str,
    membership_id: str,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    route = _load_route(db, shared_route_id)
    viewer = _require_member(db, route, current_user.user_id)

    membership = db.get(SharedRouteMemberRecord, membership_id)
    if (
        membership is None
        or membership.shared_route_id != route.shared_route_id
    ):
        raise HTTPException(status_code=404, detail="동행 정보를 찾을 수 없습니다.")

    if membership.role == "owner":
        raise HTTPException(status_code=422, detail="방장은 코스에서 제거할 수 없습니다.")

    is_self = membership.user_id == current_user.user_id
    if viewer.role != "owner" and not is_self:
        raise HTTPException(status_code=403, detail="이 동행을 제거할 권한이 없습니다.")

    db.delete(membership)
    db.commit()
    db.refresh(route)
    return _response(db, route)


@shared_route_router.post(
    "/{shared_route_id}/invites",
    response_model=SharedRouteInviteResponse,
)
def create_shared_route_invite(
    shared_route_id: str,
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    route = _load_route(db, shared_route_id)
    membership = _require_member(db, route, current_user.user_id)

    if membership.role != "owner":
        raise HTTPException(
            status_code=403,
            detail="동행 초대 링크는 방장만 만들 수 있습니다.",
        )

    token = secrets.token_urlsafe(24)
    expires_at = _now() + timedelta(
        hours=max(1, settings.friend_invite_hours)
    )

    invite = SharedRouteInviteRecord(
        invite_token=token,
        shared_route_id=route.shared_route_id,
        inviter_user_id=current_user.user_id,
        status="active",
        expires_at=expires_at,
    )
    db.add(invite)
    db.commit()

    base = settings.public_invite_base_url.strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")

    return SharedRouteInviteResponse(
        invite_token=token,
        shared_route_id=route.shared_route_id,
        invite_url=f"{base}/route-invite/{token}",
        deep_link=f"gyeongjuhanjeok://route-invite/{token}",
        expires_at=expires_at,
    )


def _load_invite(
    db: Session,
    token: str,
) -> SharedRouteInviteRecord:
    invite = db.scalar(
        select(SharedRouteInviteRecord).where(
            SharedRouteInviteRecord.invite_token == token
        )
    )
    if invite is None:
        raise HTTPException(status_code=404, detail="동행 초대 링크를 찾을 수 없습니다.")
    return invite


def _invite_valid(invite: SharedRouteInviteRecord) -> bool:
    return (
        invite.status == "active"
        and _aware(invite.expires_at) > _now()
    )


@shared_route_router.post(
    "/invites/{token}/claim",
    response_model=SharedRouteResponse,
)
def claim_shared_route_invite(
    token: str,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    invite = _load_invite(db, token)
    if not _invite_valid(invite):
        raise HTTPException(
            status_code=410,
            detail="만료되었거나 이미 사용된 동행 초대 링크입니다.",
        )
    if invite.inviter_user_id == current_user.user_id:
        raise HTTPException(status_code=422, detail="내 동행 초대 링크는 직접 사용할 수 없습니다.")

    route = _load_route(db, invite.shared_route_id)

    # 카카오 동행 초대를 수락하면 친구 관계도 함께 연결합니다.
    friendship = db.scalar(
        select(FriendshipRecord).where(
            FriendshipRecord.pair_key
            == pair_key(invite.inviter_user_id, current_user.user_id)
        )
    )
    if friendship is None:
        friendship = FriendshipRecord(
            pair_key=pair_key(invite.inviter_user_id, current_user.user_id),
            requester_user_id=invite.inviter_user_id,
            addressee_user_id=current_user.user_id,
            status="accepted",
            accepted_at=_now(),
        )
        db.add(friendship)
    else:
        friendship.status = "accepted"
        friendship.accepted_at = friendship.accepted_at or _now()
        friendship.updated_at = _now()

    membership = _membership(db, route.shared_route_id, current_user.user_id)
    if membership is None:
        db.add(
            SharedRouteMemberRecord(
                route_user_key=_route_user_key(route.shared_route_id, current_user.user_id),
                shared_route_id=route.shared_route_id,
                user_id=current_user.user_id,
                role="editor",
                added_by_user_id=invite.inviter_user_id,
            )
        )

    invite.status = "claimed"
    invite.claimed_by_user_id = current_user.user_id
    invite.claimed_at = _now()

    db.commit()
    db.refresh(route)
    return _response(db, route)


@shared_route_landing_router.get(
    "/route-invite/{token}",
    response_class=HTMLResponse,
)
def route_invite_landing(
    token: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    invite = _load_invite(db, token)
    route = _load_route(db, invite.shared_route_id)
    inviter = db.get(UserRecord, invite.inviter_user_id)

    inviter_name = html.escape(
        inviter.nickname if inviter is not None else "경주한적 사용자"
    )
    route_title = html.escape(
        str(route.route_data.get("title") or "함께 보는 경주 코스")
    )
    deep_link = f"gyeongjuhanjeok://route-invite/{invite.invite_token}"
    download_url = settings.app_download_url.strip()
    valid = _invite_valid(invite)

    if valid:
        install = (
            f'<a class="secondary" href="{html.escape(download_url)}">앱 설치하기</a>'
            if download_url
            else '<p class="hint">앱이 없다면 설치·회원가입 후 이 초대 링크를 다시 눌러주세요.</p>'
        )
        action = (
            f'<a class="primary" href="{deep_link}">경주한적에서 코스 열기</a>'
            + install
        )
        script = (
            f'<script>setTimeout(function(){{window.location.href="{deep_link}";}},250);</script>'
        )
    else:
        action = '<p class="expired">이 동행 초대 링크는 만료되었거나 이미 사용되었습니다.</p>'
        script = ""

    return HTMLResponse(
        f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>경주한적 동행 초대</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;background:#f7f4ec;margin:0;padding:28px;color:#24362f}}
.card{{max-width:520px;margin:40px auto;background:white;border-radius:24px;padding:28px;box-shadow:0 12px 40px #00000012}}
h1{{font-size:25px;margin:0 0 10px}}p{{line-height:1.6}}
.route{{font-weight:900;background:#f1ead9;padding:12px;border-radius:12px}}
a{{display:block;text-align:center;text-decoration:none;border-radius:14px;padding:14px;margin-top:12px;font-weight:800}}
.primary{{background:#315e50;color:white}}.secondary{{background:#eee7d8;color:#315e50}}
.hint{{font-size:13px;color:#6d756f}}.expired{{color:#b34b42;font-weight:700}}
</style>
</head>
<body>
<div class="card">
<h1>경주한적에서 같이 여행해요</h1>
<p><strong>{inviter_name}</strong>님이 동행으로 초대했어요.</p>
<p class="route">{route_title}</p>
{action}
</div>
{script}
</body>
</html>"""
    )
