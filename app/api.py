from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from .clients import (
    CongestionClient,
    HubTourClient,
    IntegrationError,
    NaverClient,
    RelatedTourClient,
    TourApiClient,
    WeatherClient,
    normalize_name,
)
from .config import Settings, get_settings
from .location_policy import GYEONGJU_CENTER_LATITUDE, GYEONGJU_CENTER_LONGITUDE
from .db import get_db
from .geo import haversine_km
from .schemas import (
    EtiquetteRequest,
    EtiquetteResponse,
    HealthResponse,
    JourneyCreate,
    JourneyOut,
    ModifyCourseRequest,
    Place,
    PlaceContentResponse,
    RagSearchRequest,
    RagSearchResponse,
    RecalculateRequest,
    RecommendRequest,
    RecommendResponse,
    SyncResponse,
    VisitCheckRequest,
    VisitCheckResponse,
)
from .services import (
    ContentService,
    JourneyService,
    RagService,
    RecommendationService,
    SyncService,
)

router = APIRouter()

# ---------------------------------------------------------------------------
# 홈/지도 장소 목록 혼잡도 캐시
#
# 같은 위치/반경/개수 요청에 대해 외부 API를 매번 다시 호출하지 않도록
# Settings.cache_ttl_seconds 동안 결과를 재사용합니다.
# ---------------------------------------------------------------------------

_places_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _places_cache_key(
    latitude: float,
    longitude: float,
    radius_km: float,
    limit: int,
) -> str:
    return (
        f"{round(latitude, 3)}:"
        f"{round(longitude, 3)}:"
        f"{round(radius_km, 1)}:"
        f"{limit}"
    )


def _cache_get(
    key: str,
    ttl_seconds: int,
) -> list[Place] | None:
    cached = _places_cache.get(key)

    if cached is None:
        return None

    saved_at, raw_places = cached

    if time.time() - saved_at > ttl_seconds:
        _places_cache.pop(key, None)
        return None

    return [
        Place.model_validate(item)
        for item in raw_places
    ]


def _cache_put(
    key: str,
    places: list[Place],
) -> None:
    _places_cache[key] = (
        time.time(),
        [
            place.model_dump(mode="json")
            for place in places
        ],
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    tags=["system"],
)
def health(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    try:
        db.execute(text("SELECT 1"))
        database = "ok"
    except Exception:
        database = "error"

    integrations = {
        "tour_api": (
            "configured"
            if settings.public_data_service_key
            else "not_configured"
        ),
        "congestion_api": (
            "configured"
            if settings.public_data_service_key
            else "not_configured"
        ),
        "kakao_route": (
            "configured"
            if settings.kakao_rest_api_key
            else "not_configured"
        ),
        "weather_api": (
            "configured"
            if settings.kma_service_key
            else "not_configured"
        ),
        "openai": (
            "configured"
            if settings.openai_api_key
            else "not_configured"
        ),
        "naver": (
            "configured"
            if settings.naver_client_id
            and settings.naver_client_secret
            else "not_configured"
        ),
        "youtube": (
            "configured"
            if settings.youtube_api_key
            else "not_configured"
        ),
    }

    return HealthResponse(
        status="ok" if database == "ok" else "degraded",
        version=settings.app_version,
        database=database,
        integrations=integrations,
    )


@router.get(
    "/api/v1/places",
    response_model=list[Place],
    tags=["places"],
)
async def places(
    radius_km: float = Query(8, gt=0, le=20),
    limit: int = Query(30, ge=1, le=100),
    settings: Settings = Depends(get_settings),
):
    """
    홈/지도용 관광지 목록.

    관광공사 장소 목록만 반환하지 않고 추천 코스와 동일한 방식으로
    공식 혼잡도 + NAVER 검색 트렌드 + 시간/요일 + 날씨를 결합해
    congestion_score를 계산해서 반환합니다.

    congestion_score의 기준은 0~100입니다.
    0 = 매우 한산 / 100 = 매우 혼잡
    """

    latitude = GYEONGJU_CENTER_LATITUDE
    longitude = GYEONGJU_CENTER_LONGITUDE

    cache_key = _places_cache_key(
        latitude,
        longitude,
        radius_km,
        limit,
    )

    cached = _cache_get(
        cache_key,
        settings.cache_ttl_seconds,
    )

    if cached is not None:
        return cached

    tour = TourApiClient(settings)
    congestion_client = CongestionClient(settings)
    weather_client = WeatherClient(settings)
    naver = NaverClient(settings)

    # 코스 추천과 동일한 혼잡도 계산 함수를 사용하기 위한 서비스 객체
    recommendation = RecommendationService(settings)

    try:
        result = await tour.nearby_places(
            latitude,
            longitude,
            int(radius_km * 1000),
            limit,
        )

        if not result:
            return []

        # 거리 계산
        for place in result:
            place.distance_km = round(
                haversine_km(
                    latitude,
                    longitude,
                    place.latitude,
                    place.longitude,
                ),
                3,
            )

        # ---------------------------------------------------------------
        # 1. 관광공사 공식 혼잡도
        # ---------------------------------------------------------------

        congestion_map: dict[
            str,
            tuple[float, str | None],
        ] = {}

        try:
            congestion_map = await congestion_client.score_map()
        except IntegrationError:
            # 공식 혼잡 API 장애가 홈 전체를 막지는 않게 합니다.
            congestion_map = {}

        for place in result:
            matched = congestion_map.get(
                normalize_name(place.title)
            )

            if matched:
                place.official_congestion_score = matched[0]
                place.congestion_date = matched[1]

        # ---------------------------------------------------------------
        # 2. 현재 날씨
        # ---------------------------------------------------------------

        weather: dict[str, Any] = {}

        try:
            weather = await weather_client.current(
                latitude,
                longitude,
            )
        except IntegrationError:
            weather = {}

        # ---------------------------------------------------------------
        # 3. NAVER DataLab 검색 트렌드
        #
        # 장소가 많으면 요청이 늘어나므로 홈에서는 우선 최대 15곳을
        # DataLab으로 보강하고, 나머지는 공식+시간+날씨 신호로 계산합니다.
        # ---------------------------------------------------------------

        trend_scores: dict[str, float] = {}

        try:
            trend_scores = await naver.trend_scores(
                [
                    place.title
                    for place in result[:15]
                ]
            )
        except IntegrationError:
            trend_scores = {}

        # ---------------------------------------------------------------
        # 4. 코스 추천과 동일한 예상 혼잡도 산식 적용
        # ---------------------------------------------------------------

        now = datetime.now().astimezone()

        for place in result:
            place.trend_score = trend_scores.get(
                place.title
            )

            recommendation._calculate_congestion(
                place,
                now,
                weather,
            )

        # 한산한 장소가 먼저 보이도록 정렬
        result.sort(
            key=lambda place: (
                place.congestion_score
                if place.congestion_score is not None
                else 101.0
            )
        )

        _cache_put(
            cache_key,
            result,
        )

        return result

    except IntegrationError as exc:
        raise HTTPException(
            exc.status_code,
            detail={
                "service": exc.service,
                "message": str(exc),
            },
        ) from exc


@router.get(
    "/api/v1/places/{place_id}",
    response_model=Place,
    tags=["places"],
)
async def place_detail(
    place_id: str,
    content_type_id: str = Query("12"),
    settings: Settings = Depends(get_settings),
):
    client = TourApiClient(settings)

    try:
        placeholder = Place(
            place_id=place_id,
            content_type_id=content_type_id,
            title=place_id,
            latitude=0,
            longitude=0,
        )

        return await client.detail(
            placeholder
        )

    except IntegrationError as exc:
        raise HTTPException(
            exc.status_code,
            detail={
                "service": exc.service,
                "message": str(exc),
            },
        ) from exc


@router.post(
    "/api/v1/courses/recommend",
    response_model=RecommendResponse,
    tags=["courses"],
)
async def recommend(
    body: RecommendRequest,
    settings: Settings = Depends(get_settings),
):
    try:
        return await RecommendationService(
            settings
        ).recommend(body)

    except IntegrationError as exc:
        raise HTTPException(
            exc.status_code,
            detail={
                "service": exc.service,
                "message": str(exc),
            },
        ) from exc

    except ValueError as exc:
        raise HTTPException(
            422,
            detail=str(exc),
        ) from exc


@router.post(
    "/api/v1/courses/modify",
    tags=["courses"],
)
async def modify(
    body: ModifyCourseRequest,
    settings: Settings = Depends(get_settings),
):
    try:
        return await RecommendationService(
            settings
        ).modify(
            body.course,
            body.command,
            body.user_context,
        )

    except (
        IntegrationError,
        ValueError,
    ) as exc:
        status = (
            exc.status_code
            if isinstance(
                exc,
                IntegrationError,
            )
            else 422
        )

        raise HTTPException(
            status,
            detail=str(exc),
        ) from exc


@router.post(
    "/api/v1/courses/recalculate",
    tags=["courses"],
)
async def recalculate(
    body: RecalculateRequest,
    settings: Settings = Depends(get_settings),
):
    try:
        return await RecommendationService(
            settings
        ).recalculate(body)

    except (
        IntegrationError,
        ValueError,
    ) as exc:
        status = (
            exc.status_code
            if isinstance(
                exc,
                IntegrationError,
            )
            else 422
        )

        raise HTTPException(
            status,
            detail=str(exc),
        ) from exc


@router.get(
    "/api/v1/content/{place_id}",
    response_model=PlaceContentResponse,
    tags=["content"],
)
async def content(
    place_id: str,
    title: str = Query(..., min_length=1),
    settings: Settings = Depends(get_settings),
):
    blogs, videos, _ = await ContentService(
        settings
    ).get(
        place_id,
        title,
    )

    return PlaceContentResponse(
        place_id=place_id,
        blogs=blogs,
        videos=videos,
    )


@router.get(
    "/api/v1/etiquette/place/{place_id}",
    tags=["etiquette"],
)
async def etiquette_for_place(
    place_id: str,
    content_type_id: str = Query("12"),
    settings: Settings = Depends(get_settings),
):
    """Return etiquette tips for a selected public tourist place.

    No live user GPS is accepted or transmitted.
    """
    tips = [
        "문화재와 시설물을 만지거나 훼손하지 마세요.",
        "촬영 제한 표지와 관람 동선을 지켜주세요.",
        "주변 관람객과 주민을 위해 큰 소리를 줄여주세요.",
    ]
    try:
        placeholder = Place(
            place_id=place_id,
            content_type_id=content_type_id,
            title=place_id,
            latitude=GYEONGJU_CENTER_LATITUDE,
            longitude=GYEONGJU_CENTER_LONGITUDE,
        )
        place = await TourApiClient(settings).detail(placeholder)
        if "사" in place.title or "암" in place.title:
            tips.append(
                "사찰에서는 법회와 참배를 방해하지 않도록 복장과 소음을 조심하세요."
            )
        return {"place_id": place_id, "messages": tips}
    except IntegrationError as exc:
        raise HTTPException(
            exc.status_code,
            detail={"service": exc.service, "message": str(exc)},
        ) from exc


@router.post(
    "/api/v1/journeys",
    response_model=JourneyOut,
    tags=["journeys"],
)
def create_journey(
    body: JourneyCreate,
    db: Session = Depends(get_db),
):
    return JourneyService(
        db
    ).create(
        body.course
    )


@router.get(
    "/api/v1/journeys/{journey_id}",
    response_model=JourneyOut,
    tags=["journeys"],
)
def get_journey(
    journey_id: str,
    db: Session = Depends(get_db),
):
    try:
        return JourneyService(
            db
        ).get(
            journey_id
        )

    except KeyError as exc:
        raise HTTPException(
            404,
            detail="여행을 찾을 수 없습니다.",
        ) from exc


@router.post(
    "/api/v1/journeys/{journey_id}/visits",
    response_model=VisitCheckResponse,
    tags=["journeys"],
)
def visit(
    journey_id: str,
    body: VisitCheckRequest,
    db: Session = Depends(get_db),
):
    try:
        return JourneyService(
            db
        ).visit(
            journey_id,
            body.place_id,
            body.verified_on_device,
        )

    except KeyError as exc:
        raise HTTPException(
            404,
            detail="여행을 찾을 수 없습니다.",
        ) from exc

    except ValueError as exc:
        raise HTTPException(
            422,
            detail=str(exc),
        ) from exc


@router.get(
    "/api/v1/congestion/{place_name}",
    tags=["congestion"],
)
async def congestion(
    place_name: str,
    settings: Settings = Depends(get_settings),
):
    try:
        rows = await CongestionClient(
            settings
        ).list(
            place_name
        )

        return {
            "place_name": place_name,
            "items": rows,
        }

    except IntegrationError as exc:
        raise HTTPException(
            exc.status_code,
            detail={
                "service": exc.service,
                "message": str(exc),
            },
        ) from exc


@router.get(
    "/api/v1/weather/current",
    tags=["weather"],
)
async def current_weather(
    settings: Settings = Depends(get_settings),
):
    try:
        return await WeatherClient(
            settings
        ).current(
            GYEONGJU_CENTER_LATITUDE,
            GYEONGJU_CENTER_LONGITUDE,
        )

    except IntegrationError as exc:
        raise HTTPException(
            exc.status_code,
            detail={
                "service": exc.service,
                "message": str(exc),
            },
        ) from exc


@router.get(
    "/api/v1/insights/related",
    tags=["tourism-insights"],
)
async def related_tourism(
    base_ym: str | None = None,
    settings: Settings = Depends(get_settings),
):
    try:
        return {
            "items": await RelatedTourClient(
                settings
            ).related(
                base_ym
            )
        }

    except IntegrationError as exc:
        raise HTTPException(
            exc.status_code,
            detail={
                "service": exc.service,
                "message": str(exc),
            },
        ) from exc


@router.get(
    "/api/v1/insights/hubs",
    tags=["tourism-insights"],
)
async def hub_tourism(
    base_ym: str | None = None,
    settings: Settings = Depends(get_settings),
):
    try:
        return {
            "items": await HubTourClient(
                settings
            ).hubs(
                base_ym
            )
        }

    except IntegrationError as exc:
        raise HTTPException(
            exc.status_code,
            detail={
                "service": exc.service,
                "message": str(exc),
            },
        ) from exc


@router.post(
    "/api/v1/rag/search",
    response_model=RagSearchResponse,
    tags=["rag"],
)
async def rag_search(
    body: RagSearchRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    try:
        return await RagService(
            settings,
            db,
        ).search(
            body.query,
            body.top_k,
        )

    except IntegrationError as exc:
        raise HTTPException(
            exc.status_code,
            detail={
                "service": exc.service,
                "message": str(exc),
            },
        ) from exc

    except ValueError as exc:
        raise HTTPException(
            422,
            detail=str(exc),
        ) from exc


@router.post(
    "/api/v1/admin/sync",
    response_model=SyncResponse,
    tags=["admin"],
)
async def sync(
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    try:
        return await SyncService(
            settings,
            db,
        ).sync()

    except IntegrationError as exc:
        raise HTTPException(
            exc.status_code,
            detail={
                "service": exc.service,
                "message": str(exc),
            },
        ) from exc