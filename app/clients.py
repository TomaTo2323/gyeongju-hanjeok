from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

import httpx

from .config import Settings
from .geo import latest_ultra_short_base, latlon_to_kma_grid
from .schemas import ChatTurn, ContentItem, Place, TransportMode


class IntegrationError(RuntimeError):
    def __init__(self, service: str, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.service = service
        self.status_code = status_code


def _items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        item = payload["response"]["body"]["items"]["item"]
    except (KeyError, TypeError):
        return []
    if isinstance(item, list):
        return item
    if isinstance(item, dict):
        return [item]
    return []


def _clean_html(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"<[^>]+>", " ", html.unescape(str(value)))
    return re.sub(r"\s+", " ", text).strip() or None


def _detail_info_value(
    items: list[dict[str, Any]],
    keywords: tuple[str, ...],
) -> str | None:
    for item in items:
        name = _clean_html(
            item.get("infoname")
        ) or ""

        text = _clean_html(
            item.get("infotext")
        )

        if (
            text
            and any(
                keyword in name
                for keyword in keywords
            )
        ):
            return text

    return None


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _extract_break_time(value: Any) -> str | None:
    if not value:
        return None

    text = _clean_html(value) or ""

    match = re.search(
        r"(?:브레이크\s*타임|break\s*time|쉬는\s*시간)"
        r"\s*[:：]?\s*"
        r"(\d{1,2}:\d{2})"
        r"\s*(?:~|-|–|—|부터)\s*"
        r"(\d{1,2}:\d{2})",
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    return f"{match.group(1)}~{match.group(2)}"


def _menu_segments(value: Any) -> list[str]:
    if not value:
        return []

    raw = html.unescape(str(value))
    raw = re.sub(
        r"(?is)<br\s*/?>|</(?:p|div|li)>",
        "\n",
        raw,
    )
    raw = re.sub(r"(?is)<[^>]+>", " ", raw)

    lines = [
        re.sub(r"\s+", " ", item).strip(" -·,;/")
        for item in re.split(r"[\n;]+", raw)
    ]
    lines = [item for item in lines if item]

    # 가격이 없는 단순 취급메뉴 문자열은 쉼표/슬래시 단위로 나눕니다.
    if len(lines) == 1 and "원" not in lines[0]:
        parts = [
            item.strip(" -·,;/")
            for item in re.split(r"[,/]+", lines[0])
        ]
        parts = [item for item in parts if item]
        if len(parts) > 1:
            lines = parts

    return lines


def _menu_item_from_segment(
    segment: str,
    *,
    representative: bool,
) -> dict[str, Any] | None:
    text = re.sub(r"\s+", " ", segment).strip()
    if not text:
        return None

    price_match = re.search(
        r"(\d{1,3}(?:,\d{3})+|\d{4,6})\s*원",
        text,
    )

    price = ""
    name = text

    if price_match:
        price = f"{price_match.group(1)}원"
        name = (
            text[:price_match.start()]
            .strip(" -·,;/():")
        )

    # 이름이 비어 버리면 원문을 그대로 사용합니다.
    if not name:
        name = text

    return {
        "name": name,
        "price": price,
        "source": "tour_api",
        "representative": representative,
    }


def _menu_items_from_intro(
    intro: dict[str, Any],
) -> tuple[str | None, list[dict[str, Any]]]:
    first_menu = _clean_html(
        intro.get("firstmenu")
    )

    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for value, representative in (
        (intro.get("firstmenu"), True),
        (intro.get("treatmenu"), False),
    ):
        for segment in _menu_segments(value):
            item = _menu_item_from_segment(
                segment,
                representative=representative,
            )
            if item is None:
                continue

            key = re.sub(
                r"[^0-9a-z가-힣]",
                "",
                str(item["name"]).lower(),
            )
            if not key or key in seen:
                continue

            seen.add(key)
            items.append(item)

            if len(items) >= 8:
                break

        if len(items) >= 8:
            break

    return first_menu, items


def _to_bool_free(text: str | None) -> bool | None:
    if not text:
        return None
    compact = re.sub(r"\s+", "", text).lower()
    if any(token in compact for token in ("무료", "없음", "0원", "free")):
        return True
    if any(token in compact for token in ("유료", "원", "입장료")):
        return False
    return None


CONTENT_TYPE_CATEGORY = {
    "12": "관광지",
    "14": "문화시설",
    "15": "축제·공연",
    "25": "여행코스",
    "28": "레포츠",
    "32": "숙박",
    "38": "쇼핑",
    "39": "음식점",
}


class BaseClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.timeout = httpx.Timeout(settings.http_timeout_seconds)

    async def _get(self, service: str, url: str, *, params: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                response = await client.get(url, params=params, headers=headers)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500]
            raise IntegrationError(service, f"HTTP {exc.response.status_code}: {body}", status_code=exc.response.status_code) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise IntegrationError(service, str(exc)) from exc

    async def _post(
        self,
        service: str,
        url: str,
        *,
        json_body: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        try:
            timeout = (
                httpx.Timeout(timeout_seconds)
                if timeout_seconds is not None
                else self.timeout
            )
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
            ) as client:
                response = await client.post(
                    url,
                    json=json_body,
                    headers=headers,
                )
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:500]
            raise IntegrationError(
                service,
                f"HTTP {exc.response.status_code}: {body}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.TimeoutException as exc:
            raise IntegrationError(
                service,
                f"요청 시간이 초과되었습니다. ({timeout_seconds or self.settings.http_timeout_seconds}초)",
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise IntegrationError(service, str(exc)) from exc


class TourApiClient(BaseClient):
    def _auth_params(self) -> dict[str, Any]:
        if not self.settings.public_data_service_key:
            raise IntegrationError("tour_api", "PUBLIC_DATA_SERVICE_KEY가 설정되지 않았습니다.", status_code=503)
        # data.go.kr가 URL-encoded/decoded 키를 모두 안내하므로 중복 인코딩 방지를 위해 decode 후 httpx에 전달합니다.
        return {
            "serviceKey": unquote(self.settings.public_data_service_key),
            "MobileOS": self.settings.mobile_os,
            "MobileApp": self.settings.mobile_app,
            "_type": "json",
        }

    async def nearby_places(self, latitude: float, longitude: float, radius_m: int, limit: int) -> list[Place]:
        params = self._auth_params() | {
            "mapX": longitude,
            "mapY": latitude,
            "radius": min(radius_m, 20000),
            "arrange": "E",
            "numOfRows": min(limit, 100),
            "pageNo": 1,
        }
        payload = await self._get("tour_api", f"{self.settings.tour_api_base_url}/locationBasedList2", params=params)
        places = [self._place_from_summary(item) for item in _items(payload)]
        return [p for p in places if p is not None][:limit]


    async def nearby_food_places(
        self,
        latitude: float,
        longitude: float,
        radius_m: int,
        limit: int = 30,
    ) -> list[Place]:
        """코스 삽입용 주변 음식점(contentTypeId=39) 후보."""
        params = self._auth_params() | {
            "mapX": longitude,
            "mapY": latitude,
            "radius": min(radius_m, 20000),
            "contentTypeId": "39",
            "arrange": "E",
            "numOfRows": min(limit, 100),
            "pageNo": 1,
        }
        payload = await self._get(
            "tour_api",
            f"{self.settings.tour_api_base_url}/locationBasedList2",
            params=params,
        )
        places = [
            self._place_from_summary(item)
            for item in _items(payload)
        ]
        return [
            place
            for place in places
            if place is not None
        ][:limit]

    async def gyeongju_places(
        self,
        limit: int = 1000,
    ) -> list[Place]:
        """
        경주시 관광지 전체 후보를 수집합니다.

        KorService2의 기존 지역코드(areaCode/sigunguCode)와
        법정동 코드(lDongRegnCd/lDongSignguCd)를 모두 조회한 뒤
        contentid(place_id)를 기준으로 중복을 제거합니다.
        """
        wanted = max(
            1,
            min(
                int(limit),
                1000,
            ),
        )
        rows = 100

        async def fetch_group(
            region_params: dict[str, str],
        ) -> list[dict[str, Any]]:
            async def page(
                page_no: int,
            ) -> dict[str, Any]:
                params = self._auth_params() | region_params | {
                    "arrange": "C",
                    "numOfRows": rows,
                    "pageNo": page_no,
                }

                return await self._get(
                    "tour_api",
                    (
                        f"{self.settings.tour_api_base_url}"
                        "/areaBasedList2"
                    ),
                    params=params,
                )

            first = await page(1)
            first_items = _items(first)

            try:
                total_count = int(
                    first["response"]["body"].get(
                        "totalCount",
                        len(first_items),
                    )
                )
            except (
                KeyError,
                TypeError,
                ValueError,
            ):
                total_count = len(first_items)

            max_pages = max(
                1,
                (total_count + rows - 1) // rows
                if total_count > 0
                else 1,
            )

            payloads = [first]

            if max_pages > 1:
                payloads.extend(
                    await asyncio.gather(
                        *(
                            page(page_no)
                            for page_no in range(
                                2,
                                max_pages + 1,
                            )
                        )
                    )
                )

            items: list[dict[str, Any]] = []

            for payload in payloads:
                items.extend(_items(payload))

            return items

        legacy_items, ldong_items = await asyncio.gather(
            fetch_group(
                {
                    "areaCode": str(
                        self.settings.tour_area_code
                    ),
                    "sigunguCode": str(
                        self.settings.tour_sigungu_code
                    ),
                }
            ),
            fetch_group(
                {
                    "lDongRegnCd": "47",
                    "lDongSignguCd": "130",
                }
            ),
        )

        places: list[Place] = []
        seen: set[str] = set()

        for item in legacy_items + ldong_items:
            place = self._place_from_summary(item)

            if (
                place is None
                or place.place_id in seen
            ):
                continue

            seen.add(place.place_id)
            places.append(place)

            if len(places) >= wanted:
                break

        return places

    async def keyword_search(self, keyword: str, limit: int = 20) -> list[Place]:
        params = self._auth_params() | {
            "keyword": keyword,
            "areaCode": self.settings.tour_area_code,
            "sigunguCode": self.settings.tour_sigungu_code,
            "arrange": "C",
            "numOfRows": limit,
            "pageNo": 1,
        }
        payload = await self._get("tour_api", f"{self.settings.tour_api_base_url}/searchKeyword2", params=params)
        places = [self._place_from_summary(item) for item in _items(payload)]
        return [p for p in places if p is not None]


    async def keyword_search_global(
        self,
        keyword: str,
        limit: int = 100,
    ) -> list[Place]:
        """
        디버그/보조 조회용 전국 키워드 검색.

        일부 관광지가 areaCode/sigunguCode 필터가 걸린 searchKeyword2에서
        누락되는 경우를 대비한 fallback입니다.

        일반 추천 로직은 기존 keyword_search()를 그대로 사용합니다.
        """
        params = self._auth_params() | {
            "keyword": keyword,
            "arrange": "C",
            "numOfRows": min(
                max(1, limit),
                100,
            ),
            "pageNo": 1,
        }

        payload = await self._get(
            "tour_api",
            f"{self.settings.tour_api_base_url}/searchKeyword2",
            params=params,
        )

        places = [
            self._place_from_summary(item)
            for item in _items(payload)
        ]

        return [
            place
            for place in places
            if place is not None
        ]

    async def detail(
        self,
        place: Place,
    ) -> Place:
        """
        장소 상세정보를 contentTypeId에 안전하게 맞춰 조회합니다.

        1. detailCommon2를 contentTypeId 없이 먼저 조회
        2. common 응답의 실제 contenttypeid를 채택
        3. 실제 타입으로 detailIntro2 + detailInfo2 조회

        이렇게 해야 홈/지도/저장 장소에서 contentTypeId가 비어 있거나
        오래된 값이어도 관광지/문화시설/음식점별 필드를 정상적으로 받습니다.
        """
        common_params = self._auth_params() | {
            "contentId": place.place_id,
            "defaultYN": "Y",
            "firstImageYN": "Y",
            "areacodeYN": "Y",
            "catcodeYN": "Y",
            "addrinfoYN": "Y",
            "mapinfoYN": "Y",
            "overviewYN": "Y",
            "numOfRows": 10,
            "pageNo": 1,
        }

        common: dict[str, Any] = {}

        try:
            common_res = await self._get(
                "tour_api",
                (
                    f"{self.settings.tour_api_base_url}"
                    "/detailCommon2"
                ),
                params=common_params,
            )

            common_items = _items(
                common_res
            )

            if common_items:
                common = common_items[0]

        except IntegrationError:
            common = {}

        # contentId는 맞는데 common이 비어 있는 경우,
        # 제목으로 TourAPI 검색을 한 번 더 하여 공식 contentId/type을 복구합니다.
        resolved_place = place

        if (
            not common
            and place.title
            and not place.title.isdigit()
        ):
            try:
                searched = await self.keyword_search_global(
                    place.title,
                    limit=20,
                )

                normalized_title = re.sub(
                    r"[^0-9a-z가-힣]",
                    "",
                    place.title.lower(),
                )

                exact = next(
                    (
                        item
                        for item in searched
                        if re.sub(
                            r"[^0-9a-z가-힣]",
                            "",
                            item.title.lower(),
                        )
                        == normalized_title
                    ),
                    None,
                )

                if exact is not None:
                    resolved_place = exact

                    retry_params = (
                        self._auth_params()
                        | {
                            "contentId":
                                exact.place_id,
                            "defaultYN": "Y",
                            "firstImageYN": "Y",
                            "areacodeYN": "Y",
                            "catcodeYN": "Y",
                            "addrinfoYN": "Y",
                            "mapinfoYN": "Y",
                            "overviewYN": "Y",
                            "numOfRows": 10,
                            "pageNo": 1,
                        }
                    )

                    retry_res = await self._get(
                        "tour_api",
                        (
                            f"{self.settings.tour_api_base_url}"
                            "/detailCommon2"
                        ),
                        params=retry_params,
                    )

                    retry_items = _items(
                        retry_res
                    )

                    if retry_items:
                        common = retry_items[0]

            except IntegrationError:
                pass

        resolved_content_id = str(
            common.get("contentid")
            or resolved_place.place_id
            or place.place_id
        )

        resolved_content_type = str(
            common.get("contenttypeid")
            or resolved_place.content_type_id
            or place.content_type_id
            or ""
        ).strip()

        intro: dict[str, Any] = {}
        info_items: list[dict[str, Any]] = []

        if resolved_content_type:
            intro_params = (
                self._auth_params()
                | {
                    "contentId":
                        resolved_content_id,
                    "contentTypeId":
                        resolved_content_type,
                    "numOfRows": 10,
                    "pageNo": 1,
                }
            )

            info_params = (
                self._auth_params()
                | {
                    "contentId":
                        resolved_content_id,
                    "contentTypeId":
                        resolved_content_type,
                    "numOfRows": 100,
                    "pageNo": 1,
                }
            )

            intro_task = self._get(
                "tour_api",
                (
                    f"{self.settings.tour_api_base_url}"
                    "/detailIntro2"
                ),
                params=intro_params,
            )

            info_task = self._get(
                "tour_api",
                (
                    f"{self.settings.tour_api_base_url}"
                    "/detailInfo2"
                ),
                params=info_params,
            )

            intro_res, info_res = await asyncio.gather(
                intro_task,
                info_task,
                return_exceptions=True,
            )

            if isinstance(
                intro_res,
                dict,
            ):
                items = _items(
                    intro_res
                )

                if items:
                    intro = items[0]

            if isinstance(
                info_res,
                dict,
            ):
                info_items = _items(
                    info_res
                )

        # detailIntro2의 contentType별 필드 + detailInfo2 반복정보를 합칩니다.
        operating_hours = (
            _first_value(
                intro,
                [
                    "usetime",
                    "usetimeculture",
                    "usetimefestival",
                    "usetimeleports",
                    "opentime",
                    "openperiod",
                    "opentimefood",
                    "usetimefood",
                ],
            )
            or _detail_info_value(
                info_items,
                (
                    "운영시간",
                    "이용시간",
                    "관람시간",
                    "개방시간",
                    "영업시간",
                ),
            )
        )

        rest_date = (
            _first_value(
                intro,
                [
                    "restdate",
                    "restdateculture",
                    "restdateleports",
                    "restdatefood",
                    "restdatefestival",
                ],
            )
            or _detail_info_value(
                info_items,
                (
                    "휴무",
                    "휴관",
                    "쉬는 날",
                    "쉬는날",
                ),
            )
        )

        fee_text = (
            _first_value(
                intro,
                [
                    "usefee",
                    "usefeeleports",
                    "parkingfee",
                ],
            )
            or _detail_info_value(
                info_items,
                (
                    "입장료",
                    "관람료",
                    "이용요금",
                    "요금",
                ),
            )
        )

        parking = (
            _first_value(
                intro,
                [
                    "parking",
                    "parkingculture",
                    "parkingleports",
                    "parkingfood",
                ],
            )
            or _detail_info_value(
                info_items,
                (
                    "주차",
                    "주차장",
                ),
            )
        )

        tel = (
            common.get("tel")
            or _first_value(
                intro,
                [
                    "infocenter",
                    "infocenterculture",
                    "infocenterleports",
                    "infocenterfood",
                    "sponsor1tel",
                ],
            )
            or resolved_place.tel
            or place.tel
        )

        merged = place.model_dump()

        merged.update(
            {
                "place_id":
                    resolved_content_id,
                "content_type_id":
                    resolved_content_type
                    or place.content_type_id,
                "title":
                    common.get("title")
                    or resolved_place.title
                    or place.title,
                "category":
                    CONTENT_TYPE_CATEGORY.get(
                        resolved_content_type,
                        resolved_place.category
                        or place.category,
                    ),
                "address":
                    " ".join(
                        filter(
                            None,
                            [
                                common.get("addr1"),
                                common.get("addr2"),
                            ],
                        )
                    )
                    or resolved_place.address
                    or place.address,
                "image_url":
                    common.get("firstimage")
                    or resolved_place.image_url
                    or place.image_url,
                "thumbnail_url":
                    common.get("firstimage2")
                    or resolved_place.thumbnail_url
                    or place.thumbnail_url,
                "overview":
                    _clean_html(
                        common.get("overview")
                    )
                    or resolved_place.overview
                    or place.overview,
                "latitude":
                    (
                        _to_float(
                            common.get("mapy")
                        )
                        if _to_float(
                            common.get("mapy")
                        )
                        is not None
                        else (
                            resolved_place.latitude
                            or place.latitude
                        )
                    ),
                "longitude":
                    (
                        _to_float(
                            common.get("mapx")
                        )
                        if _to_float(
                            common.get("mapx")
                        )
                        is not None
                        else (
                            resolved_place.longitude
                            or place.longitude
                        )
                    ),
                "tel": tel,
                # homepage 원문은 raw.common에도 남겨 Enricher가 href를 복구합니다.
                "homepage":
                    _clean_html(
                        common.get("homepage")
                    )
                    or resolved_place.homepage
                    or place.homepage,
                "operating_hours":
                    operating_hours,
                "rest_date":
                    rest_date,
                "fee_text":
                    fee_text,
                "parking":
                    parking,
                "raw": {
                    "summary":
                        resolved_place.raw
                        or place.raw,
                    "common":
                        common,
                    "intro":
                        intro,
                    "info":
                        info_items,
                },
            }
        )

        representative_menu, menu_items = (
            _menu_items_from_intro(
                intro
            )
        )

        merged["representative_menu"] = (
            representative_menu
            or place.representative_menu
        )

        merged["menu_items"] = (
            menu_items
            or place.menu_items
        )

        merged["break_time"] = (
            _first_value(
                intro,
                [
                    "breaktime",
                    "breaktimefood",
                    "break_time",
                ],
            )
            or _extract_break_time(
                operating_hours
            )
            or place.break_time
        )

        merged["is_free"] = _to_bool_free(
            fee_text
        )

        merged["is_night_spot"] = any(
            keyword in (
                merged.get("title")
                or ""
            )
            for keyword in (
                "야경",
                "월지",
                "월정교",
                "첨성대",
                "보문",
            )
        )

        merged["is_rest_point"] = (
            merged.get("category")
            == "음식점"
            or any(
                keyword in (
                    merged.get("title")
                    or ""
                )
                for keyword in (
                    "카페",
                    "쉼터",
                    "휴게",
                )
            )
        )

        # 로컬 개발 중 API 연결상태를 바로 확인할 수 있는 서버 로그.
        filled = [
            name
            for name, value in {
                "overview":
                    merged.get("overview"),
                "hours":
                    merged.get(
                        "operating_hours"
                    ),
                "rest":
                    merged.get("rest_date"),
                "fee":
                    merged.get("fee_text"),
                "parking":
                    merged.get("parking"),
                "tel":
                    merged.get("tel"),
                "homepage":
                    merged.get("homepage"),
            }.items()
            if value
        ]

        print(
            "[TOUR DETAIL]",
            f"id={resolved_content_id}",
            f"type={resolved_content_type or '-'}",
            f"common={bool(common)}",
            f"intro={bool(intro)}",
            f"info={len(info_items)}",
            f"filled={','.join(filled) or '-'}",
        )

        return Place.model_validate(
            merged
        )

    async def images(self, content_id: str, limit: int = 10) -> list[str]:
        params = self._auth_params() | {
            "contentId": content_id,
            "imageYN": "Y",
            "subImageYN": "Y",
            "numOfRows": limit,
            "pageNo": 1,
        }
        payload = await self._get("tour_api", f"{self.settings.tour_api_base_url}/detailImage2", params=params)
        return [item.get("originimgurl") for item in _items(payload) if item.get("originimgurl")]

    def _place_from_summary(self, item: dict[str, Any]) -> Place | None:
        lat = _to_float(item.get("mapy"))
        lon = _to_float(item.get("mapx"))
        content_id = item.get("contentid")
        title = _clean_html(item.get("title"))
        if lat is None or lon is None or not content_id or not title:
            return None
        content_type = str(item.get("contenttypeid") or "") or None
        return Place(
            place_id=str(content_id),
            content_type_id=content_type,
            title=title,
            category=CONTENT_TYPE_CATEGORY.get(content_type or "", "기타"),
            address=" ".join(filter(None, [item.get("addr1"), item.get("addr2")])) or None,
            latitude=lat,
            longitude=lon,
            image_url=item.get("firstimage") or None,
            thumbnail_url=item.get("firstimage2") or None,
            tel=item.get("tel") or None,
            raw=item,
        )


class GyeongjuOfficialTourClient(BaseClient):
    """경주시 공식 문화관광 페이지에서 방문정보를 보완합니다.

    TourAPI에 운영시간/휴무/요금/주차 같은 값이 비어 있을 때만 사용합니다.
    NAVER 웹문서 검색은 *공식 페이지 URL을 찾는 용도*로만 사용하고,
    실제 값은 gyeongju.go.kr의 공식 페이지 본문에서 다시 읽습니다.
    """

    OFFICIAL_HOSTS = {
        "gyeongju.go.kr",
        "www.gyeongju.go.kr",
        "search.gyeongju.go.kr",
    }

    FIELD_LABELS: dict[str, tuple[str, ...]] = {
        "operating_hours": (
            "관람시간", "운영시간", "이용시간", "개방시간", "영업시간",
        ),
        "rest_date": (
            "휴무일", "휴관일", "휴무", "휴관", "정기휴일",
        ),
        "fee_text": (
            "관람료", "입장료", "이용료", "요금",
        ),
        "parking": (
            "주차정보", "주차 안내", "주차안내", "주차",
        ),
        "tel": (
            "전화", "문의전화", "문의처", "문의",
        ),
        "address": (
            "주소", "위치",
        ),
    }

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.naver = NaverClient(settings)

    async def _get_text(self, url: str) -> str:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout,
                follow_redirects=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (compatible; GyeongjuHanjeok/1.0; "
                        "+https://www.gyeongju.go.kr/tour/)"
                    )
                },
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                return response.text
        except httpx.HTTPStatusError as exc:
            raise IntegrationError(
                "gyeongju_official",
                f"HTTP {exc.response.status_code}: {exc.response.text[:300]}",
                status_code=exc.response.status_code,
            ) from exc
        except httpx.HTTPError as exc:
            raise IntegrationError("gyeongju_official", str(exc)) from exc

    @classmethod
    def _is_official_url(cls, url: str) -> bool:
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
        return (
            parsed.scheme in {"http", "https"}
            and host in cls.OFFICIAL_HOSTS
            and "/tour" in path
            and "/tour_bak" not in path
        )

    @staticmethod
    def _aliases(title: str) -> list[str]:
        compact = re.sub(r"[^0-9a-z가-힣]", "", title.lower())
        aliases = [compact] if compact else []
        gyeongju = re.sub(r"[^0-9a-z가-힣]", "", "경주")
        if compact.startswith(gyeongju) and len(compact) > len(gyeongju) + 1:
            aliases.append(compact[len(gyeongju):])
        return list(dict.fromkeys(alias for alias in aliases if len(alias) >= 2))

    @staticmethod
    def _html_lines(raw_html: str) -> list[str]:
        text = re.sub(
            r"(?is)<(?:script|style|noscript)[^>]*>.*?</(?:script|style|noscript)>",
            " ",
            raw_html,
        )
        text = re.sub(
            r"(?is)<br\s*/?>|</(?:p|div|li|tr|td|th|dd|dt|section|article|h[1-6])>",
            "\n",
            text,
        )
        text = re.sub(r"(?is)<[^>]+>", " ", text)
        text = html.unescape(text).replace("\xa0", " ")
        lines: list[str] = []
        for raw_line in text.splitlines():
            line = re.sub(r"\s+", " ", raw_line).strip(" \t:-·|/")
            if line:
                lines.append(line)
        return lines

    @classmethod
    def _extract_labeled_value(
        cls,
        lines: list[str],
        labels: tuple[str, ...],
    ) -> str | None:
        for index, line in enumerate(lines):
            for label in labels:
                pos = line.find(label)
                if pos < 0:
                    continue

                value = line[pos + len(label):].strip(" \t:：-·|")
                if not value and index + 1 < len(lines):
                    value = lines[index + 1].strip()

                # 메뉴/내비게이션에 잡힌 한 단어 라벨은 값으로 쓰지 않습니다.
                if not value or value == label:
                    continue

                value = re.sub(r"\s+", " ", value).strip()
                if 1 <= len(value) <= 500:
                    return value
        return None

    @classmethod
    def _parse_fields(
        cls,
        raw_html: str,
        requested_fields: set[str],
    ) -> dict[str, str]:
        lines = cls._html_lines(raw_html)
        result: dict[str, str] = {}

        for field_name in requested_fields:
            labels = cls.FIELD_LABELS.get(field_name)
            if not labels:
                continue
            value = cls._extract_labeled_value(lines, labels)
            if value:
                result[field_name] = value

        # 경주문화관광은 "관람시간 ... 연중무휴"처럼 휴무정보를 같은 줄에
        # 함께 표기하는 경우가 많습니다.
        if "rest_date" in requested_fields and "rest_date" not in result:
            hours = result.get("operating_hours")
            if hours and "연중무휴" in hours:
                result["rest_date"] = "연중무휴"

        return result

    async def place_info(
        self,
        title: str,
        requested_fields: set[str],
    ) -> dict[str, Any]:
        if not requested_fields:
            return {}

        aliases = self._aliases(title)
        short_title = title.strip()
        if short_title.startswith("경주 "):
            short_title = short_title[3:].strip()

        queries = [
            f"경주문화관광 {short_title}",
            f"{short_title} 관람시간 경주문화관광",
        ]

        candidates: dict[str, dict[str, Any]] = {}
        for query in queries:
            try:
                documents = await self.naver.web_documents(query, limit=10)
            except IntegrationError:
                continue

            for document in documents:
                url = str(document.get("url") or "").strip()
                if not self._is_official_url(url):
                    continue

                searchable = re.sub(
                    r"[^0-9a-z가-힣]",
                    "",
                    f"{document.get('title', '')} {document.get('description', '')}".lower(),
                )
                alias_match = max(
                    (len(alias) for alias in aliases if alias in searchable),
                    default=0,
                )
                score = alias_match * 10
                if "www.gyeongju.go.kr/tour/" in url:
                    score += 5
                if "page.do" in url:
                    score += 2

                previous = candidates.get(url)
                if previous is None or score > previous["score"]:
                    candidates[url] = {**document, "score": score}

        ranked = sorted(
            candidates.values(),
            key=lambda item: item["score"],
            reverse=True,
        )[:5]

        best: dict[str, Any] = {}
        best_score = -1
        for candidate in ranked:
            url = str(candidate.get("url") or "")
            try:
                raw_html = await asyncio.wait_for(self._get_text(url), timeout=4.0)
            except (IntegrationError, asyncio.TimeoutError):
                continue

            compact_page = re.sub(
                r"[^0-9a-z가-힣]",
                "",
                html.unescape(re.sub(r"(?is)<[^>]+>", " ", raw_html)).lower(),
            )
            if aliases and not any(alias in compact_page for alias in aliases):
                continue

            fields = self._parse_fields(raw_html, requested_fields)
            if not fields:
                continue

            score = len(fields) * 100 + int(candidate.get("score") or 0)
            if score > best_score:
                best_score = score
                best = {
                    **fields,
                    "source_url": url,
                    "source_name": "경주시 경주문화관광",
                }

        return best


class RegionalVisitorClient(BaseClient):
    """
    한국관광공사 DataLabService - 기초 지자체 지역방문자수.

    경주시의 '외지인 + 외국인' 방문수요를 가져와
    최근 동일 요일 4주 평균과 비교한 지역 수요 점수(0~100)를 만듭니다.

    - score 50: 평소와 비슷
    - score > 50: 평소보다 경주 방문수요 증가
    - score < 50: 평소보다 경주 방문수요 감소

    DataLab 원천 데이터는 최신 일자가 오늘과 며칠~수주 차이 날 수 있으므로
    최대 120일 범위에서 가장 최근 확보 가능한 날짜를 자동 탐색합니다.

    캐시는 클래스 단위로 공유해 /places 요청마다 Client가 새로 생성되더라도
    동일 날짜의 DataLab API를 반복 호출하지 않도록 합니다.
    """

    BASE_URL = "https://apis.data.go.kr/B551011/DataLabService"

    _shared_daily_cache: dict[
        tuple[str, str],
        dict[str, Any] | None,
    ] = {}

    _shared_demand_cache: dict[
        tuple[str, str],
        dict[str, Any],
    ] = {}

    def __init__(self, settings: Settings):
        super().__init__(settings)

    async def _gyeongju_daily(
        self,
        target_date: date,
    ) -> dict[str, Any] | None:
        date_key = target_date.strftime("%Y%m%d")
        area_key = str(
            self.settings.admin_sigungu_code
        ).strip()
        cache_key = (
            area_key,
            date_key,
        )

        if cache_key in self._shared_daily_cache:
            return self._shared_daily_cache[cache_key]

        if not self.settings.public_data_service_key:
            raise IntegrationError(
                "regional_visitor_api",
                "PUBLIC_DATA_SERVICE_KEY가 설정되지 않았습니다.",
                status_code=503,
            )

        params = {
            "serviceKey": unquote(
                self.settings.public_data_service_key
            ),
            "pageNo": 1,
            # 하루 약 740건이므로 한 페이지에 모두 받습니다.
            "numOfRows": 1000,
            "MobileOS": self.settings.mobile_os,
            "MobileApp": self.settings.mobile_app,
            "_type": "json",
            "startYmd": date_key,
            "endYmd": date_key,
        }

        payload = await self._get(
            "regional_visitor_api",
            f"{self.BASE_URL}/locgoRegnVisitrDDList",
            params=params,
        )

        rows = [
            row
            for row in _items(payload)
            if str(row.get("signguCode") or "").strip()
            == str(self.settings.admin_sigungu_code).strip()
        ]

        if not rows:
            self._shared_daily_cache[cache_key] = None
            return None

        visitors = {
            "local": 0.0,
            "outside": 0.0,
            "foreign": 0.0,
        }

        day_name: str | None = None
        base_ymd: str | None = None

        for row in rows:
            div_code = str(
                row.get("touDivCd") or ""
            ).strip()

            count = _to_float(
                row.get("touNum")
            )

            if count is None:
                continue

            if div_code == "1":
                visitors["local"] += count
            elif div_code == "2":
                visitors["outside"] += count
            elif div_code == "3":
                visitors["foreign"] += count

            if not day_name:
                day_name = _first_value(
                    row,
                    ["daywkDivNm"],
                )

            if not base_ymd:
                base_ymd = _first_value(
                    row,
                    ["baseYmd"],
                )

        # 관광수요 신호에는 일상 이동이 많이 섞일 수 있는 현지인은 제외하고,
        # 외지인 + 외국인을 사용합니다.
        tourism_demand = (
            visitors["outside"]
            + visitors["foreign"]
        )

        if tourism_demand <= 0:
            self._shared_daily_cache[cache_key] = None
            return None

        result = {
            "date": base_ymd or date_key,
            "day_name": day_name,
            "local": round(
                visitors["local"],
                2,
            ),
            "outside": round(
                visitors["outside"],
                2,
            ),
            "foreign": round(
                visitors["foreign"],
                2,
            ),
            "tourism_demand": round(
                tourism_demand,
                2,
            ),
        }

        self._shared_daily_cache[cache_key] = result
        return result

    async def demand_score(
        self,
        reference_date: date | None = None,
    ) -> dict[str, Any]:
        """
        가장 최근 확보 가능한 경주시 방문자 수를
        같은 요일의 이전 4주 평균과 비교합니다.

        최신 데이터 탐색:
        1) 최근 7일은 하루 단위로 확인
        2) 이후 14, 21, 28 ... 최대 119일까지 주 단위 확인
        3) 데이터가 발견되면 직전 6일을 다시 확인해 가장 최신 날짜 확정

        반환 예:
        {
            "score": 63.2,
            "factor": 1.132,
            "current": 152345.0,
            "baseline": 134580.0,
            "base_date": "20260831",
            "data_lag_days": 14,
            "baseline_dates": [...]
        }
        """

        today = reference_date or date.today()
        area_key = str(
            self.settings.admin_sigungu_code
        ).strip()

        demand_cache_key = (
            area_key,
            today.isoformat(),
        )

        cached = self._shared_demand_cache.get(
            demand_cache_key
        )

        if cached is not None:
            return cached

        latest: dict[str, Any] | None = None
        latest_date: date | None = None
        latest_lag: int | None = None

        # -----------------------------------------------------------
        # 1. 최근 7일은 하루 단위 탐색
        # -----------------------------------------------------------
        for lag in range(1, 8):
            candidate_date = (
                today
                - timedelta(days=lag)
            )

            row = await self._gyeongju_daily(
                candidate_date
            )

            if row is not None:
                latest = row
                latest_date = candidate_date
                latest_lag = lag
                break

        # -----------------------------------------------------------
        # 2. 최근 7일에 없으면 주 단위로 최대 120일까지 탐색
        # -----------------------------------------------------------
        if latest is None:
            found_checkpoint: int | None = None

            for lag in range(14, 121, 7):
                candidate_date = (
                    today
                    - timedelta(days=lag)
                )

                row = await self._gyeongju_daily(
                    candidate_date
                )

                if row is not None:
                    latest = row
                    latest_date = candidate_date
                    latest_lag = lag
                    found_checkpoint = lag
                    break

            # 체크포인트에서 데이터를 찾았다면
            # 그보다 최근 6일을 확인해 실제 최신 가용일을 찾습니다.
            if (
                found_checkpoint is not None
                and found_checkpoint > 7
            ):
                lower = max(
                    8,
                    found_checkpoint - 6,
                )

                for lag in range(
                    lower,
                    found_checkpoint,
                ):
                    candidate_date = (
                        today
                        - timedelta(days=lag)
                    )

                    row = await self._gyeongju_daily(
                        candidate_date
                    )

                    if row is not None:
                        latest = row
                        latest_date = candidate_date
                        latest_lag = lag
                        break

        if (
            latest is None
            or latest_date is None
            or latest_lag is None
        ):
            raise IntegrationError(
                "regional_visitor_api",
                (
                    "최근 120일 내 경주시 지역방문자수 데이터를 "
                    "찾지 못했습니다."
                ),
            )

        # -----------------------------------------------------------
        # 3. 최신 가용일과 동일 요일의 이전 4주 평균
        # -----------------------------------------------------------
        baseline_dates = [
            latest_date
            - timedelta(days=7 * week)
            for week in range(1, 5)
        ]

        baseline_rows = await asyncio.gather(
            *(
                self._gyeongju_daily(day)
                for day in baseline_dates
            )
        )

        valid_baselines = [
            row
            for row in baseline_rows
            if row is not None
            and float(
                row.get("tourism_demand") or 0
            ) > 0
        ]

        if not valid_baselines:
            result = {
                "score": 50.0,
                "factor": 1.0,
                "current": latest["tourism_demand"],
                "baseline": None,
                "base_date": latest["date"],
                "data_lag_days": latest_lag,
                "baseline_dates": [],
                "outside": latest["outside"],
                "foreign": latest["foreign"],
            }

            self._shared_demand_cache[
                demand_cache_key
            ] = result

            return result

        baseline = sum(
            float(row["tourism_demand"])
            for row in valid_baselines
        ) / len(valid_baselines)

        current = float(
            latest["tourism_demand"]
        )

        factor = (
            current / baseline
            if baseline > 0
            else 1.0
        )

        # 비정상적인 단일 날짜 값이 전체 혼잡도를 과도하게 흔들지 않도록
        # regional factor는 ±40% 범위로 제한합니다.
        factor = max(
            0.60,
            min(
                1.40,
                factor,
            ),
        )

        # factor 1.00 -> 50점
        # factor 1.20 -> 70점
        # factor 0.80 -> 30점
        score = 50.0 + (
            factor - 1.0
        ) * 100.0

        score = max(
            10.0,
            min(
                90.0,
                score,
            ),
        )

        result = {
            "score": round(
                score,
                2,
            ),
            "factor": round(
                factor,
                4,
            ),
            "current": round(
                current,
                2,
            ),
            "baseline": round(
                baseline,
                2,
            ),
            "base_date": latest["date"],
            "data_lag_days": latest_lag,
            "baseline_dates": [
                row["date"]
                for row in valid_baselines
            ],
            "outside": latest["outside"],
            "foreign": latest["foreign"],
        }

        self._shared_demand_cache[
            demand_cache_key
        ] = result

        return result


class CongestionClient(BaseClient):
    async def list(self, name: str | None = None) -> list[dict[str, Any]]:
        if not self.settings.public_data_service_key:
            raise IntegrationError("congestion_api", "PUBLIC_DATA_SERVICE_KEY가 설정되지 않았습니다.", status_code=503)
        params: dict[str, Any] = {
            "serviceKey": unquote(self.settings.public_data_service_key),
            "pageNo": 1,
            "numOfRows": 1000,
            "MobileOS": self.settings.mobile_os,
            "MobileApp": self.settings.mobile_app,
            "areaCd": self.settings.admin_area_code,
            "signguCd": self.settings.admin_sigungu_code,
            "_type": "json",
        }
        if name:
            params["tAtsNm"] = name
        payload = await self._get("congestion_api", f"{self.settings.congestion_api_base_url}/tatsCnctrRatedList", params=params)
        return _items(payload)

    async def score_map(
        self,
    ) -> dict[str, tuple[float, str | None]]:
        rows = await self.list()

        grouped: dict[
            str,
            list[
                tuple[
                    date | None,
                    float,
                    str | None,
                ]
            ],
        ] = {}

        today = date.today()

        for row in rows:
            name = _first_value(
                row,
                [
                    "tAtsNm",
                    "touristSpotName",
                    "name",
                    "title",
                ],
            )

            if not name:
                continue

            raw = _first_numeric(
                row,
                [
                    "cnctrRate",
                    "cnctrRt",
                    "congestionRate",
                    "rate",
                    "tAtsCnctrRate",
                ],
            )

            if raw is None:
                continue

            # 관광공사 값을 예상 혼잡도 0~100 기준으로 정규화합니다.
            #
            # 0~1   형식 → 0~100
            # 0~10  형식 → 0~100
            # 0~100 형식 → 그대로 사용
            # 100 초과 값은 백분율 스케일로 보정

            if raw <= 1:
                score = raw * 100.0
            elif raw <= 10:
                score = raw * 10.0
            elif raw <= 100:
                score = raw
            else:
                score = raw / 10.0

            score = max(
                0.0,
                min(
                    100.0,
                    score,
                ),
            )

            raw_date = _first_value(
                row,
                [
                    "baseYmd",
                    "predYmd",
                    "tourYmd",
                    "date",
                ],
            )

            parsed_date: date | None = None

            if raw_date:
                digits = re.sub(
                    r"[^0-9]",
                    "",
                    str(raw_date),
                )

                if len(digits) >= 8:
                    try:
                        parsed_date = datetime.strptime(
                            digits[:8],
                            "%Y%m%d",
                        ).date()
                    except ValueError:
                        parsed_date = None

            key = _normalize_name(
                str(name)
            )

            grouped.setdefault(
                key,
                [],
            ).append(
                (
                    parsed_date,
                    score,
                    str(raw_date)
                    if raw_date
                    else None,
                )
            )

        result: dict[
            str,
            tuple[
                float,
                str | None,
            ],
        ] = {}

        for name, candidates in grouped.items():
            dated = [
                item
                for item in candidates
                if item[0] is not None
            ]

            past_or_today = [
                item
                for item in dated
                if item[0] <= today
            ]

            if past_or_today:
                chosen = min(
                    past_or_today,
                    key=lambda item:
                    (today - item[0]).days,
                )

            elif dated:
                chosen = min(
                    dated,
                    key=lambda item:
                    (item[0] - today).days,
                )

            else:
                chosen = candidates[-1]

            result[name] = (
                chosen[1],
                chosen[2],
            )

        return result


class RelatedTourClient(BaseClient):
    async def related(self, base_ym: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if not self.settings.public_data_service_key:
            raise IntegrationError("related_api", "PUBLIC_DATA_SERVICE_KEY가 설정되지 않았습니다.", status_code=503)
        ym = base_ym or (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y%m")
        params = {
            "serviceKey": unquote(self.settings.public_data_service_key), "pageNo": 1, "numOfRows": limit,
            "MobileOS": self.settings.mobile_os, "MobileApp": self.settings.mobile_app,
            "baseYm": ym, "areaCd": self.settings.admin_area_code, "signguCd": self.settings.admin_sigungu_code,
            "_type": "json",
        }
        payload = await self._get("related_api", f"{self.settings.related_api_base_url}/areaBasedList1", params=params)
        return _items(payload)


class HubTourClient(BaseClient):
    async def hubs(self, base_ym: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if not self.settings.public_data_service_key:
            raise IntegrationError("hub_api", "PUBLIC_DATA_SERVICE_KEY가 설정되지 않았습니다.", status_code=503)
        ym = base_ym or (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y%m")
        params = {
            "serviceKey": unquote(self.settings.public_data_service_key), "pageNo": 1, "numOfRows": limit,
            "MobileOS": self.settings.mobile_os, "MobileApp": self.settings.mobile_app,
            "baseYm": ym, "areaCd": self.settings.admin_area_code, "signguCd": self.settings.admin_sigungu_code,
            "_type": "json",
        }
        payload = await self._get("hub_api", f"{self.settings.hub_api_base_url}/areaBasedList1", params=params)
        return _items(payload)


class WeatherClient(BaseClient):
    async def current(self, latitude: float, longitude: float) -> dict[str, Any]:
        if not self.settings.kma_service_key:
            raise IntegrationError("weather_api", "KMA_SERVICE_KEY가 설정되지 않았습니다.", status_code=503)
        nx, ny = latlon_to_kma_grid(latitude, longitude)
        now = datetime.now()
        base_date, base_time = latest_ultra_short_base(now)
        params = {
            "serviceKey": unquote(self.settings.kma_service_key), "pageNo": 1, "numOfRows": 1000,
            "dataType": "JSON", "base_date": base_date, "base_time": base_time, "nx": nx, "ny": ny,
        }
        payload = await self._get("weather_api", f"{self.settings.kma_base_url}/getUltraSrtNcst", params=params)
        values = {item.get("category"): item.get("obsrValue") for item in _items(payload)}
        pty = str(values.get("PTY", "0"))
        temp = _to_float(values.get("T1H"))
        return {
            "temperature_c": temp,
            "precipitation_type": pty,
            "raining": pty not in ("0", "None", ""),
            "hot": temp is not None and temp >= 33,
            "humidity": _to_float(values.get("REH")),
            "wind_speed": _to_float(values.get("WSD")),
            "base_date": base_date,
            "base_time": base_time,
        }


class KakaoRouteClient(BaseClient):

    async def route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        mode: TransportMode,
        origin_name: str = "현재 위치",
        destination_name: str = "도착지",
    ) -> dict[str, Any]:

        if not self.settings.kakao_rest_api_key:
            raise IntegrationError(
                "kakao_route",
                "KAKAO_REST_API_KEY가 설정되지 않았습니다.",
                status_code=503,
            )

        # origin = (위도, 경도)
        # destination = (위도, 경도)
        olat, olon = origin
        dlat, dlon = destination

        headers = {
            "Authorization": f"KakaoAK {self.settings.kakao_rest_api_key}",
        }

        # ============================================================
        # 1. 도보
        # ============================================================

        if mode == TransportMode.walking:

            params = {
                "start_x": str(olon),
                "start_y": str(olat),
                "end_x": str(dlon),
                "end_y": str(dlat),
                "s_name": origin_name,
                "e_name": destination_name,
                "input_coord": "WGS84",
                "output_coord": "WGS84",
                "route_mode": "SHORTEST",
            }

            payload = await self._get(
                "kakao_walk",
                "https://dapi.kakao.com/v2/routing/walk",
                params=params,
                headers=headers,
            )

            if payload.get("status") != "OK":
                raise IntegrationError(
                    "kakao_walk",
                    f"도보 경로를 찾지 못했습니다. status={payload.get('status')}",
                )

            route = payload.get("route") or {}
            properties = route.get("properties") or {}

            return {
                "distance_m": int(
                    properties.get("totalDistance", 0)
                ),
                "duration_seconds": int(
                    properties.get("totalTime", 0)
                ),
                "transport": "walking",
                "transfers": 0,
                "fare": None,
                "landing_url": properties.get("landingUrl"),
            }

        # ============================================================
        # 2. 대중교통
        # ============================================================

        elif mode == TransportMode.public_transport:

            params = {
                "start_x": str(olon),
                "start_y": str(olat),
                "end_x": str(dlon),
                "end_y": str(dlat),
                "s_name": origin_name,
                "e_name": destination_name,
                "input_coord": "WGS84",
                "output_coord": "WGS84",
            }

            payload = await self._get(
                "kakao_public_transport",
                "https://dapi.kakao.com/v2/routing/publictraffic",
                params=params,
                headers=headers,
            )

            if payload.get("status") != "OK":
                raise IntegrationError(
                    "kakao_public_transport",
                    (
                        "대중교통 경로를 찾지 못했습니다. "
                        f"status={payload.get('status')}"
                    ),
                )

            routes = payload.get("routes") or []

            if not routes:
                raise IntegrationError(
                    "kakao_public_transport",
                    "대중교통 경로 결과가 없습니다.",
                )

            route = routes[0]
            properties = route.get("properties") or {}

            fare_info = properties.get("fare") or {}

            if isinstance(fare_info, dict):
                fare_value = fare_info.get("value")
            else:
                fare_value = fare_info

            return {
                "distance_m": int(
                    properties.get("totalDistance", 0)
                ),
                "duration_seconds": int(
                    properties.get("totalTime", 0)
                ),
                "transport": "public_transport",
                "transfers": int(
                    properties.get("transfers", 0)
                ),
                "fare": (
                    int(fare_value)
                    if fare_value not in (None, "")
                    else None
                ),
                "landing_url": (
                    properties.get("landingURL")
                    or properties.get("landingUrl")
                    or (payload.get("properties") or {}).get(
                        "landingURL"
                    )
                ),
            }

        # ============================================================
        # 3. 자동차
        # ============================================================

        elif mode == TransportMode.driving:

            params = {
                "origin": f"{olon},{olat}",
                "destination": f"{dlon},{dlat}",
                "summary": "true",
                "priority": "RECOMMEND",
            }

            payload = await self._get(
                "kakao_route",
                f"{self.settings.kakao_navi_base_url}/v1/directions",
                params=params,
                headers={
                    **headers,
                    "Content-Type": "application/json",
                },
            )

            routes = payload.get("routes") or []

            if not routes:
                raise IntegrationError(
                    "kakao_route",
                    "자동차 길찾기 결과가 없습니다.",
                )

            first_route = routes[0]

            if first_route.get("result_code") not in (None, 0):
                raise IntegrationError(
                    "kakao_route",
                    first_route.get(
                        "result_msg",
                        "자동차 길찾기 결과가 없습니다.",
                    ),
                )

            summary = first_route.get("summary") or {}

            fare_info = summary.get("fare") or {}

            taxi_fare = None
            toll_fare = None

            if isinstance(fare_info, dict):
                taxi_value = fare_info.get("taxi")
                toll_value = fare_info.get("toll")

                if taxi_value not in (None, ""):
                    taxi_fare = int(taxi_value)

                if toll_value not in (None, ""):
                    toll_fare = int(toll_value)

            return {
                "distance_m": int(
                    summary.get("distance", 0)
                ),
                # Kakao Mobility 자동차 길찾기의 summary.duration은
                # 공식 문서 기준 "초(seconds)" 단위입니다.
                # 예: duration=615 -> 615초 -> 약 11분.
                "duration_seconds": max(
                    0,
                    int(
                        float(
                            summary.get("duration", 0)
                            or 0
                        )
                    ),
                ),
                "transport": "driving",
                "transfers": 0,

                # 대중교통 요금 필드는 자동차에서 사용하지 않음
                "fare": None,

                # 자동차 전용 예상요금
                "taxi_fare": taxi_fare,
                "toll_fare": toll_fare,

                "landing_url": None,
            }

        # ============================================================
        # 지원하지 않는 transport 값
        # ============================================================

        else:
            raise IntegrationError(
                "kakao_route",
                f"지원하지 않는 이동수단입니다: {mode}",
                status_code=422,
            )

class KakaoLocalClient(BaseClient):
    BASE_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
    CATEGORY_URL = "https://dapi.kakao.com/v2/local/search/category.json"
    WEB_SEARCH_URL = "https://dapi.kakao.com/v2/search/web"

    async def web_search(
        self,
        query: str,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Kakao/Daum 웹문서 검색.

        장소 소개가 TourAPI에 비어 있을 때 경주문화관광,
        국가유산청, 대한민국 구석구석 같은 공식 문서를 찾는
        보완 검색으로 사용합니다. 공모전의 Kakao REST API 키를
        그대로 사용하므로 별도 검색 API 키가 필요하지 않습니다.
        """
        if not self.settings.kakao_rest_api_key:
            raise IntegrationError(
                "kakao_web_search",
                "KAKAO_REST_API_KEY가 설정되지 않았습니다.",
                status_code=503,
            )

        params: dict[str, Any] = {
            "query": query,
            "sort": "accuracy",
            "page": 1,
            "size": max(1, min(int(limit), 50)),
        }

        payload = await self._get(
            "kakao_web_search",
            self.WEB_SEARCH_URL,
            params=params,
            headers={
                "Authorization":
                    f"KakaoAK {self.settings.kakao_rest_api_key}"
            },
        )

        rows: list[dict[str, Any]] = []
        for item in payload.get("documents", []):
            rows.append(
                {
                    "title": _clean_html(item.get("title")) or "",
                    "url": item.get("url") or "",
                    "description": _clean_html(item.get("contents")) or "",
                    "datetime": item.get("datetime") or "",
                    "search_source": "kakao_daum",
                }
            )
        return rows

    async def keyword_search(
        self,
        query: str,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        if not self.settings.kakao_rest_api_key:
            raise IntegrationError(
                "kakao_local",
                "KAKAO_REST_API_KEY가 설정되지 않았습니다.",
                status_code=503,
            )

        params: dict[str, Any] = {
            "query": query,
            "size": max(1, min(limit, 15)),
            "sort": "accuracy",
        }

        if (
            latitude is not None
            and longitude is not None
            and latitude != 0
            and longitude != 0
        ):
            params.update(
                {
                    "x": longitude,
                    "y": latitude,
                    "radius": 20000,
                }
            )

        payload = await self._get(
            "kakao_local",
            self.BASE_URL,
            params=params,
            headers={
                "Authorization":
                    f"KakaoAK {self.settings.kakao_rest_api_key}"
            },
        )

        return [
            {
                "id": item.get("id") or "",
                "title": item.get("place_name") or "",
                "category": item.get("category_name") or "",
                "phone": item.get("phone") or "",
                "address": item.get("address_name") or "",
                "road_address": item.get("road_address_name") or "",
                "longitude": _to_float(item.get("x")),
                "latitude": _to_float(item.get("y")),
                "place_url": item.get("place_url") or "",
            }
            for item in payload.get("documents", [])
        ]

    async def category_search(
        self,
        category_group_code: str,
        *,
        latitude: float,
        longitude: float,
        radius_m: int = 5000,
        limit: int = 15,
    ) -> list[dict[str, Any]]:
        """
        좌표 주변 Kakao 카테고리 검색.

        AT4(관광명소)를 사용하면 지도 핀 바로 옆의 불국사,
        동궁과 월지 같은 대표 장소를 이름 없이도 찾을 수 있습니다.
        """
        if not self.settings.kakao_rest_api_key:
            raise IntegrationError(
                "kakao_local",
                "KAKAO_REST_API_KEY가 설정되지 않았습니다.",
                status_code=503,
            )

        params: dict[str, Any] = {
            "category_group_code":
                category_group_code,
            "x": longitude,
            "y": latitude,
            "radius": max(
                1,
                min(
                    int(radius_m),
                    20000,
                ),
            ),
            "size": max(
                1,
                min(
                    int(limit),
                    15,
                ),
            ),
            "sort": "distance",
        }

        payload = await self._get(
            "kakao_local",
            self.CATEGORY_URL,
            params=params,
            headers={
                "Authorization":
                    f"KakaoAK {self.settings.kakao_rest_api_key}"
            },
        )

        return [
            {
                "id": item.get("id") or "",
                "title": item.get("place_name") or "",
                "category": item.get("category_name") or "",
                "phone": item.get("phone") or "",
                "address": item.get("address_name") or "",
                "road_address":
                    item.get("road_address_name")
                    or "",
                "longitude":
                    _to_float(
                        item.get("x")
                    ),
                "latitude":
                    _to_float(
                        item.get("y")
                    ),
                "distance_m": (
                    int(item.get("distance"))
                    if str(
                        item.get("distance")
                        or ""
                    ).isdigit()
                    else None
                ),
                "place_url":
                    item.get("place_url")
                    or "",
            }
            for item
            in payload.get(
                "documents",
                [],
            )
        ]


class NaverClient(BaseClient):
    BASE_URL = "https://naverapihub.apigw.ntruss.com"

    @property
    def headers(self) -> dict[str, str]:
        if (
            not self.settings.naver_client_id
            or not self.settings.naver_client_secret
        ):
            raise IntegrationError(
                "naver",
                "NAVER_CLIENT_ID/NAVER_CLIENT_SECRET이 설정되지 않았습니다.",
                status_code=503,
            )

        return {
            "X-NCP-APIGW-API-KEY-ID": self.settings.naver_client_id,
            "X-NCP-APIGW-API-KEY": self.settings.naver_client_secret,
        }

    @property
    def json_headers(self) -> dict[str, str]:
        return {
            **self.headers,
            "Content-Type": "application/json",
        }

    async def blogs(
        self,
        query: str,
        limit: int = 5,
    ) -> list[ContentItem]:
        """
        NAVER API HUB 블로그 검색.

        현재 경주한적의 ContentService에서
        관광지 관련 블로그 콘텐츠를 가져올 때 사용합니다.
        """
        params = {
            "query": query,
            "display": max(1, min(limit, 100)),
            "start": 1,
            "sort": "sim",
            "format": "json",
        }

        payload = await self._get(
            "naver_blog",
            f"{self.BASE_URL}/search/v1/blog",
            params=params,
            headers=self.headers,
        )

        result: list[ContentItem] = []

        for item in payload.get("items", []):
            result.append(
                ContentItem(
                    title=_clean_html(item.get("title")) or "",
                    url=item.get("link", ""),
                    description=_clean_html(item.get("description")),
                    published_at=item.get("postdate"),
                    source="naver_blog",
                )
            )

        return result

    async def web_documents(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """NAVER API HUB 웹문서 검색.

        장소 소개가 TourAPI에 비어 있을 때 경주시 문화관광,
        국가유산청, 대한민국 구석구석 등 공식 웹 문서의 검색
        스니펫을 보완 근거로 사용할 때 활용합니다.
        """
        params = {
            "query": query,
            "display": max(1, min(limit, 100)),
            "start": 1,
            "format": "json",
        }

        payload = await self._get(
            "naver_web",
            f"{self.BASE_URL}/search/v1/webkr",
            params=params,
            headers=self.headers,
        )

        result: list[dict[str, Any]] = []
        for item in payload.get("items", []):
            result.append(
                {
                    "title": _clean_html(item.get("title")) or "",
                    "url": item.get("link", ""),
                    "description": _clean_html(item.get("description")) or "",
                }
            )
        return result

    async def local_search(
        self,
        query: str,
        limit: int = 5,
        sort: str = "random",
    ) -> list[dict[str, Any]]:
        """
        NAVER API HUB 지역 검색.

        네이버 지역 서비스에 등록된 업체/기관을 검색합니다.

        예:
        - 경주 불국사
        - 경주 황리단길 카페
        - 경주 대릉원 음식점

        지역 검색 API의 display 최대값은 5입니다.
        """
        if sort not in ("random", "comment"):
            sort = "random"

        params = {
            "query": query,
            "display": max(1, min(limit, 5)),
            "start": 1,
            "sort": sort,
            "format": "json",
        }

        payload = await self._get(
            "naver_local",
            f"{self.BASE_URL}/search/v1/local",
            params=params,
            headers=self.headers,
        )

        result: list[dict[str, Any]] = []

        for item in payload.get("items", []):
            result.append(
                {
                    "title": _clean_html(item.get("title")) or "",
                    "link": item.get("link") or None,
                    "category": _clean_html(item.get("category")),
                    "description": _clean_html(item.get("description")),
                    "telephone": item.get("telephone") or None,
                    "address": item.get("address") or None,
                    "road_address": item.get("roadAddress") or None,
                    "mapx": item.get("mapx"),
                    "mapy": item.get("mapy"),
                }
            )

        return result

    @staticmethod
    def _trend_keyword_variants(
        keyword: str,
    ) -> list[str]:
        """
        관광지 검색어 변형.

        예:
        '경주 동궁과 월지' -> ['경주 동궁과 월지', '동궁과 월지']

        관광공사 제목에 '경주'가 붙어 있어도 실제 검색 사용자는
        장소명만 검색할 수 있으므로 두 표현을 같은 그룹으로 묶습니다.
        """
        cleaned = re.sub(
            r"\s+",
            " ",
            keyword,
        ).strip()

        # 관광공사 제목의 설명용 대괄호 꼬리표는
        # 실제 검색어가 아니므로 NAVER 조회 전에 제거합니다.
        canonical = re.sub(
            r"\s*\[[^\]]+\]\s*",
            " ",
            cleaned,
        )
        canonical = re.sub(
            r"\s+",
            " ",
            canonical,
        ).strip()

        variants = [
            value
            for value in (
                canonical,
                cleaned,
            )
            if value
        ]

        without_gyeongju = re.sub(
            r"^경주\s*",
            "",
            canonical,
        ).strip()

        if (
            without_gyeongju
            and without_gyeongju != cleaned
        ):
            variants.append(
                without_gyeongju
            )

        # 괄호형 관광지명 처리:
        # "천마총(대릉원)" -> "천마총", "대릉원"도 같은 그룹에 포함
        parenthetical = re.findall(
            r"\(([^)]+)\)",
            without_gyeongju,
        )

        without_parentheses = re.sub(
            r"\s*\([^)]*\)",
            "",
            without_gyeongju,
        ).strip()

        if without_parentheses:
            variants.append(
                without_parentheses
            )

        for value in parenthetical:
            value = value.strip()
            if value:
                variants.append(
                    value
                )

        return list(
            dict.fromkeys(
                value
                for value in variants
                if value
            )
        )

    async def trend_signals(
        self,
        keywords: list[str],
    ) -> dict[str, dict[str, float]]:
        """
        NAVER popularity + momentum.

        V2.4.4 계산 방식은 유지하고, 최대 4개 장소 단위 API 요청을
        병렬 실행해 코스 생성 대기시간만 줄입니다.
        """
        cleaned_keywords = [
            keyword.strip()
            for keyword in keywords
            if keyword
            and keyword.strip()
        ]

        cleaned_keywords = list(
            dict.fromkeys(
                cleaned_keywords
            )
        )

        if not cleaned_keywords:
            return {}

        end_date = date.today()
        start_date = (
            end_date
            - timedelta(days=28)
        )

        anchor_keywords = [
            "경주 여행",
            "경주 관광",
            "경주 가볼만한곳",
        ]

        async def fetch_chunk(
            offset: int,
            chunk: list[str],
        ) -> dict[str, dict[str, float]]:
            group_to_keyword: dict[
                str,
                str,
            ] = {}

            keyword_groups: list[
                dict[str, Any]
            ] = [
                {
                    "groupName": "anchor",
                    "keywords": anchor_keywords,
                }
            ]

            for index, keyword in enumerate(
                chunk
            ):
                group_name = (
                    f"p{offset + index}"
                )

                group_to_keyword[
                    group_name
                ] = keyword

                keyword_groups.append(
                    {
                        "groupName": group_name,
                        "keywords": (
                            self._trend_keyword_variants(
                                keyword
                            )
                        ),
                    }
                )

            body = {
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "timeUnit": "date",
                "keywordGroups": keyword_groups,
            }

            payload = await self._post(
                "naver_datalab",
                (
                    f"{self.BASE_URL}"
                    "/search-trend/v1/search"
                ),
                json_body=body,
                headers=self.json_headers,
            )

            series_map = {
                str(
                    series.get(
                        "title",
                        "",
                    )
                ): series
                for series
                in payload.get(
                    "results",
                    [],
                )
            }

            anchor_series = series_map.get(
                "anchor",
                {},
            )

            anchor_ratios = [
                float(
                    row.get(
                        "ratio",
                        0,
                    )
                    or 0
                )
                for row
                in anchor_series.get(
                    "data",
                    [],
                )
            ]

            anchor_recent = (
                anchor_ratios[-14:]
                if anchor_ratios
                else []
            )

            anchor_recent_avg = (
                sum(anchor_recent)
                / len(anchor_recent)
                if anchor_recent
                else 0.0
            )

            chunk_result: dict[
                str,
                dict[str, float],
            ] = {}

            for group_name, keyword in group_to_keyword.items():
                series = series_map.get(
                    group_name,
                    {},
                )

                ratios = [
                    float(
                        row.get(
                            "ratio",
                            0,
                        )
                        or 0
                    )
                    for row
                    in series.get(
                        "data",
                        [],
                    )
                ]

                recent_14 = (
                    ratios[-14:]
                    if ratios
                    else []
                )

                recent_14_avg = (
                    sum(recent_14)
                    / len(recent_14)
                    if recent_14
                    else 0.0
                )

                if anchor_recent_avg > 0:
                    popularity = (
                        recent_14_avg
                        / anchor_recent_avg
                    )
                else:
                    popularity = 0.0

                popularity = max(
                    0.0,
                    min(
                        1.0,
                        popularity,
                    ),
                )

                if not ratios:
                    momentum = 0.5
                else:
                    recent = (
                        ratios[-7:]
                        or [0.0]
                    )

                    previous = (
                        ratios[-14:-7]
                        or [0.0]
                    )

                    recent_avg = (
                        sum(recent)
                        / len(recent)
                    )

                    previous_avg = (
                        sum(previous)
                        / len(previous)
                    )

                    if previous_avg <= 0:
                        momentum = (
                            0.5
                            if recent_avg <= 0
                            else 1.0
                        )
                    else:
                        growth = (
                            recent_avg
                            - previous_avg
                        ) / previous_avg

                        momentum = (
                            0.5
                            + growth / 2
                        )

                    momentum = max(
                        0.0,
                        min(
                            1.0,
                            momentum,
                        ),
                    )

                chunk_result[keyword] = {
                    "popularity": round(
                        popularity,
                        6,
                    ),
                    "momentum": round(
                        momentum,
                        6,
                    ),
                }

            return chunk_result

        jobs = [
            (
                offset,
                cleaned_keywords[
                    offset:offset + 4
                ],
            )
            for offset in range(
                0,
                len(cleaned_keywords),
                4,
            )
        ]

        parts = await asyncio.gather(
            *(
                fetch_chunk(offset, chunk)
                for offset, chunk in jobs
            )
        )

        result: dict[
            str,
            dict[str, float],
        ] = {}

        for part in parts:
            result.update(part)

        return result

    async def trend_scores(
        self,
        keywords: list[str],
    ) -> dict[str, float]:
        """
        하위 호환용 메서드.

        기존 코드가 trend_scores()를 호출할 경우
        예전과 동일하게 momentum 값만 반환합니다.
        """
        signals = await self.trend_signals(
            keywords
        )

        return {
            keyword: values.get(
                "momentum",
                0.5,
            )
            for keyword, values
            in signals.items()
        }


class YouTubeClient(BaseClient):
    async def videos(self, query: str, limit: int = 5) -> list[ContentItem]:
        if not self.settings.youtube_api_key:
            raise IntegrationError("youtube", "YOUTUBE_API_KEY가 설정되지 않았습니다.", status_code=503)
        params = {"part": "snippet", "q": query, "type": "video", "maxResults": limit, "order": "relevance", "key": self.settings.youtube_api_key}
        payload = await self._get("youtube", "https://www.googleapis.com/youtube/v3/search", params=params)
        result = []
        for item in payload.get("items", []):
            video_id = (item.get("id") or {}).get("videoId")
            snippet = item.get("snippet") or {}
            if video_id:
                result.append(ContentItem(title=snippet.get("title", ""), url=f"https://www.youtube.com/watch?v={video_id}", description=snippet.get("description"), thumbnail_url=((snippet.get("thumbnails") or {}).get("high") or {}).get("url"), published_at=snippet.get("publishedAt"), source="youtube"))
        return result


class OpenAIClient(BaseClient):
    @property
    def headers(self) -> dict[str, str]:
        if not self.settings.openai_api_key:
            raise IntegrationError(
                "openai",
                "OPENAI_API_KEY가 설정되지 않았습니다.",
                status_code=503,
            )

        return {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }

    async def parse_route_request(
        self,
        command: str,
    ) -> dict[str, Any]:
        """
        자연어 여행 요청을 추천 엔진이 이해하는 조건으로 변환합니다.

        GPT가 실제 장소를 새로 만들어내도록 하지 않고,
        사용자가 명시한 장소명/조건만 구조화합니다.
        """
        schema = {
            "type": "object",
            "properties": {
                "required_places": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "excluded_places": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "preferences": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "문화유산",
                            "자연",
                            "산책",
                            "전통마을",
                            "야경",
                            "맛집",
                            "카페",
                            "실내",
                        ],
                    },
                },
                "include_food": {
                    "type": "boolean",
                },
                "include_cafe": {
                    "type": "boolean",
                },
                "free_only": {
                    "type": "boolean",
                },
                "short_walk": {
                    "type": "boolean",
                },
            },
            "required": [
                "required_places",
                "excluded_places",
                "preferences",
                "include_food",
                "include_cafe",
                "free_only",
                "short_walk",
            ],
            "additionalProperties": False,
        }

        body = {
            "model": self.settings.openai_model,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "경주 여행 코스 요청을 구조화하세요. "
                        "사용자가 직접 말한 장소명만 required_places 또는 "
                        "excluded_places에 넣으세요. "
                        "장소명을 추측하거나 새로 만들지 마세요. "
                        "'첨성대 가고 싶어'는 required_places에 "
                        "'첨성대'를 넣습니다. "
                        "음식점/맛집 요청은 include_food, "
                        "카페 요청은 include_cafe에 반영하세요."
                    ),
                },
                {
                    "role": "user",
                    "content": command,
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "route_request",
                    "strict": True,
                    "schema": schema,
                }
            },
        }

        payload = await self._post(
            "openai",
            f"{self.settings.openai_base_url}/responses",
            json_body=body,
            headers=self.headers,
        )

        text = _extract_openai_text(
            payload
        )

        return json.loads(text)

    async def parse_course_command(
        self,
        command: str,
        place_names: list[str],
    ) -> dict[str, Any]:
        schema = {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "remove",
                        "add",
                        "replace",
                        "reorder",
                        "set_condition",
                        "keep",
                    ],
                },
                "target": {
                    "type": [
                        "string",
                        "null",
                    ],
                },
                "replacement": {
                    "type": [
                        "string",
                        "null",
                    ],
                },
                "position": {
                    "type": [
                        "integer",
                        "null",
                    ],
                },
                "conditions": {
                    "type": "array",
                    "items": {
                        "type": "string",
                    },
                },
            },
            "required": [
                "action",
                "target",
                "replacement",
                "position",
                "conditions",
            ],
            "additionalProperties": False,
        }

        body = {
            "model": self.settings.openai_model,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "한국어 관광 코스 수정 명령을 구조화하세요. "
                        "장소명은 가능한 한 제공 목록과 정확히 맞추세요."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"현재 장소: {place_names}\n"
                        f"명령: {command}"
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "course_command",
                    "strict": True,
                    "schema": schema,
                }
            },
        }

        payload = await self._post(
            "openai",
            f"{self.settings.openai_base_url}/responses",
            json_body=body,
            headers=self.headers,
        )

        text = _extract_openai_text(
            payload
        )

        return json.loads(text)

    async def answer_with_context(
        self,
        query: str,
        contexts: list[dict[str, Any]],
        history: list[ChatTurn] | None = None,
    ) -> str:
        """
        1차 답변 단계.

        검색된 RAG 자료 안에서 질문에 답할 수 있으면
        반드시 RAG 자료를 우선하여 답변합니다.

        RAG 자료만으로 질문에 충분히 답할 수 없다면
        __RAG_FALLBACK__ 을 반환하여 RagService가
        일반 OpenAI 지식 답변으로 전환할 수 있게 합니다.
        """

        def _line(
            label: str,
            value: Any,
        ) -> str:
            return (
                f"{label}: {value}"
                if value
                else ""
            )

        blocks: list[str] = []

        for context in contexts:
            fields = "\n".join(
                filter(
                    None,
                    [
                        _line(
                            "주소",
                            context.get(
                                "address"
                            ),
                        ),
                        _line(
                            "운영시간",
                            context.get(
                                "operating_hours"
                            ),
                        ),
                        _line(
                            "휴무일",
                            context.get(
                                "rest_date"
                            ),
                        ),
                        _line(
                            "요금",
                            context.get(
                                "fee_text"
                            ),
                        ),
                        _line(
                            "주차",
                            context.get(
                                "parking"
                            ),
                        ),
                        _line(
                            "유모차",
                            context.get(
                                "stroller_info"
                            ),
                        ),
                        _line(
                            "반려동물 동반",
                            context.get(
                                "pet_info"
                            ),
                        ),
                        _line(
                            "카드결제",
                            context.get(
                                "credit_card_info"
                            ),
                        ),
                        _line(
                            "홈페이지",
                            context.get(
                                "homepage"
                            ),
                        ),
                    ],
                )
            )

            blocks.append(
                f"[{context.get('title')}] | "
                f"{context.get('category')}\n"
                f"{context.get('overview') or ''}\n"
                f"{fields}"
            )

        context_text = "\n\n".join(
            blocks
        )

        history_messages = [
            {
                "role": turn.role,
                "content": turn.content,
            }
            for turn in (
                history or []
            )
        ]

        body = {
            "model": self.settings.openai_model,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "당신은 경주 관광 안내 챗봇입니다.\n"
                        "이번 단계에서는 아래 [제목]이 붙은 "
                        "RAG 자료를 최우선 근거로 사용해 "
                        "한국어로 답하세요.\n\n"

                        "- RAG 자료 안에 사용자의 질문에 "
                        "직접 답할 수 있는 충분한 근거가 있다면 "
                        "그 자료를 바탕으로 정확하게 답하세요.\n"

                        "- 답변 근거로 사용한 자료는 "
                        "대괄호 안 제목 그대로 표시하세요. "
                        "예: [경주 동궁과 월지]\n"

                        "- 관련 관광지 자료가 검색되었다는 이유만으로 "
                        "자료에 없는 내용을 추측해서 채우지 마세요.\n"

                        "- 질문의 핵심 답이 RAG 자료 안에 없거나, "
                        "현재 자료만으로 정확하게 답하기 어렵다면 "
                        "사용자에게 '자료가 부족합니다'라고 "
                        "답하지 마세요.\n"

                        "- 위 경우에는 설명, 사과, 추가 안내를 "
                        "붙이지 말고 정확히 다음 문자열 하나만 "
                        "출력하세요:\n"
                        "__RAG_FALLBACK__\n"

                        "- 역사적 사실에 여러 학설이나 견해가 있으면 "
                        "하나를 확정된 사실처럼 단정하지 마세요.\n"

                        "- 특정 국가·인종·종교 집단 전체를 "
                        "일반화하지 마세요.\n"

                        "- 이전 대화가 있다면 '그거', '거기', "
                        "'거긴' 같은 지시어가 이전 대화의 "
                        "어떤 관광지나 주제를 가리키는지 "
                        "대화 흐름을 참고하세요."
                    ),
                },
                *history_messages,
                {
                    "role": "user",
                    "content": (
                        f"질문: {query}\n\n"
                        f"참고 자료:\n"
                        f"{context_text}"
                    ),
                },
            ],
        }

        payload = await self._post(
            "openai",
            (
                f"{self.settings.openai_base_url}"
                "/responses"
            ),
            json_body=body,
            headers=self.headers,
            timeout_seconds=60.0,
        )

        return _extract_openai_text(
            payload
        ).strip()

    async def answer_without_context(
        self,
        query: str,
        history: list[ChatTurn] | None = None,
    ) -> str:
        """
        2차 답변 단계.

        RAG 자료만으로 충분히 답할 수 없을 때
        OpenAI의 일반 지식을 사용합니다.

        단, 불확실한 역사 사실이나 최신 정보는
        임의로 만들어내지 않습니다.
        """

        history_messages = [
            {
                "role": turn.role,
                "content": turn.content,
            }
            for turn in (
                history or []
            )
        ]

        body = {
            "model": self.settings.openai_model,
            "input": [
                {
                    "role": "system",
                    "content": (
                        "당신은 경주 여행과 역사·문화 정보를 "
                        "안내하는 한국어 관광 챗봇입니다.\n\n"

                        "현재 질문은 검색된 RAG 자료만으로 "
                        "충분히 답할 수 없어 OpenAI의 일반 지식을 "
                        "이용해 답하는 단계입니다.\n\n"

                        "- 널리 알려져 있고 신뢰할 수 있으며 "
                        "당신이 높은 확신을 가진 사실은 "
                        "적극적으로 답하세요.\n"

                        "- RAG에 정보가 없었다는 이유만으로 "
                        "'자료가 부족합니다'라고 회피하지 마세요.\n"

                        "- 확신할 수 없는 인물명, 연도, 건립자, "
                        "사건, 문화재의 세부 사실은 "
                        "추측해서 만들지 마세요.\n"

                        "- 여러 학설이나 견해가 존재하는 내용은 "
                        "확정된 사실과 학설을 명확히 구분해서 "
                        "설명하세요.\n"

                        "- 일부 인터넷 자료에서 널리 반복되는 주장이라도 "
                        "정설인지 확실하지 않다면 "
                        "'이런 견해가 있다'고 구분해서 설명하세요.\n"

                        "- 사용자의 질문이나 전제에 사실 오류가 "
                        "있을 가능성이 있으면 그대로 맞장구치지 말고 "
                        "알려진 사실과 견해를 구분해 설명하세요.\n"

                        "- 운영시간, 휴무일, 입장료, 행사 일정, "
                        "날씨, 현재 혼잡도처럼 수시로 바뀌는 정보는 "
                        "모델의 일반 지식만으로 최신 정보처럼 "
                        "단정하지 마세요.\n"

                        "- 정확하게 알 수 없는 부분은 "
                        "모른다고 말하세요. "
                        "하지만 확실하게 답할 수 있는 내용까지 "
                        "회피하지 마세요.\n"

                        "- 질문에 대한 직접적인 답을 먼저 말하고 "
                        "그 뒤에 필요한 설명을 덧붙이세요.\n"

                        "- 경주 관광과 관련 없는 사실을 "
                        "임의로 만들어내지 마세요."
                    ),
                },
                *history_messages,
                {
                    "role": "user",
                    "content": query,
                },
            ],
        }

        payload = await self._post(
            "openai",
            (
                f"{self.settings.openai_base_url}"
                "/responses"
            ),
            json_body=body,
            headers=self.headers,
            timeout_seconds=60.0,
        )

        return _extract_openai_text(
            payload
        ).strip()

    async def embeddings(
        self,
        texts: list[str],
    ) -> list[list[float]]:
        if not texts:
            return []

        body = {
            "model":
                self.settings.openai_embedding_model,
            "input":
                texts,
        }

        payload = await self._post(
            "openai",
            (
                f"{self.settings.openai_base_url}"
                "/embeddings"
            ),
            json_body=body,
            headers=self.headers,
        )

        return [
            row["embedding"]
            for row in sorted(
                payload.get(
                    "data",
                    [],
                ),
                key=lambda item:
                    item.get(
                        "index",
                        0,
                    ),
            )
        ]

class PhotoClient(BaseClient):
    async def search(self, keyword: str, limit: int = 10) -> list[dict[str, Any]]:
        if not self.settings.public_data_service_key:
            raise IntegrationError("photo_api", "PUBLIC_DATA_SERVICE_KEY가 설정되지 않았습니다.", status_code=503)
        params = {
            "serviceKey": unquote(self.settings.public_data_service_key), "MobileOS": self.settings.mobile_os,
            "MobileApp": self.settings.mobile_app, "_type": "json", "keyword": keyword,
            "numOfRows": limit, "pageNo": 1, "arrange": "A",
        }
        # 관광사진 API의 키워드 조회 오퍼레이션
        payload = await self._get("photo_api", f"{self.settings.photo_api_base_url}/gallerySearchList1", params=params)
        return _items(payload)


def _first_value(row: dict[str, Any], keys: Iterable[str]) -> str | None:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return _clean_html(value)
    return None


def _first_numeric(row: dict[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        value = _to_float(row.get(key))
        if value is not None:
            return value
    # 명세 변경에 대비해 concentration/rate가 포함된 숫자 필드를 탐색
    for key, value in row.items():
        lowered = key.lower()
        if "rate" in lowered or "cnctr" in lowered:
            parsed = _to_float(value)
            if parsed is not None:
                return parsed
    return None


def _normalize_name(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", value.lower())


def normalize_name(value: str) -> str:
    return _normalize_name(value)


def _extract_openai_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    for output in payload.get("output", []):
        for content in output.get("content", []):
            if content.get("type") in ("output_text", "text") and content.get("text"):
                return content["text"]
    raise IntegrationError("openai", "OpenAI 응답에서 구조화된 텍스트를 찾지 못했습니다.")
