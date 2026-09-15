from __future__ import annotations

import html
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .auth_service import get_current_user
from .config import Settings, get_settings
from .db import (
    FriendInviteRecord,
    FriendshipRecord,
    UserPublicProfileRecord,
    UserRecord,
    get_db,
)
from .member_service import ensure_public_profile, normalize_member_code, pair_key

friend_router = APIRouter(prefix="/friends", tags=["friends"])
invite_landing_router = APIRouter(tags=["invite"])


class FriendByCodeRequest(BaseModel):
    member_code: str = Field(min_length=4, max_length=20)

    @field_validator("member_code")
    @classmethod
    def clean_code(cls, value: str) -> str:
        return normalize_member_code(value)


class FriendUserResponse(BaseModel):
    user_id: str
    member_code: str
    nickname: str


class FriendshipResponse(BaseModel):
    friendship_id: str
    status: Literal["pending", "accepted"]
    direction: Literal["incoming", "outgoing", "friend"]
    user: FriendUserResponse
    created_at: datetime
    accepted_at: datetime | None = None


class FriendListResponse(BaseModel):
    friends: list[FriendshipResponse]
    incoming: list[FriendshipResponse]
    outgoing: list[FriendshipResponse]


class FriendInviteResponse(BaseModel):
    invite_token: str
    invite_url: str
    deep_link: str
    expires_at: datetime


class FriendInvitePreviewResponse(BaseModel):
    invite_token: str
    inviter: FriendUserResponse
    expires_at: datetime
    is_valid: bool


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _load_user(db: Session, user_id: str) -> UserRecord:
    user = db.get(UserRecord, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    return user


def _friend_user(db: Session, user_id: str) -> FriendUserResponse:
    user = _load_user(db, user_id)
    profile = ensure_public_profile(db, user)
    return FriendUserResponse(
        user_id=user.user_id,
        member_code=profile.member_code,
        nickname=user.nickname,
    )


def _friendship_response(
    db: Session,
    friendship: FriendshipRecord,
    viewer_user_id: str,
) -> FriendshipResponse:
    other_user_id = (
        friendship.addressee_user_id
        if friendship.requester_user_id == viewer_user_id
        else friendship.requester_user_id
    )

    if friendship.status == "accepted":
        direction: Literal["incoming", "outgoing", "friend"] = "friend"
    elif friendship.requester_user_id == viewer_user_id:
        direction = "outgoing"
    else:
        direction = "incoming"

    return FriendshipResponse(
        friendship_id=friendship.friendship_id,
        status=friendship.status,  # type: ignore[arg-type]
        direction=direction,
        user=_friend_user(db, other_user_id),
        created_at=friendship.created_at,
        accepted_at=friendship.accepted_at,
    )


def _find_friendship(db: Session, user_a: str, user_b: str) -> FriendshipRecord | None:
    return db.scalar(
        select(FriendshipRecord).where(
            FriendshipRecord.pair_key == pair_key(user_a, user_b)
        )
    )


def _invite_is_valid(invite: FriendInviteRecord) -> bool:
    return invite.status == "active" and _aware(invite.expires_at) > _now()


def _find_invite_or_404(db: Session, token: str) -> FriendInviteRecord:
    invite = db.scalar(
        select(FriendInviteRecord).where(
            FriendInviteRecord.invite_token == token.strip()
        )
    )
    if invite is None:
        raise HTTPException(status_code=404, detail="초대 링크를 찾을 수 없습니다.")
    return invite


@friend_router.get("", response_model=FriendListResponse)
def list_friends(
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ensure_public_profile(db, current_user)
    rows = db.scalars(
        select(FriendshipRecord)
        .where(
            or_(
                FriendshipRecord.requester_user_id == current_user.user_id,
                FriendshipRecord.addressee_user_id == current_user.user_id,
            )
        )
        .order_by(FriendshipRecord.updated_at.desc())
    ).all()

    friends: list[FriendshipResponse] = []
    incoming: list[FriendshipResponse] = []
    outgoing: list[FriendshipResponse] = []

    for row in rows:
        if row.status not in {"pending", "accepted"}:
            continue
        item = _friendship_response(db, row, current_user.user_id)
        if item.direction == "friend":
            friends.append(item)
        elif item.direction == "incoming":
            incoming.append(item)
        else:
            outgoing.append(item)

    return FriendListResponse(
        friends=friends,
        incoming=incoming,
        outgoing=outgoing,
    )


@friend_router.post("/requests", response_model=FriendshipResponse)
def request_friend(
    body: FriendByCodeRequest,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    current_profile = ensure_public_profile(db, current_user)
    target_profile = db.scalar(
        select(UserPublicProfileRecord).where(
            UserPublicProfileRecord.member_code == body.member_code
        )
    )

    if target_profile is None:
        raise HTTPException(status_code=404, detail="해당 회원코드를 찾을 수 없습니다.")
    if target_profile.user_id == current_user.user_id:
        raise HTTPException(status_code=422, detail="내 회원코드는 친구로 추가할 수 없습니다.")

    existing = _find_friendship(db, current_user.user_id, target_profile.user_id)
    if existing is not None:
        if existing.status == "accepted":
            return _friendship_response(db, existing, current_user.user_id)
        if existing.status == "pending":
            # 서로 친구 요청을 보냈다면 바로 수락 처리합니다.
            if existing.addressee_user_id == current_user.user_id:
                existing.status = "accepted"
                existing.accepted_at = _now()
                existing.updated_at = _now()
                db.commit()
                db.refresh(existing)
            return _friendship_response(db, existing, current_user.user_id)

    friendship = FriendshipRecord(
        pair_key=pair_key(current_user.user_id, target_profile.user_id),
        requester_user_id=current_user.user_id,
        addressee_user_id=target_profile.user_id,
        status="pending",
    )
    db.add(friendship)
    db.commit()
    db.refresh(friendship)
    return _friendship_response(db, friendship, current_user.user_id)


@friend_router.post("/{friendship_id}/accept", response_model=FriendshipResponse)
def accept_friend(
    friendship_id: str,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    friendship = db.get(FriendshipRecord, friendship_id)
    if friendship is None:
        raise HTTPException(status_code=404, detail="친구 요청을 찾을 수 없습니다.")
    if friendship.addressee_user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="이 친구 요청을 수락할 권한이 없습니다.")
    if friendship.status != "pending":
        return _friendship_response(db, friendship, current_user.user_id)

    friendship.status = "accepted"
    friendship.accepted_at = _now()
    friendship.updated_at = _now()
    db.commit()
    db.refresh(friendship)
    return _friendship_response(db, friendship, current_user.user_id)


@friend_router.delete("/{friendship_id}")
def remove_friendship(
    friendship_id: str,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    friendship = db.get(FriendshipRecord, friendship_id)
    if friendship is None:
        raise HTTPException(status_code=404, detail="친구 관계를 찾을 수 없습니다.")
    if current_user.user_id not in {
        friendship.requester_user_id,
        friendship.addressee_user_id,
    }:
        raise HTTPException(status_code=403, detail="이 친구 관계를 변경할 권한이 없습니다.")

    db.delete(friendship)
    db.commit()
    return {"message": "친구 관계를 삭제했습니다."}


@friend_router.post("/invites", response_model=FriendInviteResponse)
def create_invite(
    request: Request,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    ensure_public_profile(db, current_user)
    token = secrets.token_urlsafe(24)
    expires_at = _now() + timedelta(hours=max(1, settings.friend_invite_hours))
    invite = FriendInviteRecord(
        invite_token=token,
        inviter_user_id=current_user.user_id,
        status="active",
        expires_at=expires_at,
    )
    db.add(invite)
    db.commit()

    base = settings.public_invite_base_url.strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")

    return FriendInviteResponse(
        invite_token=token,
        invite_url=f"{base}/invite/{token}",
        deep_link=f"gyeongjuhanjeok://invite/{token}",
        expires_at=expires_at,
    )


@friend_router.get("/invites/{token}", response_model=FriendInvitePreviewResponse)
def preview_invite(
    token: str,
    db: Session = Depends(get_db),
):
    invite = _find_invite_or_404(db, token)
    return FriendInvitePreviewResponse(
        invite_token=invite.invite_token,
        inviter=_friend_user(db, invite.inviter_user_id),
        expires_at=invite.expires_at,
        is_valid=_invite_is_valid(invite),
    )


@friend_router.post("/invites/{token}/claim", response_model=FriendshipResponse)
def claim_invite(
    token: str,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    invite = _find_invite_or_404(db, token)
    if not _invite_is_valid(invite):
        raise HTTPException(status_code=410, detail="만료되었거나 이미 사용된 초대 링크입니다.")
    if invite.inviter_user_id == current_user.user_id:
        raise HTTPException(status_code=422, detail="내 초대 링크는 직접 사용할 수 없습니다.")

    existing = _find_friendship(db, invite.inviter_user_id, current_user.user_id)
    if existing is None:
        existing = FriendshipRecord(
            pair_key=pair_key(invite.inviter_user_id, current_user.user_id),
            requester_user_id=invite.inviter_user_id,
            addressee_user_id=current_user.user_id,
            status="accepted",
            accepted_at=_now(),
        )
        db.add(existing)
    else:
        existing.status = "accepted"
        existing.accepted_at = existing.accepted_at or _now()
        existing.updated_at = _now()

    invite.status = "claimed"
    invite.claimed_by_user_id = current_user.user_id
    invite.claimed_at = _now()
    db.commit()
    db.refresh(existing)
    return _friendship_response(db, existing, current_user.user_id)


@invite_landing_router.get("/invite/{token}", response_class=HTMLResponse)
def invite_landing(
    token: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    invite = _find_invite_or_404(db, token)
    inviter = _friend_user(db, invite.inviter_user_id)
    valid = _invite_is_valid(invite)

    safe_nickname = html.escape(inviter.nickname)
    safe_code = html.escape(inviter.member_code)
    deep_link = f"gyeongjuhanjeok://invite/{invite.invite_token}"
    download_url = settings.app_download_url.strip()

    if valid:
        install_button = (
            f'<a class="secondary" href="{html.escape(download_url)}">앱 설치하기</a>'
            if download_url
            else '<p class="hint">앱이 없다면 설치 후 이 카카오톡 초대 링크를 다시 눌러주세요.</p>'
        )
        action = f'''<a class="primary" href="{deep_link}">경주한적 앱에서 열기</a>{install_button}'''
        auto_script = f'''<script>setTimeout(function(){{ window.location.href = "{deep_link}"; }}, 250);</script>'''
    else:
        action = '<p class="expired">이 초대 링크는 만료되었거나 이미 사용되었습니다.</p>'
        auto_script = ''

    return HTMLResponse(f'''<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>경주한적 친구 초대</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;background:#f7f4ec;margin:0;padding:28px;color:#24362f}}
.card{{max-width:520px;margin:40px auto;background:white;border-radius:24px;padding:28px;box-shadow:0 12px 40px #00000012}}
h1{{font-size:25px;margin:0 0 10px}} p{{line-height:1.6}} .code{{font-weight:800;letter-spacing:1px;background:#f1ead9;padding:10px 12px;border-radius:12px;display:inline-block}}
a{{display:block;text-align:center;text-decoration:none;border-radius:14px;padding:14px;margin-top:12px;font-weight:800}}
.primary{{background:#315e50;color:white}} .secondary{{background:#eee7d8;color:#315e50}} .hint{{font-size:13px;color:#6d756f}} .expired{{color:#b34b42;font-weight:700}}
</style>
</head><body><div class="card"><h1>경주한적에서 같이 여행해요</h1>
<p><strong>{safe_nickname}</strong>님이 친구로 초대했어요.</p><p>회원코드 <span class="code">{safe_code}</span></p>{action}</div>{auto_script}</body></html>''')
