from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from .clients import (
    CongestionClient,
    IntegrationError,
    KakaoLocalClient,
    KakaoRouteClient,
    NaverClient,
    OpenAIClient,
    RegionalVisitorClient,
    TourApiClient,
    WeatherClient,
    YouTubeClient,
    normalize_name,
)
from .config import Settings
from .db import (
    CommunityPostRecord,
    JourneyRecord,
    KnowledgeDocument,
    PlaceRecord,
    SessionLocal,
)
from .enrichment import PlaceInfoEnricher
from .geo import haversine_km
from .optimizer import Individual, optimize_courses
from .schemas import (
    ChatTurn,
    ContentItem,
    Course,
    CoursePlace,
    CourseType,
    JourneyOut,
    Place,
    RagHit,
    RagSearchResponse,
    RecalculateRequest,
    RecommendRequest,
    RecommendResponse,
    SyncResponse,
    TransportMode,
    VisitCheckResponse,
)


# ---------------------------------------------------------------------------
# 추천 후보 정책
# ---------------------------------------------------------------------------

PRIMARY_CATEGORIES = {
    "관광지",
    "문화시설",
    "축제·공연",
    "여행코스",
    "레포츠",
}


ALLOWED_USER_CONTENT_TYPE_IDS = {
    "12",  # 관광지
    "14",  # 문화시설
    "25",  # 여행코스
    "28",  # 관광형 레포츠
    "39",  # 음식점/카페
}

EXCLUDED_USER_CATEGORIES = {
    "숙박",
    "쇼핑",
}

NON_TOURIST_FACILITY_KEYWORDS = (
    "청소년수련",
    "청소년 수련",
    "청소년문화",
    "청소년 문화",
    "수련관",
    "축구공원",
    "축구장",
    "풋살",
    "야구장",
    "야구공원",
    "체육관",
    "체육센터",
    "생활체육",
    "시민운동장",
    "종합운동장",
    "운동장",
    "수영장",
    "볼링장",
    "골프장",
    "골프클럽",
    "스포츠센터",
)

LODGING_TITLE_KEYWORDS = (
    "호텔",
    "모텔",
    "펜션",
    "게스트하우스",
    "리조트",
    "콘도",
    "민박",
    "호스텔",
    "한옥스테이",
)


def is_user_facing_travel_place(place: Place) -> bool:
    """경주한적에 노출 가능한 관광지/음식점·카페만 통과시킵니다."""
    content_type_id = str(
        place.content_type_id or ""
    ).strip()

    category = (
        place.category or ""
    ).strip()

    title = (
        place.title or ""
    ).replace(" ", "").lower()

    if (
        content_type_id
        and content_type_id
        not in ALLOWED_USER_CONTENT_TYPE_IDS
    ):
        return False

    if category in EXCLUDED_USER_CATEGORIES:
        return False

    # 음식점/카페는 허용.
    if content_type_id == "39" or category == "음식점":
        return True

    if any(
        keyword.replace(" ", "").lower() in title
        for keyword in LODGING_TITLE_KEYWORDS
    ):
        return False

    if any(
        keyword.replace(" ", "").lower() in title
        for keyword in NON_TOURIST_FACILITY_KEYWORDS
    ):
        return False

    if content_type_id in {
        "12",
        "14",
        "25",
        "28",
    }:
        return True

    return category in PRIMARY_CATEGORIES

FOOD_PREFERENCES = {
    "맛집",
    "음식",
    "식사",
    "카페",
    "디저트",
}

# ---------------------------------------------------------------------------
# 인터랙티브 코스 추천 성능 예산
# ---------------------------------------------------------------------------
# 외부 API가 느린 경우에도 사용자 응답이 30초를 넘지 않도록
# 각 단계에 짧은 예산을 둡니다. 실패한 보조 신호는 기존 설계대로
# 제외하고 남은 신호를 재정규화합니다.
RECOMMEND_NEARBY_TIMEOUT_SECONDS = 3.0
RECOMMEND_DETAIL_TIMEOUT_SECONDS = 1.8
RECOMMEND_SIGNAL_TIMEOUT_SECONDS = 2.2
RECOMMEND_REGIONAL_TIMEOUT_SECONDS = 1.0
RECOMMEND_NAVER_TIMEOUT_SECONDS = 2.2
RECOMMEND_ENRICH_TIMEOUT_SECONDS = 0.8
RECOMMEND_ROUTE_TIMEOUT_SECONDS = 3.0
RECOMMEND_CANDIDATE_CAP = 14
RECOMMEND_NSGA_POPULATION_CAP = 32
RECOMMEND_NSGA_GENERATION_CAP = 18
RECOMMEND_SERVICE_CANDIDATE_CAP = 24
RECOMMEND_SERVICE_NAVER_CAP = 12
RECOMMEND_MIN_ATTRACTION_POOL = 6

CAFE_TITLE_KEYWORDS = {
    "카페", "cafe", "coffee", "커피", "베이커리", "디저트", "찻집", "다방", "브런치",
}
CAFE_CAT3_CODES = {"A05020900"}

SHOPPING_PREFERENCES = {
    "쇼핑",
    "시장",
    "기념품",
}

LODGING_PREFERENCES = {
    "숙박",
    "호텔",
    "한옥숙소",
    "리조트",
}


# ---------------------------------------------------------------------------
# 선호 키워드 사전
# ---------------------------------------------------------------------------

PREFERENCE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "문화유산": (
        "문화재",
        "유적",
        "유적지",
        "사찰",
        "절터",
        "사지",
        "왕릉",
        "고분",
        "고분군",
        "읍성",
        "산성",
        "성곽",
        "궁",
        "서원",
        "향교",
        "첨성대",
        "동궁",
        "월지",
        "월정교",
        "계림",
        "대릉원",
        "불국사",
        "석굴암",
        "분황사",
        "황룡사",
        "교촌",
        "양동마을",
        "국립경주박물관",
        "신라",
        "릉",
    ),
    "역사": (
        "문화재", "유적", "사찰", "사지", "릉", "왕릉", "고분",
        "성", "읍성", "궁", "서원", "향교", "박물관", "신라",
    ),
    "자연": (
        "산", "숲", "공원", "호수", "연못", "계곡", "수목원",
        "정원", "습지", "둘레길", "해변", "바다",
    ),
    "야경": (
        "야경", "월정교", "동궁", "월지", "첨성대", "보문", "야간", "빛",
    ),
    "체험": (
        "체험", "공방", "마을", "전통", "한복", "레포츠",
    ),
    "실내": (
        "문화시설", "박물관", "미술관", "전시", "실내",
    ),
    "카페": (
        "카페", "커피", "디저트", "베이커리",
    ),
    "맛집": (
        "음식점", "맛집", "식당", "한식", "카페",
    ),
    "쇼핑": (
        "쇼핑", "시장", "상점", "기념품",
    ),
}


# ===========================================================================
# 혼잡도 V2
#
# 1) 장소 baseline
# 2) 한국관광공사 분류 기반 시간/요일 패턴
# 3) NAVER 최근 관심도 변화(momentum)
# 4) 날씨 × 장소 특성
# 5) 공식 혼잡도(있을 때)
# 6) 장소의 공간 분산/수용 특성 보정
#
# 주의:
# - 지역별 방문자수(DataLabService)는 이미 신청된 API이지만, 현재 clients.py에
#   전용 Client가 없으므로 이 services.py 단독 교체본에서는 호출하지 않습니다.
# - 다음 단계에서 clients.py에 RegionalVisitorClient를 추가하면
#   CONGESTION_WEIGHT_REGIONAL을 활성화해 연결할 수 있습니다.
# ===========================================================================

# V2.1 튜닝:
# - 장소 고유 baseline의 영향 강화
# - 같은 유형끼리 점수를 뭉치게 만들던 시간대 영향 축소
# - 현재 거의 50 부근에 모이는 NAVER momentum 영향 축소
# - 날씨는 보정 신호로만 사용
# - 공식 혼잡도는 존재할 때 가장 신뢰도 높은 현재 신호로 조금 강화
# V2.4:
# 경주시 지역별 방문자수(regional demand)를 실제 반영하되,
# 데이터가 오래됐을수록 영향력을 자동으로 낮추는 freshness decay를 적용합니다.
#
# regional 최대 가중치:
# - 0~7일 지연   : 0.10
# - 8~14일 지연  : 0.07
# - 15~30일 지연 : 0.03
# - 31일 이상    : 0.00 (계산 제외)
#
# 나머지 신호 구조는 V2.2에서 고정합니다.
CONGESTION_WEIGHT_BASELINE = 0.35
CONGESTION_WEIGHT_TIME = 0.15

# V2.4 NAVER 분리:
# 검색 관심도의 '규모'와 '최근 상승/하락'을 같은 값으로 보지 않습니다.
CONGESTION_WEIGHT_NAVER_POPULARITY = 0.06
CONGESTION_WEIGHT_NAVER_MOMENTUM = 0.04

CONGESTION_WEIGHT_WEATHER = 0.05
CONGESTION_WEIGHT_OFFICIAL = 0.25
CONGESTION_WEIGHT_REGIONAL = 0.10


# ---------------------------------------------------------------------------
# 관광지 기본 혼잡 성향
#
# 관광지별 실제 방문통계 baseline을 붙이기 전의 초기값입니다.
# '분류' 자체는 한국관광공사 API를 우선 사용하고, 이 표는 유명 관광지의
# 기본 체급 차이가 사라지는 문제를 막기 위한 임시 baseline 보정입니다.
# ---------------------------------------------------------------------------

PLACE_BASELINE_SCORES: dict[str, float] = {
    "황리단길": 88.0,
    "경주월드": 84.0,
    "동궁과 월지": 82.0,
    "불국사": 82.0,
    "첨성대": 80.0,
    "대릉원": 78.0,
    "석굴암": 72.0,
    "월정교": 70.0,
    "국립경주박물관": 68.0,
    "교촌마을": 62.0,
    "보문관광단지": 62.0,
    "보문호": 55.0,
    "경주읍성": 48.0,
    "분황사": 45.0,
    "황룡사지": 42.0,
    "오릉": 40.0,
    "탈해왕릉": 32.0,
}


# 관광지 이름 매칭은 부분문자열이 아니라 명확한 별칭만 허용합니다.
# 예: "불국사밀면"이 "불국사" baseline을 받는 오류 방지.
PLACE_BASELINE_ALIASES: dict[str, tuple[str, ...]] = {
    "황리단길": ("황리단길", "경주 황리단길"),
    "경주월드": ("경주월드",),
    "동궁과 월지": (
        "동궁과 월지",
        "경주 동궁과 월지",
        "안압지",
    ),
    "불국사": ("불국사", "경주 불국사"),
    "첨성대": ("첨성대", "경주 첨성대"),
    "대릉원": (
        "대릉원",
        "경주 대릉원",
        "천마총(대릉원)",
        "천마총 대릉원",
    ),
    "석굴암": ("석굴암", "경주 석굴암"),
    "월정교": ("월정교", "경주 월정교"),
    "국립경주박물관": (
        "국립경주박물관",
        "경주 국립경주박물관",
    ),
    "교촌마을": (
        "교촌마을",
        "경주 교촌마을",
        "경주교촌마을",
    ),
    "보문관광단지": (
        "보문관광단지",
        "경주 보문관광단지",
    ),
    "보문호": (
        "보문호",
        "경주 보문호",
        "보문호수",
    ),
    "경주읍성": ("경주읍성", "경주 읍성"),
    "분황사": ("분황사", "경주 분황사"),
    "황룡사지": ("황룡사지", "경주 황룡사지"),
    "오릉": ("오릉", "경주 오릉"),
    "탈해왕릉": (
        "탈해왕릉",
        "경주 탈해왕릉",
    ),
}

CATEGORY_BASELINE_SCORES: dict[str, float] = {
    "관광지": 48.0,
    "문화시설": 50.0,
    "축제·공연": 70.0,
    "여행코스": 42.0,
    "레포츠": 52.0,
    "쇼핑": 60.0,
    "음식점": 58.0,
    "숙박": 42.0,
    "기타": 45.0,
}


# 한국관광공사 contentTypeId
TOUR_CONTENT_PROFILE: dict[str, str] = {
    "12": "heritage",      # 관광지: 세부 분류(raw)가 있으면 그것을 우선
    "14": "indoor",        # 문화시설
    "15": "event",         # 축제/공연/행사
    "25": "course",        # 여행코스
    "28": "leisure",       # 레포츠
    "32": "lodging",       # 숙박
    "38": "commercial",    # 쇼핑
    "39": "commercial",    # 음식점
}

# 신분류체계 대분류 중 확실하게 활용 가능한 코드.
# raw에 lclsSystm1이 들어와 있을 때 contentTypeId보다 먼저 참고합니다.
KTO_LCLS1_PROFILE: dict[str, str] = {
    "NA": "nature",        # 자연관광
    "EV": "event",         # 축제/공연/행사
    "SH": "commercial",    # 쇼핑
    "FD": "commercial",    # 음식
}

# 한국관광공사 기본 분류만으로는 '야경 집중형'을 표현하기 어렵기 때문에
# 혼잡 패턴이 명확히 다른 장소만 최소 예외로 둡니다.
SPECIAL_NIGHT_SPOTS = {
    "동궁과 월지",
    "월정교",
}


# ---------------------------------------------------------------------------
# 혼잡도 V2 디버깅 대상
#
# 아래 관광지는 혼잡도 계산 시 FastAPI 터미널에
# 최종 점수와 각 구성요소가 출력됩니다.
# V2 튜닝이 끝난 뒤 이 상수와 출력 블록은 삭제해도 됩니다.
# ---------------------------------------------------------------------------

DEBUG_CONGESTION_PLACES = {
    "첨성대",
    "대릉원",
    "불국사",
    "동궁과 월지",
    "월정교",
    "국립경주박물관",
    "보문호",
    "탈해왕릉",
}


KST = timezone(timedelta(hours=9))


# ---------------------------------------------------------------------------
# 커뮤니티 현장 혼잡 제보 -> 코스용 실시간 보정
# ---------------------------------------------------------------------------
# V2.4.4의 baseline/time/NAVER/weather/official/regional 가중치는 절대
# 변경하지 않습니다. 아래 값은 그 결과(congestion_score)를 덮어쓰지 않고
# 코스 추천에서만 사용할 routing_congestion_score를 만드는 보조 신호입니다.
COMMUNITY_LIVE_WINDOW_HOURS = 2
COMMUNITY_LIVE_MAX_INFLUENCE = 0.20


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _community_live_signal_map(
    place_ids: list[str],
    *,
    now: datetime | None = None,
    hours: int = COMMUNITY_LIVE_WINDOW_HOURS,
) -> dict[str, dict[str, float | int | datetime]]:
    """최근 현장 제보를 장소별로 신선도 가중 평균합니다.

    - 2시간 창 밖의 제보는 코스 추천에 사용하지 않습니다.
    - 오래된 제보일수록 선형으로 영향이 감소합니다.
    - V2.4.4 원본 점수와의 실제 결합은 _apply_community_live_signal()에서
      최대 20%까지만 수행합니다.
    """
    ids = list(dict.fromkeys(str(value).strip() for value in place_ids if str(value).strip()))
    if not ids:
        return {}

    current = _as_aware_utc(now) or datetime.now(timezone.utc)
    window_seconds = max(1.0, float(hours) * 3600.0)
    cutoff = current - timedelta(hours=hours)

    db = SessionLocal()
    try:
        rows = db.scalars(
            select(CommunityPostRecord).where(
                CommunityPostRecord.post_type == "live",
                CommunityPostRecord.place_id.in_(ids),
                CommunityPostRecord.observed_at >= cutoff,
            )
        ).all()
    except Exception:
        # 커뮤니티 테이블이 아직 생성되지 않았거나 DB가 일시적으로 잠긴 경우에도
        # 기존 혼잡도/코스 추천은 그대로 동작해야 합니다.
        return {}
    finally:
        db.close()

    grouped: dict[str, list[tuple[float, float, datetime]]] = {}
    for row in rows:
        if row.place_id is None or row.crowd_percent is None or row.observed_at is None:
            continue
        observed = _as_aware_utc(row.observed_at)
        if observed is None:
            continue
        age_seconds = max(0.0, (current - observed).total_seconds())
        if age_seconds > window_seconds:
            continue
        freshness = max(0.0, 1.0 - age_seconds / window_seconds)
        if freshness <= 0:
            continue
        grouped.setdefault(str(row.place_id), []).append(
            (float(row.crowd_percent), freshness, observed)
        )

    result: dict[str, dict[str, float | int | datetime]] = {}
    for place_id, values in grouped.items():
        total_weight = sum(weight for _, weight, _ in values)
        if total_weight <= 0:
            continue
        weighted_average = sum(score * weight for score, weight, _ in values) / total_weight
        average_freshness = total_weight / len(values)

        # 제보 1건은 최대 8%, 2건 12%, 3건 16%, 4건 이상 최대 20%.
        # 여기에 평균 신선도를 곱해 오래된 제보의 영향은 추가로 줄입니다.
        raw_influence = min(
            COMMUNITY_LIVE_MAX_INFLUENCE,
            0.08 + 0.04 * max(0, len(values) - 1),
        )
        influence = raw_influence * average_freshness

        result[place_id] = {
            "score": round(max(0.0, min(100.0, weighted_average)), 2),
            "count": len(values),
            "latest_observed_at": max(observed for _, _, observed in values),
            "influence": round(max(0.0, min(COMMUNITY_LIVE_MAX_INFLUENCE, influence)), 4),
        }

    return result


def _apply_community_live_signal(
    place: Place,
    signal: dict[str, float | int | datetime] | None,
) -> None:
    """V2.4.4를 보존하면서 코스용 실시간 혼잡도만 별도로 계산합니다."""
    place.community_congestion_score = None
    place.community_report_count = 0
    place.community_latest_observed_at = None
    place.routing_congestion_score = place.congestion_score

    if not signal:
        return

    score = signal.get("score")
    if score is None:
        return

    community_score = max(0.0, min(100.0, float(score)))
    count = max(0, int(signal.get("count") or 0))
    influence = max(
        0.0,
        min(COMMUNITY_LIVE_MAX_INFLUENCE, float(signal.get("influence") or 0.0)),
    )

    place.community_congestion_score = round(community_score, 2)
    place.community_report_count = count

    latest = signal.get("latest_observed_at")
    if isinstance(latest, datetime):
        place.community_latest_observed_at = latest

    if place.congestion_score is None:
        # 원본 모델값이 비어 있을 때만 커뮤니티 값을 fallback으로 사용합니다.
        place.routing_congestion_score = round(community_score, 2)
        return

    base = float(place.congestion_score)
    routed = base * (1.0 - influence) + community_score * influence
    place.routing_congestion_score = round(max(0.0, min(100.0, routed)), 2)


def _route_congestion(place: Place) -> float | None:
    """코스 추천용 혼잡도. 화면용 V2.4.4 값과 분리합니다."""
    if place.routing_congestion_score is not None:
        return place.routing_congestion_score
    return place.congestion_score


def _route_raw_flag(
    place: Place,
    key: str,
) -> bool:
    raw = place.raw or {}

    if not isinstance(raw, dict):
        return False

    return bool(raw.get(key))


def _mark_route_flag(
    place: Place,
    key: str,
) -> Place:
    raw = (
        dict(place.raw)
        if isinstance(place.raw, dict)
        else {}
    )

    raw[key] = True
    place.raw = raw
    return place


START_PLACE_GENERIC_PENALTIES = (
    "주차장", "정류장", "화장실", "매표소", "입구", "쉼터",
    "전망대", "산책로", "둘레길",
)
START_PLACE_MICRO_NATURE_SUFFIXES = ("골", "계곡", "봉")
START_PLACE_MAJOR_HINTS = (
    "불국사", "석굴암", "첨성대", "동궁", "월지", "월정교",
    "대릉원", "계림", "분황사", "황룡사", "박물관", "미술관",
    "왕릉", "고분", "고분군", "읍성", "산성", "향교", "서원",
    "사찰", "테마파크", "경주월드", "황리단길", "전통마을", "민속마을",
)


def _start_place_name(value: str) -> str:
    text = re.sub(r"^\s*경주(?:시)?\s+", "", value.strip())
    return normalize_name(text)


def _start_place_names_match(left: str, right: str) -> bool:
    a = _start_place_name(left)
    b = _start_place_name(right)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return (
        len(shorter) >= 3
        and longer.startswith(shorter)
        and len(longer) - len(shorter) <= 8
    )


def _start_place_quality(title: str, category: str = "") -> float:
    text = f"{title} {category}".lower()
    score = 0.0

    if any(
        keyword in text
        for keyword in (
            "문화유적", "문화재", "사찰", "세계문화유산", "역사유적",
            "왕릉", "고분", "박물관", "테마파크", "관광거리", "전통마을",
        )
    ):
        score += 45.0

    if any(
        keyword.lower() in text
        for keyword in START_PLACE_MAJOR_HINTS
    ):
        score += 35.0

    if any(
        keyword in title
        for keyword in START_PLACE_GENERIC_PENALTIES
    ):
        score -= 80.0

    if any(
        title.strip().endswith(suffix)
        for suffix in START_PLACE_MICRO_NATURE_SUFFIXES
    ):
        score -= 45.0

    return score


def select_representative_start_place(
    tour_places: list[Place],
    kakao_rows: list[dict],
    latitude: float,
    longitude: float,
    *,
    max_distance_km: float = 1.5,
) -> Place | None:
    """
    TourAPI와 Kakao를 함께 비교해 출발 핀의 대표 관광지를 선택합니다.
    """
    tour_candidates: list[tuple[Place, float]] = []
    for place in tour_places:
        distance_km = haversine_km(
            latitude,
            longitude,
            place.latitude,
            place.longitude,
        )
        if distance_km <= max_distance_km:
            tour_candidates.append((place, distance_km))

    kakao_candidates: list[tuple[dict, float]] = []
    for row in kakao_rows:
        row_lat = row.get("latitude")
        row_lon = row.get("longitude")
        if row_lat is None or row_lon is None:
            continue
        distance_km = haversine_km(
            latitude,
            longitude,
            float(row_lat),
            float(row_lon),
        )
        if distance_km <= max_distance_km:
            kakao_candidates.append((row, distance_km))

    best: tuple[float, Place] | None = None

    for place, distance_km in tour_candidates:
        cross_source = any(
            _start_place_names_match(
                place.title,
                str(row.get("title") or ""),
            )
            for row, _ in kakao_candidates
        )

        score = (
            (140.0 if cross_source else 0.0)
            + _start_place_quality(
                place.title,
                place.category or "",
            )
            + max(
                0.0,
                80.0
                - distance_km / max(max_distance_km, 0.1) * 80.0,
            )
            + 10.0
        )

        if best is None or score > best[0]:
            best = (score, place)

    for row, distance_km in kakao_candidates:
        title = str(row.get("title") or "").strip()
        if not title:
            continue

        kakao_place = _kakao_row_to_place(row)
        if kakao_place is None:
            continue

        cross_source = any(
            _start_place_names_match(title, place.title)
            for place, _ in tour_candidates
        )

        score = (
            (140.0 if cross_source else 0.0)
            + _start_place_quality(
                title,
                str(row.get("category") or ""),
            )
            + max(
                0.0,
                80.0
                - distance_km / max(max_distance_km, 0.1) * 80.0,
            )
        )

        if best is None or score > best[0]:
            best = (score, kakao_place)

    if best is None:
        return None

    selected = best[1].model_copy(deep=True)
    selected.distance_km = haversine_km(
        latitude,
        longitude,
        selected.latitude,
        selected.longitude,
    )
    return selected


REQUIRED_SUBPLACE_KEYWORDS = (
    "스노우파크",
    "캘리포니아비치",
    "워터파크",
    "눈썰매장",
    "주차장",
    "매표소",
    "게이트",
    "리조트",
    "호텔",
)

REQUIRED_MAIN_PLACE_HINTS = (
    "어뮤즈먼트",
    "테마파크",
)


def _required_normalized_name(
    value: str,
) -> str:
    # "경주 첨성대"의 지역 접두어는 제거하지만
    # 고유명사 "경주월드"의 경주는 제거하지 않습니다.
    text = re.sub(
        r"^\s*경주(?:시)?\s+",
        "",
        value.strip(),
    )

    return normalize_name(
        text
    )


def _required_subplace_penalty(
    requested: str,
    title: str,
) -> int:
    wanted = _required_normalized_name(
        requested
    )
    normalized_title = (
        _required_normalized_name(
            title
        )
    )

    if not wanted or not normalized_title:
        return 0

    penalty = 0

    for keyword in REQUIRED_SUBPLACE_KEYWORDS:
        normalized_keyword = normalize_name(
            keyword
        )

        if (
            normalized_keyword
            in normalized_title
            and normalized_keyword
            not in wanted
        ):
            penalty += 10

    return penalty


def _required_match_rank(
    requested: str,
    title: str,
) -> tuple[
    int,
    int,
    int,
]:
    wanted = _required_normalized_name(
        requested
    )
    candidate = _required_normalized_name(
        title
    )

    if not wanted or not candidate:
        return (
            99,
            99,
            999,
        )

    if candidate == wanted:
        relation = 0
    elif candidate.startswith(
        wanted
    ):
        relation = 1
    elif wanted in candidate:
        relation = 2
    elif candidate in wanted:
        relation = 3
    else:
        relation = 99

    return (
        relation,
        _required_subplace_penalty(
            requested,
            title,
        ),
        abs(
            len(candidate)
            - len(wanted)
        ),
    )


def _is_weak_required_subplace(
    requested: str,
    title: str,
) -> bool:
    relation, penalty, _ = (
        _required_match_rank(
            requested,
            title,
        )
    )

    return (
        relation < 99
        and penalty > 0
    )


def _kakao_row_to_place(
    row: dict,
) -> Place | None:
    latitude = row.get(
        "latitude"
    )
    longitude = row.get(
        "longitude"
    )
    title = str(
        row.get(
            "title"
        )
        or ""
    ).strip()

    if (
        not title
        or latitude is None
        or longitude is None
    ):
        return None

    return Place(
        place_id=(
            f"kakao:"
            f"{row.get('id') or normalize_name(title)}"
        ),
        content_type_id="12",
        title=title,
        category="관광지",
        address=(
            str(
                row.get(
                    "road_address"
                )
                or row.get(
                    "address"
                )
                or ""
            ).strip()
            or None
        ),
        latitude=float(
            latitude
        ),
        longitude=float(
            longitude
        ),
        tel=(
            str(
                row.get(
                    "phone"
                )
                or ""
            ).strip()
            or None
        ),
        raw={
            "source":
                "kakao_local",
            "kakao_place_url":
                row.get(
                    "place_url"
                )
                or "",
        },
    )


def _required_index_for_name(
    places: list[Place],
    name: str,
    used: set[int],
) -> int | None:
    candidates: list[
        tuple[
            int,
            int,
            int,
            float,
            int,
        ]
    ] = []

    for index, place in enumerate(
        places
    ):
        if index in used:
            continue

        relation, subplace_penalty, length_diff = (
            _required_match_rank(
                name,
                place.title,
            )
        )

        if relation >= 99:
            continue

        candidates.append(
            (
                relation,
                subplace_penalty,
                length_diff,
                (
                    place.distance_km
                    if place.distance_km
                    is not None
                    else 9999.0
                ),
                index,
            )
        )

    if not candidates:
        return None

    candidates.sort()

    return candidates[0][-1]


def _matches_required_place(
    place: Place,
    names: list[str],
) -> bool:
    for name in names:
        relation, penalty, _ = (
            _required_match_rank(
                name,
                place.title,
            )
        )

        if (
            relation < 99
            and penalty == 0
        ):
            return True

    return False


def _progressive_route_individual(
    places: list[Place],
    request: RecommendRequest,
    max_places: int,
) -> Individual | None:
    """
    실제 여행 흐름 중심 코스 선택.

    1. 현재 위치에서 가까운 사용자 취향 관광지를 먼저 선택
    2. 다음 관광지는 직전 장소 기준으로 가까운 곳을 선택
    3. 필수 장소가 멀면 그 방향으로 진행하는 관광지를 먼저 선택한 뒤
       필수 장소에 도착
    4. 기존 recommendation_score의 45/30/25 계산은 그대로 유지하고,
       여기서는 '동선 순서'만 추가 평가
    """
    if len(places) < 2:
        return None

    service_slots = int(request.include_food) + int(request.include_cafe)

    # 4시간 기준 관광지 3~4곳 정도가 자연스럽도록 시간 예산을 반영.
    time_cap = max(
        2,
        min(
            5,
            max(
                2,
                request.available_minutes // 70,
            ),
        ),
    )

    attraction_cap = max(
        2,
        min(
            len(places),
            max(
                2,
                max_places - min(service_slots, 2),
            ),
            time_cap,
        ),
    )

    used: set[int] = set()
    selected: list[int] = []

    required_indices: list[int] = []

    for name in request.required_place_names:
        index = _required_index_for_name(
            places,
            name,
            set(required_indices),
        )

        if index is not None and index not in required_indices:
            required_indices.append(index)

    attraction_cap = max(
        attraction_cap,
        min(
            len(places),
            len(required_indices),
        ),
    )

    current = (
        request.latitude,
        request.longitude,
    )

    def base_score(index: int) -> float:
        place = places[index]
        return float(
            place.recommendation_score
            if place.recommendation_score is not None
            else 50.0
        )

    def preference_score(index: int) -> float:
        value = places[index].preference_score
        return max(
            0.0,
            min(
                100.0,
                float(value or 0.0) * 100.0,
            ),
        )

    def leg_score(
        index: int,
        origin: tuple[float, float],
    ) -> float:
        place = places[index]
        km = haversine_km(
            origin[0],
            origin[1],
            place.latitude,
            place.longitude,
        )

        reference = max(
            3.0,
            min(
                12.0,
                request.radius_km,
            ),
        )

        return max(
            0.0,
            100.0 - km / reference * 100.0,
        )

    def choose_nearby(
        origin: tuple[float, float],
        *,
        excluded: set[int],
        target_index: int | None = None,
    ) -> int | None:
        candidates = [
            index
            for index in range(len(places))
            if index not in excluded
            and index not in required_indices
        ]

        if not candidates:
            return None

        # 사용자가 테마를 선택한 경우, 우선 실제 preference_score가 있는 후보만.
        if request.preferences:
            theme_matches = [
                index
                for index in candidates
                if preference_score(index) >= 45.0
            ]

            if theme_matches:
                candidates = theme_matches

        best: tuple[float, int] | None = None

        for index in candidates:
            place = places[index]
            local_score = leg_score(
                index,
                origin,
            )

            # 기본 장소 점수는 기존 45% 한적도 + 30% 취향 + 25% 출발지 거리.
            score = (
                base_score(index) * 0.52
                + local_score * 0.33
                + preference_score(index) * 0.15
            )

            if target_index is not None:
                target = places[target_index]

                direct = haversine_km(
                    origin[0],
                    origin[1],
                    target.latitude,
                    target.longitude,
                )

                to_candidate = haversine_km(
                    origin[0],
                    origin[1],
                    place.latitude,
                    place.longitude,
                )

                candidate_to_target = haversine_km(
                    place.latitude,
                    place.longitude,
                    target.latitude,
                    target.longitude,
                )

                # 후보를 들렀다가 필수 장소로 가는 우회량.
                detour = max(
                    0.0,
                    to_candidate
                    + candidate_to_target
                    - direct,
                )

                corridor_limit = max(
                    2.0,
                    min(
                        6.0,
                        direct * 0.40,
                    ),
                )

                corridor_score = max(
                    0.0,
                    100.0
                    - detour / corridor_limit * 100.0,
                )

                progress = (
                    direct
                    - candidate_to_target
                )

                progress_score = max(
                    0.0,
                    min(
                        100.0,
                        progress
                        / max(direct, 0.1)
                        * 100.0,
                    ),
                )

                # 필수 장소 반대 방향으로 가는 후보는 강하게 감점.
                if progress <= -0.5:
                    score -= 45.0

                # 가까움 + 필수 장소 방향성 반영.
                score = (
                    score * 0.62
                    + corridor_score * 0.23
                    + progress_score * 0.15
                )

            if best is None or score > best[0]:
                best = (
                    score,
                    index,
                )

        return None if best is None else best[1]

    # ----------------------------------------------------------
    # 첫 관광지는 "출발지에서 실제로 가장 가까운 조건 일치 장소" 우선.
    # 맛집/카페는 관광지 테마가 아니므로 여기서 제외합니다.
    # ----------------------------------------------------------
    attraction_preferences = [
        preference
        for preference
        in request.preferences
        if (
            preference.strip().lower()
            not in FOOD_PREFERENCES
        )
    ]

    start_candidates: list[
        tuple[
            float,
            float,
            int,
        ]
    ] = []

    for index, place in enumerate(
        places
    ):
        theme_score = 1.0

        if attraction_preferences:
            matches = [
                _preference_match(
                    place,
                    preference,
                )
                for preference
                in attraction_preferences
            ]

            theme_score = (
                sum(matches)
                / len(matches)
                if matches
                else 0.0
            )

        if (
            attraction_preferences
            and theme_score < 0.5
            and index not in required_indices
        ):
            continue

        distance = haversine_km(
            request.latitude,
            request.longitude,
            place.latitude,
            place.longitude,
        )

        # 실제 거리가 우선, 같은 생활권 안에서는 추천점수로 tie-break.
        start_candidates.append(
            (
                distance,
                -base_score(index),
                index,
            )
        )

    if start_candidates:
        start_candidates.sort()
        first_index = start_candidates[
            0
        ][2]

        selected.append(
            first_index
        )
        used.add(
            first_index
        )

        first_place = places[
            first_index
        ]

        current = (
            first_place.latitude,
            first_place.longitude,
        )

    # ----------------------------------------------------------
    # 필수 장소가 있을 때: 현재 위치 -> 중간 관광지 -> 필수 장소
    # ----------------------------------------------------------
    for required_position, target_index in enumerate(required_indices):
        if len(selected) >= attraction_cap:
            break

        target = places[target_index]

        direct_km = haversine_km(
            current[0],
            current[1],
            target.latitude,
            target.longitude,
        )

        remaining_required = (
            len(required_indices)
            - required_position
        )

        free_slots = max(
            0,
            attraction_cap
            - len(selected)
            - remaining_required,
        )

        if direct_km >= 12:
            wanted_intermediate = 2
        elif direct_km >= 5:
            wanted_intermediate = 1
        else:
            wanted_intermediate = 0

        wanted_intermediate = min(
            wanted_intermediate,
            free_slots,
        )

        for _ in range(wanted_intermediate):
            candidate_index = choose_nearby(
                current,
                excluded=(
                    used
                    | {target_index}
                ),
                target_index=target_index,
            )

            if candidate_index is None:
                break

            candidate = places[candidate_index]

            # 실제로 필수 장소 방향으로 전진하는지 마지막 확인.
            before = haversine_km(
                current[0],
                current[1],
                target.latitude,
                target.longitude,
            )

            after = haversine_km(
                candidate.latitude,
                candidate.longitude,
                target.latitude,
                target.longitude,
            )

            if after > before + 0.8:
                break

            selected.append(candidate_index)
            used.add(candidate_index)
            current = (
                candidate.latitude,
                candidate.longitude,
            )

        if target_index not in used:
            selected.append(target_index)
            used.add(target_index)
            current = (
                target.latitude,
                target.longitude,
            )

    # ----------------------------------------------------------
    # 필수 장소가 없거나 남은 시간이 있으면 직전 장소 기준으로 이어가기.
    # ----------------------------------------------------------
    while len(selected) < attraction_cap:
        candidate_index = choose_nearby(
            current,
            excluded=used,
        )

        if candidate_index is None:
            break

        candidate = places[candidate_index]

        # 코스 후반에 갑자기 매우 먼 곳으로 튀는 것을 방지.
        leg_km = haversine_km(
            current[0],
            current[1],
            candidate.latitude,
            candidate.longitude,
        )

        # UI에서 탐색 반경을 받지 않으므로 radius_km를 동선 하드필터로
        # 다시 사용하지 않습니다. 코스 후반의 비정상적인 장거리 점프만
        # 이동수단별 안전 상한으로 막습니다.
        max_leg_km = {
            TransportMode.walking: 6.0,
            TransportMode.public_transport: 14.0,
            TransportMode.driving: 20.0,
        }.get(request.transport, 12.0)

        if selected and leg_km > max_leg_km:
            break

        selected.append(candidate_index)
        used.add(candidate_index)
        current = (
            candidate.latitude,
            candidate.longitude,
        )

    if len(selected) < 2:
        return None

    # ----------------------------------------------------------
    # 기존 Course 결과 호환용 objective 값 계산.
    # ----------------------------------------------------------
    current = (
        request.latitude,
        request.longitude,
    )

    travel_minutes = 0
    congestion = 0.0
    mismatch = 0.0

    for index in selected:
        place = places[index]

        leg_km = haversine_km(
            current[0],
            current[1],
            place.latitude,
            place.longitude,
        )

        travel_minutes += _estimate_minutes(
            leg_km,
            request.transport,
        )

        route_congestion = _route_congestion(place)
        congestion += float(
            route_congestion
            if route_congestion is not None
            else 50.0
        )

        mismatch += (
            1.0
            - max(
                0.0,
                min(
                    1.0,
                    float(
                        place.preference_score
                        or 0.0
                    ),
                ),
            )
        )

        current = (
            place.latitude,
            place.longitude,
        )

    individual = Individual(
        genes=selected,
    )

    individual.total_minutes = travel_minutes
    individual.total_distance_km = 0.0
    individual.objectives = (
        float(travel_minutes),
        float(congestion),
        float(mismatch),
    )

    return individual


def _estimate_minutes(
    distance_km: float,
    mode: TransportMode,
) -> int:
    if distance_km <= 0:
        return 0

    if mode == TransportMode.walking:
        return max(1, math.ceil(distance_km / 4.5 * 60))

    if mode == TransportMode.public_transport:
        return max(1, math.ceil(distance_km / 18.0 * 60 + 8))

    return max(1, math.ceil(distance_km / 30.0 * 60))


def _safe_local_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.astimezone()
    return value


def _raw_value(place: Place, *keys: str) -> str:
    """TourAPI 원본 raw에서 대소문자 차이까지 흡수해 값을 읽습니다."""
    raw = place.raw or {}
    if not raw:
        return ""

    lowered = {
        str(key).lower(): value
        for key, value in raw.items()
    }

    for key in keys:
        value = raw.get(key)
        if value in (None, ""):
            value = lowered.get(key.lower())

        if value not in (None, ""):
            return str(value).strip()

    return ""


def _place_profile(place: Place) -> str:
    """
    관광공사 API 분류를 우선 활용해 혼잡 패턴용 profile을 반환합니다.

    우선순위:
      1. 정말 필요한 최소 야간 예외
      2. TourAPI raw의 신분류체계(lclsSystm1)
      3. contentTypeId
      4. 현재 Place.category
      5. fallback

    즉 제목/overview에서 '박물관', '공원'을 일일이 찾는 방식은 제거합니다.
    """

    normalized_title = normalize_name(place.title)

    if place.is_night_spot or any(
        normalize_name(name) in normalized_title
        for name in SPECIAL_NIGHT_SPOTS
    ):
        return "night"

    lcls1 = _raw_value(
        place,
        "lclsSystm1",
        "lclssystm1",
    ).upper()

    if lcls1 in KTO_LCLS1_PROFILE:
        return KTO_LCLS1_PROFILE[lcls1]

    content_type_id = (
        place.content_type_id
        or _raw_value(place, "contenttypeid", "contentTypeId")
        or ""
    ).strip()

    if content_type_id in TOUR_CONTENT_PROFILE:
        return TOUR_CONTENT_PROFILE[content_type_id]

    category_map = {
        "문화시설": "indoor",
        "축제·공연": "event",
        "레포츠": "leisure",
        "여행코스": "course",
        "쇼핑": "commercial",
        "음식점": "commercial",
        "숙박": "lodging",
        "관광지": "heritage",
    }

    return category_map.get(
        (place.category or "").strip(),
        "heritage",
    )


def _canonical_place_title(
    value: str,
) -> str:
    """
    관광공사 제목 뒤의 설명용 대괄호 꼬리표를 제거해
    baseline/별칭 매칭에 사용할 대표 제목을 만듭니다.

    예:
    "경주 불국사 [유네스코 세계유산]"
        -> "경주 불국사"

    괄호는 천마총(대릉원)처럼 실제 장소명 일부일 수 있으므로
    여기서는 제거하지 않습니다.
    """
    cleaned = re.sub(
        r"\s*\[[^\]]+\]\s*",
        " ",
        value or "",
    )

    return re.sub(
        r"\s+",
        " ",
        cleaned,
    ).strip()


def _place_baseline_score(place: Place) -> float:
    """
    장소 자체의 기본 혼잡 성향(0~100).

    관광지별 실제 방문자 통계가 구축되기 전:
      주요 명소 임시 baseline -> 관광공사 분류/category baseline 순으로 사용.
    """
    normalized_title = normalize_name(
        _canonical_place_title(
            place.title
        )
    )

    for title, score in PLACE_BASELINE_SCORES.items():
        aliases = PLACE_BASELINE_ALIASES.get(
            title,
            (title,),
        )

        normalized_aliases = {
            normalize_name(alias)
            for alias in aliases
        }

        if normalized_title in normalized_aliases:
            return score

    profile = _place_profile(place)

    profile_baseline = {
        "night": 64.0,
        "nature": 42.0,
        "indoor": 52.0,
        "event": 72.0,
        "commercial": 62.0,
        "leisure": 54.0,
        "course": 42.0,
        "lodging": 40.0,
        "heritage": 48.0,
    }

    return profile_baseline.get(
        profile,
        CATEGORY_BASELINE_SCORES.get(place.category, 45.0),
    )


def _time_congestion_score(
    place: Place,
    when: datetime,
) -> float:
    """
    관광공사 분류 기반 장소 유형별 시간/요일 혼잡 신호(0~100).
    """
    local = _safe_local_datetime(when)
    hour = local.hour
    profile = _place_profile(place)

    if profile == "night":
        if 8 <= hour <= 10:
            score = 30.0
        elif 11 <= hour <= 16:
            score = 45.0
        elif 17 <= hour <= 18:
            score = 68.0
        elif 19 <= hour <= 21:
            score = 92.0
        else:
            score = 25.0

    elif profile == "indoor":
        if 9 <= hour <= 10:
            score = 45.0
        elif 11 <= hour <= 16:
            score = 72.0
        elif 17 <= hour <= 18:
            score = 52.0
        else:
            score = 20.0

    elif profile == "commercial":
        # 음식점과 쇼핑은 같은 KTO commercial 계열이지만 실제 피크가 다릅니다.
        # 음식점은 점심/저녁, 쇼핑은 오후~저녁 중심으로 분리합니다.
        category = (place.category or "").strip()
        if category == "음식점":
            if 8 <= hour <= 10:
                score = 28.0
            elif 11 <= hour <= 14:
                score = 86.0
            elif 15 <= hour <= 17:
                score = 48.0
            elif 18 <= hour <= 21:
                score = 92.0
            else:
                score = 24.0
        elif category == "쇼핑":
            if 9 <= hour <= 11:
                score = 42.0
            elif 12 <= hour <= 17:
                score = 74.0
            elif 18 <= hour <= 20:
                score = 70.0
            else:
                score = 28.0
        else:
            if 8 <= hour <= 10:
                score = 35.0
            elif 11 <= hour <= 14:
                score = 78.0
            elif 15 <= hour <= 17:
                score = 70.0
            elif 18 <= hour <= 21:
                score = 88.0
            else:
                score = 32.0

    elif profile == "lodging":
        # 숙박은 관광지처럼 한낮이 붐비기보다 체크인/저녁 시간에 수요가 집중됩니다.
        if 7 <= hour <= 10:
            score = 48.0
        elif 11 <= hour <= 14:
            score = 30.0
        elif 15 <= hour <= 18:
            score = 68.0
        elif 19 <= hour <= 22:
            score = 72.0
        else:
            score = 34.0

    elif profile == "nature":
        if 7 <= hour <= 10:
            score = 40.0
        elif 11 <= hour <= 16:
            score = 58.0
        elif 17 <= hour <= 19:
            score = 55.0
        else:
            score = 25.0

    elif profile == "event":
        if 10 <= hour <= 20:
            score = 82.0
        else:
            score = 40.0

    elif profile == "leisure":
        if 9 <= hour <= 11:
            score = 50.0
        elif 12 <= hour <= 17:
            score = 72.0
        elif 18 <= hour <= 20:
            score = 50.0
        else:
            score = 25.0

    elif profile == "course":
        if 9 <= hour <= 11:
            score = 45.0
        elif 12 <= hour <= 17:
            score = 62.0
        elif 18 <= hour <= 20:
            score = 48.0
        else:
            score = 25.0

    else:
        # 일반 역사/문화 유적
        #
        # V2.1:
        # V2에서는 11~16시 점수가 76으로 너무 높아
        # 소규모 유적도 baseline과 무관하게 50~60점대로 끌어올리는 경향이 있었다.
        # 그래서 낮 시간대 영향은 유지하되 완만하게 조정한다.
        if 8 <= hour <= 10:
            score = 42.0
        elif 11 <= hour <= 16:
            score = 68.0
        elif 17 <= hour <= 19:
            score = 58.0
        else:
            score = 26.0

    # 주말 효과를 토/일 동일값으로 뭉치지 않고 금요일 저녁과 일요일을 완만하게 구분합니다.
    weekday = local.weekday()
    if weekday == 5:  # 토요일
        if profile in {"commercial", "night", "event"}:
            score += 14.0
        elif profile != "lodging":
            score += 10.0
    elif weekday == 6:  # 일요일
        if profile in {"commercial", "night", "event"}:
            score += 11.0
        elif profile != "lodging":
            score += 8.0
    elif weekday == 4 and hour >= 17:  # 금요일 저녁
        if profile in {"commercial", "night", "event"}:
            score += 7.0
        elif profile != "lodging":
            score += 4.0

    return max(0.0, min(100.0, score))


def _weather_congestion_score(
    place: Place,
    weather: dict,
) -> float:
    """
    날씨 × 한국관광공사 장소 분류를 반영한 혼잡 신호(0~100).
    """
    if not weather:
        return 50.0

    profile = _place_profile(place)
    raining = bool(weather.get("raining"))
    hot = bool(weather.get("hot"))

    if raining:
        if profile == "indoor":
            return 78.0
        if profile == "commercial":
            return 50.0
        if profile == "night":
            return 30.0
        if profile == "nature":
            return 22.0
        if profile == "event":
            return 45.0
        if profile == "leisure":
            return 28.0
        return 28.0

    if hot:
        if profile == "indoor":
            return 72.0
        if profile == "commercial":
            return 52.0
        if profile == "nature":
            return 32.0
        if profile == "night":
            return 45.0
        if profile == "leisure":
            return 38.0
        return 35.0

    if profile == "nature":
        return 58.0
    if profile == "night":
        return 55.0

    return 50.0


def _capacity_factor(place: Place) -> float:
    """
    논문의 '수용가능 규모 대비 혼잡' 아이디어를 현재 데이터 범위에서
    보수적으로 반영한 공간 분산 계수입니다.

    실제 면적/수용인원 데이터가 확보되면 이 함수를 실제 capacity ratio로 교체합니다.
    """
    profile = _place_profile(place)

    if profile == "commercial":
        return 1.12
    if profile == "indoor":
        return 1.07
    if profile == "nature":
        return 0.84
    if profile == "course":
        return 0.90
    if profile == "night":
        return 1.04

    return 1.0


def _regional_effective_weight(
    regional_demand: dict | None,
) -> float:
    """
    지역 방문자수 데이터의 신선도에 따라 regional 가중치를 감쇠합니다.

    data_lag_days:
      0~7일   -> 10%
      8~14일  -> 7%
      15~30일 -> 3%
      31일+   -> 0%

    data_lag_days가 없으면 보수적으로 regional 신호를 사용하지 않습니다.
    """
    if not regional_demand:
        return 0.0

    lag = regional_demand.get(
        "data_lag_days"
    )

    if lag is None:
        return 0.0

    try:
        lag_days = int(lag)
    except (TypeError, ValueError):
        return 0.0

    if lag_days <= 7:
        return CONGESTION_WEIGHT_REGIONAL

    if lag_days <= 14:
        return 0.07

    if lag_days <= 30:
        return 0.03

    return 0.0


def _preference_match(
    place: Place,
    preference: str,
) -> float:
    pref = preference.strip().lower()

    if not pref:
        return 0.0

    if any(
        token in pref
        for token in (
            "한적",
            "조용",
            "사람 적",
            "혼잡 낮",
        )
    ):
        route_congestion = _route_congestion(place)
        if route_congestion is None:
            return 0.5

        return max(
            0.0,
            min(
                1.0,
                1 - route_congestion / 100.0,
            ),
        )

    haystack = (
        f"{place.title} "
        f"{place.category} "
        f"{place.overview or ''} "
        f"{place.address or ''}"
    ).lower()

    if pref in haystack:
        return 1.0

    for group, keywords in PREFERENCE_KEYWORDS.items():
        if group in pref or pref in group:
            if any(
                keyword.lower() in haystack
                for keyword in keywords
            ):
                return 1.0

    return 0.0



def _is_cafe_place(place: Place) -> bool:
    raw = place.raw or {}
    summary = raw.get("summary") if isinstance(raw, dict) else None
    raw_for_category = summary if isinstance(summary, dict) else (raw if isinstance(raw, dict) else {})

    cat3 = str(
        raw_for_category.get("cat3")
        or raw_for_category.get("lclsSystm3")
        or ""
    ).strip()

    if cat3 in CAFE_CAT3_CODES:
        return True

    haystack = f"{place.title or ''} {place.overview or ''}".lower()
    return any(keyword in haystack for keyword in CAFE_TITLE_KEYWORDS)


def _service_kind(place: Place) -> str | None:
    raw = place.raw or {}
    if isinstance(raw, dict):
        explicit = raw.get("service_kind")
        if explicit in {"food", "cafe"}:
            return explicit

    if place.category == "음식점":
        return "cafe" if _is_cafe_place(place) else "food"

    return None


def _with_service_kind(place: Place, kind: str) -> Place:
    copied = place.model_copy(deep=True)
    copied.raw = dict(copied.raw or {})
    copied.raw["service_kind"] = kind
    copied.is_rest_point = True
    return copied


def _extract_break_time_text(
    value: str | None,
) -> str | None:
    if not value:
        return None

    tagged = re.search(
        r"(?:브레이크\s*타임|break\s*time|쉬는\s*시간)"
        r"\s*[:：]?\s*"
        r"(\d{1,2}:\d{2})"
        r"\s*(?:~|-|–|—|부터)\s*"
        r"(\d{1,2}:\d{2})",
        value,
        flags=re.IGNORECASE,
    )

    if tagged:
        return (
            f"{tagged.group(1)}"
            f"~{tagged.group(2)}"
        )

    plain = re.fullmatch(
        r"\s*(\d{1,2}:\d{2})"
        r"\s*(?:~|-|–|—)\s*"
        r"(\d{1,2}:\d{2})\s*",
        value,
    )

    if plain:
        return (
            f"{plain.group(1)}"
            f"~{plain.group(2)}"
        )

    return None


def _clock_minutes(value: str) -> int | None:
    try:
        hour_text, minute_text = value.split(
            ":",
            1,
        )
        hour = int(hour_text)
        minute = int(minute_text)
    except (
        ValueError,
        AttributeError,
    ):
        return None

    if not (
        0 <= hour <= 23
        and 0 <= minute <= 59
    ):
        return None

    return hour * 60 + minute


def _break_time_range(
    place: Place,
) -> tuple[int, int] | None:
    value = (
        place.break_time
        or _extract_break_time_text(
            place.operating_hours
        )
    )

    if not value:
        return None

    match = re.search(
        r"(\d{1,2}:\d{2})"
        r"\s*(?:~|-|–|—)\s*"
        r"(\d{1,2}:\d{2})",
        value,
    )

    if not match:
        return None

    start = _clock_minutes(
        match.group(1)
    )
    end = _clock_minutes(
        match.group(2)
    )

    if (
        start is None
        or end is None
        or end <= start
    ):
        return None

    return start, end


def _service_break_overlaps(
    place: Place,
    arrival: datetime,
    stay_minutes: int,
) -> bool:
    break_range = _break_time_range(place)

    if break_range is None:
        return False

    break_start, break_end = break_range

    visit_start = (
        arrival.hour * 60
        + arrival.minute
    )
    visit_end = (
        visit_start
        + stay_minutes
    )

    return (
        visit_start < break_end
        and visit_end > break_start
    )


def _place_visit_category(
    place: Place,
) -> str:
    """
    방문 시간/체류시간 계산용 사용자 친화 분류.
    """
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

    return (
        place.category
        or "관광지"
    )


def recommended_stay_minutes(
    place: Place,
) -> int:
    """
    상세 카드에 보여줄 '추천 체류' 기준값.

    임의의 장소 하나에 45분을 일괄 적용하지 않고
    장소의 실제 종류/이름/콘텐츠 타입을 기준으로 계산합니다.
    """
    category = _place_visit_category(
        place
    )

    title = (
        place.title or ""
    ).lower()

    if category == "맛집":
        return 60

    if category == "카페":
        return 45

    content_type_id = str(
        place.content_type_id
        or ""
    )

    # 여행코스 자체는 짧은 단일 관광지보다 오래 머무는 편.
    if content_type_id == "25":
        return 120

    if any(
        keyword in title
        for keyword in (
            "박물관",
            "미술관",
            "전시관",
            "기념관",
            "과학관",
        )
    ):
        return 90

    if any(
        keyword in title
        for keyword in (
            "수목원",
            "식물원",
            "정원",
            "공원",
            "호수",
            "보문호",
            "둘레길",
            "산책로",
        )
    ):
        return 75

    if any(
        keyword in title
        for keyword in (
            "전통마을",
            "민속마을",
            "한옥마을",
            "황리단길",
            "양동마을",
            "교촌마을",
        )
    ):
        return 90

    if any(
        keyword in title
        for keyword in (
            "불국사",
            "사찰",
            "절",
            "서원",
            "향교",
            "궁",
            "월지",
        )
    ):
        return 75

    if any(
        keyword in title
        for keyword in (
            "석굴암",
            "동굴",
        )
    ):
        return 60

    if any(
        keyword in title
        for keyword in (
            "첨성대",
            "월정교",
            "왕릉",
            "고분",
            "고분군",
            "총",
            "읍성",
            "성곽",
            "사지",
        )
    ):
        return 40

    if category == "문화시설":
        return 75

    if category == "레포츠":
        return 90

    if category == "여행코스":
        return 120

    return 55


def _clock_text_to_minutes(
    value: str,
) -> int | None:
    try:
        hour_text, minute_text = value.split(
            ":",
            1,
        )
        hour = int(hour_text)
        minute = int(minute_text)
    except (
        ValueError,
        AttributeError,
    ):
        return None

    if not (
        0 <= hour <= 23
        and 0 <= minute <= 59
    ):
        return None

    return (
        hour * 60
        + minute
    )


def _first_operating_range(
    value: str | None,
) -> tuple[int, int] | None:
    if not value:
        return None

    match = re.search(
        r"(\d{1,2}:\d{2})"
        r"\s*(?:~|-|–|—|부터)\s*"
        r"(\d{1,2}:\d{2})",
        value,
    )

    if not match:
        return None

    start = _clock_text_to_minutes(
        match.group(1)
    )
    end = _clock_text_to_minutes(
        match.group(2)
    )

    if (
        start is None
        or end is None
        or end <= start
    ):
        return None

    return start, end


def _format_visit_window(
    start: int,
    end: int,
) -> str:
    start_hour = start // 60
    start_minute = start % 60
    end_hour = end // 60
    end_minute = end % 60

    return (
        f"{start_hour:02d}:"
        f"{start_minute:02d}"
        f"~"
        f"{end_hour:02d}:"
        f"{end_minute:02d}"
    )


def recommended_time_label(
    place: Place,
) -> str:
    """
    상세 카드에 표시할 장소별 추천 시간대.

    장소 성격을 기준으로 기본 추천창을 만들고,
    공식 운영시간이 확인된 경우 운영범위를 벗어나지 않도록 보정합니다.
    확인되지 않은 미래 혼잡도를 사실처럼 만들지는 않습니다.
    """
    category = _place_visit_category(
        place
    )

    title = (
        place.title or ""
    ).lower()

    # 기본 권장 시간대.
    if (
        place.is_night_spot
        or any(
            keyword in title
            for keyword in (
                "야경",
                "월정교",
                "동궁",
                "월지",
            )
        )
    ):
        desired_start = 18 * 60 + 30
        desired_end = 20 * 60 + 30

    elif category == "맛집":
        # 점심 피크 직전 중심.
        desired_start = 11 * 60
        desired_end = 12 * 60 + 30

    elif category == "카페":
        desired_start = 10 * 60 + 30
        desired_end = 12 * 60

    elif any(
        keyword in title
        for keyword in (
            "박물관",
            "미술관",
            "전시관",
            "기념관",
        )
    ):
        desired_start = 10 * 60
        desired_end = 12 * 60

    elif any(
        keyword in title
        for keyword in (
            "공원",
            "호수",
            "정원",
            "수목원",
            "숲",
            "둘레길",
            "산책로",
        )
    ):
        desired_start = 8 * 60 + 30
        desired_end = 10 * 60 + 30

    elif any(
        keyword in title
        for keyword in (
            "전통마을",
            "한옥",
            "왕릉",
            "고분",
            "첨성대",
            "불국사",
            "석굴암",
            "향교",
            "서원",
            "사지",
            "읍성",
        )
    ):
        desired_start = 9 * 60
        desired_end = 11 * 60

    else:
        desired_start = 9 * 60 + 30
        desired_end = 11 * 60 + 30

    operating_range = _first_operating_range(
        place.operating_hours
    )

    if operating_range is not None:
        open_start, open_end = (
            operating_range
        )

        # 운영 시작 직후/마감 직전 15~30분은 피합니다.
        safe_open = (
            open_start + 15
        )
        safe_close = max(
            safe_open + 30,
            open_end - 30,
        )

        start = max(
            desired_start,
            safe_open,
        )
        end = min(
            desired_end,
            safe_close,
        )

        # 원래 권장창이 운영시간과 거의 겹치지 않으면
        # 운영 시작 후 30분부터 90분 구간을 사용합니다.
        if end - start < 45:
            start = min(
                safe_open + 15,
                max(
                    safe_open,
                    safe_close - 60,
                ),
            )
            end = min(
                safe_close,
                start + 90,
            )

        if end > start:
            desired_start = start
            desired_end = end

    # 식당/카페의 확인된 브레이크타임과 겹치면 앞/뒤 구간으로 이동.
    break_range = _break_time_range(
        place
    )

    if break_range is not None:
        break_start, break_end = (
            break_range
        )

        overlaps = (
            desired_start < break_end
            and desired_end > break_start
        )

        if overlaps:
            if (
                break_start
                - desired_start
                >= 45
            ):
                desired_end = (
                    break_start
                )
            else:
                desired_start = (
                    break_end
                    + 15
                )
                desired_end = (
                    desired_start
                    + 90
                )

                if operating_range is not None:
                    _, open_end = (
                        operating_range
                    )
                    desired_end = min(
                        desired_end,
                        max(
                            desired_start + 30,
                            open_end - 30,
                        ),
                    )

    return _format_visit_window(
        desired_start,
        desired_end,
    )


def _minimum_itinerary_stay(
    place: Place,
) -> int:
    category = _place_visit_category(
        place
    )

    if category == "맛집":
        return 45

    if category == "카페":
        return 30

    return 25


def _fit_itinerary_stays(
    places: list[Place],
    *,
    available_minutes: int,
    travel_minutes: int,
) -> list[int]:
    """
    장소별 권장 체류시간을 최대한 유지하되
    사용자가 선택한 전체 여행시간 안에 들어오도록 비례 조정합니다.
    """
    recommended = [
        recommended_stay_minutes(
            place
        )
        for place in places
    ]

    if not recommended:
        return []

    budget = max(
        0,
        available_minutes
        - travel_minutes,
    )

    if (
        budget <= 0
        or sum(recommended) <= budget
    ):
        return recommended

    minimums = [
        _minimum_itinerary_stay(
            place
        )
        for place in places
    ]

    # 최소 체류시간 자체가 예산보다 크면 최소값을 그대로 사용하고
    # 기존 초과 경고/트림 로직에 맡깁니다.
    if sum(minimums) >= budget:
        return minimums

    scale = (
        budget
        / sum(recommended)
    )

    adjusted = [
        max(
            minimum,
            int(
                round(
                    recommended_value
                    * scale
                    / 5
                )
                * 5
            ),
        )
        for recommended_value, minimum
        in zip(
            recommended,
            minimums,
        )
    ]

    # 반올림으로 예산을 조금 넘으면 큰 항목부터 5분씩 줄입니다.
    while (
        sum(adjusted) > budget
        and any(
            value > minimum
            for value, minimum
            in zip(
                adjusted,
                minimums,
            )
        )
    ):
        index = max(
            range(len(adjusted)),
            key=lambda i: (
                adjusted[i]
                - minimums[i]
            ),
        )

        if (
            adjusted[index]
            > minimums[index]
        ):
            adjusted[index] -= 5
        else:
            break

    return adjusted


def _service_stay_minutes(place: Place) -> int | None:
    kind = _service_kind(place)
    if kind == "food":
        return 55
    if kind == "cafe":
        return 35
    return None


_GYEONGJU_ROUTE_POOL_CACHE: list[Place] = []
_GYEONGJU_ROUTE_POOL_CACHE_AT: float = 0.0
_GYEONGJU_ROUTE_POOL_TTL_SECONDS = 30 * 60


async def _gyeongju_route_pool(
    tour: TourApiClient,
) -> list[Place]:
    """
    출발지 주변 locationBasedList에 의존하지 않고
    경주시 전체 관광 후보를 한 번 받아 메모리에 캐시합니다.
    """
    global _GYEONGJU_ROUTE_POOL_CACHE
    global _GYEONGJU_ROUTE_POOL_CACHE_AT

    now = time.monotonic()

    if (
        _GYEONGJU_ROUTE_POOL_CACHE
        and now - _GYEONGJU_ROUTE_POOL_CACHE_AT
        < _GYEONGJU_ROUTE_POOL_TTL_SECONDS
    ):
        return [
            place.model_copy(deep=True)
            for place in _GYEONGJU_ROUTE_POOL_CACHE
        ]

    places = await tour.gyeongju_places(
        limit=500
    )

    cleaned = [
        place
        for place in places
        if is_user_facing_travel_place(
            place
        )
        and str(
            place.content_type_id
            or ""
        )
        in {
            "12",
            "14",
            "25",
            "28",
        }
    ]

    _GYEONGJU_ROUTE_POOL_CACHE = [
        place.model_copy(deep=True)
        for place in cleaned
    ]

    _GYEONGJU_ROUTE_POOL_CACHE_AT = now

    return cleaned


class RecommendationService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.tour = TourApiClient(settings)
        self.congestion = CongestionClient(settings)
        self.regional = RegionalVisitorClient(settings)
        self.weather = WeatherClient(settings)
        self.route = KakaoRouteClient(settings)
        self.kakao_local = KakaoLocalClient(settings)
        self.naver = NaverClient(settings)
        self.openai = OpenAIClient(settings)
        self.enricher = PlaceInfoEnricher(settings)

    async def recommend(
        self,
        req: RecommendRequest,
    ) -> RecommendResponse:
        unavailable: list[str] = []
        perf_started = time.perf_counter()
        perf_marks: dict[str, float] = {}

        def perf_mark(label: str) -> None:
            perf_marks[label] = round(
                time.perf_counter() - perf_started,
                3,
            )

        start = _safe_local_datetime(
            req.start_time
            or datetime.now(KST)
        ).astimezone(KST)

        # 1. 경주시 전체 관광지 후보 풀
        # 출발지 주변 locationBasedList 결과에 의존하지 않습니다.
        # 전체 후보를 한 번 캐시하고 실제 좌표 거리로 정렬합니다.
        try:
            summary_places = await asyncio.wait_for(
                _gyeongju_route_pool(
                    self.tour
                ),
                timeout=4.5,
            )
        except (
            IntegrationError,
            asyncio.TimeoutError,
        ) as exc:
            raise ValueError(
                "경주 관광지 목록을 불러오지 못했습니다. "
                "잠시 후 다시 시도해 주세요."
            ) from exc

        for place in summary_places:
            place.distance_km = haversine_km(
                req.latitude,
                req.longitude,
                place.latitude,
                place.longitude,
            )

        # ----------------------------------------------------------
        # 출발 위치 대표 관광지:
        # TourAPI + Kakao 후보를 함께 비교해 작은 주변지명보다
        # 대표 관광지를 우선합니다.
        # ----------------------------------------------------------
        try:
            kakao_start_rows = await asyncio.wait_for(
                self.kakao_local.category_search(
                    "AT4",
                    latitude=req.latitude,
                    longitude=req.longitude,
                    radius_m=2500,
                    limit=15,
                ),
                timeout=1.3,
            )
        except (
            IntegrationError,
            asyncio.TimeoutError,
        ):
            kakao_start_rows = []

        start_anchor = select_representative_start_place(
            summary_places,
            kakao_start_rows,
            req.latitude,
            req.longitude,
            max_distance_km=1.5,
        )

        if start_anchor is not None:
            start_anchor.distance_km = (
                haversine_km(
                    req.latitude,
                    req.longitude,
                    start_anchor.latitude,
                    start_anchor.longitude,
                )
            )
            _mark_route_flag(
                start_anchor,
                "route_start_anchor",
            )

            existing_index = next(
                (
                    index
                    for index, place
                    in enumerate(
                        summary_places
                    )
                    if (
                        place.place_id
                        == start_anchor.place_id
                        or (
                            normalize_name(
                                place.title
                            )
                            == normalize_name(
                                start_anchor.title
                            )
                            and haversine_km(
                                place.latitude,
                                place.longitude,
                                start_anchor.latitude,
                                start_anchor.longitude,
                            ) < 0.3
                        )
                    )
                ),
                None,
            )

            if existing_index is None:
                summary_places.append(
                    start_anchor
                )
            else:
                summary_places[
                    existing_index
                ] = start_anchor

            print(
                "[ROUTE START ANCHOR]"
                f" title={start_anchor.title}"
                f" distance_km="
                f"{start_anchor.distance_km:.3f}"
            )

        # ----------------------------------------------------------
        # 꼭 포함 장소는 "비슷한 하위시설"이 아니라 대표 장소를 찾습니다.
        # ----------------------------------------------------------
        required_summaries: list[
            Place
        ] = []
        resolved_required_names: set[
            str
        ] = set()

        for name in req.required_place_names:
            index = _required_index_for_name(
                summary_places,
                name,
                set(),
            )

            if index is None:
                continue

            candidate = summary_places[
                index
            ]

            # 경주월드 요청에 스노우파크가 잡히는 식의 약한 일치는
            # 아직 해결된 것으로 보지 않고 별도 검색합니다.
            if _is_weak_required_subplace(
                name,
                candidate.title,
            ):
                continue

            _mark_route_flag(
                candidate,
                "required_route_target",
            )
            required_summaries.append(
                candidate
            )
            resolved_required_names.add(
                name
            )

        missing_required = [
            name
            for name
            in req.required_place_names
            if name
            not in resolved_required_names
        ]

        async def resolve_missing(
            name: str,
        ) -> Place | None:
            async def tour_hits():
                try:
                    return await asyncio.wait_for(
                        self.tour.keyword_search_global(
                            name,
                            limit=30,
                        ),
                        timeout=2.0,
                    )
                except (
                    IntegrationError,
                    asyncio.TimeoutError,
                ):
                    return []

            async def kakao_hits():
                try:
                    return await asyncio.wait_for(
                        self.kakao_local.keyword_search(
                            name,
                            latitude=req.latitude,
                            longitude=req.longitude,
                            limit=10,
                        ),
                        timeout=1.5,
                    )
                except (
                    IntegrationError,
                    asyncio.TimeoutError,
                ):
                    return []

            tour_rows, kakao_rows = await asyncio.gather(
                tour_hits(),
                kakao_hits(),
            )

            valid_tour = [
                place
                for place in tour_rows
                if (
                    "경주"
                    in (
                        place.address
                        or ""
                    )
                    and is_user_facing_travel_place(
                        place
                    )
                    and _required_match_rank(
                        name,
                        place.title,
                    )[0] < 99
                )
            ]

            valid_kakao = [
                row
                for row in kakao_rows
                if (
                    "경주"
                    in str(
                        row.get(
                            "address"
                        )
                        or row.get(
                            "road_address"
                        )
                        or ""
                    )
                    and _required_match_rank(
                        name,
                        str(
                            row.get(
                                "title"
                            )
                            or ""
                        ),
                    )[0] < 99
                )
            ]

            valid_kakao.sort(
                key=lambda row:
                    _required_match_rank(
                        name,
                        str(
                            row.get(
                                "title"
                            )
                            or ""
                        ),
                    )
            )

            canonical_row = (
                valid_kakao[0]
                if valid_kakao
                else None
            )

            def tour_rank(
                place: Place,
            ):
                canonical_distance = 9999.0

                if (
                    canonical_row is not None
                    and canonical_row.get(
                        "latitude"
                    ) is not None
                    and canonical_row.get(
                        "longitude"
                    ) is not None
                ):
                    canonical_distance = (
                        haversine_km(
                            float(
                                canonical_row[
                                    "latitude"
                                ]
                            ),
                            float(
                                canonical_row[
                                    "longitude"
                                ]
                            ),
                            place.latitude,
                            place.longitude,
                        )
                    )

                return (
                    _required_match_rank(
                        name,
                        place.title,
                    ),
                    canonical_distance,
                )

            valid_tour.sort(
                key=tour_rank
            )

            if valid_tour:
                best_tour = valid_tour[
                    0
                ]

                # Kakao에는 정확한 대표 장소가 있는데
                # TourAPI 후보가 하위시설뿐이면 대표 장소를 우선합니다.
                if not (
                    _is_weak_required_subplace(
                        name,
                        best_tour.title,
                    )
                    and canonical_row
                    is not None
                    and _required_match_rank(
                        name,
                        str(
                            canonical_row.get(
                                "title"
                            )
                            or ""
                        ),
                    )[0] == 0
                ):
                    return _mark_route_flag(
                        best_tour,
                        "required_route_target",
                    )

            if canonical_row is not None:
                kakao_place = (
                    _kakao_row_to_place(
                        canonical_row
                    )
                )

                if kakao_place is not None:
                    return _mark_route_flag(
                        kakao_place,
                        "required_route_target",
                    )

            return None

        if missing_required:
            resolved = await asyncio.gather(
                *(
                    resolve_missing(
                        name
                    )
                    for name
                    in missing_required
                )
            )

            for place in resolved:
                if place is None:
                    continue

                place.distance_km = (
                    haversine_km(
                        req.latitude,
                        req.longitude,
                        place.latitude,
                        place.longitude,
                    )
                )

                duplicate_index = next(
                    (
                        index
                        for index, existing
                        in enumerate(
                            summary_places
                        )
                        if existing.place_id
                        == place.place_id
                    ),
                    None,
                )

                if duplicate_index is None:
                    summary_places.append(
                        place
                    )
                else:
                    summary_places[
                        duplicate_index
                    ] = place

                required_summaries.append(
                    place
                )

        def is_required(
            place: Place,
        ) -> bool:
            return any(
                _required_match_rank(
                    name,
                    place.title,
                )[0] < 99
                and not _is_weak_required_subplace(
                    name,
                    place.title,
                )
                for name
                in req.required_place_names
            )

        # 전체 경주시 후보를 거리순으로 정렬.
        # '반경 안에 있냐'가 아니라 실제 거리 자체가 점수에 반영됩니다.
        summary_places.sort(
            key=lambda place: (
                0 if is_required(place) else 1,
                place.distance_km
                if place.distance_km is not None
                else 9999.0,
            )
        )

        # 조건/테마 필터링에 충분한 수만 다음 단계로 보냅니다.
        # 상세 API를 수십 개 호출하지 않도록 28개로 제한.
        required_ids = {
            place.place_id
            for place in required_summaries
        }

        required_head = [
            place
            for place in summary_places
            if place.place_id in required_ids
        ]

        normal_head = [
            place
            for place in summary_places
            if place.place_id
            not in required_ids
        ][:28]

        summary_places = list(
            {
                place.place_id: place
                for place in [
                    *required_head,
                    *normal_head,
                ]
            }.values()
        )

        perf_mark(
            "gyeongju_pool"
        )

        if not summary_places:
            raise ValueError(
                "조건에 맞는 경주 관광지를 찾지 못했습니다."
            )

        # 2~3. 상세정보 / 공식 혼잡도 / 날씨 / 지역 방문수요를 병렬 조회
        async def load_details() -> list[Place]:
            # 코스 생성 단계에서는 TourAPI summary만 사용합니다.
            # 상세소개/운영시간은 사용자가 장소 상세를 열 때 조회하므로
            # 코스 생성 때 20~30개의 detail API를 기다리지 않습니다.
            return [
                place.model_copy(
                    deep=True
                )
                for place in summary_places
            ]

        async def load_congestion() -> dict[str, tuple[float, str | None]]:
            try:
                return await asyncio.wait_for(
                    self.congestion.score_map(),
                    timeout=RECOMMEND_SIGNAL_TIMEOUT_SECONDS,
                )
            except (IntegrationError, asyncio.TimeoutError):
                unavailable.append("congestion_api")
                return {}

        async def load_weather() -> dict:
            if not req.weather_aware:
                return {}

            try:
                return await asyncio.wait_for(
                    self.weather.current(
                        req.latitude,
                        req.longitude,
                    ),
                    timeout=RECOMMEND_SIGNAL_TIMEOUT_SECONDS,
                )
            except (IntegrationError, asyncio.TimeoutError):
                unavailable.append("weather_api")
                return {}

        async def load_regional() -> dict | None:
            # Regional DataLab은 현재 30일 지연 데이터라 V2.4.4에서
            # 실제 가중치가 3%뿐입니다. 따라서 초기 캐시가 없을 때
            # 여러 날짜를 오래 탐색하며 사용자 응답을 막지 않습니다.
            try:
                return await asyncio.wait_for(
                    self.regional.demand_score(),
                    timeout=RECOMMEND_REGIONAL_TIMEOUT_SECONDS,
                )
            except (IntegrationError, asyncio.TimeoutError):
                unavailable.append("regional_visitor_api")
                return None

        (
            details,
            congestion_map,
            weather,
            regional_demand,
        ) = await asyncio.gather(
            load_details(),
            load_congestion(),
            load_weather(),
            load_regional(),
        )

        perf_mark("details_and_signals")

        service_candidates: list[Place] = []

        for place in details:
            matched = congestion_map.get(
                normalize_name(place.title)
            )

            if matched:
                place.official_congestion_score = matched[0]
                place.congestion_date = matched[1]

            place.distance_km = haversine_km(
                req.latitude,
                req.longitude,
                place.latitude,
                place.longitude,
            )

            place.estimated_travel_minutes = _estimate_minutes(
                place.distance_km,
                req.transport,
            )

        # 4. 정적 후보 필터
        candidates, applied = self._filter_static(
            details,
            req,
            weather,
        )

        attraction_candidates = [
            place
            for place in candidates
            if place.category != "음식점"
        ]

        if req.include_food or req.include_cafe:
            seen_service_ids = {place.place_id for place in service_candidates}
            for place in candidates:
                if place.category == "음식점" and place.place_id not in seen_service_ids:
                    service_candidates.append(place)
                    seen_service_ids.add(place.place_id)

        candidates = attraction_candidates

        # refresh에서 직전 코스의 일부 장소를 제외했을 때
        # 관광지뿐 아니라 맛집/카페 후보에도 동일하게 적용합니다.
        excluded_service_names = [
            value.lower()
            for value in req.excluded_place_names
            if value.strip()
        ]

        visited_service_ids = {
            value.strip()
            for value in req.visited_place_ids
            if value.strip()
        }

        if excluded_service_names or visited_service_ids:
            service_candidates = [
                place
                for place in service_candidates
                if (
                    place.place_id not in visited_service_ids
                    and not any(
                        excluded_name in place.title.lower()
                        or place.title.lower() in excluded_name
                        for excluded_name in excluded_service_names
                    )
                )
            ]

        if len(candidates) < 2:
            raise ValueError(
                "조건을 충족하는 관광지가 2개 미만입니다. "
                "테마·시간·무료 조건을 완화해 주세요."
            )

        # 5. NAVER DataLab
        # popularity(상대 관심 규모) + momentum(최근 증감) 분리
        naver_signals: dict[
            str,
            dict[str, float],
        ] = {}

        try:
            naver_signals = await asyncio.wait_for(
                self.naver.trend_signals(
                    list(
                        dict.fromkeys(
                            [
                                place.title
                                for place in candidates[:7]
                            ]
                            + [
                                place.title
                                for place in service_candidates[:4]
                            ]
                        )
                    )
                ),
                timeout=RECOMMEND_NAVER_TIMEOUT_SECONDS,
            )
        except (IntegrationError, asyncio.TimeoutError):
            unavailable.append("naver_datalab")

        perf_mark("naver")

        # 6. 최근 2시간 커뮤니티 현장 제보를 한 번에 조회합니다.
        # V2.4.4 자체 점수는 유지하고, 코스용 routing_congestion_score만
        # 최대 20% 범위에서 보정합니다.
        community_signals = _community_live_signal_map(
            [
                place.place_id
                for place in [*candidates, *service_candidates]
            ],
            now=start,
        )

        if community_signals:
            applied.append(
                "최근 2시간 커뮤니티 현장 혼잡 제보 보조 반영"
            )

        # 7. 혼잡도 + 선호도 + 추천점수 계산
        for place in candidates:
            signal = naver_signals.get(
                place.title,
                {},
            )

            # 기존 프론트/스키마 호환을 위해 trend_score에는
            # momentum을 계속 저장합니다.
            place.trend_score = signal.get(
                "momentum"
            )

            self._calculate_congestion(
                place,
                start,
                weather,
                regional_demand,
                signal.get("popularity"),
            )
            _apply_community_live_signal(
                place,
                community_signals.get(place.place_id),
            )

            self._score_place(
                place,
                req,
            )

        for place in service_candidates:
            signal = naver_signals.get(place.title, {})
            place.trend_score = signal.get("momentum")
            self._calculate_congestion(
                place,
                start,
                weather,
                regional_demand,
                signal.get("popularity"),
            )
            _apply_community_live_signal(
                place,
                community_signals.get(place.place_id),
            )

        food_candidates = [
            _with_service_kind(place, "food")
            for place in service_candidates
            if not _is_cafe_place(place)
        ]
        cafe_candidates = [
            _with_service_kind(place, "cafe")
            for place in service_candidates
            if _is_cafe_place(place)
        ]

        # 식당/카페는 코스 생성 속도를 위해 summary 후보로 먼저 배치합니다.
        # 실제 운영시간/브레이크타임/메뉴는 최종 상세화면에서 보완합니다.
        perf_mark("business_summary")

        # -----------------------------------------------------------
        # 혼잡도는 "하드 제외"가 아니라 "우선 기준"으로 사용합니다.
        #
        # 기준 이하 장소가 충분하면 그대로 사용하고,
        # 2곳 미만일 때만 기준 초과 후보 중
        # 1) 혼잡도가 낮고
        # 2) 추천점수가 높은
        # 장소를 필요한 만큼 보충합니다.
        #
        # 이렇게 하면 혼잡한 날에도 코스 생성 자체가 실패하지 않으면서
        # 한적한 장소 우선 원칙은 유지됩니다.
        # -----------------------------------------------------------
        preferred_by_congestion = [
            place
            for place in candidates
            if (
                _route_congestion(place) is None
                or _route_congestion(place)
                <= self.settings.congestion_threshold
            )
        ]

        over_threshold = [
            place
            for place in candidates
            if (
                _route_congestion(place) is not None
                and _route_congestion(place)
                > self.settings.congestion_threshold
            )
        ]

        congestion_fallback_used = False
        supplemented_count = 0

        target_pool_size = min(
            RECOMMEND_MIN_ATTRACTION_POOL,
            len(candidates),
        )

        if len(preferred_by_congestion) < target_pool_size:
            congestion_fallback_used = True

            over_threshold.sort(
                key=lambda place: (
                    _route_congestion(place)
                    if _route_congestion(place) is not None
                    else 101.0,
                    -(place.recommendation_score or 0.0),
                )
            )

            needed = max(
                0,
                target_pool_size - len(preferred_by_congestion),
            )

            supplements = over_threshold[
                :needed
            ]

            supplemented_count = len(
                supplements
            )

            preferred_by_congestion.extend(
                supplements
            )

        # 정적 조건 자체를 충족하는 후보가 2곳 미만인 경우에만
        # 실제로 코스 생성 불가로 처리합니다.
        if len(preferred_by_congestion) < 2:
            raise ValueError(
                "현재 조건을 충족하는 장소가 2개 미만입니다. "
                "무료 조건·필수/제외 장소 조건을 완화해 주세요."
            )

        if regional_demand is not None:
            applied.append(
                "경주시 최근 방문수요 반영"
            )

        if congestion_fallback_used:
            applied.append(
                "혼잡도 기준 완화: "
                f"{self.settings.congestion_threshold:g}% 이하 후보 부족으로 "
                f"차선 후보 {supplemented_count}곳 보충"
            )
        else:
            applied.append(
                f"예상 혼잡도 "
                f"{self.settings.congestion_threshold:g}% 이하 우선"
            )

        # 혼잡도 기준을 통과한 후보 안에서는 기존 recommendation_score
        # (한적도 45% + 선호 30% + 거리 25%) 순서를 그대로 유지합니다.
        preferred_by_congestion.sort(
            key=lambda place:
            place.recommendation_score or 0,
            reverse=True,
        )

        required_candidate_indices: list[
            int
        ] = []

        required_candidate_places: list[
            Place
        ] = []

        for required_name in req.required_place_names:
            index = _required_index_for_name(
                candidates,
                required_name,
                set(
                    required_candidate_indices
                ),
            )

            if index is None:
                continue

            required_candidate_indices.append(
                index
            )
            required_candidate_places.append(
                candidates[index]
            )

        required_ids = {
            place.place_id
            for place in required_candidate_places
        }

        normal_priority = [
            place
            for place
            in preferred_by_congestion
            if place.place_id
            not in required_ids
        ]

        filtered = [
            *required_candidate_places,
            *normal_priority[
                : max(
                    0,
                    self.settings.max_candidate_places
                    - len(
                        required_candidate_places
                    ),
                )
            ],
        ]

        # 혹시 혼잡도 우선 풀에서 빠진 일반 후보가 있어도
        # 후보 수가 너무 적으면 추천점수 순으로 추가합니다.
        if len(filtered) < 2:
            backup = sorted(
                (
                    place
                    for place in candidates
                    if place.place_id
                    not in {
                        item.place_id
                        for item in filtered
                    }
                ),
                key=lambda place:
                    place.recommendation_score
                    or 0.0,
                reverse=True,
            )

            filtered.extend(
                backup[
                    : 2 - len(filtered)
                ]
            )

        # 7. 실제 이동 흐름 기반 순차 코스 선택
        # 기존 장소 recommendation_score(한적도45/선호30/출발거리25)는 유지하고,
        # 선택된 장소의 "순서"를 현재 위치와 필수 장소 방향으로 결정합니다.
        progressive = _progressive_route_individual(
            filtered,
            req,
            self.settings.max_course_places,
        )

        if progressive is not None:
            missing_required_after_route = [
                name
                for name in req.required_place_names
                if not any(
                    _matches_required_place(
                        filtered[index],
                        [name],
                    )
                    for index in progressive.genes
                )
            ]

            if missing_required_after_route:
                # 이 경우는 코스를 반환하는 것보다 명확히 실패시키는 것이
                # "꼭 포함" 조건을 조용히 무시하는 것보다 안전합니다.
                raise ValueError(
                    "꼭 포함할 장소를 코스에 넣지 못했습니다: "
                    + ", ".join(
                        missing_required_after_route
                    )
                )

            individuals = [progressive]
            applied.append(
                "현재 위치→가까운 취향 장소→다음 인접 장소 순차 동선 반영"
            )

            if req.required_place_names:
                applied.append(
                    "필수 장소 방향 경유 동선 반영"
                )
        else:
            # 후보가 매우 부족한 예외 상황에서만 기존 NSGA를 fallback으로 사용.
            individuals = optimize_courses(
                filtered,
                req,
                min(
                    self.settings.nsga_population_size,
                    RECOMMEND_NSGA_POPULATION_CAP,
                ),
                min(
                    self.settings.nsga_generations,
                    RECOMMEND_NSGA_GENERATION_CAP,
                ),
                self.settings.max_course_places,
            )

        perf_mark("optimizer")

        if not individuals:
            raise ValueError(
                "가용 시간 내 코스를 생성하지 못했습니다."
            )

        # 8. 코스 생성 중 상세 웹/NAVER 보완은 생략합니다.
        # 화면에서 장소 상세를 열 때 공통 상세 API가 보완하므로
        # 추천 응답시간을 우선합니다.
        perf_mark("enrichment_skipped")

        course_types = [
            CourseType.travel_min,
            CourseType.congestion_avoidance,
            CourseType.preference_fit,
        ]

        courses: list[Course] = []

        for index, individual in enumerate(
            individuals[: req.desired_course_count]
        ):
            course = await self._build_course(
                individual,
                filtered,
                req,
                start,
                course_types[
                    min(
                        index,
                        len(course_types) - 1,
                    )
                ],
                weather,
                unavailable,
                food_candidates=food_candidates,
                cafe_candidates=cafe_candidates,
            )

            courses.append(course)

        perf_mark("course_build")
        total_seconds = round(
            time.perf_counter() - perf_started,
            3,
        )

        if courses:
            print(
                "[ROUTE FLOW]"
                f" start=({req.latitude:.5f},{req.longitude:.5f})"
                f" required={req.required_place_names}"
                f" stops={[place.title for place in courses[0].places]}"
            )

        print(
            "\n[RECOMMEND PERF]"
            f" total={total_seconds}s"
            f" marks={perf_marks}"
            f" candidates={len(candidates)}"
            f" filtered={len(filtered)}"
            f" food_pool={len(food_candidates)}"
            f" cafe_pool={len(cafe_candidates)}"
            f" course_places={len(courses[0].places) if courses else 0}"
            "\n"
        )

        return RecommendResponse(
            generated_at=datetime.now(timezone.utc),
            courses=courses,
            applied_filters=applied,
            unavailable_integrations=list(
                dict.fromkeys(unavailable)
            ),
        )

    async def _details(
        self,
        places: list[Place],
    ) -> list[Place]:
        semaphore = asyncio.Semaphore(10)

        async def one(
            place: Place,
        ) -> Place:
            async with semaphore:
                try:
                    return await asyncio.wait_for(
                        self.tour.detail(place),
                        timeout=RECOMMEND_DETAIL_TIMEOUT_SECONDS,
                    )
                except (IntegrationError, asyncio.TimeoutError):
                    # 상세정보가 늦으면 summary 정보로 추천을 계속합니다.
                    return place

        return list(
            await asyncio.gather(
                *(
                    one(place)
                    for place in places
                )
            )
        )

    def _filter_static(
        self,
        places: list[Place],
        req: RecommendRequest,
        weather: dict,
    ) -> tuple[list[Place], list[str]]:
        applied = [
            f"가용시간 {req.available_minutes}분",
            "기본 관광코스에서 숙박시설 제외",
        ]

        excluded = [
            value.lower()
            for value in req.excluded_place_names
        ]

        required = [
            value.lower()
            for value in req.required_place_names
        ]

        visited_place_ids = {
            value.strip()
            for value in req.visited_place_ids
            if value.strip()
        }

        preferences = {
            value.strip().lower()
            for value in req.preferences
        }

        allow_food = (
            req.include_food
            or req.include_cafe
            or bool(
                preferences
                & FOOD_PREFERENCES
            )
        )

        allow_shopping = bool(
            preferences & SHOPPING_PREFERENCES
        )

        allow_lodging = bool(
            preferences & LODGING_PREFERENCES
        )

        result: list[Place] = []

        for place in places:
            if not is_user_facing_travel_place(place):
                continue

            title_lower = place.title.lower()

            required_match = any(
                name in title_lower
                for name in required
            )

            # 이미 방문한 장소는 기본 추천 후보에서 제외하되,
            # 사용자가 "꼭 포함"으로 지정한 경우에는 허용합니다.
            if (
                place.place_id in visited_place_ids
                and not required_match
            ):
                continue

            # 탐색 반경 하드필터는 사용하지 않습니다.
            # 실제 거리는 recommendation_score의 기존 25% 항목과
            # 순차 동선 선택에서 계속 반영됩니다.

            if excluded and any(
                value in title_lower
                for value in excluded
            ):
                continue

            # "꼭 포함"은 테마/무료/실내/날씨보다 우선합니다.
            # 여행 목적지로 노출 가능한 장소인지만 확인한 뒤 보존합니다.
            if required_match:
                result.append(
                    place
                )
                continue

            if not required_match:
                if (
                    place.category == "숙박"
                    and not allow_lodging
                ):
                    continue

                if (
                    place.category == "음식점"
                    and not allow_food
                ):
                    continue

                if (
                    place.category == "쇼핑"
                    and not allow_shopping
                ):
                    continue

                if (
                    place.category not in PRIMARY_CATEGORIES
                    and place.category
                    not in {
                        "숙박",
                        "음식점",
                        "쇼핑",
                    }
                ):
                    continue

            if (
                req.free_only
                and place.is_free is not True
            ):
                continue

            if (
                req.indoor_preferred
                and _place_profile(place) != "indoor"
                and place.category
                not in (
                    "쇼핑",
                    "음식점",
                )
            ):
                continue

            # 비가 오는 경우 야외 레포츠를 제외.
            # 나머지 야외 관광지는 V2 날씨 점수에서 감점해
            # 무조건 제거가 아닌 soft penalty로 처리합니다.
            if (
                weather.get("raining")
                and _place_profile(place) == "leisure"
                and "실내" not in (place.overview or "")
            ):
                continue

            result.append(place)

        if req.free_only:
            applied.append(
                "무료로 확인된 장소만"
            )

        if req.indoor_preferred:
            applied.append(
                "실내 우선"
            )

        if weather.get("raining"):
            applied.append(
                "강수 × 관광지 유형 반영"
            )

        if weather.get("hot"):
            applied.append(
                "폭염 × 관광지 유형 반영"
            )

        if not req.weather_aware:
            applied.append(
                "사용자 설정에 따라 현재 날씨 미반영"
            )

        if req.include_food:
            applied.append(
                "맛집 요청 반영"
            )

        if req.include_cafe:
            applied.append(
                "카페 요청 반영"
            )

        if visited_place_ids:
            applied.append(
                "이미 방문한 장소 제외"
            )

        return result, applied

    def _calculate_congestion(
        self,
        place: Place,
        when: datetime,
        weather: dict,
        regional_demand: dict | None = None,
        naver_popularity: float | None = None,
    ) -> None:
        """
        경주한적 혼잡도 V2.4.5 (0~100).

        - 장소 baseline
        - 관광공사 분류 기반 시간/요일 패턴
        - NAVER 상대 검색 관심도(popularity)
        - NAVER 최근 관심 변화(momentum)
        - 날씨 × 장소 분류
        - 경주시 지역 방문수요
        - 공식 혼잡도(있을 때)
        - 공간 분산/수용 특성 보정

        데이터가 없는 외부 신호는 임의의 50으로 채우지 않고 제외한 뒤
        남은 가중치를 재정규화합니다.
        """
        signals: list[
            tuple[str, float, float]
        ] = []

        baseline_score = _place_baseline_score(place)
        signals.append(
            (
                "baseline",
                baseline_score,
                CONGESTION_WEIGHT_BASELINE,
            )
        )

        time_score = _time_congestion_score(
            place,
            when,
        )
        signals.append(
            (
                "time",
                time_score,
                CONGESTION_WEIGHT_TIME,
            )
        )

        if naver_popularity is not None:
            popularity_score = max(
                0.0,
                min(
                    100.0,
                    naver_popularity * 100.0,
                ),
            )

            signals.append(
                (
                    "naver_popularity",
                    popularity_score,
                    CONGESTION_WEIGHT_NAVER_POPULARITY,
                )
            )

        if place.trend_score is not None:
            momentum_score = max(
                0.0,
                min(
                    100.0,
                    place.trend_score * 100.0,
                ),
            )

            signals.append(
                (
                    "naver_momentum",
                    momentum_score,
                    CONGESTION_WEIGHT_NAVER_MOMENTUM,
                )
            )

        # 날씨 데이터가 실제로 있을 때만 신호로 사용합니다.
        # 외부 데이터 누락을 중립값 50으로 채우면 가중치 재정규화 원칙을 깨므로 제외합니다.
        if weather:
            weather_score = _weather_congestion_score(
                place,
                weather,
            )
            signals.append(
                (
                    "weather",
                    weather_score,
                    CONGESTION_WEIGHT_WEATHER,
                )
            )

        regional_weight = _regional_effective_weight(
            regional_demand
        )

        if (
            regional_demand is not None
            and regional_weight > 0
        ):
            regional_score = regional_demand.get(
                "score"
            )

            if regional_score is not None:
                signals.append(
                    (
                        "regional_demand",
                        max(
                            0.0,
                            min(
                                100.0,
                                float(regional_score),
                            ),
                        ),
                        regional_weight,
                    )
                )

        if place.official_congestion_score is not None:
            official_score = max(
                0.0,
                min(
                    100.0,
                    place.official_congestion_score,
                ),
            )

            signals.append(
                (
                    "official",
                    official_score,
                    CONGESTION_WEIGHT_OFFICIAL,
                )
            )

        total_weight = sum(
            weight
            for _, _, weight in signals
        )

        if total_weight <= 0:
            place.congestion_score = None
            place.congestion_components = {}
            return

        raw_score = (
            sum(
                score * weight
                for _, score, weight in signals
            )
            / total_weight
        )

        capacity_factor = _capacity_factor(place)

        final_score = max(
            0.0,
            min(
                100.0,
                raw_score * capacity_factor,
            ),
        )

        place.congestion_score = round(
            final_score,
            2,
        )

        place.congestion_components = {
            name: round(score, 3)
            for name, score, _ in signals
        }

        place.congestion_components["capacity_factor"] = round(
            capacity_factor,
            3,
        )
        place.congestion_components["raw_score"] = round(
            raw_score,
            3,
        )

        if regional_demand is not None:
            factor = regional_demand.get(
                "factor"
            )
            if factor is not None:
                place.congestion_components[
                    "regional_factor"
                ] = round(
                    float(factor),
                    4,
                )

            lag_days = regional_demand.get(
                "data_lag_days"
            )
            if lag_days is not None:
                try:
                    place.congestion_components[
                        "regional_lag_days"
                    ] = int(lag_days)
                except (TypeError, ValueError):
                    pass

            place.congestion_components[
                "regional_weight"
            ] = round(
                regional_weight,
                3,
            )

        # -------------------------------------------------------
        # 혼잡도 V2 대표 관광지 디버깅
        #
        # FastAPI 터미널에서 다음 값을 확인할 수 있습니다.
        # - final: 최종 혼잡도
        # - profile: 관광지 분류
        # - baseline
        # - time
        # - naver_momentum
        # - weather
        # - official
        # - capacity_factor
        # - raw_score
        #
        # V2 튜닝이 끝난 뒤 아래 블록은 삭제해도 됩니다.
        # -------------------------------------------------------

        normalized_title = normalize_name(place.title)

        if any(
            normalize_name(name) in normalized_title
            or normalized_title in normalize_name(name)
            for name in DEBUG_CONGESTION_PLACES
        ):
            components = place.congestion_components

            print(
                "\n"
                "============================================================\n"
                "[CONGESTION V2.4.4 DEBUG]\n"
                f"place        : {place.title}\n"
                f"profile      : {_place_profile(place)}\n"
                f"final        : {place.congestion_score}\n"
                f"baseline     : {components.get('baseline')}\n"
                f"time         : {components.get('time')}\n"
                f"naver_pop    : {components.get('naver_popularity')}\n"
                f"naver_mom    : {components.get('naver_momentum')}\n"
                f"weather      : {components.get('weather')}\n"
                f"regional     : {components.get('regional_demand')}\n"
                f"reg_factor   : {components.get('regional_factor')}\n"
                f"reg_lag_days : {components.get('regional_lag_days')}\n"
                f"reg_weight   : {components.get('regional_weight')}\n"
                f"official     : {components.get('official')}\n"
                f"capacity     : {components.get('capacity_factor')}\n"
                f"raw_score    : {components.get('raw_score')}\n"
                "============================================================"
            )

    def _score_place(
        self,
        place: Place,
        req: RecommendRequest,
    ) -> None:
        route_congestion = _route_congestion(place)
        quiet_component = (
            1 - route_congestion / 100.0
            if route_congestion is not None
            else 0.5
        )

        distance_component = max(
            0.0,
            1 - (
                (place.distance_km or 0)
                / max(
                    req.radius_km,
                    0.1,
                )
            ),
        )

        attraction_preferences = [
            preference
            for preference
            in req.preferences
            if (
                preference.strip().lower()
                not in FOOD_PREFERENCES
            )
        ]

        if not attraction_preferences:
            preference_component = 1.0
        else:
            matches = [
                _preference_match(
                    place,
                    preference,
                )
                for preference
                in attraction_preferences
            ]

            preference_component = (
                sum(matches) / len(matches)
                if matches
                else 0.0
            )

        place.preference_score = round(
            preference_component,
            4,
        )

        place.review_score = None

        score = 100 * (
            0.45 * quiet_component
            + 0.30 * preference_component
            + 0.25 * distance_component
        )

        place.recommendation_score = round(
            score,
            2,
        )

        reasons: list[str] = []

        if quiet_component >= 0.6:
            reasons.append(
                f"코스 반영 혼잡도 {route_congestion:.0f}%로 낮음"
                if route_congestion is not None
                else "예상 혼잡도가 낮음"
            )

        if place.community_report_count > 0:
            reasons.append(
                f"최근 현장 제보 {place.community_report_count}건 반영"
            )

        if preference_component >= 0.5:
            reasons.append(
                "선호와 일치"
            )

        if distance_component >= 0.7:
            reasons.append(
                "현재 위치와 가까움"
            )

        if place.is_free is True:
            reasons.append(
                "무료 이용 가능"
            )

        place.recommendation_reason = (
            ", ".join(reasons)
            or "전체 조건을 균형 있게 충족"
        )

    async def _build_course(
        self,
        ind: Individual,
        places: list[Place],
        req: RecommendRequest,
        start: datetime,
        course_type: CourseType,
        weather: dict,
        unavailable: list[str],
        *,
        food_candidates: list[Place] | None = None,
        cafe_candidates: list[Place] | None = None,
    ) -> Course:
        route = [
            places[index].model_copy(deep=True)
            for index in ind.genes
        ]

        if any(
            "야경" in preference
            for preference in req.preferences
        ):
            # progressive 순서는 유지하되, 야경 장소가 여러 개라면
            # 같은 공간 흐름 안에서 야경 장소만 뒤쪽에 배치합니다.
            day_places = [
                place
                for place in route
                if not place.is_night_spot
            ]
            night_places = [
                place
                for place in route
                if place.is_night_spot
            ]
            route = [
                *day_places,
                *night_places,
            ]

        food_candidates = food_candidates or []
        cafe_candidates = cafe_candidates or []

        # 시작 위치 주변 음식점만 보는 것이 아니라,
        # 실제로 선택된 관광지 주변의 식당/카페를 추가 탐색합니다.
        async def load_services_near_route() -> list[Place]:
            if not (
                req.include_food
                or req.include_cafe
            ):
                return []

            async def one(
                place: Place,
            ) -> list[Place]:
                try:
                    return await asyncio.wait_for(
                        self.tour.nearby_food_places(
                            place.latitude,
                            place.longitude,
                            2200,
                            5,
                        ),
                        timeout=RECOMMEND_NEARBY_TIMEOUT_SECONDS,
                    )
                except (
                    IntegrationError,
                    asyncio.TimeoutError,
                ):
                    return []

            groups = await asyncio.gather(
                *(
                    one(place)
                    for place in route[:3]
                )
            )

            merged: list[Place] = []
            seen: set[str] = set()

            for place in (
                item
                for group in groups
                for item in group
            ):
                if place.place_id in seen:
                    continue

                seen.add(place.place_id)
                merged.append(place)

            # 코스 생성 단계에서는 주변 음식점 summary만 사용합니다.
            # 상세 운영정보는 최종 선택 후 상세화면에서 확인합니다.
            return merged[:8]

        route_service_candidates = (
            await load_services_near_route()
        )

        existing_food_ids = {
            place.place_id
            for place in food_candidates
        }
        existing_cafe_ids = {
            place.place_id
            for place in cafe_candidates
        }

        for service_place in route_service_candidates:
            if not is_user_facing_travel_place(
                service_place
            ):
                continue

            if _is_cafe_place(
                service_place
            ):
                if service_place.place_id not in existing_cafe_ids:
                    cafe_candidates.append(
                        _with_service_kind(
                            service_place,
                            "cafe",
                        )
                    )
                    existing_cafe_ids.add(
                        service_place.place_id
                    )
            else:
                if service_place.place_id not in existing_food_ids:
                    food_candidates.append(
                        _with_service_kind(
                            service_place,
                            "food",
                        )
                    )
                    existing_food_ids.add(
                        service_place.place_id
                    )

        def is_service_stop(
            place: Place,
        ) -> bool:
            raw = (
                place.raw
                if isinstance(
                    place.raw,
                    dict,
                )
                else {}
            )

            return (
                str(
                    place.content_type_id
                    or ""
                ) == "39"
                or raw.get(
                    "service_kind"
                )
                in {
                    "food",
                    "cafe",
                }
            )

        def candidate_quality(candidate: Place) -> float:
            components = candidate.congestion_components or {}
            popularity = float(components.get("naver_popularity", 25.0) or 0.0)
            quiet = 50.0
            route_congestion = _route_congestion(candidate)
            if route_congestion is not None:
                quiet = max(0.0, 100.0 - route_congestion)
            return popularity * 0.55 + quiet * 0.45

        def best_service_insertion(
            current_route: list[Place],
            candidates: list[Place],
            *,
            kind: str,
            used_ids: set[str],
        ) -> tuple[int, Place] | None:
            available = [c for c in candidates if c.place_id not in used_ids]
            if not available:
                return None

            best: tuple[float, int, Place] | None = None
            ideal_ratio = 0.50 if kind == "food" else 0.72

            insertion_positions = (
                range(1, len(current_route) + 1)
                if current_route
                else range(0, 1)
            )

            for insert_at in insertion_positions:
                previous_place = (
                    current_route[
                        insert_at - 1
                    ]
                    if insert_at > 0
                    else None
                )
                following_place = (
                    current_route[
                        insert_at
                    ]
                    if insert_at
                    < len(
                        current_route
                    )
                    else None
                )

                # 맛집과 카페를 연속으로 붙이지 않습니다.
                # 관광지 → 서비스 → 관광지 흐름을 우선합니다.
                if (
                    previous_place is not None
                    and is_service_stop(
                        previous_place
                    )
                ):
                    continue

                if (
                    following_place is not None
                    and is_service_stop(
                        following_place
                    )
                ):
                    continue

                if insert_at == 0:
                    prev_coord = (
                        req.latitude,
                        req.longitude,
                    )
                else:
                    prev = current_route[
                        insert_at - 1
                    ]
                    prev_coord = (
                        prev.latitude,
                        prev.longitude,
                    )

                next_place = current_route[insert_at] if insert_at < len(current_route) else None
                direct_km = (
                    haversine_km(
                        prev_coord[0], prev_coord[1],
                        next_place.latitude, next_place.longitude,
                    )
                    if next_place is not None
                    else 0.0
                )

                position_ratio = insert_at / max(1, len(current_route))
                position_score = max(
                    0.0,
                    100.0 - abs(position_ratio - ideal_ratio) * 100.0,
                )

                for candidate in available:
                    estimated_arrival = start

                    estimate_prev = (
                        req.latitude,
                        req.longitude,
                    )

                    default_stay = max(
                        30,
                        min(
                            75,
                            (
                                req.available_minutes
                                // max(
                                    len(current_route),
                                    1,
                                )
                            )
                            // 2,
                        ),
                    )

                    for existing in current_route[
                        :insert_at
                    ]:
                        leg_km = haversine_km(
                            estimate_prev[0],
                            estimate_prev[1],
                            existing.latitude,
                            existing.longitude,
                        )

                        estimated_arrival += timedelta(
                            minutes=_estimate_minutes(
                                leg_km,
                                req.transport,
                            )
                        )

                        estimated_arrival += timedelta(
                            minutes=(
                                _service_stay_minutes(
                                    existing
                                )
                                or default_stay
                            )
                        )

                        estimate_prev = (
                            existing.latitude,
                            existing.longitude,
                        )

                    candidate_leg_km = haversine_km(
                        estimate_prev[0],
                        estimate_prev[1],
                        candidate.latitude,
                        candidate.longitude,
                    )

                    estimated_arrival += timedelta(
                        minutes=_estimate_minutes(
                            candidate_leg_km,
                            req.transport,
                        )
                    )

                    candidate_stay = (
                        _service_stay_minutes(
                            candidate
                        )
                        or 45
                    )

                    if _service_break_overlaps(
                        candidate,
                        estimated_arrival,
                        candidate_stay,
                    ):
                        continue

                    first_leg = haversine_km(
                        prev_coord[0], prev_coord[1],
                        candidate.latitude, candidate.longitude,
                    )
                    if next_place is not None:
                        second_leg = haversine_km(
                            candidate.latitude, candidate.longitude,
                            next_place.latitude, next_place.longitude,
                        )
                        detour_km = max(0.0, first_leg + second_leg - direct_km)
                    else:
                        detour_km = first_leg

                    detour_score = max(
                        0.0,
                        100.0 - detour_km * 30.0,
                    )

                    weather_score = (
                        90.0
                        if (
                            weather.get("raining")
                            or weather.get("hot")
                        )
                        else 60.0
                    )

                    arrival_minutes = (
                        estimated_arrival.hour * 60
                        + estimated_arrival.minute
                    )

                    if kind == "food":
                        # 점심 11:30~13:30, 저녁 17:30~19:30 중 가까운 창.
                        meal_centers = (
                            12 * 60 + 30,
                            18 * 60 + 30,
                        )
                        meal_gap = min(
                            abs(
                                arrival_minutes - center
                            )
                            for center in meal_centers
                        )
                        time_fit = max(
                            0.0,
                            100.0
                            - meal_gap / 120.0 * 100.0,
                        )
                    else:
                        # 카페는 점심 이후/오후 휴식 시간 선호.
                        cafe_center = 15 * 60
                        gap = abs(
                            arrival_minutes
                            - cafe_center
                        )
                        time_fit = max(
                            0.0,
                            100.0
                            - gap / 210.0 * 100.0,
                        )

                    score = (
                        detour_score * 0.48
                        + candidate_quality(candidate) * 0.22
                        + position_score * 0.12
                        + time_fit * 0.13
                        + weather_score * 0.05
                    )

                    if best is None or score > best[0]:
                        best = (score, insert_at, candidate)

            return None if best is None else (best[1], best[2])

        used_service_ids: set[str] = set()

        if req.include_food:
            chosen = best_service_insertion(
                route, food_candidates, kind="food", used_ids=used_service_ids
            )
            if chosen is not None:
                insert_at, place = chosen
                route.insert(insert_at, _with_service_kind(place, "food"))
                used_service_ids.add(place.place_id)

        if req.include_cafe:
            chosen = best_service_insertion(
                route, cafe_candidates, kind="cafe", used_ids=used_service_ids
            )
            if chosen is not None:
                insert_at, place = chosen
                route.insert(insert_at, _with_service_kind(place, "cafe"))
                used_service_ids.add(place.place_id)

        # 최종 안전장치: 사용자가 선택하지 않은 맛집/카페는
        # 어떤 경로로 들어왔더라도 최종 코스에서 제거합니다.
        cleaned_route: list[Place] = []

        for place in route:
            raw = (
                place.raw
                if isinstance(
                    place.raw,
                    dict,
                )
                else {}
            )

            service_kind = raw.get(
                "service_kind"
            )

            content_type = str(
                place.content_type_id
                or ""
            )

            is_cafe = (
                service_kind == "cafe"
                or (
                    content_type == "39"
                    and _is_cafe_place(
                        place
                    )
                )
            )

            is_food = (
                service_kind == "food"
                or (
                    content_type == "39"
                    and not is_cafe
                )
            )

            if (
                is_food
                and not req.include_food
            ):
                continue

            if (
                is_cafe
                and not req.include_cafe
            ):
                continue

            cleaned_route.append(
                place
            )

        route = cleaned_route

        # 구간의 출발/도착 좌표는 코스 순서만 알면 이미 확정되므로
        # Kakao 길찾기를 순차 호출하지 않고 모든 구간을 병렬 조회합니다.
        segment_specs: list[
            tuple[tuple[float, float], Place]
        ] = []

        previous_coord = (
            req.latitude,
            req.longitude,
        )

        for place in route:
            segment_specs.append(
                (
                    previous_coord,
                    place,
                )
            )
            previous_coord = (
                place.latitude,
                place.longitude,
            )

        async def resolve_segment(
            origin: tuple[float, float],
            place: Place,
        ) -> dict:
            try:
                route_info = await asyncio.wait_for(
                    self.route.route(
                        origin,
                        (
                            place.latitude,
                            place.longitude,
                        ),
                        req.transport,
                        destination_name=place.title,
                    ),
                    timeout=RECOMMEND_ROUTE_TIMEOUT_SECONDS,
                )

                return {
                    "travel_minutes": max(
                        1,
                        math.ceil(
                            route_info["duration_seconds"] / 60
                        ),
                    ),
                    "distance_m": int(
                        route_info["distance_m"]
                    ),
                    "transfers": int(
                        route_info.get("transfers") or 0
                    ),
                    "fare": route_info.get("fare"),
                    "taxi_fare": route_info.get("taxi_fare"),
                    "toll_fare": route_info.get("toll_fare"),
                    "navigation_url": route_info.get("landing_url"),
                    "fallback": False,
                }

            except (IntegrationError, asyncio.TimeoutError):
                km = haversine_km(
                    origin[0],
                    origin[1],
                    place.latitude,
                    place.longitude,
                )

                return {
                    "travel_minutes": _estimate_minutes(
                        km,
                        req.transport,
                    ),
                    "distance_m": round(km * 1000),
                    "transfers": 0,
                    "fare": None,
                    "taxi_fare": None,
                    "toll_fare": None,
                    "navigation_url": None,
                    "fallback": True,
                }

        segment_results = await asyncio.gather(
            *(
                resolve_segment(origin, place)
                for origin, place in segment_specs
            )
        )

        print(
            "[ROUTE SEGMENTS]",
            [
                {
                    "to": place.title,
                    "minutes": int(
                        segment[
                            "travel_minutes"
                        ]
                    ),
                    "distance_m": int(
                        segment[
                            "distance_m"
                        ]
                    ),
                    "fallback": bool(
                        segment.get(
                            "fallback"
                        )
                    ),
                }
                for place, segment
                in zip(
                    route,
                    segment_results,
                )
            ],
        )

        if any(
            item.get("fallback")
            for item in segment_results
        ):
            if "kakao_route" not in unavailable:
                unavailable.append("kakao_route")

        current_time = start
        total_distance_m = 0
        result_places: list[CoursePlace] = []

        itinerary_stays = _fit_itinerary_stays(
            route,
            available_minutes=req.available_minutes,
            travel_minutes=sum(
                int(
                    segment[
                        "travel_minutes"
                    ]
                )
                for segment in segment_results
            ),
        )

        for index, (place, segment, place_stay) in enumerate(
            zip(
                route,
                segment_results,
                itinerary_stays,
            ),
            start=1,
        ):
            travel_minutes = int(
                segment["travel_minutes"]
            )
            distance_m = int(
                segment["distance_m"]
            )

            current_time += timedelta(
                minutes=travel_minutes
            )

            result_places.append(
                CoursePlace(
                    **place.model_dump(),
                    order=index,
                    arrival_time=current_time,
                    stay_minutes=place_stay,
                    travel_minutes_from_previous=travel_minutes,
                    travel_distance_m_from_previous=distance_m,
                    transfers_from_previous=int(
                        segment.get("transfers") or 0
                    ),
                    fare_from_previous=segment.get("fare"),
                    taxi_fare_from_previous=segment.get("taxi_fare"),
                    toll_fare_from_previous=segment.get("toll_fare"),
                    navigation_url=segment.get("navigation_url"),
                    navigation_provider=(
                        "kakao_navi_sdk"
                        if req.transport == TransportMode.driving
                        else "kakao_map"
                    ),
                    navigation_available=True,
                )
            )

            current_time += timedelta(
                minutes=place_stay
            )
            total_distance_m += distance_m

        total_minutes = sum(
            place.travel_minutes_from_previous
            + place.stay_minutes
            for place in result_places
        )

        labels = {
            CourseType.travel_min:
                "한적 여행",

            CourseType.congestion_avoidance:
                "혼잡 회피",

            CourseType.preference_fit:
                "취향 맞춤",
        }

        # 저장했을 때 코스가 전부 같은 이름으로 보이지 않도록
        # 선택한 테마 + 가용 시간 + 대표 시작/끝 장소를 제목에 반영합니다.
        title_preferences = [
            str(item).strip()
            for item in req.preferences
            if str(item).strip()
            and str(item).strip() not in {"맛집", "카페"}
        ]

        theme_label = (
            "·".join(title_preferences[:2])
            if title_preferences
            else labels[course_type]
        )

        attraction_titles = [
            place.title.strip()
            for place in result_places
            if place.title.strip()
            and _service_kind(place) is None
        ]

        if not attraction_titles:
            attraction_titles = [
                place.title.strip()
                for place in result_places
                if place.title.strip()
            ]

        if len(attraction_titles) >= 2:
            place_label = (
                f"{attraction_titles[0]}"
                f"→{attraction_titles[-1]}"
            )
        elif attraction_titles:
            place_label = attraction_titles[0]
        else:
            place_label = "경주 여행"

        available_hours = max(
            1,
            round(req.available_minutes / 60),
        )

        dynamic_course_title = (
            f"{theme_label} "
            f"{available_hours}시간 · "
            f"{place_label}"
        )

        weather_summary = None

        if weather:
            weather_summary = (
                f"기온 "
                f"{weather.get('temperature_c', '?')}℃"
                f" / "
                f"{'강수 있음' if weather.get('raining') else '강수 없음'}"
            )

        warnings: list[str] = []

        has_food_stop = any(_service_kind(place) == "food" for place in result_places)
        has_cafe_stop = any(_service_kind(place) == "cafe" for place in result_places)

        if req.include_food and not has_food_stop:
            warnings.append("요청한 맛집 후보를 현재 동선 주변에서 찾지 못했습니다.")
        if req.include_cafe and not has_cafe_stop:
            warnings.append("요청한 카페 후보를 현재 동선 주변에서 찾지 못했습니다.")

        for service_place in result_places:
            if (
                _service_kind(service_place)
                in {"food", "cafe"}
                and service_place.arrival_time is not None
                and _service_break_overlaps(
                    service_place,
                    service_place.arrival_time,
                    service_place.stay_minutes,
                )
            ):
                warnings.append(
                    f"{service_place.title} 도착 예정시간이 "
                    "브레이크타임과 겹칠 수 있어 확인이 필요합니다."
                )

        if total_minutes > req.available_minutes:
            warnings.append(
                "실제 경로 기준으로 가용시간을 "
                f"{total_minutes - req.available_minutes}분 "
                "초과할 수 있습니다."
            )

        if (
            req.include_rest_stops
            and req.transport == TransportMode.walking
            and total_distance_m > 2000
            and not any(
                place.is_rest_point
                for place in result_places
            )
        ):
            warnings.append(
                "누적 도보 거리가 2km를 넘습니다. "
                "휴식 장소를 고려하세요."
            )

        return Course(
            course_id=str(uuid4()),
            title=dynamic_course_title,
            type=course_type,
            total_minutes=total_minutes,
            total_distance_km=round(
                total_distance_m / 1000,
                2,
            ),
            objective_values={
                "travel": round(
                    ind.objectives[0],
                    3,
                ),
                "congestion": round(
                    ind.objectives[1],
                    3,
                ),
                "preference_mismatch": round(
                    ind.objectives[2],
                    3,
                ),
            },
            places=result_places,
            weather_summary=weather_summary,
            warnings=warnings,
        )

    async def modify(
        self,
        course: Course,
        command: str,
        context: RecommendRequest,
    ) -> Course:
        try:
            action = await self.openai.parse_course_command(
                command,
                [
                    place.title
                    for place in course.places
                ],
            )
        except IntegrationError:
            action = _rule_based_command(
                command,
                [
                    place.title
                    for place in course.places
                ],
            )

        places = [
            place.model_copy(deep=True)
            for place in course.places
        ]

        target = (
            action.get("target")
            or ""
        ).lower()

        kind = action.get(
            "action"
        )

        if kind == "remove":
            places = [
                place
                for place in places
                if target
                not in place.title.lower()
            ]

        elif (
            kind == "reorder"
            and target
        ):
            matches = [
                place
                for place in places
                if target
                in place.title.lower()
            ]

            places = [
                place
                for place in places
                if target
                not in place.title.lower()
            ]

            if matches:
                position = action.get(
                    "position"
                )

                index = max(
                    0,
                    min(
                        len(places),
                        (
                            position - 1
                            if isinstance(
                                position,
                                int,
                            )
                            else len(places)
                        ),
                    ),
                )

                places[
                    index:index
                ] = matches

        elif kind in (
            "add",
            "replace",
        ):
            query = (
                action.get("replacement")
                or action.get("target")
            )

            if query:
                found = await self.tour.keyword_search(
                    query,
                    limit=5,
                )

                if found:
                    detail = await self.tour.detail(
                        found[0]
                    )

                    course_place = CoursePlace(
                        **detail.model_dump(),
                        order=1,
                        stay_minutes=(
                            self.settings.default_stay_minutes
                        ),
                    )

                    if (
                        kind == "replace"
                        and target
                    ):
                        places = [
                            course_place
                            if target
                            in place.title.lower()
                            else place
                            for place in places
                        ]

                    else:
                        places.append(
                            course_place
                        )

        elif kind == "set_condition":
            new_context = context.model_copy(
                deep=True
            )

            conditions = action.get(
                "conditions"
            ) or []

            if any(
                "무료" in item
                for item in conditions
            ):
                new_context.free_only = True

            if (
                any(
                    "야경" in item
                    for item in conditions
                )
                and "야경"
                not in new_context.preferences
            ):
                new_context.preferences.append(
                    "야경"
                )

            response = await self.recommend(
                new_context
            )

            return response.courses[0]

        if len(places) < 1:
            raise ValueError(
                "코스에서 모든 장소를 제거할 수 없습니다."
            )

        for index, place in enumerate(
            places,
            1,
        ):
            place.order = index

        course.places = places

        course.title = (
            f"{course.title} (수정됨)"
        )

        course.total_minutes = sum(
            place.travel_minutes_from_previous
            + place.stay_minutes
            for place in places
        )

        course.total_distance_km = round(
            sum(
                place.travel_distance_m_from_previous
                for place in places
            )
            / 1000,
            2,
        )

        return course

    async def recalculate(
        self,
        req: RecalculateRequest,
    ) -> Course:
        remaining = [
            place
            for place in req.course.places
            if not place.visited
        ]

        if not remaining:
            return req.course

        now = datetime.now().astimezone()

        try:
            congestion_map = await self.congestion.score_map()
        except IntegrationError:
            congestion_map = {}

        try:
            weather = await self.weather.current(
                req.current_latitude,
                req.current_longitude,
            )
        except IntegrationError:
            weather = {}

        try:
            regional_demand = await self.regional.demand_score()
        except IntegrationError:
            regional_demand = None

        next_place = remaining[0]

        try:
            next_naver_signals = await self.naver.trend_signals(
                [next_place.title]
            )
        except IntegrationError:
            next_naver_signals = {}

        matched = congestion_map.get(
            normalize_name(next_place.title)
        )

        next_place.official_congestion_score = (
            matched[0] if matched else None
        )
        next_place.congestion_date = (
            matched[1] if matched else None
        )
        next_signal = next_naver_signals.get(
            next_place.title,
            {},
        )

        next_place.trend_score = next_signal.get(
            "momentum"
        )

        self._calculate_congestion(
            next_place,
            now,
            weather,
            regional_demand,
            next_signal.get("popularity"),
        )
        next_community_signal = _community_live_signal_map(
            [next_place.place_id],
            now=now,
        )
        _apply_community_live_signal(
            next_place,
            next_community_signal.get(next_place.place_id),
        )

        current_congestion = _route_congestion(next_place)

        if (
            current_congestion is None
            or current_congestion
            <= self.settings.congestion_threshold
        ):
            return req.course

        candidates = await self.tour.nearby_places(
            req.current_latitude,
            req.current_longitude,
            10000,
            self.settings.max_candidate_places,
        )

        details = await self._details(
            candidates
        )
        alternative_community_signals = _community_live_signal_map(
            [place.place_id for place in details],
            now=now,
        )

        try:
            alternative_naver_signals = await self.naver.trend_signals(
                [
                    place.title
                    for place in details[:15]
                ]
            )
        except IntegrationError:
            alternative_naver_signals = {}

        alternatives: list[Place] = []
        same_category_candidates: list[
            Place
        ] = []

        for place in details:
            if not is_user_facing_travel_place(place):
                continue

            match = congestion_map.get(
                normalize_name(place.title)
            )

            place.official_congestion_score = (
                match[0] if match else None
            )
            place.congestion_date = (
                match[1] if match else None
            )
            alternative_signal = (
                alternative_naver_signals.get(
                    place.title,
                    {},
                )
            )

            place.trend_score = alternative_signal.get(
                "momentum"
            )

            self._calculate_congestion(
                place,
                now,
                weather,
                regional_demand,
                alternative_signal.get(
                    "popularity"
                ),
            )
            _apply_community_live_signal(
                place,
                alternative_community_signals.get(place.place_id),
            )

            if place.category != next_place.category:
                continue

            same_category_candidates.append(
                place
            )

            route_congestion = _route_congestion(place)
            if (
                route_congestion is None
                or route_congestion
                <= self.settings.replacement_congestion_threshold
            ):
                alternatives.append(place)

        # 동일 카테고리 후보 자체는 있는데 혼잡도 기준 이하가 하나도 없으면
        # 가장 덜 혼잡하고 가까운 장소를 차선 대체지로 사용합니다.
        if (
            not alternatives
            and same_category_candidates
        ):
            same_category_candidates.sort(
                key=lambda place: (
                    _route_congestion(place)
                    if _route_congestion(place) is not None
                    else 101.0,
                    haversine_km(
                        req.current_latitude,
                        req.current_longitude,
                        place.latitude,
                        place.longitude,
                    ),
                )
            )

            alternatives = [
                same_category_candidates[0]
            ]

        if not alternatives:
            raise ValueError(
                "동일 카테고리의 대체 장소를 찾지 못했습니다."
            )

        alternatives.sort(
            key=lambda place: (
                _route_congestion(place)
                if _route_congestion(place) is not None
                else 101.0,
                haversine_km(
                    req.current_latitude,
                    req.current_longitude,
                    place.latitude,
                    place.longitude,
                ),
            )
        )

        replacement = CoursePlace(
            **alternatives[0].model_dump(),
            order=next_place.order,
            stay_minutes=next_place.stay_minutes,
        )

        req.course.places = [
            replacement
            if place.place_id == next_place.place_id
            else place
            for place in req.course.places
        ]

        req.course.title = (
            f"{req.course.title} "
            "(실시간 재조정)"
        )

        req.course.warnings.append(
            f"{next_place.title} 예상 혼잡도 "
            f"{current_congestion:.0f}%로 "
            f"{replacement.title}(으)로 교체했습니다."
        )

        return req.course


def _related_content_aliases(
    title: str,
) -> list[str]:
    normalized = normalize_name(title)
    aliases = [normalized] if normalized else []

    for prefix in ("경주시", "경주"):
        p = normalize_name(prefix)
        if (
            normalized.startswith(p)
            and len(normalized) > len(p) + 1
        ):
            aliases.append(normalized[len(p):])

    return list(
        dict.fromkeys(
            alias
            for alias in aliases
            if len(alias) >= 2
        )
    )


def _content_mentions_place(
    item: ContentItem,
    title: str,
) -> bool:
    blob = normalize_name(
        f"{item.title} {item.description or ''}"
    )
    return any(
        alias in blob
        for alias in _related_content_aliases(title)
    )


def _allowed_content_url(
    url: str,
    kind: str,
) -> bool:
    try:
        host = (
            urlparse(url).hostname
            or ""
        ).lower()
    except ValueError:
        return False

    if kind == "blog":
        return (
            host == "blog.naver.com"
            or host.endswith(".blog.naver.com")
        )

    if kind == "video":
        return (
            host in {
                "youtube.com",
                "www.youtube.com",
                "m.youtube.com",
                "youtu.be",
            }
            or host.endswith(".youtube.com")
        )

    return False


def _strict_related_items(
    items: list[ContentItem],
    *,
    title: str,
    kind: str,
    limit: int = 5,
) -> list[ContentItem]:
    result: list[ContentItem] = []
    seen: set[str] = set()

    for item in items:
        if not item.url or item.url in seen:
            continue

        if not _allowed_content_url(
            item.url,
            kind,
        ):
            continue

        if not _content_mentions_place(
            item,
            title,
        ):
            continue

        seen.add(item.url)
        result.append(item)

        if len(result) >= limit:
            break

    return result


class ContentService:
    def __init__(
        self,
        settings: Settings,
    ):
        self.naver = NaverClient(
            settings
        )
        self.youtube = YouTubeClient(
            settings
        )

    async def get(
        self,
        place_id: str,
        title: str,
    ) -> tuple[
        list[ContentItem],
        list[ContentItem],
        list[str],
    ]:
        unavailable: list[
            str
        ] = []

        # 넉넉히 검색한 뒤 "정확한 장소명 언급 + 허용 도메인"으로
        # 후처리합니다. 검색 결과 순위만 믿고 그대로 노출하지 않습니다.
        blog_task = self.naver.blogs(
            f"경주 {title}",
            12,
        )

        video_task = self.youtube.videos(
            f"경주 {title}",
            12,
        )

        blogs, videos = await asyncio.gather(
            blog_task,
            video_task,
            return_exceptions=True,
        )

        if isinstance(
            blogs,
            Exception,
        ):
            blogs = []
            unavailable.append(
                "naver_blog"
            )

        if isinstance(
            videos,
            Exception,
        ):
            videos = []
            unavailable.append(
                "youtube"
            )

        blogs = _strict_related_items(
            list(blogs),
            title=title,
            kind="blog",
            limit=5,
        )

        videos = _strict_related_items(
            list(videos),
            title=title,
            kind="video",
            limit=5,
        )

        return (
            blogs,
            videos,
            unavailable,
        )


class JourneyService:
    def __init__(
        self,
        db: Session,
    ):
        self.db = db

    def create(
        self,
        course: Course,
    ) -> JourneyOut:
        record = JourneyRecord(
            course=course.model_dump(
                mode="json"
            ),
            completed_place_ids=[],
        )

        self.db.add(
            record
        )

        self.db.commit()
        self.db.refresh(
            record
        )

        return self._out(
            record
        )

    def get(
        self,
        journey_id: str,
    ) -> JourneyOut:
        record = self.db.get(
            JourneyRecord,
            journey_id,
        )

        if not record:
            raise KeyError(
                journey_id
            )

        return self._out(
            record
        )

    def visit(
        self,
        journey_id: str,
        place_id: str,
        verified_on_device: bool,
    ) -> VisitCheckResponse:
        record = self.db.get(
            JourneyRecord,
            journey_id,
        )

        if not record:
            raise KeyError(
                journey_id
            )

        course = Course.model_validate(
            record.course
        )

        place = next(
            (
                item
                for item in course.places
                if item.place_id
                == place_id
            ),
            None,
        )

        if not place:
            raise ValueError(
                "해당 장소가 코스에 없습니다."
            )

        # GPS/dwell verification is performed on the user's device.
        # The backend never receives the live GPS coordinate.
        distance_m = 0.0
        completed = bool(verified_on_device)

        if (
            completed
            and place_id
            not in record.completed_place_ids
        ):
            record.completed_place_ids = [
                *record.completed_place_ids,
                place_id,
            ]

            place.visited = True

            record.course = course.model_dump(
                mode="json"
            )

            record.updated_at = datetime.now(
                timezone.utc
            )

            self.db.commit()

        reason = (
            "단말기에서 위치 기반 방문 조건을 확인해 방문 완료 처리했습니다."
            if completed
            else "단말기에서 방문 완료 조건이 확인되지 않았습니다."
        )

        return VisitCheckResponse(
            completed=completed,
            distance_m=round(
                distance_m,
                1,
            ),
            reason=reason,
        )

    @staticmethod
    def _out(
        record: JourneyRecord,
    ) -> JourneyOut:
        return JourneyOut(
            journey_id=record.journey_id,
            course=Course.model_validate(
                record.course
            ),
            started_at=record.started_at,
            updated_at=record.updated_at,
            completed_place_ids=(
                record.completed_place_ids
                or []
            ),
        )


class SyncService:
    def __init__(
        self,
        settings: Settings,
        db: Session,
    ):
        self.settings = settings
        self.db = db
        self.tour = TourApiClient(
            settings
        )
        self.openai = OpenAIClient(
            settings
        )

    # 관광공사 원본 데이터에서 areacode가 비어있어 지역 기반 목록 조회(gyeongju_places)에
    # 걸리지 않는 경주 대표 유적지 목록. 실제 TourAPI 응답을 확인해 areacode가 빈 값으로
    # 나오는 것을 확인한 항목들만 넣었다. 지역 기반 목록/코스 추천 로직(gyeongju_places)은
    # 그대로 두고, RAG 색인용으로만 아래 검색 결과를 보충한다.
    LANDMARK_BACKFILL_KEYWORDS = (
        "첨성대", "불국사", "석굴암", "경주 대릉원", "양동마을", "분황사",
        "경주 계림", "월정교", "황룡사지", "감은사지", "문무대왕릉",
        "국립경주박물관", "보문호", "경주 오릉", "태종무열왕릉", "경주 진덕여왕릉",
        "경주 포석정", "경주 김유신묘", "경주 월성",
    )

    # 관광지 데이터와 별도로 RAG 코퍼스에 포함하는 에티켓 안내 문서.
    # 기존 /api/v1/etiquette/nearby(app/api.py)의 규칙 기반 안내 문구를 그대로
    # 재사용해 새 사실을 지어내지 않고, 검색 가능한 문서 형태로만 정리했다.
    ETIQUETTE_DOCUMENTS = (
        {
            "doc_id": "etiquette-general",
            "title": "경주 문화재·관광지 공통 에티켓",
            "category": "에티켓",
            "text": (
                "경주의 문화재와 관광지를 방문할 때 지켜야 할 공통 에티켓입니다.\n"
                "- 문화재와 시설물을 만지거나 훼손하지 마세요.\n"
                "- 촬영 제한 표지와 관람 동선을 지켜주세요.\n"
                "- 주변 관람객과 주민을 위해 큰 소리를 줄여주세요."
            ),
        },
        {
            "doc_id": "etiquette-temple",
            "title": "경주 사찰 방문 에티켓",
            "category": "에티켓",
            "text": (
                "불국사, 석굴암 등 경주의 사찰을 방문할 때 지켜야 할 에티켓입니다.\n"
                "- 사찰에서는 법회와 참배를 방해하지 않도록 복장과 소음을 조심하세요.\n"
                "- 문화재와 시설물을 만지거나 훼손하지 마세요.\n"
                "- 촬영 제한 표지와 관람 동선을 지켜주세요."
            ),
        },
        {
            "doc_id": "etiquette-operating-hours",
            "title": "관람시간·야간개장·휴무일 확인 안내",
            "category": "에티켓",
            "text": (
                "경주 관광지의 관람 가능 시간, 야간 개장 여부, 휴무일은 관광지마다 다릅니다.\n"
                "특정 관광지의 야간 관람 가능 여부나 휴무일이 궁금하면, 이 챗봇에서 해당 "
                "관광지명을 함께 물어보면 관광공사에 등록된 운영시간·휴무일 정보를 안내해 "
                "드립니다. 등록된 정보가 없는 경우에는 확인이 어렵다고 안내합니다."
            ),
        },
        {
            "doc_id": "etiquette-parking-fee",
            "title": "주차·입장료 확인 안내",
            "category": "에티켓",
            "text": (
                "경주 관광지의 주차 가능 여부와 입장료(요금)는 관광지마다 다르며, 관광공사에 "
                "등록된 정보가 있는 경우에만 안내할 수 있습니다. 특정 관광지의 주차나 요금이 "
                "궁금하면 관광지명을 함께 물어보세요. 등록된 정보가 없으면 확인이 어렵다고 "
                "안내합니다."
            ),
        },
    )

    # 유모차/휠체어 등 접근성 정보는 TourAPI(관광공사)에는 거의 비어 있어, 한국관광공사가
    # 운영하는 무장애 관광정보 사이트(열린관광 모두의 여행, access.visitkorea.or.kr)의
    # 공식 페이지를 사람이 직접 확인해 정리했다. 방문객이 많이 묻는 주요 관광지만 우선
    # 포함했고, 시설은 바뀔 수 있어 문서마다 확인 시점과 출처를 명시한다.
    ACCESSIBILITY_DOCUMENTS = (
        {
            "doc_id": "accessibility-donggung-wolji",
            "title": "경주 동궁과 월지 접근성(유모차·휠체어) 안내",
            "category": "접근성",
            "text": (
                "경주 동궁과 월지의 무장애 편의시설 정보입니다 (2026년 9월 확인, 출처: 열린관광 "
                "모두의 여행 access.visitkorea.or.kr, 한국관광공사).\n"
                "- 유모차: 대여 서비스는 확인되지 않으나, 유아용 편의시설이 있어 유모차 출입은 가능합니다.\n"
                "- 휠체어 대여: 관리사무실에서 3대 대여 가능합니다.\n"
                "- 이동로: 주출입구와 전각 주변은 평탄하지만, 외부 탐방로는 흙길이며 곳곳에 불규칙하게 "
                "기울거나 경사진 구간이 있어 휠체어 이용자는 접근이 어려운 곳이 있습니다.\n"
                "- 장애인 주차장: 입구 맞은편 가장 가까운 곳에 7면 있습니다.\n"
                "- 장애인 화장실: 있습니다.\n"
                "- 수유실: 정보가 확인되지 않았습니다."
            ),
        },
        {
            "doc_id": "accessibility-cheomseongdae",
            "title": "경주 첨성대 접근성(유모차·휠체어) 안내",
            "category": "접근성",
            "text": (
                "경주 첨성대의 무장애 편의시설 정보입니다 (2026년 9월 확인, 출처: 열린관광 모두의 "
                "여행 access.visitkorea.or.kr, 한국관광공사).\n"
                "- 휠체어: 출입구까지 턱이 없어 접근 가능합니다. 다만 휠체어 대여 서비스는 확인되지 "
                "않았습니다.\n"
                "- 장애인 주차장: 있습니다.\n"
                "- 장애인 화장실: 월성입구에 있습니다.\n"
                "- 유모차: 대여 서비스는 확인되지 않았고, 여자화장실 내 기저귀갈이대만 확인됩니다.\n"
                "- 수유실: 정보가 확인되지 않았습니다."
            ),
        },
        {
            "doc_id": "accessibility-bulguksa",
            "title": "경주 불국사 접근성(유모차·휠체어) 안내",
            "category": "접근성",
            "text": (
                "경주 불국사의 무장애 편의시설 정보입니다 (2026년 9월 확인, 출처: 열린관광 모두의 "
                "여행 access.visitkorea.or.kr, 한국관광공사).\n"
                "- 유모차: 대여 가능합니다.\n"
                "- 휠체어: 무료로 8대까지 대여 가능하며, 주출입구에서 대웅전까지 경사로가 설치되어 "
                "이동이 원활합니다.\n"
                "- 장애인 주차장: 주출입구 근처에 있고, 장애인 탑승 차량은 매표소까지 진입할 수 "
                "있습니다.\n"
                "- 장애인 화장실: 주차장과 사찰 내부에 각 1곳씩 있습니다.\n"
                "- 수유실: 정보가 확인되지 않았습니다."
            ),
        },
        {
            "doc_id": "accessibility-seokguram",
            "title": "경주 석굴암 접근성(유모차·휠체어) 안내",
            "category": "접근성",
            "text": (
                "경주 석굴암의 무장애 편의시설 정보입니다 (2026년 9월 확인, 출처: 열린관광 모두의 "
                "여행 access.visitkorea.or.kr, 한국관광공사).\n"
                "- 휠체어: 일주문에서 대여 가능합니다. 주출입구는 턱이 없어 접근 가능하지만, "
                "주차장에서 매표소까지 경사로가 있어 도움이 필요할 수 있습니다.\n"
                "- 장애인 주차장: 석굴암주차장에 2면 있습니다.\n"
                "- 장애인 화장실: 있습니다.\n"
                "- 유모차 대여·수유실: 정보가 확인되지 않았습니다."
            ),
        },
        {
            "doc_id": "accessibility-daereungwon",
            "title": "경주 대릉원 접근성(유모차·휠체어) 안내",
            "category": "접근성",
            "text": (
                "경주 대릉원(천마총 일원)의 무장애 편의시설 정보입니다 (2026년 9월 확인, 출처: "
                "열린관광 모두의 여행 access.visitkorea.or.kr, 한국관광공사).\n"
                "- 유모차: 정문 매표소에서 대여 가능합니다.\n"
                "- 휠체어: 대여 가능하며, 출입구까지 턱이 없어 접근하기 좋습니다.\n"
                "- 장애인 주차장: 있습니다.\n"
                "- 장애인 화장실: 있습니다.\n"
                "- 수유실: 정보가 확인되지 않았습니다."
            ),
        },
        {
            "doc_id": "accessibility-gyeongju-museum",
            "title": "국립경주박물관 접근성(유모차·휠체어) 안내",
            "category": "접근성",
            "text": (
                "국립경주박물관의 무장애 편의시설 정보입니다 (2026년 9월 확인, 출처: 열린관광 모두의 "
                "여행 access.visitkorea.or.kr, 한국관광공사).\n"
                "- 유모차: 대여 가능합니다.\n"
                "- 휠체어: 정문에서 대여 가능하며, 주출입구에 경사로와 엘리베이터가 있습니다.\n"
                "- 장애인 주차장: 있습니다.\n"
                "- 장애인 화장실: 있습니다.\n"
                "- 수유실: 신라역사관에 있습니다.\n"
                "- 그 밖에 시각장애인을 위한 음성안내기 대여와 전시관별 점자 키오스크가 있습니다."
            ),
        },
        {
            "doc_id": "accessibility-bomun",
            "title": "경주 보문관광단지·보문호 접근성(유모차·휠체어) 안내",
            "category": "접근성",
            "text": (
                "경주 보문관광단지(보문호 일원)의 무장애 편의시설 정보입니다 (2026년 9월 확인, 출처: "
                "열린관광 모두의 여행 access.visitkorea.or.kr, 한국관광공사).\n"
                "- 휠체어: 대여 가능(여행코스 지도 참고)하며, 출입구까지 턱이 없어 접근 가능합니다.\n"
                "- 이동로: 도보길 대부분은 평탄하지만 경사진 구간이 여러 곳 있어 보호자 동반을 "
                "권장합니다.\n"
                "- 장애인 주차장: 있습니다.\n"
                "- 장애인 화장실: 있습니다.\n"
                "- 유모차 대여·수유실: 정보가 확인되지 않았습니다."
            ),
        },
        {
            "doc_id": "accessibility-bunhwangsa",
            "title": "경주 분황사 접근성(유모차·휠체어) 안내",
            "category": "접근성",
            "text": (
                "경주 분황사의 무장애 편의시설 정보입니다 (2026년 9월 확인, 출처: 열린관광 모두의 "
                "여행 access.visitkorea.or.kr, 한국관광공사).\n"
                "- 휠체어: 주출입구에 경사로가 있어 접근 가능하지만, 접근로에 흙·돌 구간이 있어 "
                "완전히 평탄하지는 않습니다.\n"
                "- 장애인 주차장: 7대 규모로 있습니다.\n"
                "- 장애인 화장실: 있습니다.\n"
                "- 유모차 대여·수유실: 정보가 확인되지 않았습니다."
            ),
        },
        {
            "doc_id": "accessibility-woljeonggyo",
            "title": "경주 월정교 접근성(유모차·휠체어) 안내",
            "category": "접근성",
            "text": (
                "경주 월정교의 무장애 편의시설 정보입니다 (2026년 9월 확인, 출처: 열린관광 모두의 "
                "여행 access.visitkorea.or.kr, 한국관광공사).\n"
                "- 휠체어: 출입구까지 턱이 없고 주출입구에 경사로가 있어 접근 가능합니다.\n"
                "- 장애인 주차장: 월정교주차장에 14대 규모로 있습니다.\n"
                "- 장애인 화장실: 있습니다.\n"
                "- 유모차 대여·휠체어 대여 서비스: 정보가 확인되지 않았습니다."
            ),
        },
        {
            "doc_id": "accessibility-yangdong",
            "title": "경주 양동마을 접근성(유모차·휠체어) 안내",
            "category": "접근성",
            "text": (
                "경주 양동마을(유네스코 세계유산)의 무장애 편의시설 정보입니다 (2026년 9월 확인, "
                "출처: 열린관광 모두의 여행 access.visitkorea.or.kr, 한국관광공사).\n"
                "- 휠체어: 사무실에서 4대 대여 가능합니다.\n"
                "- 장애인 주차장: 전용 구역이 있습니다.\n"
                "- 장애인 화장실: 있습니다.\n"
                "- 유모차 대여, 휠체어 이동로 상세정보, 수유실: 정보가 확인되지 않았습니다. 마을이 "
                "넓고 옛 골목길 위주라 방문 전 경주 장애인관광도우미센터(054-762-2630)에 문의하는 "
                "것을 권장합니다."
            ),
        },
        {
            "doc_id": "accessibility-oreung",
            "title": "경주 오릉 접근성(유모차·휠체어) 안내",
            "category": "접근성",
            "text": (
                "경주 오릉의 무장애 편의시설 정보입니다 (2026년 9월 확인, 출처: 열린관광 모두의 여행 "
                "access.visitkorea.or.kr, 한국관광공사).\n"
                "- 유모차: 입구 매표소에서 1대 대여 가능합니다.\n"
                "- 휠체어: 입구 매표소에서 1대 대여 가능합니다. 주요 보행로는 평탄하지만, 숭덕전과 "
                "알영전으로 가는 접근로는 계단이라 휠체어로 관람하기 어렵습니다.\n"
                "- 장애인 주차장: 주출입구 바로 앞에 2대 있습니다.\n"
                "- 장애인 화장실: 오릉 내부에 있습니다(남녀공용, 고정식 수평손잡이만).\n"
                "- 수유실: 정보가 확인되지 않았습니다."
            ),
        },
        {
            "doc_id": "accessibility-kimyusin-tomb",
            "title": "경주 김유신묘 접근성(유모차·휠체어) 안내",
            "category": "접근성",
            "text": (
                "경주 김유신묘의 무장애 편의시설 정보입니다 (2026년 9월 확인, 출처: 열린관광 모두의 "
                "여행 access.visitkorea.or.kr, 한국관광공사).\n"
                "- 유모차·휠체어: 매표소에서 각 1대(수동)씩 대여 가능하며, 신분증이 필요합니다.\n"
                "- 이동로: 주차장 진입 전 경사로가 있고 나무 데크 바닥이며, 휠체어 이동로는 약 30m "
                "경사로로 여유 공간이 넓은 편입니다.\n"
                "- 장애인 주차장: 2대 주차 가능하며 주변 공간이 충분합니다.\n"
                "- 장애인 화장실: 주차장 옆에 있으며 시설관리자 호출 장치가 있습니다.\n"
                "- 수유실: 정보가 확인되지 않았습니다."
            ),
        },
        {
            "doc_id": "accessibility-wolseong",
            "title": "경주 월성(반월성) 접근성(유모차·휠체어) 안내",
            "category": "접근성",
            "text": (
                "경주 월성(반월성)의 무장애 편의시설 정보입니다 (2026년 9월 확인, 출처: 열린관광 "
                "모두의 여행 access.visitkorea.or.kr, 한국관광공사).\n"
                "- 이동로: 보행로 대부분은 경사로 또는 평탄한 흙포장길이지만, 석빙고 내부는 계단이라 "
                "휠체어로 관람할 수 없습니다.\n"
                "- 장애인 화장실: 월성 입구 오른쪽, 첨성대 방향에 남녀 구분된 공중화장실 내 장애인용 "
                "화장실이 있습니다.\n"
                "- 유모차·휠체어 대여, 장애인 주차장, 수유실: 정보가 확인되지 않았습니다."
            ),
        },
        {
            "doc_id": "accessibility-gyeongju-world",
            "title": "경주월드 접근성(유모차·휠체어) 안내",
            "category": "접근성",
            "text": (
                "경주월드(놀이공원·캘리포니아비치)의 무장애 편의시설 정보입니다 (2026년 9월 확인, "
                "출처: 열린관광 모두의 여행 access.visitkorea.or.kr, 한국관광공사).\n"
                "- 유모차: 대여료 2,000원이며 대여 시 신분증을 맡겨야 합니다.\n"
                "- 휠체어: 무료 대여이며 복지카드가 필요합니다.\n"
                "- 장애인 주차장: 주출입구 바로 앞에 12대 있습니다.\n"
                "- 장애인 화장실: 손잡이·등받이가 있고 관리자 호출 장치가 있습니다.\n"
                "- 수유실: 있습니다.\n"
                "- 원내 이동 공간은 충분한 편입니다."
            ),
        },
        {
            "doc_id": "accessibility-expo-park",
            "title": "경주엑스포대공원·경주타워 접근성(유모차·휠체어) 안내",
            "category": "접근성",
            "text": (
                "경주엑스포대공원(경주타워 포함)의 무장애 편의시설 정보입니다 (2026년 9월 확인, "
                "출처: 열린관광 모두의 여행 access.visitkorea.or.kr, 한국관광공사).\n"
                "- 유모차·휠체어: 종합안내센터에서 각각 무료로 대여할 수 있습니다. 출입통로는 단차가 "
                "없고 경사가 매우 완만합니다.\n"
                "- 이동로: 공원 보행로는 대부분 평지이거나 완만한 경사로입니다. 다만 공원 가장 깊은 "
                "곳에 있는 솔거미술관은 경사가 급해 동반자의 도움이 필요할 수 있고, 화랑숲(야간 "
                "유료 체험 구간)은 산길이라 휠체어 접근이 어렵습니다.\n"
                "- 장애인 주차장: 서쪽 주차장에 10대 이상 있습니다.\n"
                "- 장애인 화장실: 경주타워를 비롯한 대형 건물과 야외 곳곳에 있습니다.\n"
                "- 수유실: 정보가 확인되지 않았습니다."
            ),
        },
    )

    # 외국인 방문객이 궁금해할 만한 내용을 정리한 문서. 특정 국가·인종을 겨냥해
    # "이 나라 사람은 이렇다"는 식으로 일반화하지 않고, ① 신라와 다른 문화권의 실제
    # 역사적 교류(검증된 유물·유적 기준), ② 국적과 무관하게 누구에게나 적용되는 종교
    # /문화시설 예절, ③ 외국인 여행자 실용정보로만 구성했다. 학계에서 이견이 있는
    # 내용(처용 설화)은 정설처럼 쓰지 않고 이견이 있다는 점을 그대로 남겼다.
    INTERNATIONAL_VISITOR_DOCUMENTS = (
        {
            "doc_id": "intl-silk-road-glass",
            "title": "신라와 실크로드: 고분 속 유리공예품",
            "category": "역사·국제교류",
            "text": (
                "경주 고분에서는 신라와 서역(지금의 중앙아시아·서아시아·지중해 지역)의 교류를 "
                "보여주는 유리그릇이 다수 출토되었습니다 (출처: 국립중앙박물관, 우리역사넷).\n"
                "- 황남대총을 비롯한 경주의 왕릉급 무덤에서 복원된 유리용기만 20여 점이며, 로마제국 "
                "후기(4~5세기)의 로만글라스나 사산조 페르시아(3~7세기)의 사산글라스로 분석됩니다.\n"
                "- 황남대총 북분 출토 유리그릇은 사산조 페르시아 계통으로, 북방 초원길을 거쳐 고구려를 "
                "통해 신라로 들어온 것으로 추정됩니다.\n"
                "- 이 유물들은 국립경주박물관에 전시되어 있으며, 신라가 실크로드를 통해 먼 지역과 "
                "교류했음을 보여주는 대표적인 증거로 꼽힙니다."
            ),
        },
        {
            "doc_id": "intl-gyerimro-sword",
            "title": "경주 계림로 보검: 중앙아시아 양식의 유물",
            "category": "역사·국제교류",
            "text": (
                "경주 계림로 보검(보물, 흔히 '신라 황금보검'으로도 알려짐)은 신라와 중앙아시아의 "
                "교류를 보여주는 대표 유물입니다 (출처: 국가유산청 국가유산포털).\n"
                "- 1973년 경주 황남동 계림로 14호분에서 발굴되었으며, 길이 36cm로 금·가넷·마노 등으로 "
                "장식되어 있습니다.\n"
                "- 삼국시대에 흔했던 환두대도와는 형태·문양이 전혀 달라, 한반도가 아닌 서역에서 만들어진 "
                "검으로 확인됩니다. 장식에 쓰인 석류석은 동유럽산이며, 문양도 불가리아 트라키아 시대 "
                "유물과 유사합니다.\n"
                "- 현재 국립경주박물관에 소장되어 있으며, 신라 문화의 국제적 성격을 보여주는 대표 "
                "유물로 평가됩니다."
            ),
        },
        {
            "doc_id": "intl-wonseong-tomb-statue",
            "title": "경주 원성왕릉(괘릉) 무인상: 서역인 모습의 석상",
            "category": "역사·국제교류",
            "text": (
                "경주 원성왕릉(통일신라 제38대 원성왕의 무덤, 흔히 '괘릉'이라 불림)에는 이국적인 "
                "외모의 무인상이 세워져 있어 실제로 방문해서 볼 수 있는 국제교류의 흔적입니다 "
                "(출처: 국가유산청 국가유산포털).\n"
                "- 무덤 입구 좌우에 문인상·무인상·사자상·석주가 배치되어 있는데, 이 중 무인상 한 쌍은 "
                "깊은 눈, 넓은 코, 숱 많은 수염 등 서역인의 얼굴 특징을 사실적으로 표현하고 있습니다.\n"
                "- 학계에서는 무역을 위해 신라에 왔다가 정착한 서역인을 모델로 했을 것으로 추정하며, "
                "통일신라시대 동서 문화 교류를 보여주는 중요한 자료로 평가합니다.\n"
                "- 보물로 지정되어 있으며, 경주 시내에서 불국사 방향으로 가는 길에 위치합니다."
            ),
        },
        {
            "doc_id": "intl-cheoyong-theory",
            "title": "처용 설화와 서역인설 (학계 이견 있음)",
            "category": "역사·국제교류",
            "text": (
                "경주·울산 지역에 전해지는 처용 설화를 둘러싸고 처용을 아랍·페르시아계 서역인으로 "
                "보는 학설이 있지만, 학계에서 정설로 합의된 내용은 아니므로 사실처럼 단정해서 안내하지 "
                "않아야 합니다 (출처: 한국연구재단 학술논문, 언론 칼럼).\n"
                "- 처용의 정체에 대해서는 지방 호족의 아들이라는 설, 아랍·페르시아계 서역인이라는 설 "
                "등 여러 해석이 있습니다.\n"
                "- 아랍상인설을 주장하는 쪽은 처용 설화의 배경인 울산 개운포가 통일신라 시기 국제 "
                "무역항이었다는 점을 근거로 듭니다.\n"
                "- 다만 신라·고려·조선을 통틀어 처용을 아랍인이라고 명시한 기록은 없어, 이 학설에 "
                "대한 학계의 반론도 있습니다. 질문에 답할 때는 '~라는 설이 있다'는 식으로 안내하고, "
                "확정된 사실처럼 말하지 않아야 합니다."
            ),
        },
        {
            "doc_id": "intl-temple-etiquette-detail",
            "title": "한국 사찰 참배 예절 상세 안내 (외국인 방문객용)",
            "category": "예절",
            "text": (
                "종교와 국적에 관계없이 한국 사찰을 방문하는 모든 사람에게 적용되는 참배 예절입니다 "
                "(출처: 대한불교조계종 관련 안내, 국내 여행 매체).\n"
                "- 법당 등 실내에 들어갈 때는 신발을 벗습니다.\n"
                "- 불상 앞에서 예를 표할 때는 두 손을 모아 합장합니다. 절(1배 또는 3배)을 하는 경우가 "
                "많지만, 참배자가 아니라면 절을 하지 않고 가볍게 목례만 해도 실례가 아닙니다. 무릎을 "
                "꿇기 어려우면 합장 후 허리를 숙이는 반절로 대신할 수 있습니다.\n"
                "- 사진 촬영은 반드시 허용 여부를 먼저 확인하세요. 예불 중인 스님이나 참배객, 불상을 "
                "인물 사진 찍듯 촬영하는 것은 무례하게 받아들여질 수 있습니다.\n"
                "- 실내에서는 모자와 선글라스를 벗는 것이 예의입니다.\n"
                "- 사찰 내에서는 음주와 흡연이 금지됩니다. 육류나 마늘·파 등 냄새가 강한 음식(오신채)을 "
                "가지고 들어가는 것도 삼가야 합니다.\n"
                "- 노출이 심한 복장보다는 무릎과 어깨를 가리는 단정한 복장을 권장합니다(한국 사찰은 "
                "동남아 일부 국가처럼 복장을 엄격히 통제하지는 않지만, 단정한 차림이 예의로 여겨집니다)."
            ),
        },
        {
            "doc_id": "intl-tourist-info-center",
            "title": "경주 외국인 관광안내소 및 다국어 해설 안내",
            "category": "실용정보",
            "text": (
                "경주에는 외국어 통역이 가능한 관광안내소와 다국어 문화관광해설사가 있습니다 (출처: "
                "경주문화관광 공식 홈페이지 gyeongju.go.kr/tour).\n"
                "- 불국사 관광안내소: 054-746-4747\n"
                "- 경주 터미널 관광안내소: 054-772-9289\n"
                "- 서라벌 관광안내소: 054-777-1330\n"
                "- 경주역 관광안내소: 054-771-1336\n"
                "- 문화관광해설사는 한국어 외에 영어·일본어·중국어 해설이 가능하며, 대릉원·분황사· "
                "불국사·동궁과 월지·양동마을·첨성대·석굴암 등 주요 유적지에 외국어 해설사가 배치되어 "
                "있습니다. 이용을 원하면 해당 관광지 안내소나 경주문화관광 홈페이지에서 사전 신청할 "
                "수 있습니다."
            ),
        },
        {
            "doc_id": "intl-practical-info",
            "title": "경주 여행 실용정보: 유심·환전·교통",
            "category": "실용정보",
            "text": (
                "경주를 방문하는 외국인 여행자가 자주 묻는 실용정보입니다.\n"
                "- 유심(SIM)·이심(eSIM): 인천공항 등에서 사전 구매해 오거나 국내 통신사 대리점에서 "
                "구매할 수 있습니다. 경주 시내에는 공항만큼 다양한 선택지가 없을 수 있어, 되도록 "
                "방문 전에 준비하는 것을 권장합니다.\n"
                "- 환전: 해외에서 발급된 카드로 출금 가능한 Global ATM을 이용하면 별도 환전 없이도 "
                "현금을 인출할 수 있습니다. 교통카드 충전, 전통시장, 일부 소규모 식당에서는 현금이 "
                "필요할 수 있습니다.\n"
                "- 교통카드: 시내버스 이용 시 교통카드(T-money 등)를 사용하면 편리하며, 편의점에서 "
                "구매·충전할 수 있습니다.\n"
                "- 공공 와이파이: 경주문화관광 홈페이지 안내에 따르면 주요 관광지에서 공공 와이파이 "
                "서비스를 제공합니다."
            ),
        },
        {
            "doc_id": "intl-food-dietary",
            "title": "경주 여행 중 채식·할랄 등 식단 안내",
            "category": "실용정보",
            "text": (
                "경주 내 할랄 인증 음식점이나 채식 전문 식당에 대한 구체적이고 최신인 목록은 이 "
                "챗봇의 자료로는 확인되지 않습니다. 정직하게 안내하면 다음과 같습니다.\n"
                "- 특정 식당이 할랄 인증을 받았는지, 채식 메뉴가 있는지는 방문 전 해당 식당에 직접 "
                "문의하거나 예약 시 요청하는 것이 가장 확실합니다.\n"
                "- 한국관광공사는 무슬림 여행자를 위한 안내 자료를 별도로 운영하고 있어, 대도시 기준 "
                "정보를 참고할 수 있습니다. 다만 경주처럼 상대적으로 작은 관광도시는 대도시보다 "
                "선택지가 제한적일 수 있습니다.\n"
                "- 알레르기나 특정 식이 제한이 있다면 식당 방문 시 사전에 명확히 전달하는 것을 "
                "권장합니다.\n"
                "이 항목은 정보가 부족한 채로 남겨두는 것이 추측성 안내보다 안전하다고 판단해 이렇게 "
                "정리했습니다."
            ),
        },
    )

    # 경주에는 비슷하게 생긴 왕릉·고분이 많아 "이건 누구 무덤이냐"는 질문이 자주 나온다.
    # 관광지별 overview는 길고 백과사전식이라 짧은 식별 질문에는 오히려 잘 안 맞는 경우가
    # 있어서, 핵심 인물·시대·특징만 짧게 정리한 문서를 따로 둔다. 전설/설화는 역사적 사실과
    # 구분해서 "~라는 전설이 있다"는 식으로만 적는다. 전부 국가유산청·한국민족문화대백과·
    # 위키백과 등 공개 자료 기준으로 정리했다 (2026년 9월 확인).
    LANDMARK_HISTORY_DOCUMENTS = (
        {
            "doc_id": "history-oreung",
            "title": "경주 오릉: 신라 시조 박혁거세의 무덤",
            "category": "역사·정체성",
            "text": (
                "경주 오릉은 신라를 세운 시조 박혁거세 거서간(재위 기원전 57~서기 4)과 왕비 "
                "알영부인, 그리고 남해 차차웅·유리 이사금·파사 이사금 등 초기 박씨 왕들의 무덤으로 "
                "전해집니다. 봉토무덤 4기와 원형무덤 1기로 이루어져 있으며, 사적으로 지정되어 "
                "있습니다. 동쪽에는 시조의 위패를 모신 숭덕전이, 그 뒤로는 왕비 탄생 설화와 관련된 "
                "알영정이 있습니다."
            ),
        },
        {
            "doc_id": "history-muyeol-tomb",
            "title": "경주 태종무열왕릉: 삼국통일 기초를 놓은 무열왕",
            "category": "역사·정체성",
            "text": (
                "태종무열왕릉은 신라 제29대 무열왕(김춘추, 재위 654~661)의 무덤입니다. 무열왕은 "
                "당나라와 연합해 백제를 정복하며 삼국통일의 기초를 놓았고, 실제 통일은 아들인 "
                "문무왕 때 고구려를 멸망시키며 완성되었습니다. 무덤 앞에는 국보로 지정된 "
                "태종무열왕릉비가 있는데, 거북 모양 받침돌과 용 모양 머릿돌로 장식되어 당나라의 "
                "영향을 보여줍니다. 무덤은 경주 서악동, 첨성대에서 서쪽으로 이동하면 있는 서악동 "
                "고분군 바로 앞에 있습니다."
            ),
        },
        {
            "doc_id": "history-heungdeok-tomb",
            "title": "경주 흥덕왕릉: 청해진을 세운 흥덕왕",
            "category": "역사·정체성",
            "text": (
                "흥덕왕릉은 신라 제42대 흥덕왕(본명 김수종)의 무덤으로, 경주 안강읍에 있습니다. "
                "흥덕왕은 장보고를 시켜 완도에 청해진을 설치해 서해를 방어하게 했고, 당에서 가져온 "
                "차 종자를 지리산에 심어 재배하도록 한 것으로 유명합니다. 봉토 둘레돌에 십이지신상을 "
                "새겼고, 무덤을 지키는 무인석상은 원성왕릉(괘릉)의 무인상과 마찬가지로 서역인의 "
                "얼굴을 하고 있습니다."
            ),
        },
        {
            "doc_id": "history-jindeok-tomb",
            "title": "경주 진덕여왕릉: 신라의 마지막 성골 여왕",
            "category": "역사·정체성",
            "text": (
                "진덕여왕릉은 신라 제28대 진덕여왕(재위 647~654)의 무덤입니다. 본명은 승만이며, "
                "선덕여왕의 사촌동생이자 신라의 두 번째 여왕이고, 성골 신분으로는 마지막 왕입니다. "
                "김춘추(훗날 무열왕)와 김유신의 도움으로 왕위에 올라 관료·군사 조직을 정비하고 당의 "
                "제도를 적극 받아들였습니다. 무덤 둘레돌에는 갑옷을 입고 무기를 든 십이지신상이 "
                "새겨져 있습니다."
            ),
        },
        {
            "doc_id": "history-kimyusin",
            "title": "경주 김유신묘: 흥무대왕으로 추봉된 통일 공신",
            "category": "역사·정체성",
            "text": (
                "김유신묘는 신라의 삼국통일에 중심 역할을 한 김유신(595~673) 장군의 무덤으로 "
                "전해집니다. 김유신은 660년 백제를, 668년 고구려를 정벌하는 데 앞장섰고, 훗날 "
                "흥덕왕 때 왕이 아니었음에도 '흥무대왕'으로 추봉되었습니다. 무덤 둘레돌에는 십이지 "
                "신상을 새긴 버팀돌이 일정한 간격으로 배치되어 있는데, 이런 십이지신상 무덤 양식은 "
                "통일신라 이후 성덕왕릉에서 시작된 것으로 봅니다."
            ),
        },
        {
            "doc_id": "history-seoak-tombs",
            "title": "경주 서악동 고분군: 무열왕릉 뒤편의 대형 무덤들",
            "category": "역사·정체성",
            "text": (
                "서악동 고분군은 태종무열왕릉 바로 뒤편 구릉에 분포하는 4개의 대형 무덤을 가리키며, "
                "사적으로 지정되어 있습니다. 무열왕릉과 함께 둘러보기 좋은 위치에 있어, 신라 초기 "
                "왕릉의 규모와 형태를 함께 살펴볼 수 있는 곳으로 꼽힙니다."
            ),
        },
        {
            "doc_id": "history-seongdeok-tomb",
            "title": "경주 성덕왕릉: 신라 전성기를 이끈 성덕왕",
            "category": "역사·정체성",
            "text": (
                "성덕왕릉은 신라 제33대 성덕왕(재위 701~737)의 무덤으로, 경주에서 불국사 방향으로 "
                "가는 길의 동남쪽 소나무숲 속에 있습니다. 성덕왕은 당나라와 활발히 교류하며 정치적 "
                "으로 가장 안정된 신라의 전성기를 이끈 왕으로 평가받습니다. 무덤 둘레에 십이지신상을 "
                "새긴 양식이 이 왕릉에서 처음 시작된 것으로 봅니다."
            ),
        },
        {
            "doc_id": "history-gyeongdeok-tomb",
            "title": "경주 경덕왕릉: 불국사를 완성한 경덕왕",
            "category": "역사·정체성",
            "text": (
                "경덕왕릉은 신라 제35대 경덕왕(이름 헌영, 성덕왕의 아들)의 무덤이라 전해집니다. "
                "경덕왕 10년(751년)에 불국사가 완공되었고, 당과도 활발히 교역하며 신라의 전성기를 "
                "이어갔습니다. 경주 시가지에서 서남쪽으로 떨어진 구릉에 자리하고 있습니다."
            ),
        },
        {
            "doc_id": "history-hwangnyongsa",
            "title": "경주 황룡사지: 몽골 침입으로 사라진 9층 목탑",
            "category": "역사·정체성",
            "text": (
                "황룡사지는 신라 최대 규모였던 사찰 황룡사의 터입니다. 553년(진흥왕 14년) 공사를 "
                "시작해 645년(선덕여왕 14년) 9층 목탑이 완성되었고, 이후 약 600년간 경주의 랜드마크 "
                "역할을 했습니다. 하지만 1238년(고려 고종 25년) 몽골의 3차 침입으로 불타 없어진 "
                "뒤로는 다시 세워지지 못했고, 지금은 터와 초석만 남아 있습니다."
            ),
        },
        {
            "doc_id": "history-gameunsa",
            "title": "경주 감은사지: 문무왕의 호국룡 설화",
            "category": "역사·정체성",
            "text": (
                "감은사지는 문무왕이 삼국통일 이후 왜의 침입을 막기 위해 짓기 시작한 절터로, 아들 "
                "신문왕 때인 682년에 완공되었습니다. 문무왕은 죽으면서 '죽은 뒤 나라를 지키는 용이 "
                "되어 불법을 받들고 나라를 지키겠다'는 유언을 남겼다고 전해지며, 화장한 유골은 동해 "
                "대왕암에 모셔졌다는 설화가 있습니다. 금당 아래에 용이 드나들 수 있도록 구멍을 낸 "
                "구조가 남아 있어, 이 설화를 건축적으로 구현한 사례로 이야기됩니다."
            ),
        },
        {
            "doc_id": "history-gyerim-forest",
            "title": "경주 계림: 김알지 탄생 설화와 신라 김씨의 시작",
            "category": "역사·정체성",
            "text": (
                "계림은 경주 김씨의 시조 김알지가 태어났다고 전해지는 숲입니다. 삼국사기에 따르면 "
                "탈해왕 9년(65년), 숲속 나무에 걸린 금궤에서 흰 닭이 울고 그 안에서 사내아이가 "
                "나왔다는 설화가 전합니다. 금궤에서 나왔다 해서 성을 '김'이라 했고, 이때부터 이 "
                "숲을 '계림'이라 부르며 한때 나라 이름으로도 쓰였습니다. 김알지의 6대손 미추가 "
                "김씨 최초로 신라 왕이 되었습니다."
            ),
        },
        {
            "doc_id": "history-poseokjeong",
            "title": "경주 포석정: 신라 왕실 연회 장소이자 경애왕의 비극",
            "category": "역사·정체성",
            "text": (
                "포석정은 신라 왕실의 별궁으로, 제49대 헌강왕(875~885) 무렵 조성된 것으로 봅니다. "
                "중국 왕희지의 유상곡수연(물 위에 술잔을 띄워 시를 짓는 연회)을 본떠 만든 곳으로 "
                "알려져 있습니다. 삼국사기에 따르면 경애왕 4년(927년) 11월, 왕이 이곳에서 연회를 "
                "벌이던 중 후백제 견훤의 기습을 받아 왕이 죽고 왕비와 신하들도 해를 입었다는 기록이 "
                "있습니다."
            ),
        },
        {
            "doc_id": "history-kimdaeseong",
            "title": "불국사·석굴암 창건 설화: 김대성의 두 부모 이야기",
            "category": "역사·정체성",
            "text": (
                "불국사와 석굴암의 창건에는 김대성의 설화가 전해집니다 (삼국유사 '대성효이세부모' "
                "조). 751년(경덕왕 10년) 김대성이 전생의 부모를 위해 석굴암을, 현생의 부모를 위해 "
                "불국사를 짓기 시작했다고 전합니다. 설화에 따르면 가난한 집 아들이었던 대성이 시주 "
                "공덕으로 부잣집 아들로 다시 태어났다고 합니다. 774년 김대성이 완성을 보지 못하고 "
                "세상을 떠나자, 나라에서 불국사 공사를 마무리했습니다."
            ),
        },
        {
            "doc_id": "history-seongdeok-bell",
            "title": "성덕대왕신종(에밀레종): 전설과 실제 역사 구분",
            "category": "역사·정체성",
            "text": (
                "국립경주박물관에 있는 성덕대왕신종은 국보로 지정된 신라의 대표 범종입니다. 경덕왕이 "
                "아버지 성덕왕의 공덕을 기리기 위해 종을 만들려다 뜻을 이루지 못했고, 그 아들 "
                "혜공왕이 771년에 완성했습니다. 아기를 시주해 넣었다는 '에밀레종' 전설로 널리 "
                "알려져 있지만, 이는 전설일 뿐 역사적 사실로 확인되지 않습니다. 실제 기록상 이 종은 "
                "본래 봉덕사에 걸렸다가 절이 폐사된 뒤 영묘사로 옮겨졌고, 조선시대에는 경주읍성 "
                "남문 밖에서 성문 개폐 시간을 알리는 종으로 쓰였습니다."
            ),
        },
        {
            "doc_id": "history-jureonggu",
            "title": "주령구: 동궁과 월지에서 나온 신라 시대 놀이 주사위",
            "category": "역사·정체성",
            "text": (
                "주령구는 정사각형 면 6개와 정육각형 면 8개로 이루어진 14면체 나무 주사위로, 1975년 "
                "동궁과 월지(당시 안압지) 발굴 중 출토되었습니다. 각 면에는 술자리에서 걸리면 해야 "
                "하는 다양한 벌칙이 적혀 있어, 신라인들의 풍류와 음주 문화를 보여주는 유물로 꼽힙니다. "
                "안타깝게도 출토된 진품은 보존 처리 도중 불에 타 없어졌고, 지금은 복제품만 남아 "
                "있습니다."
            ),
        },
        {
            "doc_id": "history-cheomseongdae-debate",
            "title": "첨성대의 용도 논쟁: 정설로 합의되지 않음",
            "category": "역사·정체성",
            "text": (
                "첨성대의 정확한 용도는 학계에서 아직 정설로 합의되지 않았으며, 여러 학설이 "
                "제기됩니다. 1970년대까지는 천문대라는 데 별다른 이견이 없었지만, 이후 구조적으로 "
                "천문 관측에 적합하지 않다는 지적이 나오며 다양한 학설이 제기되었습니다.\n"
                "- 천문대설: 삼국유사에 '별을 바라보는 곳'이라 적힌 데 근거합니다.\n"
                "- 지점 정렬설: 선덕여왕릉과 함께 동지 일출선에 맞춰 배치되었다는 점에 근거합니다.\n"
                "- 우물설: 생김새가 우물과 닮았다는 점에 근거합니다.\n"
                "- 상징물설·제천단설: 실제 관측 도구가 아니라 당대 수학·천문 지식을 반영한 상징적 "
                "구조물, 또는 불교의 수미산을 형상화한 제단이라는 주장입니다.\n"
                "질문에 답할 때는 '~라는 학설이 있다'는 식으로 안내하고, 특정 학설을 정답처럼 "
                "단정하지 않아야 합니다."
            ),
        },
    )

    # 왕릉·고분은 종교시설과는 다른 방식으로 존중해야 할 대상이라, 사찰 예절과 별도로 문서를
    # 둔다. 봉분(무덤 자체)에 올라가는 관광객이 실제로 자주 있어 안내가 필요하다.
    TOMB_ETIQUETTE_DOCUMENTS = (
        {
            "doc_id": "etiquette-tomb",
            "title": "경주 왕릉·고분 방문 예절",
            "category": "예절",
            "text": (
                "경주의 왕릉과 고분(대릉원, 오릉, 김유신묘, 각 왕릉 등)은 실제 무덤이므로 사찰과는 "
                "다른 방식의 예절이 필요합니다.\n"
                "- 봉분(둥근 무덤 자체) 위에 올라가거나 앉지 마세요. 잔디가 덮여 있어 언덕처럼 "
                "보이지만 실제로는 무덤입니다.\n"
                "- 둘레돌, 십이지신상, 문인석·무인석 등 석물을 만지거나 그 위에 올라가지 마세요.\n"
                "- 정해진 관람로를 벗어나 봉분 사이로 가로질러 다니지 마세요.\n"
                "- 다른 문화재와 마찬가지로 큰 소리를 내지 않고, 지정된 곳 외에서는 음식물 섭취를 "
                "삼가는 것이 좋습니다."
            ),
        },
    )

    async def _sync_etiquette_documents(self) -> int:
        if not self.settings.openai_api_key:
            return 0

        all_documents = (
            self.ETIQUETTE_DOCUMENTS
            + self.ACCESSIBILITY_DOCUMENTS
            + self.INTERNATIONAL_VISITOR_DOCUMENTS
            + self.LANDMARK_HISTORY_DOCUMENTS
            + self.TOMB_ETIQUETTE_DOCUMENTS
        )
        texts = [f"{doc['title']}\n{doc['category']}\n{doc['text']}" for doc in all_documents]
        embeddings = await self.openai.embeddings(texts)

        for doc, embedding in zip(all_documents, embeddings, strict=False):
            record = self.db.get(KnowledgeDocument, doc["doc_id"])
            if record:
                record.title = doc["title"]
                record.category = doc["category"]
                record.text = doc["text"]
                record.embedding = embedding
                record.updated_at = datetime.now(timezone.utc)
            else:
                self.db.add(
                    KnowledgeDocument(
                        doc_id=doc["doc_id"],
                        title=doc["title"],
                        category=doc["category"],
                        text=doc["text"],
                        embedding=embedding,
                    )
                )

        return len(all_documents)

    async def sync(
        self,
    ) -> SyncResponse:
        started = datetime.now(
            timezone.utc
        )

        summaries = await self.tour.gyeongju_places(
            limit=1000
        )

        semaphore = asyncio.Semaphore(
            8
        )

        async def detail(
            place: Place,
        ) -> Place:
            async with semaphore:
                try:
                    return await self.tour.detail(
                        place
                    )
                except IntegrationError:
                    return place

        places = list(
            await asyncio.gather(
                *(
                    detail(place)
                    for place in summaries
                )
            )
        )

        embeddings: list[
            list[float] | None
        ] = [
            None
        ] * len(places)

        if self.settings.openai_api_key:
            texts = [
                (
                    f"{place.title}\n"
                    f"{place.category}\n"
                    f"{place.overview or ''}\n"
                    f"{place.address or ''}"
                )
                for place in places
            ]

            for offset in range(
                0,
                len(texts),
                50,
            ):
                batch = await self.openai.embeddings(
                    texts[
                        offset:
                        offset + 50
                    ]
                )

                embeddings[
                    offset:
                    offset + len(batch)
                ] = batch

        stored = 0

        for place, embedding in zip(
            places,
            embeddings,
            strict=False,
        ):
            record = self.db.get(
                PlaceRecord,
                place.place_id,
            )

            data = place.model_dump(
                mode="json"
            )

            if record:
                record.title = place.title
                record.category = place.category
                record.latitude = place.latitude
                record.longitude = place.longitude
                record.data = data
                record.embedding = embedding
                record.updated_at = datetime.now(
                    timezone.utc
                )
            else:
                self.db.add(
                    PlaceRecord(
                        place_id=place.place_id,
                        title=place.title,
                        category=place.category,
                        latitude=place.latitude,
                        longitude=place.longitude,
                        data=data,
                        embedding=embedding,
                    )
                )

            stored += 1

        embedded_docs = await self._sync_etiquette_documents()

        self.db.commit()

        return SyncResponse(
            fetched=len(summaries),
            stored=stored,
            embedded=sum(
                1
                for item in embeddings
                if item
            ) + embedded_docs,
            started_at=started,
            completed_at=datetime.now(
                timezone.utc
            ),
        )


def _rule_based_command(
    command: str,
    place_names: list[str],
) -> dict:
    command_lower = command.lower()

    target = next(
        (
            name
            for name in place_names
            if name.lower()
            in command_lower
        ),
        None,
    )

    ordinal_map = {
        "첫 번째": 1,
        "첫번째": 1,
        "두 번째": 2,
        "두번째": 2,
        "세 번째": 3,
        "세번째": 3,
        "마지막": len(place_names),
    }

    for token, index in ordinal_map.items():
        if (
            token in command_lower
            and 0 < index <= len(place_names)
        ):
            target = place_names[
                index - 1
            ]
            break

    if any(
        token in command_lower
        for token in (
            "빼",
            "제외",
            "삭제",
        )
    ):
        return {
            "action": "remove",
            "target": target,
            "replacement": None,
            "position": None,
            "conditions": [],
        }

    if (
        any(
            token in command_lower
            for token in (
                "마지막",
                "앞으로",
                "순서",
            )
        )
        and target
    ):
        position = (
            len(place_names)
            if "마지막"
            in command_lower
            else 1
        )

        return {
            "action": "reorder",
            "target": target,
            "replacement": None,
            "position": position,
            "conditions": [],
        }

    conditions = [
        token
        for token in (
            "무료",
            "야경",
            "실내",
        )
        if token
        in command_lower
    ]

    if conditions:
        return {
            "action": "set_condition",
            "target": None,
            "replacement": None,
            "position": None,
            "conditions": conditions,
        }

    return {
        "action": "keep",
        "target": target,
        "replacement": None,
        "position": None,
        "conditions": [],
    }


class RagService:
    def __init__(
        self,
        settings: Settings,
        db: Session,
    ):
        self.settings = settings
        self.db = db
        self.openai = OpenAIClient(
            settings
        )

    async def search(
        self,
        query: str,
        top_k: int,
        history: list[ChatTurn] | None = None,
    ) -> RagSearchResponse:
        history = history or []

        # "그거 주차는 되나요?" 같은 후속 질문은 현재 질문만 임베딩하면 어떤 장소를
        # 말하는지 검색이 못 잡는다. 직전 사용자 발화를 붙여서 검색용 텍스트를
        # 만들되, 화면/응답에 쓰는 query 자체는 원래 질문 그대로 둔다.
        last_user_turn = next(
            (turn.content for turn in reversed(history) if turn.role == "user"),
            None,
        )
        search_text = f"{last_user_turn}\n{query}" if last_user_turn else query

        vector = (
            await self.openai.embeddings(
                [search_text]
            )
        )[0]

        place_records = self.db.scalars(
            select(PlaceRecord).where(PlaceRecord.embedding.is_not(None))
        ).all()
        doc_records = self.db.scalars(
            select(KnowledgeDocument).where(KnowledgeDocument.embedding.is_not(None))
        ).all()

        if not place_records and not doc_records:
            raise ValueError(
                "RAG 인덱스가 없습니다. "
                "먼저 POST /api/v1/admin/sync를 실행하세요."
            )

        place_scored = sorted(
            ((_cosine(vector, record.embedding or []), "place", record) for record in place_records),
            key=lambda item: item[0],
            reverse=True,
        )
        doc_scored = sorted(
            ((_cosine(vector, record.embedding or []), "etiquette", record) for record in doc_records),
            key=lambda item: item[0],
            reverse=True,
        )

        # 관광지(300여 건)와 지식 문서(에티켓/접근성, 10여 건)를 한 풀에서 top-k로
        # 경쟁시키면 지식 문서가 항상 밀려나 답변에 반영되지 못한다. 그래서 관광지는
        # top-k만 뽑되, 지식 문서는 개수가 적으니 전부 포함해 둔다. 문서 문구가 서로
        # 비슷해(예: 관광지별 접근성 안내) 임베딩 유사도만으로는 정확한 장소를 놓칠 수
        # 있어서, 개수가 많지 않은 지금은 순위 대신 전부 넘기고 LLM이 관련된 것만
        # 골라 쓰게 한다. 문서가 크게 늘어나면 다시 상위 N개로 제한하는 게 맞다.
        
        # 수정: 관광지는 기존 top_k를 유지하고,
        # 지식 문서는 질문과 가장 유사한 상위 5개만 LLM에 전달한다.
        selected = sorted(
            place_scored[:top_k] + doc_scored[:5],
            key=lambda item: item[0],
            reverse=True,
        )

        if not selected or selected[0][0] < self.settings.rag_min_similarity:
            return RagSearchResponse(
                query=query,
                answer="질문과 관련해 확인할 수 있는 자료가 부족합니다. 관광지명을 함께 알려주시면 다시 찾아볼게요.",
                hits=[],
                grounded=False,
            )

        # LLM에는 selected(관광지 top-k + 지식 문서 전체)를 다 보여줘 정확한 장소를
        # 놓치지 않게 하되, 화면에는 실제로 유사도가 높은 상위 항목만 "참고한 자료"로
        # 보여준다 — 매번 지식 문서 10여 개가 전부 칩으로 뜨면 오히려 혼란스럽다.
        display_pool = selected[:top_k]

        contexts: list[dict] = []
        for score, source_type, record in selected:
            if source_type == "place":
                contexts.append(record.data or {})
            else:
                contexts.append({"title": record.title, "category": record.category, "overview": record.text})

        hits: list[RagHit] = []
        for score, source_type, record in display_pool:
            if source_type == "place":
                data = record.data or {}
                hits.append(
                    RagHit(
                        source_type="place",
                        place_id=record.place_id,
                        title=record.title,
                        category=record.category,
                        similarity=round(score, 4),
                        overview=data.get("overview"),
                        address=data.get("address"),
                        operating_hours=data.get("operating_hours"),
                        rest_date=data.get("rest_date"),
                        fee_text=data.get("fee_text"),
                        parking=data.get("parking"),
                        stroller_info=data.get("stroller_info"),
                        pet_info=data.get("pet_info"),
                        homepage=data.get("homepage"),
                    )
                )
            else:
                hits.append(
                    RagHit(
                        source_type="etiquette",
                        place_id=record.doc_id,
                        title=record.title,
                        category=record.category,
                        similarity=round(score, 4),
                        overview=record.text,
                    )
                )

        answer = await self.openai.answer_with_context(
            query,
            contexts,
            history=history,
        )

        return RagSearchResponse(
            query=query,
            answer=answer,
            hits=hits,
        )


def _cosine(
    a: list[float],
    b: list[float],
) -> float:
    if (
        not a
        or not b
        or len(a) != len(b)
    ):
        return 0.0

    dot = sum(
        x * y
        for x, y in zip(
            a,
            b,
            strict=False,
        )
    )

    norm_a = math.sqrt(
        sum(
            x * x
            for x in a
        )
    )

    norm_b = math.sqrt(
        sum(
            y * y
            for y in b
        )
    )

    return (
        dot / (norm_a * norm_b)
        if norm_a
        and norm_b
        else 0.0
    )
