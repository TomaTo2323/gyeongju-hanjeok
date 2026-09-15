from __future__ import annotations

from datetime import datetime, timezone
import json
import asyncio
import random
import time
import re
import secrets
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .clients import (
    CongestionClient,
    IntegrationError,
    KakaoLocalClient,
    NaverClient,
    OpenAIClient,
    RegionalVisitorClient,
    TourApiClient,
    WeatherClient,
    normalize_name,
)
from .config import Settings, get_settings
from .location_policy import GYEONGJU_CENTER_LATITUDE, GYEONGJU_CENTER_LONGITUDE
from .enrichment import PlaceInfoEnricher
from .geo import haversine_km
from .schemas import Course, CoursePlace, CourseType, Place, RecommendRequest, TransportMode
from .services import (
    ContentService,
    RecommendationService,
    _apply_community_live_signal,
    _community_live_signal_map,
    _gyeongju_route_pool,
    is_user_facing_travel_place,
    select_representative_start_place,
    recommended_stay_minutes,
    recommended_time_label,
)

compat_router = APIRouter(tags=["frontend-compat"])

DEFAULT_LATITUDE = GYEONGJU_CENTER_LATITUDE
DEFAULT_LONGITUDE = GYEONGJU_CENTER_LONGITUDE


PLACE_DETAIL_CACHE_TTL_SECONDS = 15 * 60

async def _nearest_tourist_pool(
    settings: Settings,
) -> list[Place]:
    # 코스 추천과 동일한 30분 경주시 전체 관광지 캐시를 공유합니다.
    # 출발 위치 선택 화면에서 한 번 불러오면 코스 생성 때 다시
    # areaBasedList2 전체 페이지를 받을 필요가 없습니다.
    return await _gyeongju_route_pool(
        TourApiClient(
            settings
        )
    )


_PLACE_DETAIL_CORE_CACHE: dict[
    str,
    tuple[float, Place],
] = {}

_PLACE_DETAIL_FULL_CACHE: dict[
    str,
    tuple[float, Place],
] = {}


def _place_detail_cache_key(
    place_id: str,
    title: str,
) -> str:
    normalized_title = normalize_name(
        title or ""
    )

    return (
        f"{place_id.strip()}::"
        f"{normalized_title}"
    )


def _place_detail_cache_get(
    cache: dict[
        str,
        tuple[float, Place],
    ],
    key: str,
) -> Place | None:
    cached = cache.get(key)

    if cached is None:
        return None

    cached_at, place = cached

    if (
        time.monotonic()
        - cached_at
        > PLACE_DETAIL_CACHE_TTL_SECONDS
    ):
        cache.pop(
            key,
            None,
        )
        return None

    return place.model_copy(
        deep=True
    )


def _place_detail_cache_set(
    cache: dict[
        str,
        tuple[float, Place],
    ],
    key: str,
    place: Place,
) -> None:
    cache[key] = (
        time.monotonic(),
        place.model_copy(
            deep=True
        ),
    )



DEBUG_CONGESTION_PLACE_NAMES = [
    "첨성대",
    "대릉원",
    "불국사",
    "동궁과 월지",
    "월정교",
    "국립경주박물관",
    "보문호",
    "탈해왕릉",
]


DEBUG_CONGESTION_ALIASES: dict[str, tuple[str, ...]] = {
    "첨성대": (
        "첨성대",
        "경주 첨성대",
    ),
    "대릉원": (
        "대릉원",
        "경주 대릉원",
        "천마총(대릉원)",
        "천마총 대릉원",
    ),
    "불국사": (
        "불국사",
        "경주 불국사",
    ),
    "동궁과 월지": (
        "동궁과 월지",
        "경주 동궁과 월지",
        "안압지",
    ),
    "월정교": (
        "월정교",
        "경주 월정교",
    ),
    "국립경주박물관": (
        "국립경주박물관",
        "경주 국립경주박물관",
    ),
    "보문호": (
        "보문호",
        "경주 보문호",
        "보문호수",
    ),
    "탈해왕릉": (
        "탈해왕릉",
        "경주 탈해왕릉",
    ),
}


class FrontRecommendRequest(BaseModel):
    # Live user GPS is never accepted by the backend.
    available_hours: float = Field(default=4, gt=0, le=24)

    @property
    def start_latitude(self) -> float:
        return DEFAULT_LATITUDE

    @property
    def start_longitude(self) -> float:
        return DEFAULT_LONGITUDE
    transport_type: str = "car"
    radius_km: float = Field(default=15, gt=0, le=30)
    preferred_categories: list[str] = Field(default_factory=list)
    avoid_paid: bool = False
    weather_aware: bool = True
    include_rest_stops: bool = True
    expected_include: str = ""
    expected_exclude: str = ""
    memo: str = ""
    visited_place_ids: list[str] = Field(default_factory=list)


class FrontRefreshRequest(BaseModel):
    route_id: str = ""
    reason: str = "manual_refresh"
    current_route: dict[str, Any] = Field(default_factory=dict)
    preferences: FrontRecommendRequest


class FrontModifyRequest(BaseModel):
    route_id: str = ""
    message: str = Field(min_length=1, max_length=500)
    current_route: dict[str, Any] = Field(default_factory=dict)
    preferences: FrontRecommendRequest | None = None


class FrontCheckInRequest(BaseModel):
    place_id: str = Field(min_length=1)


def _split_names(value: str) -> list[str]:
    normalized = value.replace("\n", ",").replace("/", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _resolve_transport_mode(value: str) -> TransportMode:
    normalized = value.strip().lower()

    if normalized in {
        "walk",
        "walking",
        "foot",
        "도보",
    }:
        return TransportMode.walking

    if normalized in {
        "public_transport",
        "publictransit",
        "transit",
        "bus",
        "대중교통",
    }:
        return TransportMode.public_transport

    return TransportMode.driving


def _memo_intents(value: str) -> list[str]:
    """
    자유 요청 문장을 preference 한 덩어리로 넣지 않고,
    현재 추천 엔진이 이해할 수 있는 구조화된 의도만 추출합니다.

    예:
    "부모님과 맛집 들렀다가 카페도 가고 싶어"
        -> ["맛집", "카페"]
    """
    text = value.strip().lower()
    if not text:
        return []

    intents: list[str] = []

    keyword_groups: tuple[
        tuple[str, tuple[str, ...]],
        ...,
    ] = (
        (
            "맛집",
            (
                "맛집",
                "음식",
                "식당",
                "식사",
                "점심",
                "저녁",
                "밥",
                "한식",
            ),
        ),
        (
            "카페",
            (
                "카페",
                "커피",
                "디저트",
                "베이커리",
            ),
        ),
        (
            "야경",
            (
                "야경",
                "야간",
                "밤",
            ),
        ),
        (
            "문화유산",
            (
                "문화유산",
                "문화재",
                "유적",
                "역사",
            ),
        ),
        (
            "자연",
            (
                "자연",
                "숲",
                "호수",
                "공원",
            ),
        ),
        (
            "산책",
            (
                "산책",
                "걷기",
                "걷는",
            ),
        ),
        (
            "실내",
            (
                "실내",
                "비 안 맞",
                "비를 피",
            ),
        ),
    )

    for canonical, keywords in keyword_groups:
        if any(keyword in text for keyword in keywords):
            intents.append(canonical)

    return intents


async def _apply_gpt_route_request(
    request: RecommendRequest,
    memo: str,
    settings: Settings,
    *,
    allow_service_changes: bool = False,
) -> RecommendRequest:
    """
    자유 요청이 있을 때만 GPT를 1회 호출합니다.
    GPT는 장소를 추천하는 것이 아니라 사용자의 문장을
    구조화된 코스 조건으로 변환하는 역할만 합니다.
    """
    text = memo.strip()

    if not text:
        return request

    try:
        parsed = await asyncio.wait_for(
            OpenAIClient(
                settings
            ).parse_route_request(
                text
            ),
            timeout=4.0,
        )
    except Exception as exc:
        # GPT/네트워크/JSON 파싱이 실패해도
        # 코스 생성 자체는 절대 실패시키지 않습니다.
        print(
            "[GPT ROUTE INTENT FALLBACK]"
            f" error={type(exc).__name__}: {exc}"
        )
        parsed = {}

    updated = request.model_copy(
        deep=True
    )

    # GPT가 실패하거나 장소명을 놓쳐도
    # "첨성대 가고 싶어", "경주월드 포함해줘" 같은 명확한 문장은
    # 필수 장소로 한 번 더 안전하게 해석합니다.
    simple_required = ""

    for suffix in (
        "가고 싶어요",
        "가고싶어요",
        "가고 싶어",
        "가고싶어",
        "포함해 주세요",
        "포함해주세요",
        "포함해줘",
        "넣어 주세요",
        "넣어줘",
    ):
        if suffix not in text:
            continue

        candidate = text.split(
            suffix,
            1,
        )[0].strip()

        candidate = re.sub(
            r"^(이번에는|이번엔|코스에|그리고|나는|저는)\s*",
            "",
            candidate,
        ).strip()

        if (
            2 <= len(candidate) <= 30
            and " " not in candidate
        ):
            simple_required = candidate

        break

    updated.required_place_names = list(
        dict.fromkeys(
            [
                *updated.required_place_names,
                *(
                    [simple_required]
                    if simple_required
                    else []
                ),
                *[
                    str(value).strip()
                    for value in (
                        parsed.get(
                            "required_places"
                        )
                        or []
                    )
                    if str(value).strip()
                ],
            ]
        )
    )

    updated.excluded_place_names = list(
        dict.fromkeys(
            [
                *updated.excluded_place_names,
                *[
                    str(value).strip()
                    for value in (
                        parsed.get(
                            "excluded_places"
                        )
                        or []
                    )
                    if str(value).strip()
                ],
            ]
        )
    )

    updated.preferences = list(
        dict.fromkeys(
            [
                *updated.preferences,
                *[
                    str(value).strip()
                    for value in (
                        parsed.get(
                            "preferences"
                        )
                        or []
                    )
                    if (
                        str(value).strip()
                        and (
                            allow_service_changes
                            or str(value).strip()
                            not in {
                                "맛집",
                                "카페",
                            }
                        )
                    )
                ],
            ]
        )
    )

    if allow_service_changes:
        lower_text = text.lower()

        remove_food = any(
            phrase in lower_text
            for phrase in (
                "맛집 빼",
                "식당 빼",
                "음식점 빼",
                "맛집 제외",
                "식당 제외",
            )
        )

        remove_cafe = any(
            phrase in lower_text
            for phrase in (
                "카페 빼",
                "카페 제외",
                "커피 빼",
            )
        )

        if remove_food:
            updated.include_food = False
            updated.preferences = [
                value
                for value in updated.preferences
                if value != "맛집"
            ]
        elif parsed.get(
            "include_food"
        ):
            updated.include_food = True

            if "맛집" not in updated.preferences:
                updated.preferences.append(
                    "맛집"
                )

        if remove_cafe:
            updated.include_cafe = False
            updated.preferences = [
                value
                for value in updated.preferences
                if value != "카페"
            ]
        elif parsed.get(
            "include_cafe"
        ):
            updated.include_cafe = True

            if "카페" not in updated.preferences:
                updated.preferences.append(
                    "카페"
                )

    if parsed.get(
        "free_only"
    ):
        updated.free_only = True

    if (
        parsed.get(
            "short_walk"
        )
        and "산책" not in updated.preferences
    ):
        # short_walk는 별도 스키마 필드가 없어 memo에 남기고
        # 산책을 강제 추가하지는 않습니다.
        pass

    print(
        "[GPT ROUTE INTENT]"
        f" memo={text!r}"
        f" required={updated.required_place_names}"
        f" excluded={updated.excluded_place_names}"
        f" preferences={updated.preferences}"
    )

    return updated


def _to_backend_request(
    body: FrontRecommendRequest,
    *,
    seed: int | None = None,
) -> RecommendRequest:
    transport = _resolve_transport_mode(
        body.transport_type
    )

    memo_intents = [
        intent
        for intent in _memo_intents(
            body.memo
        )
        if intent not in {
            "맛집",
            "카페",
        }
    ]

    selected_categories = list(
        body.preferred_categories
    )

    # 초기 코스의 맛집/카페 포함 여부는 사용자가 누른 칩만 따릅니다.
    include_food = (
        "맛집" in selected_categories
    )
    include_cafe = (
        "카페" in selected_categories
    )

    preferences = list(
        dict.fromkeys(
            [
                *selected_categories,
                *memo_intents,
            ]
        )
    )

    return RecommendRequest(
        available_minutes=max(
            60,
            round(
                body.available_hours * 60
            ),
        ),
        transport=transport,
        radius_km=30.0,
        preferences=preferences,
        weather_aware=body.weather_aware,
        include_rest_stops=body.include_rest_stops,
        include_food=include_food,
        include_cafe=include_cafe,
        memo=body.memo.strip(),
        visited_place_ids=list(
            dict.fromkeys(
                body.visited_place_ids
            )
        ),
        free_only=body.avoid_paid,
        indoor_preferred=(
            "실내" in preferences
        ),
        required_place_names=_split_names(
            body.expected_include
        ),
        excluded_place_names=_split_names(
            body.expected_exclude
        ),
        desired_course_count=1,
        seed=seed,
    )


def _quiet_score(congestion_score: float | None) -> int:
    """
    프론트 호환용 한적도.

    백엔드의 congestion_score는 이제 0~100 기준입니다.
    따라서 quiet_score = 100 - congestion_score 로 변환합니다.
    """
    if congestion_score is None:
        return 50

    return round(
        max(
            0.0,
            min(
                100.0,
                100.0 - congestion_score,
            ),
        )
    )


def _front_place_category(
    place: Place,
) -> str:
    raw = place.raw or {}

    service_kind = (
        raw.get("service_kind")
        if isinstance(raw, dict)
        else None
    )

    title = (
        place.title or ""
    ).lower()

    if (
        service_kind == "cafe"
        or (
            str(
                place.content_type_id
                or ""
            ) == "39"
            and any(
                keyword in title
                for keyword in (
                    "카페",
                    "커피",
                    "베이커리",
                    "디저트",
                    "찻집",
                    "다방",
                )
            )
        )
    ):
        return "카페"

    if (
        service_kind == "food"
        or str(
            place.content_type_id
            or ""
        ) == "39"
        or place.category == "음식점"
    ):
        return "맛집"

    if (
        place.category
        and place.category != "기타"
    ):
        return place.category

    return {
        "12": "관광지",
        "14": "문화시설",
        "25": "여행코스",
        "28": "레포츠",
    }.get(
        str(
            place.content_type_id
            or ""
        ),
        "관광지",
    )



_OVERVIEW_FEATURES = (
    "야경",
    "한옥",
    "산책",
    "벚꽃",
    "단풍",
    "전망",
    "사진",
    "포토존",
    "정원",
    "숲",
    "호수",
    "문화재",
    "유적",
    "왕릉",
    "고분",
    "박물관",
    "전시",
    "디저트",
    "케이크",
    "베이커리",
    "커피",
    "브런치",
    "밀면",
    "국밥",
    "한우",
    "빵",
)


def _overview_place_aliases(
    title: str,
) -> list[str]:
    normalized = normalize_name(title)

    aliases = [
        normalized
    ] if normalized else []

    for prefix in (
        "경주시",
        "경주",
    ):
        normalized_prefix = normalize_name(
            prefix
        )

        if (
            normalized.startswith(
                normalized_prefix
            )
            and len(normalized)
            > len(normalized_prefix) + 1
        ):
            aliases.append(
                normalized[
                    len(normalized_prefix):
                ]
            )

    return list(
        dict.fromkeys(
            alias
            for alias in aliases
            if len(alias) >= 2
        )
    )


def _overview_mentions_place(
    title: str,
    text: str,
) -> bool:
    normalized = normalize_name(
        text
    )

    return any(
        alias in normalized
        for alias in _overview_place_aliases(
            title
        )
    )


def _food_or_cafe_label(
    place: Place,
) -> str:
    category = _front_place_category(
        place
    )

    if category in {
        "맛집",
        "카페",
    }:
        return category

    return "음식점"


def _tourism_kind(
    place: Place,
) -> str:
    title = (
        place.title or ""
    )

    # 장소명이 실제 유적 유형을 직접 말해주는 경우에는
    # 주소가 아니라 그 유적의 성격을 설명합니다.
    if any(
        keyword in title
        for keyword in (
            "석조여래불상",
            "석불",
            "마애불",
            "불상",
        )
    ):
        return "불교 조각 문화유산"

    if any(
        keyword in title
        for keyword in (
            "석탑",
            "목탑",
            "탑",
        )
    ):
        return "불교 건축 문화유산"

    if any(
        keyword in title
        for keyword in (
            "사지",
            "절터",
        )
    ):
        return "옛 사찰 터를 살펴볼 수 있는 불교 유적"

    if any(
        keyword in title
        for keyword in (
            "왕릉",
            "능",
        )
    ):
        return "왕릉 유적"

    if any(
        keyword in title
        for keyword in (
            "고분",
            "고분군",
            "총",
        )
    ):
        return "고분 유적"

    if any(
        keyword in title
        for keyword in (
            "읍성",
            "성곽",
            "산성",
        )
    ):
        return "성곽 유적"

    if any(
        keyword in title
        for keyword in (
            "박물관",
            "기념관",
        )
    ):
        return "역사·문화 자료를 관람하는 문화시설"

    if any(
        keyword in title
        for keyword in (
            "미술관",
            "전시관",
            "갤러리",
        )
    ):
        return "전시를 관람하는 문화시설"

    if any(
        keyword in title
        for keyword in (
            "사찰",
            "불국사",
            "석굴암",
        )
    ):
        return "불교 문화유산"

    if any(
        keyword in title
        for keyword in (
            "향교",
            "서원",
        )
    ):
        return "전통 교육·유교 문화유산"

    if any(
        keyword in title
        for keyword in (
            "전통마을",
            "민속마을",
            "한옥마을",
            "양동",
            "교촌",
        )
    ):
        return "전통 건축과 마을 풍경을 살펴보는 문화관광지"

    if any(
        keyword in title
        for keyword in (
            "공원",
            "수목원",
            "정원",
            "숲",
            "호수",
            "보문호",
        )
    ):
        return "산책과 자연 경관을 즐기는 관광지"

    category = _front_place_category(
        place
    )

    if category == "문화시설":
        return "문화·전시 시설"

    if category == "여행코스":
        return "여러 지점을 함께 둘러보는 여행 코스"

    if category == "레포츠":
        return "체험형 관광지"

    return "경주의 관광지"


def _looks_like_location_description(
    value: str,
) -> bool:
    """
    주소/위치 *자체만* 풀어쓴 짧은 문장을 장소 소개에서 제외합니다.

    주의:
    관광 소개문에는 "위치해 있다", "인접해 있다" 같은 표현이 매우
    흔합니다. 예를 들어 금리단길 공식 소개도 상점/음식점이 "위치해"
    있다고 설명합니다. 따라서 위치 표현 하나만으로 정상 소개문을
    버리면 안 됩니다.
    """
    text = re.sub(
        r"\s+",
        " ",
        value or "",
    ).strip()

    if not text:
        return True

    location_tokens = (
        "위치한",
        "위치해",
        "소재한",
        "소재해",
        "주소는",
        "도로명",
        "번길",
    )

    has_location_phrase = any(
        token in text
        for token in location_tokens
    )

    has_address_number = bool(
        re.search(
            r"(?:로|길)\s*\d+(?:-\d+)?",
            text,
        )
    )

    # 실제 관광지의 성격/볼거리/역사/상권 등을 설명하는 표현.
    # 이런 내용이 하나라도 있으면 위치 표현이 섞여 있어도 소개문입니다.
    informative_tokens = (
        "유적",
        "문화유산",
        "불상",
        "석탑",
        "사찰",
        "왕릉",
        "무덤",
        "고분",
        "성곽",
        "박물관",
        "전시",
        "역사",
        "신라",
        "전통",
        "문화",
        "관광",
        "명소",
        "번화가",
        "상권",
        "상점",
        "음식점",
        "카페",
        "공연",
        "버스킹",
        "즐길거리",
        "즐길 거리",
        "볼거리",
        "볼 거리",
        "체험",
        "산책",
        "경관",
        "공원",
        "정원",
        "숲",
        "대표 메뉴",
        "전문",
    )

    if any(
        token in text
        for token in informative_tokens
    ):
        return False

    # 충분히 긴 문단은 주소 안내일 가능성이 낮으므로 보존합니다.
    # 짧은 "경주시 OOO로 123에 위치한 곳" 유형만 걸러냅니다.
    if len(text) >= 90:
        return False

    address_only_patterns = (
        r"^(?:경상북도\s*)?경주시\s+.+(?:로|길)\s*\d+(?:-\d+)?(?:에|에\s+)?(?:위치|소재).*$",
        r"^.+(?:로|길)\s*\d+(?:-\d+)?$",
    )

    if any(
        re.search(pattern, text)
        for pattern in address_only_patterns
    ):
        return True

    return (
        len(text) <= 70
        and (has_location_phrase or has_address_number)
    )


def _menu_names(
    place: Place,
) -> list[str]:
    names: list[str] = []

    for item in (
        place.menu_items or []
    ):
        if not isinstance(
            item,
            dict,
        ):
            continue

        name = str(
            item.get("name")
            or item.get("menu")
            or ""
        ).strip()

        if name:
            names.append(name)

    return list(
        dict.fromkeys(
            names
        )
    )


def _grounded_fallback_overview(
    place: Place,
) -> str:
    """
    원문 소개가 없을 때도 주소를 소개문으로 반복하지 않습니다.

    - 관광지: 확인 가능한 유적/시설 유형을 설명
    - 맛집/카페: 확인된 대표메뉴/메뉴 성격을 설명
    - 확인되지 않은 역사·유명세는 생성하지 않음
    """
    category = _front_place_category(
        place
    )

    if category in {
        "맛집",
        "카페",
    }:
        label = _food_or_cafe_label(
            place
        )

        representative = (
            place.representative_menu
            or ""
        ).strip()

        menus = [
            menu
            for menu in _menu_names(
                place
            )
            if (
                not representative
                or normalize_name(menu)
                != normalize_name(
                    representative
                )
            )
        ]

        parts = [
            f"{place.title}은(는) {label}입니다."
        ]

        if representative:
            parts.append(
                f"확인된 대표 메뉴는 "
                f"{representative}입니다."
            )

        if menus:
            parts.append(
                "함께 확인되는 메뉴는 "
                + ", ".join(
                    menus[:3]
                )
                + "입니다."
            )

        if (
            not representative
            and not menus
        ):
            parts.append(
                "대표 메뉴와 특징은 상세정보를 확인 중입니다."
            )

        return " ".join(parts)

    kind = _tourism_kind(
        place
    )

    return (
        f"{place.title}은(는) "
        f"{kind}입니다."
    )


async def _quick_overview_from_naver(
    place: Place,
    settings: Settings,
) -> tuple[
    str | None,
    str | None,
    dict[str, str] | None,
]:
    """
    FULL 상세조회에서 소개문을 빠르게 보완합니다.

    우선순위
      1. 정확 장소명 NAVER 지역검색 description
      2. Kakao/Daum + NAVER 웹문서 검색에서 찾은 공식/공공 문서
      3. 서로 다른 NAVER 블로그 2개 이상에서 반복되는 특징

    이전 버전은 NAVER 웹문서 검색 결과에 공식 페이지가 잡히지 않으면
    소개가 비는 경우가 있었습니다. 현재는 이미 프로젝트에서 사용하는
    KAKAO_REST_API_KEY로 Daum 웹문서 검색도 함께 수행해 공식 설명을
    찾을 확률을 높입니다.
    """
    naver = NaverClient(settings)
    kakao = KakaoLocalClient(settings)

    async def local():
        try:
            return await naver.local_search(
                f"경주 {place.title}",
                limit=5,
                sort="random",
            )
        except IntegrationError:
            return []

    async def naver_web_docs():
        try:
            return await naver.web_documents(
                f"경주 {place.title} 소개",
                limit=12,
            )
        except IntegrationError:
            return []

    async def kakao_web_docs():
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for query in (
            f"경주 {place.title}",
            f"{place.title} 경주문화관광",
        ):
            try:
                found = await kakao.web_search(
                    query,
                    limit=10,
                )
            except IntegrationError:
                continue
            for row in found:
                url = str(row.get("url") or "").strip()
                key = url or (
                    str(row.get("title") or "")
                    + str(row.get("description") or "")
                )
                if key and key not in seen:
                    seen.add(key)
                    rows.append(row)
        return rows

    async def blogs():
        try:
            return await naver.blogs(
                f"경주 {place.title} 소개 대표 특징",
                limit=8,
            )
        except IntegrationError:
            return []

    try:
        local_rows, naver_web_rows, kakao_web_rows, blog_rows = (
            await asyncio.wait_for(
                asyncio.gather(
                    local(),
                    naver_web_docs(),
                    kakao_web_docs(),
                    blogs(),
                ),
                timeout=5.2,
            )
        )
    except asyncio.TimeoutError:
        return None, None, None

    wanted = normalize_name(place.title)

    # 1) NAVER 지역검색의 정확 장소 설명
    for row in local_rows:
        row_title = normalize_name(str(row.get("title") or ""))
        if row_title != wanted:
            continue

        description = re.sub(
            r"\s+",
            " ",
            str(row.get("description") or ""),
        ).strip()

        if (
            len(description) >= 12
            and not _looks_like_location_description(description)
        ):
            print(
                "[PLACE OVERVIEW SEARCH]",
                f"title={place.title!r}",
                "source=naver_local",
            )
            return description[:420], "naver_local", None

    # 2) 공식/공공기관 웹문서. Kakao/Daum과 NAVER 결과를 합쳐 평가합니다.
    trusted_domains: tuple[tuple[str, int, str], ...] = (
        ("gyeongju.go.kr", 0, "경주문화관광"),
        ("khs.go.kr", 1, "국가유산청"),
        ("heritage.go.kr", 1, "국가유산청"),
        ("visitkorea.or.kr", 2, "대한민국 구석구석"),
        ("encykorea.aks.ac.kr", 3, "한국민족문화대백과사전"),
    )

    combined_web: list[tuple[str, dict[str, Any]]] = [
        *(
            ("kakao_daum", row)
            for row in kakao_web_rows
        ),
        *(
            ("naver_web", row)
            for row in naver_web_rows
        ),
    ]

    ranked_web: list[tuple[int, str, dict[str, Any], str]] = []
    for search_source, row in combined_web:
        url = str(row.get("url") or "").strip()
        if not url:
            continue

        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            continue

        source_label = ""
        domain_rank = 99
        for domain, rank, label in trusted_domains:
            if host == domain or host.endswith(f".{domain}"):
                source_label = label
                domain_rank = rank
                break

        if domain_rank == 99:
            continue

        title = re.sub(
            r"\s+",
            " ",
            str(row.get("title") or ""),
        ).strip()
        description = re.sub(
            r"\s+",
            " ",
            str(row.get("description") or ""),
        ).strip()

        combined = f"{title} {description}".strip()
        if not _overview_mentions_place(place.title, combined):
            continue
        if len(description) < 20:
            continue
        if _looks_like_location_description(description):
            continue

        normalized_title = normalize_name(title)
        title_bonus = 0 if wanted and wanted in normalized_title else 2
        # 공모전/서비스 연동 맥락에서 Kakao/Daum 검색을 우선 사용하되
        # 출처 신뢰도(공식 도메인)가 검색엔진보다 더 큰 영향을 줍니다.
        engine_bonus = 0 if search_source == "kakao_daum" else 1
        score = domain_rank * 10 + title_bonus + engine_bonus
        ranked_web.append(
            (score, search_source, row, source_label)
        )

    if ranked_web:
        ranked_web.sort(key=lambda item: item[0])
        _, search_source, row, source_label = ranked_web[0]
        description = re.sub(
            r"\s+",
            " ",
            str(row.get("description") or ""),
        ).strip()
        url = str(row.get("url") or "").strip()
        title = re.sub(
            r"\s+",
            " ",
            str(row.get("title") or source_label),
        ).strip()

        link = {
            "title": title or source_label,
            "url": url,
            "type": "official_web",
        }
        print(
            "[PLACE OVERVIEW SEARCH]",
            f"title={place.title!r}",
            f"source={search_source}",
            f"official={source_label}",
            f"naver_web={len(naver_web_rows)}",
            f"kakao_web={len(kakao_web_rows)}",
        )
        return description[:420], "official_web", link

    # 3) 공식 설명을 못 찾았을 때만 블로그 복수 합의를 보조로 사용합니다.
    feature_urls: dict[str, set[str]] = {}
    for item in blog_rows:
        snippet = re.sub(
            r"\s+",
            " ",
            f"{item.title}. {item.description or ''}",
        ).strip()

        if (
            not snippet
            or not item.url
            or not _overview_mentions_place(place.title, snippet)
        ):
            continue

        for feature in _OVERVIEW_FEATURES:
            if feature in snippet:
                feature_urls.setdefault(feature, set()).add(item.url)

    repeated = [
        feature
        for feature, urls in feature_urls.items()
        if len(urls) >= 2
    ]

    if repeated:
        category = _front_place_category(place)
        features = "·".join(repeated[:2])

        print(
            "[PLACE OVERVIEW SEARCH]",
            f"title={place.title!r}",
            "source=naver_blog_consensus",
        )

        if category in {"맛집", "카페"}:
            return (
                f"{place.title}. {features} 관련 방문 후기가 "
                f"반복적으로 확인되는 {category}입니다.",
                "naver_blog_consensus",
                None,
            )

        return (
            f"{place.title}. {features} 관련 방문 후기가 "
            "반복적으로 확인되는 관광지입니다.",
            "naver_blog_consensus",
            None,
        )

    print(
        "[PLACE OVERVIEW SEARCH]",
        f"title={place.title!r}",
        "source=none",
        f"naver_web={len(naver_web_rows)}",
        f"kakao_web={len(kakao_web_rows)}",
    )
    return None, None, None


def _front_overview_text(place: Place) -> str:
    """
    카드/상세에 표시할 장소 소개.

    관광지는 단순 유형 문장(예: "탈해왕릉은 왕릉 유적입니다")을
    소개문으로 노출하지 않습니다. TourAPI/공식 홈페이지/NAVER에서
    실제 소개 문장을 확인했을 때만 보여주고, 확인되지 않으면 빈 값으로
    내려 프론트가 명확히 '소개 정보 확인 중/미확인' 상태를 표시합니다.

    맛집/카페는 대표메뉴처럼 확인 가능한 구조화 정보가 있으므로 기존
    grounded fallback을 사용할 수 있습니다.
    """
    overview = (place.overview or "").strip()
    if overview and not _looks_like_location_description(overview):
        return overview

    if _front_place_category(place) in {"맛집", "카페"}:
        return _grounded_fallback_overview(place)

    return ""


def _operating_hours_card_label(value: str | None) -> str:
    """카드용 짧은 운영시간 라벨. 추천시간과 혼동되지 않게 운영정보만 사용."""
    text = re.sub(r"\s+", " ", value or "").strip()
    if not text:
        return ""

    if any(token in text for token in ("24시간", "상시", "항상 개방")):
        return "상시 개방"

    # 첫 번째 명확한 HH:MM~HH:MM 범위만 카드에 노출합니다.
    match = re.search(
        r"([01]?\d|2[0-3])[:시]\s*([0-5]?\d)?\s*(?:~|[-–]|부터)\s*"
        r"([01]?\d|2[0-3])[:시]\s*([0-5]?\d)?",
        text,
    )
    if match:
        sh = int(match.group(1))
        sm = int(match.group(2) or 0)
        eh = int(match.group(3))
        em = int(match.group(4) or 0)
        return f"{sh:02d}:{sm:02d}~{eh:02d}:{em:02d}"

    # 형식이 복잡한 경우 임의 시간을 만들지 않고 원문 일부만 제공합니다.
    return text[:28]


def _place_to_front(place: Place) -> dict[str, Any]:
    return {
        "id": place.place_id,
        "place_id": place.place_id,
        "content_type_id": place.content_type_id,
        "name": place.title,
        "title": place.title,
        "address": place.address or "경주시",
        "latitude": place.latitude,
        "longitude": place.longitude,
        "quiet_score": _quiet_score(place.congestion_score),
        "routing_quiet_score": _quiet_score(
            place.routing_congestion_score
            if place.routing_congestion_score is not None
            else place.congestion_score
        ),
        "congestion_score": place.congestion_score,
        "community_congestion_score": place.community_congestion_score,
        "community_report_count": place.community_report_count,
        "community_latest_observed_at": (
            place.community_latest_observed_at.isoformat()
            if place.community_latest_observed_at is not None
            else None
        ),
        "routing_congestion_score": place.routing_congestion_score,
        "category": _front_place_category(place),
        "description": _front_overview_text(place),
        # description과 같은 값을 overview 별칭으로도 내려 디버깅/구버전
        # 프론트 호환성을 높입니다. 신규 Flutter는 description을 사용합니다.
        "overview": _front_overview_text(place),
        "image_url": place.image_url or place.thumbnail_url or "",
        "distance_km": place.distance_km or 0,
        "recommended_time": recommended_time_label(
            place
        ),
        "stay_minutes": (
            place.stay_minutes
            if isinstance(
                place,
                CoursePlace,
            )
            else recommended_stay_minutes(
                place
            )
        ),
        "is_paid": None if place.is_free is None else not place.is_free,
        "is_free": place.is_free,
        "operating_hours": place.operating_hours,
        "operating_hours_label": _operating_hours_card_label(place.operating_hours),
        "break_time": place.break_time,
        "rest_date": place.rest_date,
        "fee_text": place.fee_text,
        "parking": place.parking,
        "phone": place.tel or "",
        "tel": place.tel or "",
        "homepage": place.homepage or "",
        "kakao_place_url": place.kakao_place_url or "",
        "representative_menu": place.representative_menu or "",
        "menu_items": place.menu_items,
        "content_links": place.content_links,
        "info_confidence": place.info_confidence or "unknown",
        "info_confidence_label": place.info_confidence_label,
        "is_rest_point": place.is_rest_point,
    }


def _course_to_front(course: Course) -> dict[str, Any]:
    stops: list[dict[str, Any]] = []
    quiet_scores: list[int] = []
    for place in course.places:
        place_json = _place_to_front(place)
        quiet_scores.append(place_json["routing_quiet_score"])

        if place.travel_minutes_from_previous:
            if place.order == 1:
                transport_instruction = (
                    "출발 위치에서 약 "
                    f"{place.travel_minutes_from_previous}분 이동"
                )
            else:
                transport_instruction = (
                    "이전 장소에서 약 "
                    f"{place.travel_minutes_from_previous}분 이동"
                )
        else:
            transport_instruction = "현재 위치에서 이동"

        stops.append(
            {
                "order": place.order,
                "arrival_time": place.arrival_time.isoformat() if place.arrival_time else "",
                "stay_minutes": place.stay_minutes,
                "travel_minutes": place.travel_minutes_from_previous,
                "transport_instruction": transport_instruction,
                "is_rest_stop": place.is_rest_point,
                "place": place_json,
            }
        )
    summary = " · ".join(course.warnings) if course.warnings else "혼잡도와 이동 부담을 줄여 구성한 여행 코스입니다."
    return {
        "route": {
            "id": course.course_id,
            "route_id": course.course_id,
            "title": course.title,
            "summary": summary,
            "total_minutes": course.total_minutes,
            "total_distance_km": course.total_distance_km,
            "average_quiet_score": round(sum(quiet_scores) / len(quiet_scores)) if quiet_scores else 70,
            "weather_summary": course.weather_summary or "",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "stops": stops,
        }
    }


async def _enrich_congestion(
    places: list[Place],
    settings: Settings,
    *,
    latitude: float = DEFAULT_LATITUDE,
    longitude: float = DEFAULT_LONGITUDE,
) -> list[Place]:
    """
    홈/지도/상세에서 코스 추천과 동일한 예상 혼잡도 로직을 사용합니다.

    공식 관광 혼잡도
    + NAVER DataLab 검색 관심도
    + 현재 시간/요일
    + 현재 날씨
    -> congestion_score 0~100

    공식 데이터나 NAVER 데이터가 없어도 RecommendationService의
    _calculate_congestion()이 현재 존재하는 신호만 재정규화해 계산합니다.
    """

    if not places:
        return places

    recommendation = RecommendationService(settings)

    # ---------------------------------------------------------------
    # 1. 관광공사 공식 혼잡도
    # ---------------------------------------------------------------
    try:
        score_map = await CongestionClient(settings).score_map()
    except IntegrationError:
        score_map = {}

    for place in places:
        match = score_map.get(
            normalize_name(place.title)
        )

        if match:
            place.official_congestion_score = match[0]
            place.congestion_date = match[1]

    # ---------------------------------------------------------------
    # 2. NAVER DataLab
    # ---------------------------------------------------------------
    naver_signals: dict[
        str,
        dict[str, float],
    ] = {}

    try:
        naver_signals = await NaverClient(
            settings
        ).trend_signals(
            [
                place.title
                for place in places
            ]
        )
    except IntegrationError:
        naver_signals = {}

    # ---------------------------------------------------------------
    # 3. 현재 날씨
    # ---------------------------------------------------------------
    weather: dict[str, Any] = {}

    try:
        weather = await WeatherClient(settings).current(
            latitude,
            longitude,
        )
    except IntegrationError:
        weather = {}

    # ---------------------------------------------------------------
    # 4. 한국관광공사 DataLab 경주시 지역 방문수요
    # ---------------------------------------------------------------
    regional_demand: dict[str, Any] | None = None

    try:
        regional_demand = await RegionalVisitorClient(
            settings
        ).demand_score()

        print(
            "\n"
            "[REGIONAL VISITOR DEBUG] SUCCESS\n"
            f"data={regional_demand}\n"
        )

    except IntegrationError as exc:
        print(
            "\n"
            "[REGIONAL VISITOR DEBUG] INTEGRATION ERROR\n"
            f"service={exc.service}\n"
            f"status_code={exc.status_code}\n"
            f"message={exc}\n"
        )
        regional_demand = None

    except Exception as exc:
        print(
            "\n"
            "[REGIONAL VISITOR DEBUG] UNEXPECTED ERROR\n"
            f"type={type(exc).__name__}\n"
            f"message={exc}\n"
        )
        regional_demand = None

    # ---------------------------------------------------------------
    # 5. 코스 추천과 동일한 예상 혼잡도 계산
    # ---------------------------------------------------------------
    now = datetime.now().astimezone()

    for place in places:
        signal = naver_signals.get(
            place.title,
            {},
        )

        # 기존 호환 필드에는 momentum 유지
        place.trend_score = signal.get(
            "momentum"
        )

        recommendation._calculate_congestion(
            place,
            now,
            weather,
            regional_demand,
            signal.get("popularity"),
        )

    community_signals = _community_live_signal_map(
        [place.place_id for place in places],
        now=now,
    )
    for place in places:
        _apply_community_live_signal(
            place,
            community_signals.get(place.place_id),
        )

    return places


def _raw_text_blob(place: Place) -> str:
    """
    TourAPI raw 응답의 세부 분류(cat1/cat2/cat3, lclsSystm*)까지
    홈 카테고리 매칭에 활용할 수 있도록 문자열로 평탄화합니다.
    """
    chunks: list[str] = []

    def collect(value: Any) -> None:
        if value is None:
            return

        if isinstance(value, dict):
            for key, item in value.items():
                chunks.append(str(key))
                collect(item)
            return

        if isinstance(value, (list, tuple, set)):
            for item in value:
                collect(item)
            return

        chunks.append(str(value))

    collect(place.raw or {})

    chunks.extend(
        [
            place.title or "",
            place.category or "",
            place.overview or "",
            place.address or "",
            place.content_type_id or "",
        ]
    )

    return " ".join(chunks).lower()


def _matches_home_category(
    place: Place,
    category: str,
) -> bool:
    """
    홈 버튼의 사용자 친화적 카테고리를
    한국관광공사 대분류/세부분류와 장소명에 매핑합니다.

    TourAPI Place.category는 보통 관광지/문화시설처럼 큰 분류라서
    '자연' == place.category 식의 단순 비교를 사용하면 안 됩니다.
    """
    normalized = category.replace("·", "").strip()

    if not normalized or normalized == "전체":
        return True

    blob = _raw_text_blob(place)
    title = (place.title or "").lower()

    # 음식/카페 등 백엔드의 실제 대분류 값이 넘어오는 경우는
    # 기존 방식도 함께 지원합니다.
    if normalized in (place.category or "").replace("·", ""):
        return True

    if normalized == "자연":
        # 신분류 lclsSystm1의 NA = 자연관광.
        if "lclssystm1 na" in blob or "lclssystm1: na" in blob:
            return True

        keywords = (
            "산",
            "숲",
            "공원",
            "호수",
            "호",
            "연못",
            "계곡",
            "수목원",
            "정원",
            "습지",
            "둘레길",
            "산책로",
            "생태",
            "보문",
        )
        return any(keyword in title or keyword in blob for keyword in keywords)

    if normalized == "문화유산":
        keywords = (
            "문화재",
            "유적",
            "유적지",
            "사찰",
            "절",
            "사지",
            "왕릉",
            "릉",
            "고분",
            "총",
            "읍성",
            "성",
            "궁",
            "교",
            "서원",
            "향교",
            "박물관",
            "신라",
            "첨성대",
            "불국사",
            "석굴암",
            "대릉원",
            "동궁",
            "월지",
            "월정교",
        )
        return any(keyword in title or keyword in blob for keyword in keywords)

    if normalized == "전통마을":
        keywords = (
            "전통마을",
            "민속마을",
            "한옥마을",
            "마을",
            "한옥",
            "양동",
            "교촌",
            "촌",
        )
        return any(keyword in title or keyword in blob for keyword in keywords)

    if normalized == "야경":
        # 관광공사 분류만으로 야경을 표현하기 어려워 대표 야간 명소와
        # 야간/조명 관련 명칭을 함께 사용합니다.
        keywords = (
            "야경",
            "야간",
            "밤",
            "조명",
            "월정교",
            "동궁",
            "월지",
            "첨성대",
            "보문",
        )
        return any(keyword in title or keyword in blob for keyword in keywords)

    return normalized.lower() in blob


async def _list_places(
    *,
    latitude: float,
    longitude: float,
    radius_km: float,
    limit: int,
    query: str,
    category: str,
    settings: Settings,
) -> list[dict[str, Any]]:
    client = TourApiClient(settings)

    try:
        if query.strip():
            search_query = query.strip()

            # 1) 우선 기존 경주 지역필터 검색
            places = await client.keyword_search(
                search_query,
                limit=max(limit, 30),
            )

            # 2) KorService2 지역필터 검색에서 유명 관광지가 누락되는 경우가 있어
            #    결과가 없으면 전국 키워드 검색 후 경주 지역 결과만 남깁니다.
            if not places:
                global_places = await client.keyword_search_global(
                    search_query,
                    limit=max(limit, 50),
                )

                gyeongju_places: list[Place] = []

                for place in global_places:
                    address = (place.address or "").strip()

                    # 주소에 경주가 명시되어 있으면 가장 확실하게 허용
                    if "경주" in address:
                        gyeongju_places.append(place)
                        continue

                    # 주소 정보가 약한 데이터는 경주 중심 반경 50km 이내인지 확인
                    try:
                        distance_from_gyeongju = haversine_km(
                            DEFAULT_LATITUDE,
                            DEFAULT_LONGITUDE,
                            place.latitude,
                            place.longitude,
                        )
                    except Exception:
                        continue

                    if distance_from_gyeongju <= 50.0:
                        gyeongju_places.append(place)

                places = gyeongju_places

            # 3) 그래도 없으면 "경주 + 검색어" 형태도 한 번 시도합니다.
            if not places and not search_query.startswith("경주"):
                places = await client.keyword_search_global(
                    f"경주 {search_query}",
                    limit=max(limit, 50),
                )

                places = [
                    place
                    for place in places
                    if (
                        "경주" in (place.address or "")
                        or haversine_km(
                            DEFAULT_LATITUDE,
                            DEFAULT_LONGITUDE,
                            place.latitude,
                            place.longitude,
                        ) <= 50.0
                    )
                ]

            # 4) 검색 결과는 정확한 장소명에 가까운 순서로 우선 정렬
            normalized_query = normalize_name(search_query)

            def search_rank(place: Place) -> tuple[int, int]:
                normalized_title = normalize_name(place.title)

                if normalized_title == normalized_query:
                    match_rank = 0
                elif normalized_query in normalized_title:
                    match_rank = 1
                elif normalized_title in normalized_query:
                    match_rank = 2
                else:
                    match_rank = 3

                return (
                    match_rank,
                    abs(len(normalized_title) - len(normalized_query)),
                )

            places.sort(key=search_rank)
            places = places[:limit]
        else:
            # 홈 카테고리 필터는 조회 후 세부 분류를 판별하므로
            # 최초 후보를 충분히 가져와야 특정 카테고리가 0건으로
            # 보이는 문제를 줄일 수 있습니다.
            nearby_limit = (
                max(limit, 60)
                if category and category != "전체"
                else limit
            )

            places = await client.nearby_places(
                latitude,
                longitude,
                int(radius_km * 1000),
                nearby_limit,
            )

    except IntegrationError as exc:
        raise HTTPException(
            exc.status_code,
            detail={
                "service": exc.service,
                "message": str(exc),
            },
        ) from exc

    for place in places:
        place.distance_km = round(
            haversine_km(
                latitude,
                longitude,
                place.latitude,
                place.longitude,
            ),
            3,
        )

    # 홈/지도/검색에는 실제 여행 목적지와 음식점/카페만 노출합니다.
    places = [
        place
        for place in places
        if is_user_facing_travel_place(place)
    ]

    if category and category != "전체":
        places = [
            place
            for place in places
            if _matches_home_category(
                place,
                category,
            )
        ]

        # 사용자 화면에는 요청한 limit까지만 반환합니다.
        places = places[:limit]
    places = await _enrich_congestion(
        places,
        settings,
        latitude=latitude,
        longitude=longitude,
    )

    places.sort(
        key=lambda place: (
            place.congestion_score
            if place.congestion_score is not None
            else 101.0
        )
    )

    return [
        _place_to_front(place)
        for place in places
    ]



@compat_router.get("/debug/congestion-v2")
async def debug_congestion_v2(
    settings: Settings = Depends(get_settings),
):
    """
    혼잡도 V2.4.3 대표 관광지 8곳 비교용 디버그 API.

    장소 해석 순서:
    1. 경주 지역필터 keyword_search()
    2. 못 찾으면 전국 keyword_search_global()
    3. 전국 검색 결과는 경주 중심 반경 50km 안의 장소만 허용
    4. 음식점/쇼핑/숙박 제외
    5. 정확한 장소명/별칭 우선 매칭

    혼잡도 계산식과 가중치는 변경하지 않습니다.
    """
    tour = TourApiClient(settings)

    selected: list[Place] = []
    missing: list[str] = []
    resolved: dict[str, str] = {}
    source_used: dict[str, str] = {}

    allowed_categories = {
        "관광지",
        "문화시설",
        "여행코스",
        "레포츠",
    }

    def is_gyeongju_area(
        place: Place,
    ) -> bool:
        """
        전국 검색 fallback에서 동명이인 타지역 관광지를 제거합니다.

        - 주소에 '경주'가 있으면 허용
        - 주소가 없거나 표기가 다르면 경주 중심에서 50km 이내인지 확인
        """
        address = (
            place.address
            or ""
        )

        if "경주" in address:
            return True

        try:
            distance = haversine_km(
                DEFAULT_LATITUDE,
                DEFAULT_LONGITUDE,
                place.latitude,
                place.longitude,
            )
        except Exception:
            return False

        return distance <= 50.0

    for target_name in DEBUG_CONGESTION_PLACE_NAMES:
        aliases = DEBUG_CONGESTION_ALIASES.get(
            target_name,
            (target_name,),
        )

        normalized_aliases = {
            normalize_name(alias)
            for alias in aliases
        }

        chosen: Place | None = None

        # -------------------------------------------------------
        # 1. 기존 경주 지역 필터 검색
        # -------------------------------------------------------
        regional_candidates: dict[str, Place] = {}

        for alias in aliases:
            try:
                found = await tour.keyword_search(
                    alias,
                    limit=100,
                )
            except IntegrationError:
                found = []

            for place in found:
                if place.category not in allowed_categories:
                    continue

                regional_candidates[
                    place.place_id
                ] = place

        regional_list = list(
            regional_candidates.values()
        )

        chosen = next(
            (
                place
                for place in regional_list
                if normalize_name(place.title)
                in normalized_aliases
            ),
            None,
        )

        if chosen is not None:
            source_used[target_name] = (
                "regional_keyword_search"
            )

        # -------------------------------------------------------
        # 2. 지역검색에서 못 찾으면 전국 키워드 검색
        #    단, 경주 지역으로 판단되는 결과만 허용
        # -------------------------------------------------------
        if chosen is None:
            global_candidates: dict[str, Place] = {}

            for alias in aliases:
                try:
                    found = await tour.keyword_search_global(
                        alias,
                        limit=100,
                    )
                except IntegrationError:
                    found = []

                for place in found:
                    if place.category not in allowed_categories:
                        continue

                    if not is_gyeongju_area(place):
                        continue

                    global_candidates[
                        place.place_id
                    ] = place

            global_list = list(
                global_candidates.values()
            )

            # 정확 별칭 일치 우선
            chosen = next(
                (
                    place
                    for place in global_list
                    if normalize_name(place.title)
                    in normalized_aliases
                ),
                None,
            )

            # 정확 일치가 없다면 target 문자열 부분일치
            if chosen is None:
                target_normalized = normalize_name(
                    target_name
                )

                partial_candidates = [
                    place
                    for place in global_list
                    if (
                        target_normalized
                        in normalize_name(place.title)
                        or normalize_name(place.title)
                        in target_normalized
                    )
                ]

                chosen = min(
                    partial_candidates,
                    key=lambda place: (
                        abs(
                            len(
                                normalize_name(
                                    place.title
                                )
                            )
                            - len(
                                target_normalized
                            )
                        ),
                        len(place.title),
                    ),
                    default=None,
                )

            if chosen is not None:
                source_used[target_name] = (
                    "global_keyword_fallback_gyeongju"
                )

        if chosen is None:
            missing.append(
                target_name
            )
            continue

        selected.append(
            chosen
        )
        resolved[target_name] = (
            chosen.title
        )

    if not selected:
        raise HTTPException(
            404,
            detail="대표 관광지를 찾지 못했습니다.",
        )

    enriched = await _enrich_congestion(
        selected,
        settings,
        latitude=DEFAULT_LATITUDE,
        longitude=DEFAULT_LONGITUDE,
    )

    id_to_target: dict[str, str] = {}

    for target_name, actual_title in resolved.items():
        for place in selected:
            if place.title == actual_title:
                id_to_target[
                    place.place_id
                ] = target_name
                break

    rows: list[dict[str, Any]] = []

    for place in enriched:
        components = (
            place.congestion_components
            or {}
        )

        target = id_to_target.get(
            place.place_id,
            place.title,
        )

        rows.append(
            {
                "target": target,
                "place": place.title,
                "lookup_source": (
                    source_used.get(target)
                ),
                "category": place.category,
                "address": place.address,
                "final": place.congestion_score,
                "baseline": components.get(
                    "baseline"
                ),
                "time": components.get(
                    "time"
                ),
                "naver_pop": components.get(
                    "naver_popularity"
                ),
                "naver_mom": components.get(
                    "naver_momentum"
                ),
                "weather": components.get(
                    "weather"
                ),
                "regional": components.get(
                    "regional_demand"
                ),
                "reg_factor": components.get(
                    "regional_factor"
                ),
                "reg_lag_days": components.get(
                    "regional_lag_days"
                ),
                "reg_weight": components.get(
                    "regional_weight"
                ),
                "official": components.get(
                    "official"
                ),
                "capacity": components.get(
                    "capacity_factor"
                ),
                "raw_score": components.get(
                    "raw_score"
                ),
            }
        )

    return {
        "version": "V2.4.3-debug-geofix",
        "count": len(rows),
        "missing": missing,
        "resolved": resolved,
        "lookup_source": source_used,
        "places": rows,
    }


@compat_router.get("/places")
async def front_places(
    query: str = "",
    category: str = "",
    radius_km: float = Query(15, gt=0, le=20),
    limit: int = Query(30, ge=1, le=100),
    settings: Settings = Depends(get_settings),
):
    return {
        "places": await _list_places(
            latitude=DEFAULT_LATITUDE,
            longitude=DEFAULT_LONGITUDE,
            radius_km=radius_km,
            limit=limit,
            query=query,
            category=category,
            settings=settings,
        )
    }


def _exact_local_match(
    place: Place,
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    target = normalize_name(place.title)

    exact = [
        item
        for item in candidates
        if normalize_name(
            str(item.get("title") or "")
        ) == target
    ]

    pool = exact or [
        item
        for item in candidates
        if (
            target
            and (
                target
                in normalize_name(
                    str(item.get("title") or "")
                )
                or normalize_name(
                    str(item.get("title") or "")
                )
                in target
            )
        )
    ]

    if not pool:
        return None

    # 경주 주소를 우선합니다.
    pool.sort(
        key=lambda item: (
            0
            if "경주" in (
                str(item.get("road_address") or "")
                + str(item.get("address") or "")
            )
            else 1
        )
    )

    return pool[0]


def _price_after_menu_name(
    snippet: str,
    menu_name: str,
) -> str | None:
    if not snippet or not menu_name:
        return None

    # 공백 차이를 어느 정도 허용합니다.
    tokens = [
        re.escape(token)
        for token in menu_name.split()
        if token
    ]

    if not tokens:
        return None

    flexible_name = r"\s*".join(tokens)

    match = re.search(
        flexible_name
        + r".{0,50}?"
        + r"(\d{1,3}(?:,\d{3})+|\d{4,6})\s*원",
        snippet,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    return f"{match.group(1)}원"


async def _fill_menu_prices_from_naver(
    place: Place,
    settings: Settings,
) -> Place:
    if (
        str(place.content_type_id or "") != "39"
        or not place.menu_items
    ):
        return place

    # TourAPI가 이미 준 가격은 절대 덮어쓰지 않습니다.
    pending_names = [
        str(item.get("name") or "").strip()
        for item in place.menu_items[:5]
        if (
            str(item.get("name") or "").strip()
            and not str(item.get("price") or "").strip()
        )
    ]

    if not pending_names:
        return place

    try:
        blogs = await NaverClient(settings).blogs(
            f"경주 {place.title} 메뉴 가격",
            10,
        )
    except IntegrationError:
        return place

    exact_blogs = []

    target_aliases = {
        normalize_name(place.title),
    }

    title_norm = normalize_name(place.title)
    if title_norm.startswith("경주") and len(title_norm) > 3:
        target_aliases.add(title_norm[2:])

    for item in blogs:
        blob = normalize_name(
            f"{item.title} {item.description or ''}"
        )

        if not any(
            alias and alias in blob
            for alias in target_aliases
        ):
            continue

        exact_blogs.append(item)

    if not exact_blogs:
        return place

    updated: list[dict[str, Any]] = []

    for item in place.menu_items:
        current = dict(item)
        menu_name = str(
            current.get("name") or ""
        ).strip()

        if (
            not menu_name
            or str(
                current.get("price") or ""
            ).strip()
        ):
            updated.append(current)
            continue

        votes: dict[str, set[str]] = {}

        for blog in exact_blogs:
            snippet = (
                f"{blog.title}. "
                f"{blog.description or ''}"
            )

            price = _price_after_menu_name(
                snippet,
                menu_name,
            )

            if not price:
                continue

            votes.setdefault(
                price,
                set(),
            ).add(blog.url)

        # 서로 다른 NAVER 블로그 URL 2곳 이상이 같은 가격을 말할 때만
        # 가격 보완값으로 채택합니다.
        consensus = [
            (price, urls)
            for price, urls in votes.items()
            if len(urls) >= 2
        ]

        if consensus:
            consensus.sort(
                key=lambda pair: len(pair[1]),
                reverse=True,
            )
            current["price"] = consensus[0][0]
            current["source"] = (
                "naver_blog_consensus"
            )

        updated.append(current)

    place.menu_items = updated
    return place


async def _load_place_detail_core(
    *,
    seed: Place,
    settings: Settings,
    latitude: float,
    longitude: float,
) -> Place:
    """
    빠른 상세정보 단계.

    TourAPI 공식 상세정보를 가장 먼저 확보하고,
    Kakao 장소 연결 + 현재 혼잡도는 병렬로 처리합니다.
    느린 공식 홈페이지/NAVER 블로그/YouTube 보완은 여기서 기다리지 않습니다.
    """
    started = time.monotonic()

    tour = TourApiClient(
        settings
    )

    kakao_task: asyncio.Task[
        list[dict[str, Any]]
    ] | None = None

    if (
        seed.title
        and not seed.title.isdigit()
    ):
        kakao_task = asyncio.create_task(
            KakaoLocalClient(
                settings
            ).keyword_search(
                f"경주 {seed.title}",
                latitude=seed.latitude,
                longitude=seed.longitude,
                limit=5,
            )
        )

    congestion_task = asyncio.create_task(
        _enrich_congestion(
            [
                seed.model_copy(
                    deep=True
                )
            ],
            settings,
            latitude=(
                latitude
                or seed.latitude
                or DEFAULT_LATITUDE
            ),
            longitude=(
                longitude
                or seed.longitude
                or DEFAULT_LONGITUDE
            ),
        )
    )

    try:
        place = await tour.detail(
            seed
        )
    except IntegrationError:
        place = seed.model_copy(
            deep=True
        )

    if (
        seed.title.strip()
        and (
            not place.title.strip()
            or place.title.strip().isdigit()
        )
    ):
        place.title = (
            seed.title.strip()
        )

    # Kakao Local은 전화/주소/상세 URL 보조.
    if kakao_task is not None:
        try:
            kakao_results = await asyncio.wait_for(
                kakao_task,
                timeout=2.0,
            )

            kakao_match = _exact_local_match(
                place,
                kakao_results,
            )

            if kakao_match:
                if not place.tel:
                    place.tel = (
                        kakao_match.get(
                            "phone"
                        )
                        or None
                    )

                if not place.address:
                    place.address = (
                        kakao_match.get(
                            "road_address"
                        )
                        or kakao_match.get(
                            "address"
                        )
                        or None
                    )

                place.kakao_place_url = (
                    kakao_match.get(
                        "place_url"
                    )
                    or place.kakao_place_url
                )

                raw = dict(
                    place.raw
                    or {}
                )

                raw[
                    "kakao_local"
                ] = kakao_match

                place.raw = raw

        except (
            asyncio.TimeoutError,
            IntegrationError,
        ):
            kakao_task.cancel()

    # 혼잡도는 최대 2.5초까지만 기다리고,
    # 실패하면 카드에서 넘어온 기존 혼잡도 값을 그대로 유지합니다.
    try:
        congestion_result = await asyncio.wait_for(
            congestion_task,
            timeout=2.5,
        )

        if congestion_result:
            enriched_congestion = (
                congestion_result[0]
            )

            place.congestion_score = (
                enriched_congestion
                .congestion_score
            )

            place.quiet_score = (
                enriched_congestion
                .quiet_score
            )

            place.congestion_reason = (
                enriched_congestion
                .congestion_reason
            )

    except (
        asyncio.TimeoutError,
        Exception,
    ):
        congestion_task.cancel()

    elapsed = (
        time.monotonic()
        - started
    )

    print(
        "[PLACE DETAIL PERF]",
        f"stage=core",
        f"id={place.place_id}",
        f"elapsed={elapsed:.3f}s",
    )

    return place


async def _load_place_detail_full(
    *,
    core: Place,
    settings: Settings,
) -> Place:
    """
    느린 보완 단계.

    공식 홈페이지/NAVER 기반 운영정보,
    대표메뉴 가격, 정확 일치 블로그/YouTube를 병렬로 확인합니다.
    각 작업에 짧은 상한을 둬 화면이 20초 이상 멈추지 않게 합니다.
    """
    started = time.monotonic()

    base = core.model_copy(
        deep=True
    )

    async def enrich_info() -> Place:
        try:
            return await asyncio.wait_for(
                PlaceInfoEnricher(
                    settings
                ).enrich(
                    base.model_copy(
                        deep=True
                    )
                ),
                timeout=6.5,
            )
        except (
            asyncio.TimeoutError,
            Exception,
        ):
            return base.model_copy(
                deep=True
            )

    async def enrich_menu() -> Place:
        try:
            return await asyncio.wait_for(
                _fill_menu_prices_from_naver(
                    base.model_copy(
                        deep=True
                    ),
                    settings,
                ),
                timeout=4.0,
            )
        except (
            asyncio.TimeoutError,
            Exception,
        ):
            return base.model_copy(
                deep=True
            )

    async def load_contents() -> tuple[
        list[Any],
        list[Any],
        list[Any],
    ]:
        try:
            return await asyncio.wait_for(
                ContentService(
                    settings
                ).get(
                    base.place_id,
                    base.title,
                ),
                timeout=4.0,
            )
        except (
            asyncio.TimeoutError,
            Exception,
        ):
            return (
                [],
                [],
                [],
            )

    async def load_quick_overview() -> tuple[
        str | None,
        str | None,
        dict[str, str] | None,
    ]:
        try:
            return await _quick_overview_from_naver(
                base,
                settings,
            )
        except Exception:
            return (
                None,
                None,
                None,
            )

    (
        enriched_place,
        menu_place,
        contents,
        quick_overview,
    ) = await asyncio.gather(
        enrich_info(),
        enrich_menu(),
        load_contents(),
        load_quick_overview(),
    )

    result = enriched_place.model_copy(
        deep=True
    )

    # 메뉴 보완 작업의 결과만 필요한 필드에 합칩니다.
    if (
        menu_place.representative_menu
        and not result.representative_menu
    ):
        result.representative_menu = (
            menu_place
            .representative_menu
        )

    if menu_place.menu_items:
        result.menu_items = (
            menu_place.menu_items
        )

    overview_link = quick_overview[2]

    if not result.overview:
        (
            overview_text,
            overview_source,
            overview_link,
        ) = quick_overview

        if overview_text:
            result.overview = (
                overview_text
            )
            result.overview_source = (
                overview_source
            )
            result.info_sources = list(
                dict.fromkeys(
                    [
                        *result.info_sources,
                        overview_source,
                    ]
                )
            )

            if (
                overview_source == "official_web"
                and result.info_confidence in {None, "unknown"}
            ):
                result.info_confidence = "medium"
                result.info_confidence_label = "공식·보조 정보"

    blogs, videos, _ = contents

    result.content_links = [
        *(
            [
                {
                    "title": "공식 홈페이지",
                    "url": result.homepage,
                    "type": "official_website",
                }
            ]
            if result.homepage
            else []
        ),
        *(
            [overview_link]
            if overview_link
            and overview_link.get("url")
            and overview_link.get("url") != result.homepage
            else []
        ),
        *[
            {
                "title": item.title,
                "url": item.url,
                "type": item.source,
            }
            for item in [*blogs, *videos]
            if item.url
        ],
    ]

    elapsed = (
        time.monotonic()
        - started
    )

    print(
        "[PLACE DETAIL PERF]",
        f"stage=full",
        f"id={result.place_id}",
        f"elapsed={elapsed:.3f}s",
    )

    return result


@compat_router.get("/places/{place_id}")
async def front_place_detail(
    place_id: str,
    content_type_id: str = "",
    title: str = "",
    address: str = "",
    stage: str = "core",
    settings: Settings = Depends(
        get_settings
    ),
):
    """
    홈/지도/코스/저장한 장소가 공통으로 사용하는 단일 상세조회 API.

    stage=core:
      TourAPI 공식정보 + Kakao 연결 + 혼잡도만 빠르게 반환

    stage=full:
      core 결과를 재사용해 공식 홈페이지/NAVER 보완,
      메뉴가격, 정확 일치 블로그/YouTube까지 추가
    """
    normalized_stage = (
        stage.strip().lower()
    )

    if normalized_stage not in {
        "core",
        "full",
    }:
        normalized_stage = "core"

    key = _place_detail_cache_key(
        place_id,
        title,
    )

    # 이미 full 결과가 있으면 core 요청에도 즉시 full을 사용합니다.
    full_cached = (
        _place_detail_cache_get(
            _PLACE_DETAIL_FULL_CACHE,
            key,
        )
    )

    if full_cached is not None:
        return {
            "place":
                _place_to_front(
                    full_cached
                )
        }

    core = _place_detail_cache_get(
        _PLACE_DETAIL_CORE_CACHE,
        key,
    )

    if core is None:
        seed = Place(
            place_id=place_id,
            content_type_id=(
                content_type_id
                or None
            ),
            title=(
                title.strip()
                or place_id
            ),
            address=(
                address.strip()
                or None
            ),
            latitude=DEFAULT_LATITUDE,
            longitude=DEFAULT_LONGITUDE,
        )

        core = await _load_place_detail_core(
            seed=seed,
            settings=settings,
            latitude=DEFAULT_LATITUDE,
            longitude=DEFAULT_LONGITUDE,
        )

        _place_detail_cache_set(
            _PLACE_DETAIL_CORE_CACHE,
            key,
            core,
        )

    if normalized_stage == "core":
        return {
            "place":
                _place_to_front(
                    core
                )
        }

    full = await _load_place_detail_full(
        core=core,
        settings=settings,
    )

    _place_detail_cache_set(
        _PLACE_DETAIL_FULL_CACHE,
        key,
        full,
    )

    # full 결과를 core 캐시에도 올려 다음 진입을 즉시 처리합니다.
    _place_detail_cache_set(
        _PLACE_DETAIL_CORE_CACHE,
        key,
        full,
    )

    return {
        "place":
            _place_to_front(
                full
            )
    }


@compat_router.get("/congestion/now")
async def front_congestion_now(settings: Settings = Depends(get_settings)):
    return {"places": await _list_places(latitude=DEFAULT_LATITUDE, longitude=DEFAULT_LONGITUDE, radius_km=15, limit=30, query="", category="", settings=settings)}


@compat_router.get("/congestion/{place_id}")
async def front_congestion(
    place_id: str,
    title: str = "",
    settings: Settings = Depends(get_settings),
):
    place_name = title.strip()

    try:
        seed = Place(
            place_id=place_id,
            content_type_id="12",
            title=place_name or place_id,
            latitude=DEFAULT_LATITUDE,
            longitude=DEFAULT_LONGITUDE,
        )

        try:
            place = await TourApiClient(settings).detail(seed)
        except IntegrationError:
            place = seed

        if place_name:
            place.title = place_name

        place = (
            await _enrich_congestion(
                [place],
                settings,
                latitude=DEFAULT_LATITUDE,
                longitude=DEFAULT_LONGITUDE,
            )
        )[0]

        return {
            "place_id": place_id,
            "name": place.title,
            "congestion_score": place.congestion_score,
            "quiet_score": _quiet_score(
                place.congestion_score
            ),
            "community_congestion_score": place.community_congestion_score,
            "community_report_count": place.community_report_count,
            "community_latest_observed_at": (
                place.community_latest_observed_at.isoformat()
                if place.community_latest_observed_at is not None
                else None
            ),
            "routing_congestion_score": place.routing_congestion_score,
            "measured_at": place.congestion_date,
            "congestion_components": place.congestion_components,
            "official_congestion_score": place.official_congestion_score,
            "trend_score": place.trend_score,
        }

    except IntegrationError as exc:
        raise HTTPException(
            exc.status_code,
            detail={
                "service": exc.service,
                "message": str(exc),
            },
        ) from exc


@compat_router.post("/routes/recommend")
async def front_recommend(
    body: FrontRecommendRequest,
    settings: Settings = Depends(
        get_settings
    ),
):
    try:
        request = _to_backend_request(
            body
        )

        request = await _apply_gpt_route_request(
            request,
            body.memo,
            settings,
        )

        response = await RecommendationService(
            settings
        ).recommend(
            request
        )

        if not response.courses:
            raise ValueError(
                "추천 가능한 코스가 없습니다."
            )

        return _course_to_front(
            response.courses[0]
        )
    except IntegrationError as exc:
        raise HTTPException(exc.status_code, detail={"service": exc.service, "message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


def _current_route_stop_names(
    current_route: dict[str, Any],
) -> list[str]:
    """프론트가 보낸 현재 코스에서 장소명을 순서대로 추출합니다."""
    stops = current_route.get("stops")

    if not isinstance(stops, list):
        return []

    names: list[str] = []

    for raw in stops:
        if not isinstance(raw, dict):
            continue

        name = str(
            raw.get("name")
            or raw.get("title")
            or ""
        ).strip()

        if name:
            names.append(name)

    return list(dict.fromkeys(names))


def _same_route_refresh_request(
    body: FrontRefreshRequest,
) -> tuple[RecommendRequest, list[str], int]:
    """
    같은 조건은 유지하되 직전 코스가 그대로 반복되지 않도록 합니다.

    - 매 refresh마다 명시적인 새로운 seed 사용
    - 직전 코스의 변경 가능한 장소 중 약 절반을 이번 요청에서 제외
    - 사용자가 '꼭 포함'으로 지정한 장소는 제외하지 않음
    """
    seed = secrets.randbelow(2_147_483_647)

    request = _to_backend_request(
        body.preferences,
        seed=seed,
    )

    current_names = _current_route_stop_names(
        body.current_route
    )

    required = [
        name.lower()
        for name in request.required_place_names
    ]

    changeable = [
        name
        for name in current_names
        if not any(
            required_name in name.lower()
            or name.lower() in required_name
            for required_name in required
        )
    ]

    # 순서를 매번 바꿔 연속 refresh 시 같은 장소만 고정 제외되지 않게 함
    rng = random.Random(seed)
    rng.shuffle(changeable)

    if changeable:
        exclude_count = max(
            1,
            (len(changeable) + 1) // 2,
        )
        refresh_exclusions = changeable[:exclude_count]
    else:
        refresh_exclusions = []

    merged_exclusions = list(
        dict.fromkeys(
            [
                *request.excluded_place_names,
                *refresh_exclusions,
            ]
        )
    )

    request = request.model_copy(
        update={
            "excluded_place_names": merged_exclusions,
            "seed": seed,
        }
    )

    return request, refresh_exclusions, seed


@compat_router.post("/routes/recommend/refresh")
async def front_refresh(
    body: FrontRefreshRequest,
    settings: Settings = Depends(get_settings),
):
    try:
        request, refresh_exclusions, seed = (
            _same_route_refresh_request(body)
        )

        print(
            "\n[ROUTE REFRESH]"
            f" reason={body.reason}"
            f" seed={seed}"
            f" excluded_previous={refresh_exclusions}"
            "\n"
        )

        request = await _apply_gpt_route_request(
            request,
            request.memo,
            settings,
        )

        response = await RecommendationService(
            settings
        ).recommend(request)

        if not response.courses:
            raise ValueError(
                "대체 가능한 코스가 없습니다."
            )

        return _course_to_front(
            response.courses[0]
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


async def _course_from_front(current_route: dict[str, Any], settings: Settings) -> Course:
    stops = current_route.get("stops") if isinstance(current_route.get("stops"), list) else []
    places: list[CoursePlace] = []
    tour = TourApiClient(settings)
    for index, raw in enumerate(stops, start=1):
        if not isinstance(raw, dict):
            continue
        place_id = str(raw.get("place_id") or raw.get("placeId") or raw.get("id") or "").strip()
        title = str(raw.get("name") or raw.get("title") or place_id).strip()
        content_type_id = str(raw.get("content_type_id") or raw.get("contentTypeId") or "12").strip()
        if not place_id:
            continue
        try:
            detail = await tour.detail(Place(place_id=place_id, content_type_id=content_type_id, title=title, latitude=0, longitude=0))
        except IntegrationError:
            detail = Place(place_id=place_id, content_type_id=content_type_id, title=title or place_id, latitude=DEFAULT_LATITUDE, longitude=DEFAULT_LONGITUDE)
        places.append(
            CoursePlace(
                **detail.model_dump(),
                order=index,
                stay_minutes=recommended_stay_minutes(
                    detail
                ),
            )
        )
    if not places:
        raise ValueError("현재 코스 장소 정보가 없습니다.")
    return Course(
        course_id=str(current_route.get("id") or current_route.get("route_id") or "frontend-course"),
        title=str(current_route.get("title") or "경주한적 추천 코스"),
        type=CourseType.preference_fit,
        total_minutes=len(places) * 60,
        total_distance_km=0,
        objective_values={},
        places=places,
    )



def _is_cafe_like(place: Place) -> bool:
    text = " ".join(
        [
            place.title or "",
            place.category or "",
            place.overview or "",
            str((place.raw or {}).get("cat3") or ""),
        ]
    ).lower()

    return any(
        keyword in text
        for keyword in (
            "카페",
            "커피",
            "베이커리",
            "디저트",
            "찻집",
            "다방",
        )
    )


@compat_router.post("/routes/replace-stop")
async def front_replace_stop(
    body: dict[str, Any],
    settings: Settings = Depends(get_settings),
):
    """현재 코스의 선택한 한 장소만 다른 추천 장소로 교체합니다."""
    try:
        current_route = (
            body.get("current_route")
            or body.get("currentRoute")
            or body.get("course")
            or {}
        )

        target_place_id = str(
            body.get("target_place_id")
            or body.get("targetPlaceId")
            or ""
        ).strip()

        target_name = str(
            body.get("target_name")
            or body.get("targetName")
            or ""
        ).strip()

        # User GPS is not accepted. The server uses only the fixed Gyeongju anchor.
        start_latitude = DEFAULT_LATITUDE
        start_longitude = DEFAULT_LONGITUDE

        transport_raw = str(
            body.get("transport_type")
            or body.get("transportType")
            or "walking"
        ).strip()

        course = await _course_from_front(
            current_route,
            settings,
        )

        target: CoursePlace | None = None

        for place in course.places:
            if (
                target_place_id
                and place.place_id == target_place_id
            ):
                target = place
                break

            if (
                target_name
                and normalize_name(place.title)
                == normalize_name(target_name)
            ):
                target = place
                break

        if target is None:
            raise ValueError(
                "바꿀 장소를 현재 코스에서 찾지 못했습니다."
            )

        tour = TourApiClient(settings)

        nearby = await tour.nearby_places(
            target.latitude,
            target.longitude,
            20000,
            60,
        )

        recommendation = RecommendationService(
            settings
        )

        details = await recommendation._details(
            nearby
        )

        existing_ids = {
            place.place_id
            for place in course.places
        }

        target_is_food = (
            str(target.content_type_id or "") == "39"
            or target.category == "음식점"
        )

        target_is_cafe = (
            target_is_food
            and _is_cafe_like(target)
        )

        candidates: list[Place] = []

        for candidate in details:
            if candidate.place_id in existing_ids:
                continue

            if not is_user_facing_travel_place(
                candidate
            ):
                continue

            candidate_is_food = (
                str(candidate.content_type_id or "") == "39"
                or candidate.category == "음식점"
            )

            # 관광지는 관광지로, 식당은 식당으로, 카페는 카페로 교체합니다.
            if target_is_food:
                if not candidate_is_food:
                    continue

                if (
                    target_is_cafe
                    and not _is_cafe_like(candidate)
                ):
                    continue

                if (
                    not target_is_cafe
                    and _is_cafe_like(candidate)
                ):
                    continue
            elif candidate_is_food:
                continue

            candidates.append(candidate)

        same_category = [
            candidate
            for candidate in candidates
            if candidate.category == target.category
        ]

        if same_category:
            candidates = same_category

        if not candidates:
            raise ValueError(
                "조건에 맞는 대체 장소를 찾지 못했습니다."
            )

        candidates = await _enrich_congestion(
            candidates[:30],
            settings,
            latitude=target.latitude,
            longitude=target.longitude,
        )

        def candidate_score(candidate: Place) -> float:
            distance_km = haversine_km(
                target.latitude,
                target.longitude,
                candidate.latitude,
                candidate.longitude,
            )

            proximity = max(
                0.0,
                100.0 - distance_km * 7.0,
            )

            quiet = (
                100.0 - candidate.congestion_score
                if candidate.congestion_score is not None
                else 50.0
            )

            same_type = (
                100.0
                if str(candidate.content_type_id or "")
                == str(target.content_type_id or "")
                else 60.0
            )

            return (
                quiet * 0.50
                + proximity * 0.35
                + same_type * 0.15
            )

        candidates.sort(
            key=candidate_score,
            reverse=True,
        )

        chosen = candidates[0]

        replacement = CoursePlace(
            **chosen.model_dump(),
            order=target.order,
            stay_minutes=target.stay_minutes,
        )

        updated_places = [
            replacement
            if place.place_id == target.place_id
            else place
            for place in course.places
        ]

        speed_km_per_hour = {
            "walking": 4.5,
            "public_transport": 18.0,
            "driving": 28.0,
        }.get(
            transport_raw,
            4.5,
        )

        previous_lat = start_latitude
        previous_lon = start_longitude
        total_distance_km = 0.0
        total_minutes = 0

        for order, place in enumerate(
            updated_places,
            start=1,
        ):
            distance_km = haversine_km(
                previous_lat,
                previous_lon,
                place.latitude,
                place.longitude,
            )

            travel_minutes = max(
                1,
                round(
                    distance_km
                    / speed_km_per_hour
                    * 60
                ),
            )

            place.order = order
            place.travel_minutes_from_previous = travel_minutes
            place.travel_distance_m_from_previous = round(
                distance_km * 1000
            )

            total_distance_km += distance_km
            total_minutes += (
                travel_minutes
                + place.stay_minutes
            )

            previous_lat = place.latitude
            previous_lon = place.longitude

        course.places = updated_places
        course.total_minutes = total_minutes
        course.total_distance_km = round(
            total_distance_km,
            2,
        )

        course.warnings = [
            f"{target.title} 대신 {chosen.title}(으)로 바꿨어요."
        ]

        return _course_to_front(course)

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


@compat_router.post("/chat/modify-course")
async def front_modify(
    body: FrontModifyRequest,
    settings: Settings = Depends(
        get_settings
    ),
):
    """
    '코스를 조금 바꾸고 싶어요'는 부분 수정이 아니라
    사용자의 기존 출발지/시간/교통/테마를 유지한 채
    자연어 요청을 새 조건으로 더해 코스를 다시 계산합니다.

    예:
    "첨성대 가고 싶어"
      -> required_place_names += ["첨성대"]
      -> 전체 경주 후보에서 첨성대를 포함해 재추천
    """
    try:
        if body.preferences is not None:
            request = _to_backend_request(
                body.preferences
            )
        else:
            # 구버전 프론트 fallback.
            request = RecommendRequest(
                desired_course_count=1,
            )

        request.memo = body.message.strip()

        request = await _apply_gpt_route_request(
            request,
            body.message,
            settings,
            allow_service_changes=True,
        )

        # GPT가 실패했을 때도 "OO 가고 싶어" 정도는 규칙 기반으로 복구.
        if not request.required_place_names:
            text = body.message.strip()

            for suffix in (
                "가고 싶어",
                "가고싶어",
                "가고 싶어요",
                "가고싶어요",
                "넣어줘",
                "포함해줘",
                "포함해 줘",
            ):
                if suffix in text:
                    candidate = text.split(
                        suffix,
                        1,
                    )[0].strip()

                    candidate = re.sub(
                        r"^(이번에는|이번엔|코스에|그리고)\s*",
                        "",
                        candidate,
                    ).strip()

                    if (
                        2 <= len(candidate) <= 30
                    ):
                        request.required_place_names.append(
                            candidate
                        )

                    break

        response = await RecommendationService(
            settings
        ).recommend(
            request
        )

        if not response.courses:
            raise ValueError(
                "요청을 반영한 코스를 만들지 못했습니다."
            )

        return _course_to_front(
            response.courses[0]
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


@compat_router.get("/etiquette/place/{place_id}")
async def front_etiquette(place_id: str, content_type_id: str = "12", settings: Settings = Depends(get_settings)):
    tips = [
        "문화재와 시설물을 만지거나 훼손하지 마세요.",
        "촬영 제한 표지와 지정된 관람 동선을 지켜주세요.",
        "주변 관람객과 주민을 위해 큰 소리를 줄여주세요.",
    ]
    try:
        seed = Place(place_id=place_id, content_type_id=content_type_id, title=place_id, latitude=0, longitude=0)
        place = await TourApiClient(settings).detail(seed)
        if "사" in place.title or "암" in place.title:
            tips.append("사찰에서는 법회와 참배를 방해하지 않도록 복장과 소음을 조심하세요.")
    except IntegrationError:
        pass
    return {"place_id": place_id, "tips": tips}


@compat_router.post("/visits/check-in")
def front_check_in(body: FrontCheckInRequest):
    return {"place_id": body.place_id, "completed": True, "reason": "프론트 호환 모드에서 로컬 방문 기록을 승인했습니다."}


@compat_router.get("/weather/current")
async def front_weather(settings: Settings = Depends(get_settings)):
    try:
        weather = await WeatherClient(settings).current(
            DEFAULT_LATITUDE,
            DEFAULT_LONGITUDE,
        )
    except IntegrationError as exc:
        raise HTTPException(exc.status_code, detail={"service": exc.service, "message": str(exc)}) from exc
    raining = bool(weather.get("raining"))
    hot = bool(weather.get("hot"))
    summary = "비" if raining else "맑음 또는 구름 조금"
    if hot:
        summary += " · 무더위"
    return {
        **weather,
        "summary": summary,
        "temperature": weather.get("temperature_c"),
        "outdoor_suitable": not raining and not hot,
    }


@compat_router.get("/contents/place/{place_id}")
async def front_contents(place_id: str, title: str = "", settings: Settings = Depends(get_settings)):
    resolved_title = title.strip()
    if not resolved_title:
        try:
            seed = Place(place_id=place_id, content_type_id="12", title=place_id, latitude=0, longitude=0)
            resolved_title = (await TourApiClient(settings).detail(seed)).title
        except IntegrationError:
            resolved_title = place_id
    blogs, videos, _ = await ContentService(settings).get(place_id, resolved_title)
    links = [
        {"title": item.title, "url": item.url, "type": item.source}
        for item in [*blogs, *videos]
        if item.url
    ]
    return {"place_id": place_id, "links": links}