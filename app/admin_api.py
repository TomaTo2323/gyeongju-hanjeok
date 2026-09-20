from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .auth_service import require_admin
from .db import (
    CommunityCommentRecord,
    CommunityPostHiddenRecord,
    CommunityPostRecord,
    CommunityPostReportRecord,
    CommunityRecommendationRecord,
    CommunitySavedCourseRecord,
    UserRecord,
    get_db,
)
from .member_service import ensure_public_profile


admin_router = APIRouter(prefix="/admin", tags=["admin"])


class AdminUserResponse(BaseModel):
    user_id: str
    member_code: str
    email: str
    nickname: str
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None


class AdminUserActiveUpdate(BaseModel):
    is_active: bool


class AdminReportResponse(BaseModel):
    report_key: str
    post_id: str
    reporter_user_id: str
    reporter_nickname: str | None = None
    reason: str
    detail: str
    created_at: datetime
    post_type: str | None = None
    post_title: str | None = None
    post_content: str | None = None
    author_user_id: str | None = None
    author_nickname: str | None = None


class AdminPostResponse(BaseModel):
    post_id: str
    author_user_id: str
    author_nickname: str | None = None
    post_type: str
    title: str
    content: str
    place_id: str | None = None
    crowd_percent: int | None = None
    report_count: int = 0
    created_at: datetime


class AdminCommentResponse(BaseModel):
    comment_id: str
    post_id: str
    author_user_id: str
    author_nickname: str | None = None
    content: str
    created_at: datetime


class AdminMessageResponse(BaseModel):
    message: str


def _admin_user_response(db: Session, user: UserRecord) -> AdminUserResponse:
    profile = ensure_public_profile(db, user)
    return AdminUserResponse(
        user_id=user.user_id,
        member_code=profile.member_code,
        email=user.email,
        nickname=user.nickname,
        role=getattr(user, "role", "user"),
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


def _admin_post_response(db: Session, post: CommunityPostRecord) -> AdminPostResponse:
    author = db.get(UserRecord, post.author_user_id)
    report_count = db.scalar(
        select(func.count())
        .select_from(CommunityPostReportRecord)
        .where(CommunityPostReportRecord.post_id == post.post_id)
    )
    return AdminPostResponse(
        post_id=post.post_id,
        author_user_id=post.author_user_id,
        author_nickname=author.nickname if author else None,
        post_type=post.post_type,
        title=post.title,
        content=post.content,
        place_id=post.place_id,
        crowd_percent=post.crowd_percent,
        report_count=int(report_count or 0),
        created_at=post.created_at,
    )


@admin_router.get("/users", response_model=list[AdminUserResponse])
def list_users(
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=100, ge=1, le=300),
    _: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = list(
        db.scalars(
            select(UserRecord)
            .order_by(UserRecord.created_at.desc())
            .limit(limit)
        )
    )
    keyword = q.strip().lower()
    if keyword:
        rows = [
            row for row in rows
            if keyword in row.email.lower()
            or keyword in row.nickname.lower()
            or keyword in row.user_id.lower()
        ]
    return [_admin_user_response(db, row) for row in rows]


@admin_router.patch("/users/{user_id}/active", response_model=AdminUserResponse)
def set_user_active(
    user_id: str,
    body: AdminUserActiveUpdate,
    current_admin: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.get(UserRecord, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    if user.user_id == current_admin.user_id and body.is_active is False:
        raise HTTPException(status_code=400, detail="현재 관리자 계정은 비활성화할 수 없습니다.")

    user.is_active = body.is_active
    db.commit()
    db.refresh(user)
    return _admin_user_response(db, user)


@admin_router.get("/reports", response_model=list[AdminReportResponse])
def list_reports(
    limit: int = Query(default=100, ge=1, le=300),
    _: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
):
    reports = list(
        db.scalars(
            select(CommunityPostReportRecord)
            .order_by(CommunityPostReportRecord.created_at.desc())
            .limit(limit)
        )
    )

    result: list[AdminReportResponse] = []
    for report in reports:
        post = db.get(CommunityPostRecord, report.post_id)
        reporter = db.get(UserRecord, report.reporter_user_id)
        author = db.get(UserRecord, post.author_user_id) if post is not None else None

        result.append(
            AdminReportResponse(
                report_key=report.report_key,
                post_id=report.post_id,
                reporter_user_id=report.reporter_user_id,
                reporter_nickname=reporter.nickname if reporter else None,
                reason=report.reason,
                detail=report.detail,
                created_at=report.created_at,
                post_type=post.post_type if post else None,
                post_title=post.title if post else None,
                post_content=post.content if post else None,
                author_user_id=post.author_user_id if post else None,
                author_nickname=author.nickname if author else None,
            )
        )
    return result


@admin_router.delete("/reports/{report_key}", response_model=AdminMessageResponse)
def dismiss_report(
    report_key: str,
    _: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
):
    report = db.get(CommunityPostReportRecord, report_key)
    if report is None:
        raise HTTPException(status_code=404, detail="신고 내역을 찾을 수 없습니다.")
    db.delete(report)
    db.commit()
    return AdminMessageResponse(message="신고를 처리 완료했습니다.")


@admin_router.get("/posts", response_model=list[AdminPostResponse])
def list_posts(
    post_type: str = Query(default="", max_length=20),
    q: str = Query(default="", max_length=100),
    limit: int = Query(default=100, ge=1, le=300),
    _: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
):
    stmt = select(CommunityPostRecord)
    if post_type.strip():
        stmt = stmt.where(CommunityPostRecord.post_type == post_type.strip())
    rows = list(
        db.scalars(
            stmt.order_by(CommunityPostRecord.created_at.desc()).limit(limit)
        )
    )

    keyword = q.strip().lower()
    if keyword:
        rows = [
            row for row in rows
            if keyword in row.title.lower()
            or keyword in row.content.lower()
        ]
    return [_admin_post_response(db, row) for row in rows]


@admin_router.get(
    "/posts/{post_id}/comments",
    response_model=list[AdminCommentResponse],
)
def list_post_comments(
    post_id: str,
    _: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
):
    post = db.get(CommunityPostRecord, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="게시물을 찾을 수 없습니다.")

    rows = list(
        db.scalars(
            select(CommunityCommentRecord)
            .where(CommunityCommentRecord.post_id == post_id)
            .order_by(CommunityCommentRecord.created_at.asc())
        )
    )
    result: list[AdminCommentResponse] = []
    for row in rows:
        author = db.get(UserRecord, row.author_user_id)
        result.append(
            AdminCommentResponse(
                comment_id=row.comment_id,
                post_id=row.post_id,
                author_user_id=row.author_user_id,
                author_nickname=author.nickname if author else None,
                content=row.content,
                created_at=row.created_at,
            )
        )
    return result


@admin_router.delete("/posts/{post_id}", response_model=AdminMessageResponse)
def admin_delete_post(
    post_id: str,
    _: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
):
    post = db.get(CommunityPostRecord, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="게시물을 찾을 수 없습니다.")

    db.execute(delete(CommunityRecommendationRecord).where(
        CommunityRecommendationRecord.post_id == post_id
    ))
    db.execute(delete(CommunityCommentRecord).where(
        CommunityCommentRecord.post_id == post_id
    ))
    db.execute(delete(CommunitySavedCourseRecord).where(
        CommunitySavedCourseRecord.source_post_id == post_id
    ))
    db.execute(delete(CommunityPostReportRecord).where(
        CommunityPostReportRecord.post_id == post_id
    ))
    db.execute(delete(CommunityPostHiddenRecord).where(
        CommunityPostHiddenRecord.post_id == post_id
    ))
    db.delete(post)
    db.commit()
    return AdminMessageResponse(message="게시물을 삭제했습니다.")


@admin_router.delete("/comments/{comment_id}", response_model=AdminMessageResponse)
def admin_delete_comment(
    comment_id: str,
    _: UserRecord = Depends(require_admin),
    db: Session = Depends(get_db),
):
    comment = db.get(CommunityCommentRecord, comment_id)
    if comment is None:
        raise HTTPException(status_code=404, detail="댓글을 찾을 수 없습니다.")
    db.delete(comment)
    db.commit()
    return AdminMessageResponse(message="댓글을 삭제했습니다.")
