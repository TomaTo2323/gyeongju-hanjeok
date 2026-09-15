from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import socket
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .clients import IntegrationError, NaverClient, OpenAIClient, normalize_name
from .config import Settings
from .schemas import Place


# 공식 홈페이지 내부에서 우선 탐색할 링크 키워드
_DETAIL_LINK_KEYWORDS = (
    "관광",
    "문화재",
    "소개",
    "이용안내",
    "관람안내",
    "관람료",
    "입장료",
    "이용요금",
    "요금",
    "운영시간",
    "관람시간",
    "개방시간",
    "휴관",
    "휴무",
    "주차",
    "시설안내",
    "방문안내",
)

# NAVER 지역검색 결과의 link가 아래 서비스 자체 링크면
# '공식 홈페이지'로 간주하지 않습니다.
_BLOCKED_OFFICIAL_HOST_TOKENS = (
    "naver.com",
    "naver.me",
    "blog.naver.com",
    "place.naver.com",
    "kakao.com",
    "instagram.com",
    "facebook.com",
    "youtube.com",
    "youtu.be",
)

# 블로그 검색은 필드별로 분리합니다.
_BLOG_QUERY_SUFFIXES = {
    "overview": (
        "소개",
        "볼거리",
        "관광지 정보",
    ),
    "fee": (
        "입장료",
        "관람료",
        "이용요금",
        "무료",
    ),
    "hours": (
        "운영시간",
        "영업시간",
        "관람시간",
        "개방시간",
        "브레이크타임",
        "브레이크 타임",
    ),
    "rest_date": (
        "휴무일",
        "휴관일",
        "쉬는날",
    ),
    "parking": (
        "주차",
        "주차장",
    ),
}


def _fact_lines(
    text: str,
    keywords: tuple[str, ...],
) -> list[str]:
    lines = [
        re.sub(
            r"\s+",
            " ",
            line,
        ).strip()
        for line in re.split(
            r"[\n\r]+",
            text,
        )
    ]

    result: list[str] = []

    for line in lines:
        if (
            line
            and any(
                keyword in line
                for keyword in keywords
            )
        ):
            result.append(
                line[:260]
            )

    return result


def _extract_hours_fact(
    text: str,
) -> str | None:
    for line in _fact_lines(
        text,
        (
            "운영시간",
            "영업시간",
            "이용시간",
            "관람시간",
            "개방시간",
        ),
    ):
        if re.search(
            r"\d{1,2}:\d{2}",
            line,
        ):
            return line

    return None


def _extract_fee_fact(
    text: str,
) -> str | None:
    for line in _fact_lines(
        text,
        (
            "입장료",
            "관람료",
            "이용요금",
            "요금",
            "무료",
        ),
    ):
        if (
            "무료" in line
            or re.search(
                r"\d[\d,]*\s*원",
                line,
            )
        ):
            return line

    return None


def _extract_rest_fact(
    text: str,
) -> str | None:
    for line in _fact_lines(
        text,
        (
            "휴무",
            "휴관",
            "쉬는 날",
            "쉬는날",
            "연중무휴",
        ),
    ):
        return line

    return None


def _extract_parking_fact(
    text: str,
) -> str | None:
    for line in _fact_lines(
        text,
        (
            "주차",
            "주차장",
        ),
    ):
        if any(
            token in line
            for token in (
                "가능",
                "불가",
                "있음",
                "없음",
                "무료",
                "유료",
                "전용",
                "공영",
                "주차장",
            )
        ):
            return line

    return None


def _normalized_time_range(
    text: str,
) -> str | None:
    ranges = re.findall(
        r"(\d{1,2}:\d{2})"
        r"\s*(?:~|-|–|—|부터|까지)\s*"
        r"(\d{1,2}:\d{2})",
        text,
    )

    if not ranges:
        return None

    start, end = ranges[0]
    return f"{start}~{end}"


def _blog_consensus_fact(
    blocks: list[dict[str, str]],
    field: str,
) -> str | None:
    candidates: dict[
        str,
        list[tuple[str, str]],
    ] = {}

    for block in blocks:
        if (
            block.get("type")
            != "naver_blog"
            or block.get("field")
            != field
        ):
            continue

        text = block.get("text") or ""
        url = block.get("url") or ""

        if field == "hours":
            fact = _normalized_time_range(
                text
            )
        elif field == "rest_date":
            if "연중무휴" in text:
                fact = "연중무휴"
            else:
                match = re.search(
                    r"(?:매주\s*)?"
                    r"([월화수목금토일])요일"
                    r".{0,12}"
                    r"(?:휴무|휴관|쉼)",
                    text,
                )
                fact = (
                    f"매주 {match.group(1)}요일 휴무"
                    if match
                    else None
                )
        elif field == "parking":
            if "주차 불가" in text or "주차불가" in text:
                fact = "주차 불가"
            elif (
                "전용주차" in text
                or "전용 주차" in text
            ):
                fact = "전용 주차장 이용 가능"
            elif (
                "주차 가능" in text
                or "주차가능" in text
            ):
                fact = "주차 가능"
            elif "공영주차" in text:
                fact = "인근 공영주차장 이용"
            else:
                fact = None
        else:
            fact = None

        if not fact or not url:
            continue

        candidates.setdefault(
            fact,
            [],
        ).append(
            (
                url,
                text,
            )
        )

    ranked = [
        (
            fact,
            {
                url
                for url, _
                in values
            },
        )
        for fact, values
        in candidates.items()
    ]

    ranked.sort(
        key=lambda item: len(
            item[1]
        ),
        reverse=True,
    )

    if (
        ranked
        and len(
            ranked[0][1]
        ) >= 2
    ):
        return ranked[0][0]

    return None



_OVERVIEW_OPERATION_KEYWORDS = (
    "운영시간",
    "영업시간",
    "관람시간",
    "개방시간",
    "입장료",
    "관람료",
    "이용요금",
    "주차",
    "휴무",
    "휴관",
    "전화",
    "주소",
    "오시는 길",
)

_OVERVIEW_NARRATIVE_KEYWORDS = (
    "문화재",
    "유적",
    "유적지",
    "관광지",
    "사찰",
    "왕릉",
    "고분",
    "박물관",
    "미술관",
    "전시",
    "공원",
    "정원",
    "산책",
    "전망",
    "야경",
    "한옥",
    "전통",
    "카페",
    "음식점",
    "대표",
    "시그니처",
    "전문",
    "메뉴",
)


def _usable_overview_text(
    value: str | None,
) -> str | None:
    text = _compact(value)

    if not text:
        return None

    if len(text) < 12:
        return None

    # 운영정보만 있는 문장은 소개문으로 쓰지 않습니다.
    if (
        any(
            keyword in text
            for keyword in _OVERVIEW_OPERATION_KEYWORDS
        )
        and not any(
            keyword in text
            for keyword in _OVERVIEW_NARRATIVE_KEYWORDS
        )
    ):
        return None

    return text[:420]


def _naver_local_overview(
    sources: list[dict[str, str]],
) -> str | None:
    for source in sources:
        if source.get("type") != "naver_local":
            continue

        raw = source.get("text") or ""

        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue

        description = _usable_overview_text(
            payload.get("description")
        )

        if description:
            return description

    return None


def _official_overview(
    title: str,
    sources: list[dict[str, str]],
) -> str | None:
    aliases = _place_name_aliases(title)

    for source in sources:
        if source.get("type") != "official_homepage":
            continue

        text = source.get("text") or ""

        lines = [
            re.sub(
                r"\s+",
                " ",
                line,
            ).strip()
            for line in text.splitlines()
        ]

        for line in lines:
            candidate = _usable_overview_text(
                line
            )

            if not candidate:
                continue

            normalized = normalize_name(
                candidate
            )

            if not any(
                alias in normalized
                for alias in aliases
            ):
                continue

            # 메뉴/내비게이션처럼 지나치게 짧은 문구는 제외.
            if len(candidate) < 28:
                continue

            return candidate[:420]

    return None


def _apply_deterministic_sources(
    place: Place,
    sources: list[dict[str, str]],
) -> None:
    """
    OpenAI 호출 성공 여부와 관계없이 공식 홈페이지/NAVER 근거에서
    명확하게 파싱 가능한 운영정보를 먼저 채웁니다.
    """
    if not place.overview:
        value = _naver_local_overview(
            sources
        )

        if value:
            place.overview = value
            place.overview_source = (
                "naver_local"
            )

    if not place.overview:
        value = _official_overview(
            place.title,
            sources,
        )

        if value:
            place.overview = value
            place.overview_source = (
                "official_homepage"
            )

    official_texts = [
        source.get("text") or ""
        for source in sources
        if source.get("type")
        == "official_homepage"
    ]

    if official_texts:
        combined = "\n".join(
            official_texts
        )

        if not place.operating_hours:
            value = _extract_hours_fact(
                combined
            )

            if value:
                place.operating_hours = value
                place.operating_hours_source = (
                    "official_homepage"
                )

        if not place.fee_text:
            value = _extract_fee_fact(
                combined
            )

            if value:
                place.fee_text = value
                place.fee_source = (
                    "official_homepage"
                )
                place.is_free = _to_bool_free(
                    value
                )

        if not place.rest_date:
            value = _extract_rest_fact(
                combined
            )

            if value:
                place.rest_date = value
                place.rest_date_source = (
                    "official_homepage"
                )

        if not place.parking:
            value = _extract_parking_fact(
                combined
            )

            if value:
                place.parking = value
                place.parking_source = (
                    "official_homepage"
                )

    # 블로그만 근거일 때는 서로 다른 URL 2개 이상이 같은 사실을 말해야 사용.
    if not place.operating_hours:
        value = _blog_consensus_fact(
            sources,
            "hours",
        )

        if value:
            place.operating_hours = value
            place.operating_hours_source = (
                "naver_blog_consensus"
            )

    if not place.rest_date:
        value = _blog_consensus_fact(
            sources,
            "rest_date",
        )

        if value:
            place.rest_date = value
            place.rest_date_source = (
                "naver_blog_consensus"
            )

    if not place.parking:
        value = _blog_consensus_fact(
            sources,
            "parking",
        )

        if value:
            place.parking = value
            place.parking_source = (
                "naver_blog_consensus"
            )

    used_sources = [
        value
        for value in (
            place.overview_source,
            place.operating_hours_source,
            place.fee_source,
            place.rest_date_source,
            place.parking_source,
        )
        if value
    ]

    place.info_sources = list(
        dict.fromkeys(
            [
                *place.info_sources,
                *used_sources,
            ]
        )
    )


def _compact(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _place_name_aliases(title: str) -> list[str]:
    normalized = normalize_name(title)
    aliases = [normalized] if normalized else []

    for prefix in ("경주시", "경주"):
        normalized_prefix = normalize_name(prefix)
        if (
            normalized.startswith(normalized_prefix)
            and len(normalized) > len(normalized_prefix) + 1
        ):
            aliases.append(
                normalized[len(normalized_prefix):]
            )

    return list(
        dict.fromkeys(
            alias
            for alias in aliases
            if len(alias) >= 2
        )
    )


def _text_mentions_place(
    title: str,
    text: str,
) -> bool:
    normalized_text = normalize_name(text)
    return any(
        alias in normalized_text
        for alias in _place_name_aliases(title)
    )


def _strip_html_document(raw_html: str) -> str:
    text = re.sub(
        r"(?is)<(script|style|noscript|svg|template)\b.*?</\1>",
        " ",
        raw_html,
    )
    text = re.sub(r"(?is)<!--.*?-->", " ", text)
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|div|li|tr|h[1-6])>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _extract_href_from_html(value: str | None) -> str | None:
    if not value:
        return None

    decoded = html.unescape(value)

    match = re.search(
        r'(?is)\bhref\s*=\s*["\']([^"\']+)["\']',
        decoded,
    )
    if match:
        return match.group(1).strip()

    direct = re.search(
        r'(?i)\bhttps?://[^\s<>"\']+',
        decoded,
    )
    if direct:
        return direct.group(0).rstrip(").,;")

    return None


def _homepage_from_place(place: Place) -> str | None:
    # 이미 URL 형태로 정리돼 있으면 그대로 사용
    if place.homepage:
        direct = _extract_href_from_html(place.homepage)
        if direct:
            return direct

        if place.homepage.startswith(("http://", "https://")):
            return place.homepage

    # TourAPI detailCommon2의 원본 homepage에는
    # <a href="..."> 형태가 남아 있을 수 있으므로 raw에서도 복구
    raw = place.raw or {}
    common = raw.get("common") if isinstance(raw, dict) else None

    if isinstance(common, dict):
        homepage_raw = common.get("homepage")
        href = _extract_href_from_html(
            str(homepage_raw) if homepage_raw else None
        )
        if href:
            return href

    return None


def _is_probably_official_external_url(url: str | None) -> bool:
    if not url:
        return False

    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    host = (parsed.hostname or "").lower()

    if parsed.scheme not in ("http", "https") or not host:
        return False

    return not any(
        token in host
        for token in _BLOCKED_OFFICIAL_HOST_TOKENS
    )


def _extract_candidate_links(
    raw_html: str,
    base_url: str,
    limit: int = 5,
) -> list[str]:
    """
    공식 홈페이지 첫 페이지에서 이용안내/관람료/소개 등
    정보성 하위 페이지를 우선순위로 찾아냅니다.
    """
    result: list[str] = []

    base = urlparse(base_url)
    base_host = (base.hostname or "").lower()

    pattern = re.compile(
        r'(?is)<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>'
    )

    ranked: list[tuple[int, str]] = []

    for href, anchor_html in pattern.findall(raw_html):
        anchor = _strip_html_document(anchor_html)
        absolute = urljoin(
            base_url,
            html.unescape(href).strip(),
        )

        try:
            parsed = urlparse(absolute)
        except ValueError:
            continue

        host = (parsed.hostname or "").lower()

        if (
            parsed.scheme not in ("http", "https")
            or not host
            or host != base_host
        ):
            continue

        haystack = f"{anchor} {parsed.path} {parsed.query}".lower()

        score = sum(
            1
            for keyword in _DETAIL_LINK_KEYWORDS
            if keyword.lower() in haystack
        )

        if score:
            ranked.append((score, absolute))

    ranked.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    for _, url in ranked:
        if url not in result:
            result.append(url)

        if len(result) >= limit:
            break

    return result


def _to_bool_free(text: str | None) -> bool | None:
    if not text:
        return None

    compact = re.sub(r"\s+", "", text).lower()

    if any(
        token in compact
        for token in (
            "무료",
            "0원",
            "free",
        )
    ):
        return True

    if any(
        token in compact
        for token in (
            "유료",
            "원",
            "입장료",
            "관람료",
            "이용료",
        )
    ):
        return False

    return None


def _extract_openai_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]

    for output in payload.get("output", []):
        for content in output.get("content", []):
            if (
                content.get("type") in ("output_text", "text")
                and content.get("text")
            ):
                return content["text"]

    raise IntegrationError(
        "openai",
        "장소 정보 보완 응답에서 텍스트를 찾지 못했습니다.",
    )



def _normalize_won_amount(value: str) -> int | None:
    digits = re.sub(r"[^0-9]", "", value)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _fee_claim_from_text(text: str) -> str | None:
    """
    NAVER 블로그 검색 스니펫에서 '입장/관람/이용 요금'에 직접 연결된
    명확한 무료/금액 표현만 추출합니다.

    주차 무료, 체험 무료 등 다른 종류의 '무료'는 입장료로 오인하지 않습니다.
    """
    compact = _compact(text)
    if not compact:
        return None

    lowered = compact.lower()

    fee_words = (
        "입장료",
        "관람료",
        "이용료",
        "이용요금",
        "입장 요금",
        "관람 요금",
    )

    # "입장료 무료", "관람료 없음", "무료 입장/관람" 등
    free_patterns = (
        r"(?:입장료|관람료|이용료|이용요금|입장\s*요금|관람\s*요금)"
        r".{0,25}?(?:무료|없음|0\s*원)",
        r"(?:무료\s*(?:입장|관람)|입장\s*무료|관람\s*무료)",
    )

    if any(re.search(pattern, lowered) for pattern in free_patterns):
        return "무료"

    # "입장료 3,000원", "관람료는 2000원"처럼 요금 키워드 바로 뒤 금액만 추출
    amount_pattern = re.compile(
        r"(?:입장료|관람료|이용료|이용요금|입장\s*요금|관람\s*요금)"
        r"[^0-9]{0,25}"
        r"([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{1,6})\s*원"
    )

    match = amount_pattern.search(lowered)
    if match:
        amount = _normalize_won_amount(match.group(1))
        if amount is not None:
            return f"{amount:,}원"

    # 요금 키워드가 전혀 없는 '무료'는 입장료 근거로 사용하지 않음
    if not any(word in lowered for word in fee_words):
        return None

    return None


def _fee_consensus_from_blog_blocks(
    blocks: list[dict[str, str]],
) -> str | None:
    """
    서로 다른 URL 2개 이상에서 동일한 입장료 사실이 확인될 때만 반환합니다.
    """
    evidence: dict[str, set[str]] = {}

    for block in blocks:
        if (
            block.get("type") != "naver_blog"
            or block.get("field") != "fee"
        ):
            continue

        url = block.get("url") or ""
        if not url:
            continue

        claim = _fee_claim_from_text(
            block.get("text") or ""
        )
        if not claim:
            continue

        evidence.setdefault(claim, set()).add(url)

    confirmed = [
        (claim, urls)
        for claim, urls in evidence.items()
        if len(urls) >= 2
    ]

    if not confirmed:
        return None

    # 더 많은 독립 URL이 같은 내용을 말하는 값을 우선합니다.
    confirmed.sort(
        key=lambda item: len(item[1]),
        reverse=True,
    )

    best_claim, _ = confirmed[0]

    if best_claim == "무료":
        return "무료"

    return f"입장료 {best_claim}"

def _source_rank(source: str | None) -> int:
    """
    숫자가 작을수록 신뢰도가 높습니다.
    """
    return {
        "tour_api": 0,
        "official_homepage": 0,
        "official_web": 0,
        "naver_local": 1,
        "naver_blog_consensus": 2,
        None: 99,
    }.get(source, 99)


def _confidence_label(value: str | None) -> str:
    return {
        "high": "공식 정보",
        "medium": "공식·보조 정보",
        "low": "참고 정보",
        "unknown": "확인 필요",
        None: "확인 필요",
    }.get(value, "확인 필요")


class PlaceInfoEnricher:
    """
    최종 추천 코스에 실제로 포함된 장소만 상세정보를 보완합니다.

    우선순위
      1. 한국관광공사 API 원본
      2. 관광공사에 등록된 공식 홈페이지
      3. NAVER 지역 검색
      4. NAVER 블로그 검색(복수 결과 합의가 있을 때만)
      5. OpenAI는 수집된 자료를 구조화/요약할 뿐,
         자료에 없는 사실을 생성하지 않습니다.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.naver = NaverClient(settings)
        self.openai = OpenAIClient(settings)

        self.timeout = httpx.Timeout(
            max(
                5.0,
                float(settings.http_timeout_seconds),
            )
        )

        self._cache: dict[str, Place] = {}

    async def enrich(
        self,
        place: Place,
    ) -> Place:
        cached = self._cache.get(place.place_id)

        if cached is not None:
            return cached.model_copy(deep=True)

        result = place.model_copy(deep=True)

        # TourAPI에서 이미 값이 있는 필드의 출처는
        # 보완 시작 전에 명확히 표시합니다.
        self._mark_existing_tour_sources(result)

        source_blocks: list[dict[str, str]] = []

        tour_existing = {
            "overview": result.overview,
            "fee_text": result.fee_text,
            "operating_hours": result.operating_hours,
            "rest_date": result.rest_date,
            "parking": result.parking,
            "tel": result.tel,
            "homepage": result.homepage,
        }

        if any(tour_existing.values()):
            source_blocks.append(
                {
                    "type": "tour_api",
                    "field": "existing",
                    "query": "",
                    "url": "",
                    "text": json.dumps(
                        tour_existing,
                        ensure_ascii=False,
                    ),
                }
            )

        # ------------------------------------------------------------------
        # 1. 관광공사에 등록된 공식 홈페이지
        # ------------------------------------------------------------------
        homepage_url = _homepage_from_place(result)

        if homepage_url:
            result.homepage = homepage_url

            official_pages = await self._fetch_official_pages(
                homepage_url
            )

            for url, text in official_pages:
                source_blocks.append(
                    {
                        "type": "official_homepage",
                        "field": "all",
                        "query": "",
                        "url": url,
                        "text": text,
                    }
                )

        # ------------------------------------------------------------------
        # 2. NAVER 지역 검색
        # ------------------------------------------------------------------
        local_results: list[dict[str, Any]] = []

        try:
            local_results = await self.naver.local_search(
                f"경주 {result.title}",
                limit=5,
                sort="random",
            )
        except IntegrationError:
            local_results = []

        best_local = self._best_local_match(
            result,
            local_results,
        )

        if best_local:
            source_blocks.append(
                {
                    "type": "naver_local",
                    "field": "overview",
                    "query": f"경주 {result.title}",
                    "url": best_local.get("link") or "",
                    "text": json.dumps(
                        {
                            "title": best_local.get("title"),
                            "category": best_local.get("category"),
                            "description": best_local.get("description"),
                            "telephone": best_local.get("telephone"),
                            "address": best_local.get("address"),
                            "road_address": best_local.get("road_address"),
                        },
                        ensure_ascii=False,
                    ),
                }
            )

            if (
                not result.tel
                and best_local.get("telephone")
            ):
                result.tel = best_local["telephone"]

            local_link = best_local.get("link")

            # 관광공사에서 홈페이지를 주지 않았지만
            # NAVER 지역검색이 외부 사이트를 반환한 경우만 보조 후보로 사용
            if (
                not homepage_url
                and _is_probably_official_external_url(local_link)
            ):
                official_pages = await self._fetch_official_pages(
                    local_link
                )

                if official_pages:
                    result.homepage = local_link

                    for url, text in official_pages:
                        source_blocks.append(
                            {
                                "type": "official_homepage",
                                "field": "all",
                                "query": "",
                                "url": url,
                                "text": text,
                            }
                        )

        # ------------------------------------------------------------------
        # 3. NAVER 블로그 - 필드별로 별도 검색
        # ------------------------------------------------------------------
        blog_blocks = await self._collect_blog_evidence(
            result.title
        )

        source_blocks.extend(blog_blocks)

        # NAVER 블로그 스니펫에서 입장료가 매우 명확하고,
        # 서로 다른 URL 2개 이상이 동일한 사실을 말하면
        # OpenAI보다 먼저 보수적으로 확정합니다.
        if not result.fee_text:
            fee_consensus = _fee_consensus_from_blog_blocks(
                blog_blocks
            )

            if fee_consensus:
                result.fee_text = fee_consensus
                result.fee_source = "naver_blog_consensus"
                result.is_free = _to_bool_free(
                    fee_consensus
                )
                result.info_sources = list(
                    dict.fromkeys(
                        [
                            *result.info_sources,
                            "naver_blog_consensus",
                        ]
                    )
                )

        if not source_blocks:
            self._recalculate_confidence(
                result
            )
            result.info_enriched_at = datetime.now(
                timezone.utc
            )

            self._cache[place.place_id] = result.model_copy(
                deep=True
            )
            return result

        # ------------------------------------------------------------------
        # 4. 규칙 기반 확정
        # ------------------------------------------------------------------
        # OpenAI 키/모델/네트워크 상태와 무관하게,
        # 공식 홈페이지와 NAVER 복수 합의에서 명확한 값은 먼저 채웁니다.
        _apply_deterministic_sources(
            result,
            source_blocks,
        )

        # ------------------------------------------------------------------
        # 5. OpenAI 구조화 (선택적 추가 보완)
        # ------------------------------------------------------------------
        try:
            extracted = await self._extract_with_openai(
                result,
                source_blocks,
            )
        except IntegrationError:
            extracted = {}

        self._apply_extracted(
            result,
            extracted,
        )

        # 최종 출처 기반으로 전체 신뢰도를 다시 계산합니다.
        # 일부 운영정보만 TourAPI에 있다고 무조건 high가 되지 않습니다.
        self._recalculate_confidence(result)

        result.info_enriched_at = datetime.now(
            timezone.utc
        )

        self._cache[place.place_id] = result.model_copy(
            deep=True
        )

        return result

    @staticmethod
    def _mark_existing_tour_sources(
        place: Place,
    ) -> None:
        mapping = (
            ("overview", "overview_source"),
            ("fee_text", "fee_source"),
            ("operating_hours", "operating_hours_source"),
            ("rest_date", "rest_date_source"),
            ("parking", "parking_source"),
        )

        has_tour_value = False

        for field, source_field in mapping:
            if getattr(place, field, None):
                has_tour_value = True

                if not getattr(place, source_field, None):
                    setattr(
                        place,
                        source_field,
                        "tour_api",
                    )

        if has_tour_value:
            place.info_sources = list(
                dict.fromkeys(
                    [
                        *place.info_sources,
                        "tour_api",
                    ]
                )
            )

    async def _collect_blog_evidence(
        self,
        title: str,
    ) -> list[dict[str, str]]:
        """
        '입장료 운영시간 이용요금'을 한 번에 검색하지 않고
        필드별 검색어를 각각 실행합니다.

        동일 URL 중복은 필드별로 제거합니다.
        """
        tasks: list[
            tuple[str, str, Any]
        ] = []

        for field, suffixes in _BLOG_QUERY_SUFFIXES.items():
            for suffix in suffixes:
                query = f"경주 {title} {suffix}"

                tasks.append(
                    (
                        field,
                        query,
                        self.naver.blogs(
                            query,
                            limit=5,
                        ),
                    )
                )

        gathered = await asyncio.gather(
            *(
                task
                for _, _, task in tasks
            ),
            return_exceptions=True,
        )

        blocks: list[dict[str, str]] = []

        seen: set[tuple[str, str]] = set()

        for (
            field,
            query,
            _,
        ), result in zip(
            tasks,
            gathered,
            strict=False,
        ):
            if isinstance(result, Exception):
                continue

            for item in result:
                if not item.url:
                    continue

                dedupe_key = (
                    field,
                    item.url,
                )

                if dedupe_key in seen:
                    continue

                seen.add(
                    dedupe_key
                )

                snippet = _compact(
                    f"{item.title}. "
                    f"{item.description or ''}"
                )

                if not snippet:
                    continue

                # 다른 관광지/업체의 블로그 정보가 섞이지 않도록
                # 검색결과 제목 또는 설명에 현재 장소명이 실제로
                # 들어간 경우만 상세정보 근거로 사용합니다.
                if not _text_mentions_place(
                    title,
                    snippet,
                ):
                    continue

                blocks.append(
                    {
                        "type": "naver_blog",
                        "field": field,
                        "query": query,
                        "url": item.url,
                        "text": snippet,
                    }
                )

        return blocks

    async def _fetch_official_pages(
        self,
        start_url: str,
    ) -> list[tuple[str, str]]:
        if not await self._is_safe_public_url(
            start_url
        ):
            return []

        first = await self._fetch_html(
            start_url
        )

        if first is None:
            return []

        final_url, raw_html = first

        pages: list[
            tuple[str, str]
        ] = []

        main_text = _strip_html_document(
            raw_html
        )

        if main_text:
            pages.append(
                (
                    final_url,
                    main_text[:16000],
                )
            )

        # 정보성 하위 페이지 최대 4개 추가
        links = _extract_candidate_links(
            raw_html,
            final_url,
            limit=4,
        )

        for link in links:
            if not await self._is_safe_public_url(
                link
            ):
                continue

            fetched = await self._fetch_html(
                link
            )

            if fetched is None:
                continue

            page_url, page_html = fetched

            page_text = _strip_html_document(
                page_html
            )

            if page_text:
                pages.append(
                    (
                        page_url,
                        page_text[:16000],
                    )
                )

        return pages

    async def _fetch_html(
        self,
        url: str,
    ) -> tuple[str, str] | None:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "GyeongjuHanjeok/1.0 "
                        "(tourism-information-enrichment)"
                    )
                },
            ) as client:
                response = await client.get(
                    url
                )

                response.raise_for_status()

                content_type = (
                    response.headers.get(
                        "content-type"
                    )
                    or ""
                ).lower()

                if (
                    "text/html" not in content_type
                    and "application/xhtml+xml"
                    not in content_type
                ):
                    return None

                # 너무 큰 페이지는 제외
                if len(response.content) > 1_500_000:
                    return None

                final_url = str(
                    response.url
                )

                if not await self._is_safe_public_url(
                    final_url
                ):
                    return None

                return (
                    final_url,
                    response.text,
                )

        except (
            httpx.HTTPError,
            UnicodeError,
            ValueError,
        ):
            return None

    async def _is_safe_public_url(
        self,
        url: str,
    ) -> bool:
        """
        서버가 관광지 홈페이지를 직접 요청하므로
        localhost/private IP 등에 접근하지 못하게 막습니다.
        """
        try:
            parsed = urlparse(
                url
            )
        except ValueError:
            return False

        if parsed.scheme not in (
            "http",
            "https",
        ):
            return False

        host = parsed.hostname

        if not host:
            return False

        lowered = host.lower()

        if lowered in {
            "localhost",
            "localhost.localdomain",
        }:
            return False

        try:
            literal = ipaddress.ip_address(
                lowered
            )

            return not (
                literal.is_private
                or literal.is_loopback
                or literal.is_link_local
                or literal.is_reserved
                or literal.is_multicast
            )

        except ValueError:
            pass

        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                parsed.port
                or (
                    443
                    if parsed.scheme == "https"
                    else 80
                ),
                type=socket.SOCK_STREAM,
            )
        except OSError:
            return False

        if not infos:
            return False

        for info in infos:
            address = info[4][0]

            try:
                ip = ipaddress.ip_address(
                    address
                )
            except ValueError:
                return False

            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
            ):
                return False

        return True

    @staticmethod
    def _best_local_match(
        place: Place,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not rows:
            return None

        wanted = normalize_name(
            place.title
        )

        ranked: list[
            tuple[int, dict[str, Any]]
        ] = []

        for row in rows:
            title = normalize_name(
                str(
                    row.get("title")
                    or ""
                )
            )

            score = 0

            if title == wanted:
                score += 100

            elif wanted and (
                wanted in title
                or title in wanted
            ):
                score += 60

            address = (
                row.get("road_address")
                or row.get("address")
                or ""
            )

            if "경주" in str(address):
                score += 20

            ranked.append(
                (
                    score,
                    row,
                )
            )

        ranked.sort(
            key=lambda item: item[0],
            reverse=True,
        )

        best_score, best = ranked[0]

        return (
            best
            if best_score >= 20
            else None
        )

    async def _extract_with_openai(
        self,
        place: Place,
        sources: list[dict[str, str]],
    ) -> dict[str, Any]:
        schema = {
            "type": "object",
            "properties": {
                "overview": {
                    "type": [
                        "string",
                        "null",
                    ],
                },
                "overview_source": {
                    "type": [
                        "string",
                        "null",
                    ],
                    "enum": [
                        "tour_api",
                        "official_homepage",
                        "naver_local",
                        "naver_blog_consensus",
                        None,
                    ],
                },
                "fee_text": {
                    "type": [
                        "string",
                        "null",
                    ],
                },
                "fee_source": {
                    "type": [
                        "string",
                        "null",
                    ],
                    "enum": [
                        "tour_api",
                        "official_homepage",
                        "naver_local",
                        "naver_blog_consensus",
                        None,
                    ],
                },
                "operating_hours": {
                    "type": [
                        "string",
                        "null",
                    ],
                },
                "operating_hours_source": {
                    "type": [
                        "string",
                        "null",
                    ],
                    "enum": [
                        "tour_api",
                        "official_homepage",
                        "naver_local",
                        "naver_blog_consensus",
                        None,
                    ],
                },
                "rest_date": {
                    "type": [
                        "string",
                        "null",
                    ],
                },
                "rest_date_source": {
                    "type": [
                        "string",
                        "null",
                    ],
                    "enum": [
                        "tour_api",
                        "official_homepage",
                        "naver_local",
                        "naver_blog_consensus",
                        None,
                    ],
                },
                "parking": {
                    "type": [
                        "string",
                        "null",
                    ],
                },
                "parking_source": {
                    "type": [
                        "string",
                        "null",
                    ],
                    "enum": [
                        "tour_api",
                        "official_homepage",
                        "naver_local",
                        "naver_blog_consensus",
                        None,
                    ],
                },
                "sources_used": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "tour_api",
                            "official_homepage",
                            "naver_local",
                            "naver_blog_consensus",
                        ],
                    },
                },
            },
            "required": [
                "overview",
                "overview_source",
                "fee_text",
                "fee_source",
                "operating_hours",
                "operating_hours_source",
                "rest_date",
                "rest_date_source",
                "parking",
                "parking_source",
                "sources_used",
            ],
            "additionalProperties": False,
        }

        source_text = "\n\n".join(
            (
                f"[SOURCE {index + 1}]\n"
                f"type={source['type']}\n"
                f"field={source.get('field', '')}\n"
                f"query={source.get('query', '')}\n"
                f"url={source.get('url', '')}\n"
                f"text={source.get('text', '')[:16000]}"
            )
            for index, source
            in enumerate(sources)
        )

        current = {
            "overview": place.overview,
            "fee_text": place.fee_text,
            "operating_hours": place.operating_hours,
            "rest_date": place.rest_date,
            "parking": place.parking,
        }

        system_prompt = (
            "너는 경주 관광정보 검증 및 구조화 모듈이다. "
            "반드시 제공된 CURRENT VALUES와 SOURCE에 명시된 사실만 사용한다. "
            "상식, 기억, 추측, 일반적인 관광 상식을 절대 추가하지 않는다. "

            "출처 우선순위는 tour_api/official_homepage가 가장 높고, "
            "그 다음 naver_local, 마지막이 naver_blog이다. "

            "CURRENT VALUES에 이미 값이 있는 필드는 그대로 유지한다. "

            "overview 규칙: "
            "장소의 역사, 특징, 시설, 문화재 성격, 볼거리 등 "
            "'장소 자체를 설명하는 정보'가 실제 SOURCE에 있을 때만 작성한다. "
            "운영시간, 휴무일, 주차 가능 여부, 주소만 가지고 overview를 만들면 안 된다. "
            "즉 '상시 개방이며 주차 가능합니다' 같은 운영정보 재서술만으로 "
            "overview를 채우지 않는다. 설명 근거가 없으면 null이다. "

            "fee_text 규칙: "
            "입장료/관람료/이용요금/무료 여부가 SOURCE에 명시되어 있을 때만 채운다. "
            "요금 정보가 없다는 이유로 무료라고 추정하지 않는다. "
            "official_homepage 또는 tour_api 한 곳에서 명확히 확인되면 사용 가능하다. "
            "naver_blog만 근거인 경우에는 서로 다른 URL의 검색결과 2개 이상이 "
            "동일한 요금 또는 동일한 무료/유료 사실을 명시할 때만 채우고 "
            "fee_source를 naver_blog_consensus로 한다. "

            "operating_hours/rest_date/parking도 동일하다. "
            "공식 출처는 한 곳의 명확한 정보로 사용할 수 있지만, "
            "블로그만 근거인 경우 서로 다른 URL 2개 이상의 일치가 필요하다. "

            "NAVER 지역검색의 description은 overview 보조자료로 사용할 수 있지만, "
            "그 description에 직접 적혀 있지 않은 요금이나 운영시간을 추정해서는 안 된다. "

            "overview는 근거가 충분할 때만 1~3문장의 자연스러운 한국어로 요약하되, "
            "원문에 없는 사실은 추가하지 않는다. "
            "근거가 부족하거나 서로 충돌하면 해당 필드는 null로 둔다."
        )

        body = {
            "model": self.settings.openai_model,
            "input": [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": (
                        f"장소명: {place.title}\n"
                        f"주소: {place.address or ''}\n"
                        f"CURRENT VALUES: "
                        f"{json.dumps(current, ensure_ascii=False)}\n\n"
                        f"SOURCES:\n{source_text}"
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "place_information_enrichment",
                    "strict": True,
                    "schema": schema,
                }
            },
        }

        payload = await self.openai._post(
            "openai",
            f"{self.settings.openai_base_url}/responses",
            json_body=body,
            headers=self.openai.headers,
        )

        return json.loads(
            _extract_openai_text(
                payload
            )
        )

    @staticmethod
    def _apply_extracted(
        place: Place,
        extracted: dict[str, Any],
    ) -> None:
        if not extracted:
            return

        field_source_pairs = (
            (
                "overview",
                "overview_source",
            ),
            (
                "fee_text",
                "fee_source",
            ),
            (
                "operating_hours",
                "operating_hours_source",
            ),
            (
                "rest_date",
                "rest_date_source",
            ),
            (
                "parking",
                "parking_source",
            ),
        )

        for (
            field,
            source_field,
        ) in field_source_pairs:
            current_value = getattr(
                place,
                field,
            )

            # TourAPI 기존값을 다른 출처로 덮어쓰지 않습니다.
            if current_value:
                if not getattr(
                    place,
                    source_field,
                    None,
                ):
                    setattr(
                        place,
                        source_field,
                        "tour_api",
                    )

                continue

            value = _compact(
                extracted.get(field)
            )

            source = extracted.get(
                source_field
            )

            # 값은 있는데 출처가 없으면 버립니다.
            if value and source:
                setattr(
                    place,
                    field,
                    value,
                )

                setattr(
                    place,
                    source_field,
                    source,
                )

        if place.fee_text:
            place.is_free = _to_bool_free(
                place.fee_text
            )

        used = [
            str(item)
            for item in (
                extracted.get(
                    "sources_used"
                )
                or []
            )
            if item
        ]

        place.info_sources = list(
            dict.fromkeys(
                [
                    *place.info_sources,
                    *used,
                ]
            )
        )

    @staticmethod
    def _recalculate_confidence(
        place: Place,
    ) -> None:
        """
        핵심 정보의 존재 여부와 출처를 함께 보고 신뢰도를 계산합니다.

        high    = 공식 API/공식 홈페이지 중심
        medium  = 공식 정보 + 보조 검색 혼합
        low     = 블로그 복수 합의 중심
        unknown = 확인 가능한 핵심 근거 부족
        """
        core_pairs = (
            (
                place.overview,
                place.overview_source,
            ),
            (
                place.fee_text,
                place.fee_source,
            ),
            (
                place.operating_hours,
                place.operating_hours_source,
            ),
        )

        filled = [
            (
                value,
                source,
            )
            for value, source
            in core_pairs
            if value
        ]

        if not filled:
            place.info_confidence = "unknown"
            place.info_confidence_label = _confidence_label(
                place.info_confidence
            )
            return

        ranks = [
            _source_rank(source)
            for _, source
            in filled
        ]

        if (
            len(filled) >= 2
            and all(
                rank == 0
                for rank in ranks
            )
        ):
            place.info_confidence = "high"

        elif any(
            rank <= 1
            for rank in ranks
        ):
            place.info_confidence = "medium"

        elif any(
            rank == 2
            for rank in ranks
        ):
            place.info_confidence = "low"

        else:
            place.info_confidence = "unknown"

        place.info_confidence_label = _confidence_label(
            place.info_confidence
        )
