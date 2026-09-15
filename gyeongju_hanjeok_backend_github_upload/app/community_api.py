from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import String, delete, func, select
from sqlalchemy.orm import Session

from .auth_service import get_current_user
from .db import (
    CommunityCommentRecord,
    CommunityPostRecord,
    CommunityRecommendationRecord,
    CommunitySavedCourseRecord,
    JourneyRecord,
    PlaceRecord,
    UserRecord,
    get_db,
)
from .member_service import ensure_public_profile

community_router = APIRouter(prefix="/community", tags=["community"])


class CommunityPostType(StrEnum):
    course = "course"
    live = "live"
    travel = "travel"


class AuthorResponse(BaseModel):
    user_id: str
    nickname: str
    member_code: str


class CrowdBucket(BaseModel):
    min_percent: int
    max_percent: int
    key: str
    label: str
    color_key: str


class CommunityPostCreate(BaseModel):
    post_type: CommunityPostType
    title: str = Field(default="", max_length=160)
    content: str = Field(min_length=1, max_length=6000)
    image_urls: list[str] = Field(default_factory=list, max_length=10)
    tags: list[str] = Field(default_factory=list, max_length=12)
    related_place_ids: list[str] = Field(default_factory=list, max_length=20)

    # 코스 후기
    course_snapshot: dict[str, Any] | None = None
    source_journey_id: str | None = Field(default=None, max_length=64)
    course_rating: float | None = Field(default=None, ge=1, le=5)
    travel_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")

    # 현재 혼잡/한적 제보
    place_id: str | None = Field(default=None, max_length=64)
    crowd_percent: int | None = Field(default=None, ge=0, le=100)
    observed_at: datetime | None = None

    @field_validator("image_urls", "tags", "related_place_ids")
    @classmethod
    def dedupe_strings(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @model_validator(mode="after")
    def validate_type_fields(self):
        if self.post_type == CommunityPostType.course:
            if self.course_snapshot is None and not self.source_journey_id:
                raise ValueError("코스 후기는 course_snapshot 또는 source_journey_id가 필요합니다.")
            if self.course_rating is None:
                raise ValueError("코스 후기는 course_rating이 필요합니다.")
        elif self.post_type == CommunityPostType.live:
            if not self.place_id:
                raise ValueError("현장 제보는 place_id가 필요합니다.")
            if self.crowd_percent is None:
                raise ValueError("현장 제보는 crowd_percent가 필요합니다.")
        return self


class CommunityPostUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    content: str | None = Field(default=None, min_length=1, max_length=6000)
    image_urls: list[str] | None = Field(default=None, max_length=10)
    tags: list[str] | None = Field(default=None, max_length=12)
    related_place_ids: list[str] | None = Field(default=None, max_length=20)
    course_rating: float | None = Field(default=None, ge=1, le=5)
    travel_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("image_urls", "tags", "related_place_ids")
    @classmethod
    def dedupe_strings(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class CommunityPostResponse(BaseModel):
    post_id: str
    post_type: CommunityPostType
    author: AuthorResponse
    title: str
    content: str
    image_urls: list[str]
    tags: list[str]
    related_place_ids: list[str]

    course_snapshot: dict[str, Any] | None = None
    source_journey_id: str | None = None
    course_rating: float | None = None
    travel_date: str | None = None

    place_id: str | None = None
    place_title: str | None = None
    crowd_percent: int | None = None
    crowd_bucket: CrowdBucket | None = None
    observed_at: datetime | None = None
    live_is_recent: bool = False

    recommendation_count: int
    comment_count: int
    recommended_by_me: bool
    course_saved_by_me: bool
    created_at: datetime
    updated_at: datetime


class CommunityPostListResponse(BaseModel):
    items: list[CommunityPostResponse]
    total: int
    limit: int
    offset: int


class RecommendationResponse(BaseModel):
    post_id: str
    recommended: bool
    recommendation_count: int


class CommentCreate(BaseModel):
    content: str = Field(min_length=1, max_length=1200)


class CommentUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=1200)


class CommentResponse(BaseModel):
    comment_id: str
    post_id: str
    author: AuthorResponse
    content: str
    created_at: datetime
    updated_at: datetime


class SavedCourseResponse(BaseModel):
    saved_course_id: str
    source_post_id: str
    course_snapshot: dict[str, Any]
    created_at: datetime


class CourseCopyPlaceResponse(BaseModel):
    place_id: str
    found: bool
    title: str | None = None
    category: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    current_data: dict[str, Any] | None = None
    data_updated_at: datetime | None = None


class CourseCopyResponse(BaseModel):
    source_post_id: str
    source_author_nickname: str
    course_snapshot: dict[str, Any]
    place_ids: list[str]
    source_place_names: list[str] = Field(default_factory=list)
    current_places: list[CourseCopyPlaceResponse]
    missing_place_ids: list[str]
    include_food: bool = False
    include_cafe: bool = False
    suggested_available_minutes: int | None = None
    # 프론트가 현재 출발 위치를 넣어 /routes/recommend에 재요청할 때 사용할 수 있는
    # 최소 패치. 기존 코스의 관광지는 required_place_names로 보존합니다.
    same_course_request_patch: dict[str, Any] = Field(default_factory=dict)
    requires_live_refresh: bool = True
    message: str


class LiveSummaryResponse(BaseModel):
    place_id: str
    place_title: str | None = None
    hours: int
    report_count: int
    community_average_percent: float | None = None
    community_bucket: CrowdBucket | None = None
    latest_observed_at: datetime | None = None
    official_congestion_score: float | None = None
    recent_reports: list[CommunityPostResponse] = Field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _author(db: Session, user_id: str) -> AuthorResponse:
    user = db.get(UserRecord, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="작성자 정보를 찾을 수 없습니다.")
    profile = ensure_public_profile(db, user)
    return AuthorResponse(
        user_id=user.user_id,
        nickname=user.nickname,
        member_code=profile.member_code,
    )


def _crowd_bucket(percent: float | int) -> CrowdBucket:
    # 홈 화면과 동일: 청 0~20 / 녹 20~40 / 황 40~60 / 홍 60~80 / 적 80~100
    value = max(0.0, min(100.0, float(percent)))
    if value < 20:
        return CrowdBucket(min_percent=0, max_percent=20, key="very_quiet", label="매우 한적", color_key="blue")
    if value < 40:
        return CrowdBucket(min_percent=20, max_percent=40, key="quiet", label="한적", color_key="green")
    if value < 60:
        return CrowdBucket(min_percent=40, max_percent=60, key="normal", label="보통", color_key="yellow")
    if value < 80:
        return CrowdBucket(min_percent=60, max_percent=80, key="busy", label="혼잡", color_key="crimson")
    return CrowdBucket(min_percent=80, max_percent=100, key="very_busy", label="매우 혼잡", color_key="red")


def _recommendation_key(post_id: str, user_id: str) -> str:
    return f"{post_id}:{user_id}"


def _save_key(post_id: str, user_id: str) -> str:
    return f"{post_id}:{user_id}"


def _load_post(db: Session, post_id: str) -> CommunityPostRecord:
    post = db.get(CommunityPostRecord, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="커뮤니티 글을 찾을 수 없습니다.")
    return post


def _recommend_count(db: Session, post_id: str) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(CommunityRecommendationRecord).where(
                CommunityRecommendationRecord.post_id == post_id
            )
        )
        or 0
    )


def _comment_count(db: Session, post_id: str) -> int:
    return int(
        db.scalar(
            select(func.count()).select_from(CommunityCommentRecord).where(
                CommunityCommentRecord.post_id == post_id
            )
        )
        or 0
    )


def _place_title(db: Session, place_id: str | None) -> str | None:
    if not place_id:
        return None
    place = db.get(PlaceRecord, place_id)
    return place.title if place else None


def _response(db: Session, post: CommunityPostRecord, viewer_user_id: str) -> CommunityPostResponse:
    recommended = db.get(
        CommunityRecommendationRecord,
        _recommendation_key(post.post_id, viewer_user_id),
    ) is not None
    saved = db.scalar(
        select(CommunitySavedCourseRecord).where(
            CommunitySavedCourseRecord.save_key == _save_key(post.post_id, viewer_user_id)
        )
    ) is not None

    observed = _aware(post.observed_at) if post.observed_at else None
    return CommunityPostResponse(
        post_id=post.post_id,
        post_type=CommunityPostType(post.post_type),
        author=_author(db, post.author_user_id),
        title=post.title,
        content=post.content,
        image_urls=list(post.image_urls or []),
        tags=list(post.tags or []),
        related_place_ids=list(post.related_place_ids or []),
        course_snapshot=post.course_snapshot,
        source_journey_id=post.source_journey_id,
        course_rating=post.course_rating,
        travel_date=post.travel_date,
        place_id=post.place_id,
        place_title=_place_title(db, post.place_id),
        crowd_percent=post.crowd_percent,
        crowd_bucket=_crowd_bucket(post.crowd_percent) if post.crowd_percent is not None else None,
        observed_at=post.observed_at,
        live_is_recent=(observed is not None and observed >= _now() - timedelta(hours=2)),
        recommendation_count=_recommend_count(db, post.post_id),
        comment_count=_comment_count(db, post.post_id),
        recommended_by_me=recommended,
        course_saved_by_me=saved,
        created_at=post.created_at,
        updated_at=post.updated_at,
    )


def _snapshot_place_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    payload: Any = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else snapshot
    rows: list[dict[str, Any]] = []

    places = payload.get("places") if isinstance(payload, dict) else None
    if isinstance(places, list):
        for item in places:
            if isinstance(item, dict):
                rows.append(item)

    stops = payload.get("stops") if isinstance(payload, dict) else None
    if isinstance(stops, list):
        for stop in stops:
            if not isinstance(stop, dict):
                continue
            place = stop.get("place") if isinstance(stop.get("place"), dict) else stop
            if isinstance(place, dict):
                rows.append(place)

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        key = str(row.get("place_id") or row.get("id") or row.get("title") or row.get("name") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


def _snapshot_place_ids(snapshot: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for item in _snapshot_place_rows(snapshot):
        raw = item.get("place_id") or item.get("id")
        if raw is not None:
            ids.append(str(raw))
    return list(dict.fromkeys(ids))


def _snapshot_place_name(item: dict[str, Any]) -> str:
    return str(item.get("title") or item.get("name") or "").strip()


def _snapshot_service_kind(item: dict[str, Any]) -> str | None:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("category", "type", "content_type", "service_kind", "title", "name")
    ).lower()
    if any(token in text for token in ("카페", "cafe", "coffee", "커피", "디저트", "베이커리")):
        return "cafe"
    if any(token in text for token in ("음식", "맛집", "식당", "restaurant", "food")):
        return "food"
    return None


def _snapshot_total_minutes(snapshot: dict[str, Any]) -> int | None:
    payload: Any = snapshot.get("route") if isinstance(snapshot.get("route"), dict) else snapshot
    if not isinstance(payload, dict):
        return None
    raw = payload.get("total_minutes") or payload.get("duration_minutes")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return max(60, min(1440, value))


def _journey_snapshot(db: Session, journey_id: str) -> dict[str, Any]:
    journey = db.get(JourneyRecord, journey_id)
    if journey is None:
        raise HTTPException(status_code=404, detail="완료한 코스를 찾을 수 없습니다.")

    place_ids = _snapshot_place_ids(journey.course)
    completed = set(journey.completed_place_ids or [])
    if place_ids and not set(place_ids).issubset(completed):
        raise HTTPException(status_code=422, detail="완료한 코스만 커뮤니티에 평가할 수 있습니다.")
    return journey.course


def _validate_live_observed_at(value: datetime | None) -> datetime:
    observed = _aware(value) if value else _now()
    if observed > _now() + timedelta(minutes=10):
        raise HTTPException(status_code=422, detail="현장 제보 시간이 현재보다 너무 앞서 있습니다.")
    if observed < _now() - timedelta(hours=6):
        raise HTTPException(status_code=422, detail="'지금 여기' 제보는 최근 6시간 이내 정보만 등록할 수 있습니다.")
    return observed


def _default_title(db: Session, body: CommunityPostCreate, course_snapshot: dict[str, Any] | None) -> str:
    if body.title.strip():
        return body.title.strip()
    if body.post_type == CommunityPostType.live and body.place_id:
        place = db.get(PlaceRecord, body.place_id)
        place_name = place.title if place else "현재 장소"
        bucket = _crowd_bucket(body.crowd_percent or 0)
        return f"{place_name} 지금 {bucket.label}해요"
    if body.post_type == CommunityPostType.course and course_snapshot:
        route = course_snapshot.get("route") if isinstance(course_snapshot.get("route"), dict) else course_snapshot
        title = route.get("title") or route.get("name") if isinstance(route, dict) else None
        if title:
            return f"{title} 코스 후기"
        return "다녀온 코스 후기"
    return body.content.strip()[:30]


@community_router.post("/posts", response_model=CommunityPostResponse, status_code=status.HTTP_201_CREATED)
def create_post(
    body: CommunityPostCreate,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    snapshot = body.course_snapshot
    if body.post_type == CommunityPostType.course and snapshot is None and body.source_journey_id:
        snapshot = _journey_snapshot(db, body.source_journey_id)

    if body.post_type == CommunityPostType.live:
        if db.get(PlaceRecord, body.place_id) is None:
            raise HTTPException(status_code=404, detail="현장 제보를 연결할 관광지를 찾을 수 없습니다.")
        observed_at = _validate_live_observed_at(body.observed_at)
    else:
        observed_at = None

    post = CommunityPostRecord(
        author_user_id=current_user.user_id,
        post_type=body.post_type.value,
        title=_default_title(db, body, snapshot),
        content=body.content.strip(),
        image_urls=body.image_urls,
        tags=body.tags,
        related_place_ids=body.related_place_ids,
        course_snapshot=snapshot if body.post_type == CommunityPostType.course else None,
        source_journey_id=body.source_journey_id if body.post_type == CommunityPostType.course else None,
        course_rating=body.course_rating if body.post_type == CommunityPostType.course else None,
        travel_date=body.travel_date if body.post_type == CommunityPostType.course else None,
        place_id=body.place_id if body.post_type == CommunityPostType.live else None,
        crowd_percent=body.crowd_percent if body.post_type == CommunityPostType.live else None,
        observed_at=observed_at,
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return _response(db, post, current_user.user_id)


@community_router.get("/posts", response_model=CommunityPostListResponse)
def list_posts(
    post_type: CommunityPostType | None = None,
    sort: Literal["latest", "recommended"] = "latest",
    place_id: str | None = None,
    author_user_id: str | None = None,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    filters = []
    if post_type is not None:
        filters.append(CommunityPostRecord.post_type == post_type.value)
    if place_id:
        filters.append(
            (CommunityPostRecord.place_id == place_id)
            | CommunityPostRecord.related_place_ids.cast(String).contains(f'"{place_id}"')
        )
    if author_user_id:
        filters.append(CommunityPostRecord.author_user_id == author_user_id)

    total = int(
        db.scalar(
            select(func.count()).select_from(CommunityPostRecord).where(*filters)
        )
        or 0
    )

    query = select(CommunityPostRecord).where(*filters)
    if sort == "recommended":
        recommend_count = (
            select(func.count())
            .select_from(CommunityRecommendationRecord)
            .where(CommunityRecommendationRecord.post_id == CommunityPostRecord.post_id)
            .correlate(CommunityPostRecord)
            .scalar_subquery()
        )
        query = query.order_by(recommend_count.desc(), CommunityPostRecord.created_at.desc())
    else:
        query = query.order_by(CommunityPostRecord.created_at.desc())

    rows = db.scalars(query.offset(offset).limit(limit)).all()
    return CommunityPostListResponse(
        items=[_response(db, row, current_user.user_id) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@community_router.get("/posts/mine", response_model=CommunityPostListResponse)
def list_my_posts(
    post_type: CommunityPostType | None = None,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    filters = [CommunityPostRecord.author_user_id == current_user.user_id]
    if post_type is not None:
        filters.append(CommunityPostRecord.post_type == post_type.value)
    total = int(db.scalar(select(func.count()).select_from(CommunityPostRecord).where(*filters)) or 0)
    rows = db.scalars(
        select(CommunityPostRecord)
        .where(*filters)
        .order_by(CommunityPostRecord.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return CommunityPostListResponse(
        items=[_response(db, row, current_user.user_id) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@community_router.get("/posts/{post_id}", response_model=CommunityPostResponse)
def get_post(
    post_id: str,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _response(db, _load_post(db, post_id), current_user.user_id)


@community_router.patch("/posts/{post_id}", response_model=CommunityPostResponse)
def update_post(
    post_id: str,
    body: CommunityPostUpdate,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    post = _load_post(db, post_id)
    if post.author_user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="내가 작성한 글만 수정할 수 있습니다.")

    values = body.model_dump(exclude_unset=True)
    for field, value in values.items():
        if field == "course_rating" and post.post_type != CommunityPostType.course.value:
            continue
        if field == "travel_date" and post.post_type != CommunityPostType.course.value:
            continue
        setattr(post, field, value)
    post.updated_at = _now()
    db.commit()
    db.refresh(post)
    return _response(db, post, current_user.user_id)


@community_router.delete("/posts/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_post(
    post_id: str,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    post = _load_post(db, post_id)
    if post.author_user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="내가 작성한 글만 삭제할 수 있습니다.")

    db.execute(delete(CommunityRecommendationRecord).where(CommunityRecommendationRecord.post_id == post_id))
    db.execute(delete(CommunityCommentRecord).where(CommunityCommentRecord.post_id == post_id))
    db.execute(delete(CommunitySavedCourseRecord).where(CommunitySavedCourseRecord.source_post_id == post_id))
    db.delete(post)
    db.commit()
    return None


@community_router.post("/posts/{post_id}/recommend", response_model=RecommendationResponse)
def recommend_post(
    post_id: str,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _load_post(db, post_id)
    key = _recommendation_key(post_id, current_user.user_id)
    if db.get(CommunityRecommendationRecord, key) is None:
        db.add(
            CommunityRecommendationRecord(
                recommendation_key=key,
                post_id=post_id,
                user_id=current_user.user_id,
            )
        )
        db.commit()
    return RecommendationResponse(
        post_id=post_id,
        recommended=True,
        recommendation_count=_recommend_count(db, post_id),
    )


@community_router.delete("/posts/{post_id}/recommend", response_model=RecommendationResponse)
def unrecommend_post(
    post_id: str,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _load_post(db, post_id)
    key = _recommendation_key(post_id, current_user.user_id)
    row = db.get(CommunityRecommendationRecord, key)
    if row is not None:
        db.delete(row)
        db.commit()
    return RecommendationResponse(
        post_id=post_id,
        recommended=False,
        recommendation_count=_recommend_count(db, post_id),
    )


@community_router.get("/posts/{post_id}/comments", response_model=list[CommentResponse])
def list_comments(
    post_id: str,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _load_post(db, post_id)
    rows = db.scalars(
        select(CommunityCommentRecord)
        .where(CommunityCommentRecord.post_id == post_id)
        .order_by(CommunityCommentRecord.created_at.asc())
    ).all()
    return [
        CommentResponse(
            comment_id=row.comment_id,
            post_id=row.post_id,
            author=_author(db, row.author_user_id),
            content=row.content,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


@community_router.post("/posts/{post_id}/comments", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
def create_comment(
    post_id: str,
    body: CommentCreate,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _load_post(db, post_id)
    row = CommunityCommentRecord(
        post_id=post_id,
        author_user_id=current_user.user_id,
        content=body.content.strip(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return CommentResponse(
        comment_id=row.comment_id,
        post_id=row.post_id,
        author=_author(db, row.author_user_id),
        content=row.content,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@community_router.patch("/comments/{comment_id}", response_model=CommentResponse)
def update_comment(
    comment_id: str,
    body: CommentUpdate,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.get(CommunityCommentRecord, comment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="댓글을 찾을 수 없습니다.")
    if row.author_user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="내 댓글만 수정할 수 있습니다.")
    row.content = body.content.strip()
    row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return CommentResponse(
        comment_id=row.comment_id,
        post_id=row.post_id,
        author=_author(db, row.author_user_id),
        content=row.content,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@community_router.delete("/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_comment(
    comment_id: str,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = db.get(CommunityCommentRecord, comment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="댓글을 찾을 수 없습니다.")
    if row.author_user_id != current_user.user_id:
        raise HTTPException(status_code=403, detail="내 댓글만 삭제할 수 있습니다.")
    db.delete(row)
    db.commit()
    return None


@community_router.post("/posts/{post_id}/save-course", response_model=SavedCourseResponse, status_code=status.HTTP_201_CREATED)
def save_course_from_post(
    post_id: str,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    post = _load_post(db, post_id)
    if post.post_type != CommunityPostType.course.value or not post.course_snapshot:
        raise HTTPException(status_code=422, detail="코스 후기만 내 코스에 저장할 수 있습니다.")

    key = _save_key(post_id, current_user.user_id)
    existing = db.scalar(select(CommunitySavedCourseRecord).where(CommunitySavedCourseRecord.save_key == key))
    if existing is None:
        existing = CommunitySavedCourseRecord(
            save_key=key,
            user_id=current_user.user_id,
            source_post_id=post_id,
            course_snapshot=post.course_snapshot,
        )
        db.add(existing)
        db.commit()
        db.refresh(existing)

    return SavedCourseResponse(
        saved_course_id=existing.saved_course_id,
        source_post_id=existing.source_post_id,
        course_snapshot=existing.course_snapshot,
        created_at=existing.created_at,
    )


@community_router.delete("/posts/{post_id}/save-course", status_code=status.HTTP_204_NO_CONTENT)
def unsave_course_from_post(
    post_id: str,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    key = _save_key(post_id, current_user.user_id)
    row = db.scalar(select(CommunitySavedCourseRecord).where(CommunitySavedCourseRecord.save_key == key))
    if row is not None:
        db.delete(row)
        db.commit()
    return None


@community_router.get("/saved-courses", response_model=list[SavedCourseResponse])
def list_saved_courses(
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(CommunitySavedCourseRecord)
        .where(CommunitySavedCourseRecord.user_id == current_user.user_id)
        .order_by(CommunitySavedCourseRecord.created_at.desc())
    ).all()
    return [
        SavedCourseResponse(
            saved_course_id=row.saved_course_id,
            source_post_id=row.source_post_id,
            course_snapshot=row.course_snapshot,
            created_at=row.created_at,
        )
        for row in rows
    ]


@community_router.get("/posts/{post_id}/course-copy", response_model=CourseCopyResponse)
def get_course_copy_seed(
    post_id: str,
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    post = _load_post(db, post_id)
    if post.post_type != CommunityPostType.course.value or not post.course_snapshot:
        raise HTTPException(status_code=422, detail="코스 후기에서만 코스를 따라갈 수 있습니다.")

    place_ids = _snapshot_place_ids(post.course_snapshot)
    current_places: list[CourseCopyPlaceResponse] = []
    missing: list[str] = []
    for place_id in place_ids:
        place = db.get(PlaceRecord, place_id)
        if place is None:
            missing.append(place_id)
            current_places.append(CourseCopyPlaceResponse(place_id=place_id, found=False))
            continue
        current_places.append(
            CourseCopyPlaceResponse(
                place_id=place.place_id,
                found=True,
                title=place.title,
                category=place.category,
                latitude=place.latitude,
                longitude=place.longitude,
                current_data=place.data,
                data_updated_at=place.updated_at,
            )
        )

    author = _author(db, post.author_user_id)
    snapshot_rows = _snapshot_place_rows(post.course_snapshot)
    source_place_names = [
        name
        for name in (_snapshot_place_name(item) for item in snapshot_rows)
        if name
    ]
    service_kinds = {
        kind
        for kind in (_snapshot_service_kind(item) for item in snapshot_rows)
        if kind
    }
    attraction_names = [
        _snapshot_place_name(item)
        for item in snapshot_rows
        if _snapshot_place_name(item) and _snapshot_service_kind(item) is None
    ]
    suggested_minutes = _snapshot_total_minutes(post.course_snapshot)

    return CourseCopyResponse(
        source_post_id=post.post_id,
        source_author_nickname=author.nickname,
        course_snapshot=post.course_snapshot,
        place_ids=place_ids,
        source_place_names=source_place_names,
        current_places=current_places,
        missing_place_ids=missing,
        include_food="food" in service_kinds,
        include_cafe="cafe" in service_kinds,
        suggested_available_minutes=suggested_minutes,
        same_course_request_patch={
            "required_place_names": attraction_names,
            "include_food": "food" in service_kinds,
            "include_cafe": "cafe" in service_kinds,
            **(
                {"available_minutes": suggested_minutes}
                if suggested_minutes is not None
                else {}
            ),
        },
        requires_live_refresh=True,
        message=(
            "게시 당시 코스를 그대로 저장할 수 있고, 다시 추천할 때는 "
            "same_course_request_patch에 현재 출발 위치·이동수단을 더해 "
            "현재 V2.4.4 혼잡도와 최근 현장 제보를 반영해 재계산할 수 있습니다."
        ),
    )


@community_router.get("/places/{place_id}/live-summary", response_model=LiveSummaryResponse)
def live_summary(
    place_id: str,
    hours: int = Query(default=2, ge=1, le=6),
    current_user: UserRecord = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    place = db.get(PlaceRecord, place_id)
    if place is None:
        raise HTTPException(status_code=404, detail="관광지를 찾을 수 없습니다.")

    cutoff = _now() - timedelta(hours=hours)
    rows = db.scalars(
        select(CommunityPostRecord)
        .where(
            CommunityPostRecord.post_type == CommunityPostType.live.value,
            CommunityPostRecord.place_id == place_id,
            CommunityPostRecord.observed_at >= cutoff,
        )
        .order_by(CommunityPostRecord.observed_at.desc())
    ).all()

    percents = [row.crowd_percent for row in rows if row.crowd_percent is not None]
    average = round(sum(percents) / len(percents), 1) if percents else None
    latest = max((_aware(row.observed_at) for row in rows if row.observed_at), default=None)

    raw_score = None
    if isinstance(place.data, dict):
        raw_score = place.data.get("official_congestion_score")
        if raw_score is None:
            raw_score = place.data.get("congestion_score")
    try:
        official_score = float(raw_score) if raw_score is not None else None
    except (TypeError, ValueError):
        official_score = None

    return LiveSummaryResponse(
        place_id=place.place_id,
        place_title=place.title,
        hours=hours,
        report_count=len(rows),
        community_average_percent=average,
        community_bucket=_crowd_bucket(average) if average is not None else None,
        latest_observed_at=latest,
        official_congestion_score=official_score,
        recent_reports=[_response(db, row, current_user.user_id) for row in rows[:5]],
    )
