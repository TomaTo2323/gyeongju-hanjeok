from __future__ import annotations

from datetime import datetime
from enum import Enum, StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class TransportMode(str, Enum):
    walking = "walking"
    public_transport = "public_transport"
    driving = "driving"


class CourseType(StrEnum):
    travel_min = "travel_min"
    congestion_avoidance = "congestion_avoidance"
    preference_fit = "preference_fit"


class Place(BaseModel):
    place_id: str
    content_type_id: str | None = None
    title: str
    category: str = "기타"
    address: str | None = None
    latitude: float
    longitude: float
    image_url: str | None = None
    thumbnail_url: str | None = None
    overview: str | None = None
    tel: str | None = None
    homepage: str | None = None
    kakao_place_url: str | None = None

    # 음식점/카페 메뉴
    representative_menu: str | None = None
    menu_items: list[dict[str, Any]] = Field(default_factory=list)

    # 상세화면 관련 콘텐츠 (정확한 장소명 검증 후에만 포함)
    content_links: list[dict[str, Any]] = Field(default_factory=list)

    # 관광공사/보완 상세정보
    operating_hours: str | None = None
    break_time: str | None = None
    rest_date: str | None = None
    fee_text: str | None = None
    is_free: bool | None = None
    parking: str | None = None

    # 각 상세정보의 실제 출처
    overview_source: str | None = None
    fee_source: str | None = None
    operating_hours_source: str | None = None
    rest_date_source: str | None = None
    parking_source: str | None = None
    info_sources: list[str] = Field(default_factory=list)
    info_confidence: str | None = None

    # 프론트 표시용 한국어 라벨
    # high    -> 공식 정보
    # medium  -> 공식·보조 정보
    # low     -> 참고 정보
    # unknown -> 확인 필요
    info_confidence_label: str = "확인 필요"

    info_enriched_at: datetime | None = None

    # 혼잡도/추천 신호
    official_congestion_score: float | None = None
    # V2.4.4 자체 계산 결과. 커뮤니티 제보로 덮어쓰지 않습니다.
    congestion_score: float | None = None
    congestion_date: str | None = None
    congestion_components: dict[str, float] = Field(default_factory=dict)

    # 최근 커뮤니티 "지금 여기" 제보를 별도로 집계한 값입니다.
    # 원본 V2.4.4는 보존하고, 코스 추천에서만 제한적인 실시간 보정에 사용합니다.
    community_congestion_score: float | None = None
    community_report_count: int = 0
    community_latest_observed_at: datetime | None = None
    routing_congestion_score: float | None = None
    trend_score: float | None = None
    review_score: float | None = None
    preference_score: float | None = None
    distance_km: float | None = None
    estimated_travel_minutes: int | None = None
    recommendation_score: float | None = None
    recommendation_reason: str | None = None

    is_night_spot: bool = False
    is_rest_point: bool = False
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)


class CoursePlace(Place):
    order: int
    arrival_time: datetime | None = None
    stay_minutes: int = 45
    travel_minutes_from_previous: int = 0
    travel_distance_m_from_previous: int = 0
    transfers_from_previous: int = 0

    # 이동수단별 요금
    # public_transport: 버스/지하철 등 대중교통 예상요금
    # driving: Kakao Mobility가 제공하는 택시 예상요금/통행료
    fare_from_previous: int | None = None
    taxi_fare_from_previous: int | None = None
    toll_fare_from_previous: int | None = None

    navigation_url: str | None = None

    # driving          -> kakao_navi_sdk
    # walking/transit  -> kakao_map
    navigation_provider: str = "kakao"
    navigation_available: bool = True

    visited: bool = False


class Course(BaseModel):
    course_id: str
    title: str
    type: CourseType
    total_minutes: int
    total_distance_km: float
    objective_values: dict[str, float]
    places: list[CoursePlace]
    weather_summary: str | None = None
    warnings: list[str] = Field(default_factory=list)


class RecommendRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    start_time: datetime | None = None
    available_minutes: int = Field(default=240, ge=60, le=1440)
    transport: TransportMode = TransportMode.walking
    radius_km: float = Field(default=8, gt=0, le=30)
    preferences: list[str] = Field(default_factory=list)

    # 프론트 코스 생성 옵션
    weather_aware: bool = True
    include_rest_stops: bool = True
    include_food: bool = False
    include_cafe: bool = False
    memo: str = ""
    visited_place_ids: list[str] = Field(default_factory=list)

    free_only: bool = False
    indoor_preferred: bool = False
    required_place_names: list[str] = Field(default_factory=list)
    excluded_place_names: list[str] = Field(default_factory=list)
    desired_course_count: int = Field(default=3, ge=1, le=3)
    seed: int | None = None

    @field_validator(
        "preferences",
        "required_place_names",
        "excluded_place_names",
        "visited_place_ids",
    )
    @classmethod
    def clean_list(cls, values: list[str]) -> list[str]:
        return list(
            dict.fromkeys(
                value.strip()
                for value in values
                if value.strip()
            )
        )


class RecommendResponse(BaseModel):
    generated_at: datetime
    source: str = "live_external_apis"
    courses: list[Course]
    applied_filters: list[str]
    unavailable_integrations: list[str] = Field(default_factory=list)


class ModifyCourseRequest(BaseModel):
    course: Course
    command: str = Field(min_length=1, max_length=500)
    user_context: RecommendRequest


class RecalculateRequest(BaseModel):
    course: Course
    current_latitude: float
    current_longitude: float
    remaining_available_minutes: int = Field(ge=30, le=1440)
    transport: TransportMode
    preferences: list[str] = Field(default_factory=list)


class JourneyCreate(BaseModel):
    course: Course


class JourneyOut(BaseModel):
    journey_id: str
    course: Course
    started_at: datetime
    updated_at: datetime
    completed_place_ids: list[str] = Field(default_factory=list)


class VisitCheckRequest(BaseModel):
    place_id: str
    current_latitude: float
    current_longitude: float
    dwell_minutes: int = Field(ge=0)


class VisitCheckResponse(BaseModel):
    completed: bool
    distance_m: float
    reason: str


class ContentItem(BaseModel):
    title: str
    url: str
    description: str | None = None
    thumbnail_url: str | None = None
    published_at: str | None = None
    source: str


class PlaceContentResponse(BaseModel):
    place_id: str
    blogs: list[ContentItem] = Field(default_factory=list)
    videos: list[ContentItem] = Field(default_factory=list)


class EtiquetteRequest(BaseModel):
    latitude: float
    longitude: float
    radius_m: int = Field(default=100, ge=30, le=500)


class EtiquetteResponse(BaseModel):
    nearby_places: list[Place]
    messages: list[str]


class SyncResponse(BaseModel):
    fetched: int
    stored: int
    embedded: int
    started_at: datetime
    completed_at: datetime


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str
    integrations: dict[str, str]


class RagSearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=500)
    top_k: int = Field(default=5, ge=1, le=10)


class RagHit(BaseModel):
    place_id: str
    title: str
    category: str
    similarity: float
    overview: str | None = None
    address: str | None = None


class RagSearchResponse(BaseModel):
    query: str
    answer: str
    hits: list[RagHit]