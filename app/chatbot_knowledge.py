from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings

HERITAGE_LIST_URL = "https://www.khs.go.kr/cha/SearchKindOpenapiList.do"
HERITAGE_DETAIL_URL = "https://www.khs.go.kr/cha/SearchKindOpenapiDt.do"

_DYNAMIC_TOKENS = (
    "오늘", "지금", "현재", "내일", "모레", "이번주", "이번 주", "요즘", "최근",
    "혼잡", "붐비", "사람 많", "대기", "날씨", "기온", "비 와", "눈 와",
    "행사", "축제", "공연", "이벤트",
    "운영시간", "운영 시간", "관람시간", "관람 시간", "영업시간", "영업 시간",
    "몇 시", "몇시", "휴무", "휴관", "입장료", "관람료", "요금", "가격", "비용", "주차",
)

_INTENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("creator", (
        "누가 만들", "누가 지", "누가 세", "만든 사람", "지은 사람", "세운 사람",
        "건립한 사람", "축조한 사람", "창건한 사람", "창건자",
    )),
    ("construction_period", (
        "언제 만들", "언제 지", "언제 세", "건립 시기", "건립시기", "축조 시기", "축조시기",
        "만든 시기", "지은 시기", "몇 년", "몇년도", "몇 년도", "어느 시대", "시대가",
    )),
    ("purpose", (
        "왜 만들", "왜 지", "왜 세", "무슨 목적", "어떤 목적", "목적이", "용도", "어디에 쓰",
        "무엇에 쓰", "뭐에 쓰", "무슨 역할", "어떤 역할",
    )),
    ("designation_date", ("지정일", "지정된 날", "언제 지정", "등록일", "등록된 날")),
    ("designation", (
        "국보야", "보물이야", "사적이야", "국가유산 종목", "문화유산 종목", "종목이", "지정 종목",
        "어떤 국가유산", "어떤 문화유산",
    )),
    ("owner", ("소유자", "누구 소유", "누가 소유")),
    ("administrator", ("관리자", "관리기관", "관리 단체", "관리단체", "누가 관리")),
    ("heritage_address", ("국가유산 주소", "문화유산 주소", "소재지")),
    ("history", ("역사", "유래", "역사적 배경", "배경 알려")),
    ("significance", ("의미", "가치", "왜 중요", "왜 유명", "중요한 이유", "특징")),
)

_POOR_ANSWER_TOKENS = (
    "확인할 수 없습니다", "확인할 수 없어요", "자료가 부족", "정보가 부족",
    "알 수 없습니다", "알 수 없어요", "관광지명을 함께", "__RAG_FALLBACK__",
)


def _norm(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", value).lower()


def is_dynamic_query(query: str) -> bool:
    lowered = query.lower()
    return any(token in lowered for token in _DYNAMIC_TOKENS)


def classify_stable_intent(query: str) -> str | None:
    if is_dynamic_query(query):
        return None
    lowered = query.lower()
    for intent, tokens in _INTENT_RULES:
        if any(token in lowered for token in tokens):
            return intent
    return None


def make_cache_key(place_id: str, intent: str) -> str:
    raw = f"place:{place_id}|intent:{intent}"
    if len(raw) <= 240:
        return raw
    return f"chat:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def cacheable_answer(answer: str) -> bool:
    text = (answer or "").strip()
    return len(text) >= 8 and not any(token in text for token in _POOR_ANSWER_TOKENS)


def contexts_hash(contexts: list[dict[str, Any]]) -> str:
    raw = json.dumps(contexts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _xml_item(item: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for child in list(item):
        key = child.tag.rsplit("}", 1)[-1].lower()
        value = _clean_text(" ".join(part.strip() for part in child.itertext() if part and part.strip()))
        if value:
            result[key] = value
    return result


def _first(mapping: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key.lower())
        if value:
            return value
    return None


def _format_yyyymmdd(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) == 8:
        return f"{digits[:4]}년 {int(digits[4:6])}월 {int(digits[6:8])}일"
    return value


@dataclass(slots=True)
class HeritageResult:
    name: str
    designation: str | None
    number_name: str | None
    era: str | None
    designation_date: str | None
    address: str | None
    owner: str | None
    administrator: str | None
    description: str | None
    ccba_kdcd: str
    ccba_asno: str
    ccba_ctcd: str
    ccba_cpno: str | None = None

    @property
    def source_url(self) -> str:
        return (
            f"{HERITAGE_DETAIL_URL}?ccbaKdcd={self.ccba_kdcd}"
            f"&ccbaAsno={self.ccba_asno}&ccbaCtcd={self.ccba_ctcd}"
        )

    def to_context(self) -> dict[str, Any]:
        return {
            "source": "국가유산청 국가유산 정보 OPEN API",
            "title": self.name,
            "category": self.designation or "국가유산",
            "overview": self.description,
            "heritage_designation": self.designation,
            "heritage_number_name": self.number_name,
            "heritage_era": self.era,
            "heritage_designation_date": self.designation_date,
            "address": self.address,
            "heritage_owner": self.owner,
            "heritage_administrator": self.administrator,
            "source_url": self.source_url,
        }

    def to_hit_dict(self) -> dict[str, Any]:
        identifier = self.ccba_cpno or f"{self.ccba_kdcd}-{self.ccba_asno}-{self.ccba_ctcd}"
        return {
            "source_type": "etiquette",
            "place_id": f"heritage:{identifier}",
            "title": f"국가유산청 · {self.name}",
            "category": self.designation or "국가유산",
            "similarity": 1.0,
            "overview": self.description,
            "address": self.address,
            "homepage": self.source_url,
        }


class HeritageLookupClient:
    """국가유산청 Open API를 질문 시점에만 조회하며 원문 전체를 DB에 적재하지 않습니다."""

    def __init__(self, settings: Settings, *, ttl_seconds: int = 21600):
        self.settings = settings
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, HeritageResult | None]] = {}

    @property
    def timeout(self) -> float:
        configured = float(getattr(self.settings, "http_timeout_seconds", 20.0) or 20.0)
        return max(3.0, min(configured, 10.0))

    async def lookup(self, place_name: str) -> HeritageResult | None:
        key = _norm(place_name)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        try:
            result = await self._lookup_uncached(place_name)
        except Exception:
            result = None
        self._cache[key] = (now + self.ttl_seconds, result)
        return result

    async def _lookup_uncached(self, place_name: str) -> HeritageResult | None:
        params = {"ccbaMnm1": place_name, "pageUnit": 50, "pageIndex": 1, "ccbaCncl": "N"}
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=True,
            headers={"User-Agent": "GyeongjuHanjeok/1.0"},
        ) as client:
            response = await client.get(HERITAGE_LIST_URL, params=params)
            response.raise_for_status()
            items = self._parse_items(response.content)
            selected = self._select_gyeongju_item(place_name, items)
            if not selected:
                return None
            kdcd = _first(selected, "ccbakdcd")
            asno = _first(selected, "ccbaasno")
            ctcd = _first(selected, "ccbactcd")
            if not kdcd or not asno or not ctcd:
                return None
            detail_response = await client.get(
                HERITAGE_DETAIL_URL,
                params={"ccbaKdcd": kdcd, "ccbaAsno": asno, "ccbaCtcd": ctcd},
            )
            detail_response.raise_for_status()
            details = self._parse_items(detail_response.content)
            detail = details[0] if details else selected
            merged = dict(selected)
            merged.update(detail)
            return self._to_result(merged, kdcd=kdcd, asno=asno, ctcd=ctcd)

    @staticmethod
    def _parse_items(content: bytes) -> list[dict[str, str]]:
        root = ET.fromstring(content)
        return [_xml_item(item) for item in root.findall(".//item")]

    @staticmethod
    def _select_gyeongju_item(place_name: str, items: list[dict[str, str]]) -> dict[str, str] | None:
        wanted = _norm(place_name).removeprefix(_norm("경주"))
        scored: list[tuple[int, dict[str, str]]] = []
        for item in items:
            name = _first(item, "ccbamnm1") or ""
            city = _first(item, "ccsiname") or ""
            province = _first(item, "ccbactcdnm") or ""
            address = _first(item, "ccbalsad", "ccbalcad") or ""
            if (_first(item, "ccbacncl") or "N").upper() == "Y":
                continue
            score = 0
            normalized_name = _norm(name).removeprefix(_norm("경주"))
            if normalized_name == wanted:
                score += 20
            elif wanted and (wanted in normalized_name or normalized_name in wanted):
                score += 8
            if "경주" in city or "경주" in address:
                score += 10
            if "경상북도" in province or "경북" in province:
                score += 3
            scored.append((score, item))
        scored.sort(key=lambda row: row[0], reverse=True)
        return scored[0][1] if scored and scored[0][0] >= 8 else None

    @staticmethod
    def _to_result(data: dict[str, str], *, kdcd: str, asno: str, ctcd: str) -> HeritageResult:
        return HeritageResult(
            name=_first(data, "ccbamnm1") or "국가유산",
            designation=_first(data, "ccmaname"),
            number_name=_first(data, "crltsnonm"),
            era=_first(data, "cccename"),
            designation_date=_format_yyyymmdd(_first(data, "ccbaasdt")),
            address=_first(data, "ccbalcad", "ccbalsad"),
            owner=_first(data, "ccbaposs"),
            administrator=_first(data, "ccbaadmin"),
            description=_first(data, "content", "ccbacndt"),
            ccba_kdcd=kdcd,
            ccba_asno=asno,
            ccba_ctcd=ctcd,
            ccba_cpno=_first(data, "ccbacpno"),
        )


def direct_answer_from_heritage(place_name: str, intent: str, result: HeritageResult) -> str | None:
    if intent == "construction_period" and result.era:
        return f"국가유산청 공식 자료 기준, {place_name}의 시대는 {result.era}입니다."
    if intent == "designation" and result.designation:
        number = f" {result.number_name}" if result.number_name else ""
        return f"국가유산청 공식 자료 기준, {place_name}는 {result.designation}{number}로 등록되어 있습니다."
    if intent == "designation_date" and result.designation_date:
        return f"국가유산청 공식 자료 기준, {place_name}의 지정(등록)일은 {result.designation_date}입니다."
    if intent == "owner" and result.owner:
        return f"국가유산청 공식 자료 기준, {place_name}의 소유자는 {result.owner}입니다."
    if intent == "administrator" and result.administrator:
        return f"국가유산청 공식 자료 기준, {place_name}의 관리자는 {result.administrator}입니다."
    if intent == "heritage_address" and result.address:
        return f"국가유산청 공식 자료 기준, {place_name}의 소재지는 {result.address}입니다."
    return None


def local_place_context(place_data: dict[str, Any], *, title: str) -> dict[str, Any]:
    context = dict(place_data or {})
    context.setdefault("title", title)
    context.setdefault("source", "한국관광공사·경주한적 DB")
    return context


def resolved_generation_query(place_name: str, query: str) -> str:
    return (
        f"대상 장소는 '{place_name}'입니다. 사용자 질문: {query}\n"
        "답변은 다른 사용자에게 재사용할 수 있도록 장소명을 명확히 적고, "
        "제공된 공식 자료에 없는 사실은 추측하지 마세요."
    )
