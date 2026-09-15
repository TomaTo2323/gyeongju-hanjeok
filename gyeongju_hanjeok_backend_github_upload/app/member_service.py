from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import UserPublicProfileRecord, UserRecord

_MEMBER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def normalize_member_code(value: str) -> str:
    value = value.strip().upper().replace(" ", "")
    if value.startswith("GJ") and not value.startswith("GJ-"):
        value = "GJ-" + value[2:].lstrip("-")
    return value


def generate_member_code(db: Session) -> str:
    for _ in range(40):
        suffix = "".join(secrets.choice(_MEMBER_ALPHABET) for _ in range(6))
        code = f"GJ-{suffix}"
        exists = db.scalar(
            select(UserPublicProfileRecord.user_id).where(
                UserPublicProfileRecord.member_code == code
            )
        )
        if exists is None:
            return code
    raise RuntimeError("회원코드를 생성하지 못했습니다.")


def ensure_public_profile(
    db: Session,
    user: UserRecord,
    *,
    commit_if_created: bool = True,
) -> UserPublicProfileRecord:
    profile = db.get(UserPublicProfileRecord, user.user_id)
    if profile is not None:
        return profile

    profile = UserPublicProfileRecord(
        user_id=user.user_id,
        member_code=generate_member_code(db),
    )
    db.add(profile)
    db.flush()

    if commit_if_created:
        db.commit()
        db.refresh(profile)

    return profile


def pair_key(user_id_a: str, user_id_b: str) -> str:
    first, second = sorted([user_id_a, user_id_b])
    return f"{first}:{second}"
