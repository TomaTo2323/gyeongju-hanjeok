from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth_service import (
    create_access_token,
    get_current_user,
    hash_password,
    normalize_email,
    validate_email,
    validate_password,
    verify_password,
)
from .config import Settings, get_settings
from .db import UserConsentRecord, UserRecord, get_db
from .member_service import ensure_public_profile


auth_router = APIRouter(prefix="/auth", tags=["auth"])


class SignupRequest(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    nickname: str = Field(min_length=2, max_length=20)
    terms_agreed: bool
    privacy_agreed: bool
    location_agreed: bool = False
    notification_agreed: bool = False

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        try:
            return validate_email(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("password")
    @classmethod
    def clean_password(cls, value: str) -> str:
        try:
            return validate_password(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("nickname")
    @classmethod
    def clean_nickname(cls, value: str) -> str:
        nickname = value.strip()
        if len(nickname) < 2:
            raise ValueError("닉네임은 2자 이상이어야 합니다.")
        return nickname


class LoginRequest(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class ConsentUpdateRequest(BaseModel):
    location_agreed: bool | None = None
    notification_agreed: bool | None = None


class ConsentResponse(BaseModel):
    terms_agreed: bool
    privacy_agreed: bool
    location_agreed: bool
    notification_agreed: bool
    terms_version: str
    privacy_version: str
    location_version: str
    notification_version: str
    agreed_at: datetime


class UserResponse(BaseModel):
    user_id: str
    role: str = "user"
    member_code: str
    email: str
    nickname: str
    created_at: datetime
    consents: ConsentResponse


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserResponse


class MessageResponse(BaseModel):
    message: str


def _consent_for_user(db: Session, user_id: str) -> UserConsentRecord:
    consent = db.scalar(
        select(UserConsentRecord).where(UserConsentRecord.user_id == user_id)
    )
    if consent is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="사용자 동의 정보를 확인할 수 없습니다.",
        )
    return consent


def _user_response(db: Session, user: UserRecord) -> UserResponse:
    consent = _consent_for_user(db, user.user_id)
    public_profile = ensure_public_profile(db, user)
    return UserResponse(
        user_id=user.user_id,
        role=user.role,
        member_code=public_profile.member_code,
        email=user.email,
        nickname=user.nickname,
        created_at=user.created_at,
        consents=ConsentResponse(
            terms_agreed=consent.terms_agreed,
            privacy_agreed=consent.privacy_agreed,
            location_agreed=consent.location_agreed,
            notification_agreed=consent.notification_agreed,
            terms_version=consent.terms_version,
            privacy_version=consent.privacy_version,
            location_version=consent.location_version,
            notification_version=consent.notification_version,
            agreed_at=consent.agreed_at,
        ),
    )


@auth_router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def signup(
    body: SignupRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not body.terms_agreed or not body.privacy_agreed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="서비스 이용약관과 개인정보 수집·이용 동의는 필수입니다.",
        )

    email = normalize_email(body.email)
    existing = db.scalar(select(UserRecord).where(UserRecord.email == email))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="이미 가입된 이메일입니다.",
        )

    user = UserRecord(
        email=email,
        password_hash=hash_password(body.password),
        nickname=body.nickname.strip(),
    )
    db.add(user)
    db.flush()
    ensure_public_profile(
        db,
        user,
        commit_if_created=False,
    )

    consent = UserConsentRecord(
        user_id=user.user_id,
        terms_agreed=True,
        privacy_agreed=True,
        location_agreed=body.location_agreed,
        notification_agreed=body.notification_agreed,
        terms_version=settings.terms_version,
        privacy_version=settings.privacy_version,
        location_version=settings.location_consent_version,
        notification_version=settings.notification_consent_version,
    )
    db.add(consent)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="이미 가입된 이메일입니다.",
        ) from exc

    db.refresh(user)
    token, expires_at = create_access_token(user, settings)
    return AuthResponse(
        access_token=token,
        expires_at=expires_at,
        user=_user_response(db, user),
    )


@auth_router.post("/login", response_model=AuthResponse)
def login(
    body: LoginRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    email = normalize_email(body.email)
    user = db.scalar(select(UserRecord).where(UserRecord.email == email))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="이메일 또는 비밀번호가 올바르지 않습니다.",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="사용할 수 없는 계정입니다.",
        )

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)

    token, expires_at = create_access_token(user, settings)
    return AuthResponse(
        access_token=token,
        expires_at=expires_at,
        user=_user_response(db, user),
    )


@auth_router.get("/me", response_model=UserResponse)
def me(
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _user_response(db, current_user)


@auth_router.patch("/consents", response_model=UserResponse)
def update_consents(
    body: ConsentUpdateRequest,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if (
        body.location_agreed is None
        and body.notification_agreed is None
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="변경할 동의 항목이 없습니다.",
        )

    consent = _consent_for_user(db, current_user.user_id)

    if body.location_agreed is not None:
        consent.location_agreed = body.location_agreed
        consent.location_version = settings.location_consent_version

    if body.notification_agreed is not None:
        consent.notification_agreed = body.notification_agreed
        consent.notification_version = settings.notification_consent_version

    consent.updated_at = datetime.now(timezone.utc)
    db.commit()
    return _user_response(db, current_user)

@auth_router.post("/logout", response_model=MessageResponse)
def logout(_: UserRecord = Depends(get_current_user)):
    # 현재 액세스 토큰은 서버 세션이 없는 서명 토큰입니다.
    # 앱에서 토큰을 삭제하면 로그아웃이 완료됩니다.
    return MessageResponse(message="로그아웃되었습니다.")
