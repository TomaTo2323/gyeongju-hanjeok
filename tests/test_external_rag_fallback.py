import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clients import KoreanHeritageClient, TrustedWebSourceClient
from app.config import Settings
from app.db import Base, KnowledgeDocument, PlaceRecord
from app.services import RagService


def _settings():
    return Settings(
        _env_file=None,
        openai_api_key="test-openai",
        kakao_rest_api_key="test-kakao",
    )


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[PlaceRecord.__table__, KnowledgeDocument.__table__],
    )
    return Session(engine)


def test_heritage_client_parses_list_and_detail(monkeypatch):
    client = KoreanHeritageClient(_settings())

    list_xml = """
    <result>
      <item>
        <ccbaMnm1>경주 천마총 장니 천마도</ccbaMnm1>
        <ccbaCtcdNm>경상북도</ccbaCtcdNm>
        <ccsiName>경주시</ccsiName>
        <ccbaKdcd>11</ccbaKdcd>
        <ccbaAsno>02070000</ccbaAsno>
        <ccbaCtcd>37</ccbaCtcd>
        <ccbaCncl>N</ccbaCncl>
      </item>
    </result>
    """
    detail_xml = """
    <result><item>
      <ccbaMnm1>경주 천마총 장니 천마도</ccbaMnm1>
      <ccmaName>국보</ccmaName>
      <ccceName>신라</ccceName>
      <ccbaLcad>경상북도 경주시</ccbaLcad>
      <ccbaAdmin>국립경주박물관</ccbaAdmin>
      <content>천마총에서 출토된 말다래에 그려진 천마 그림이다.</content>
    </item></result>
    """

    async def fake_get_xml(url, *, params):
        return detail_xml if "Dt.do" in url else list_xml

    monkeypatch.setattr(client, "_get_xml", fake_get_xml)
    result = asyncio.run(client.contexts("천마총", limit=2))

    assert len(result) == 1
    assert result[0]["source_name"] == "국가유산청"
    assert "신라" in result[0]["overview"]
    assert "천마총에서 출토" in result[0]["overview"]
    assert "SearchKindOpenapiDt.do" in result[0]["source_url"]


def test_trusted_web_client_rejects_untrusted_urls():
    client = TrustedWebSourceClient(_settings())
    assert client.trusted_source_name("https://gyeongju.museum.go.kr/page") == "국립경주박물관"
    assert client.trusted_source_name("https://www.khs.go.kr/cha/page") == "국가유산청"
    assert client.trusted_source_name("https://blog.example.com/post") is None


def test_internal_insufficient_answer_falls_back_to_heritage_and_daum(monkeypatch):
    db = _session()
    db.add(
        PlaceRecord(
            place_id="tm-1",
            title="천마총",
            category="관광지",
            latitude=35.0,
            longitude=129.0,
            data={
                "place_id": "tm-1",
                "title": "천마총",
                "category": "관광지",
                "latitude": 35.0,
                "longitude": 129.0,
                "overview": "경주 대릉원에 있는 고분",
            },
            embedding=[0.0, 1.0],
        )
    )
    db.add(
        KnowledgeDocument(
            doc_id="irrelevant",
            title="관광 예절",
            category="예절",
            text="관광지에서는 질서를 지켜 주세요.",
            embedding=[0.0, 1.0],
        )
    )
    db.commit()

    service = RagService(_settings(), db)
    answer_calls = []

    async def fake_embeddings(texts):
        return [[0.0, 1.0]]

    async def fake_answer(query, contexts, history=None):
        answer_calls.append(contexts)
        if len(answer_calls) == 1:
            return "확인할 수 있는 자료가 부족합니다."
        return "공식 자료에 따르면 천마총의 피장자는 명확히 밝혀지지 않았습니다."

    async def fake_heritage_contexts(title, limit=2):
        assert "천마총" in title
        return [{
            "title": "국가유산청 - 경주 천마총 장니 천마도",
            "category": "국가유산 공식자료",
            "overview": "천마총에서 출토된 신라시대 유물이다.",
            "homepage": "https://www.khs.go.kr/cha/detail",
            "source_url": "https://www.khs.go.kr/cha/detail",
            "source_name": "국가유산청",
        }]

    async def fake_daum_documents(query, limit=10):
        return [
            {
                "title": "천마총 이야기 - 국립경주박물관",
                "contents": "천마총 피장자에 관한 설명",
                "url": "https://gyeongju.museum.go.kr/contents/tomb",
            },
            {
                "title": "개인 블로그",
                "contents": "추정 이야기",
                "url": "https://example.com/blog",
            },
        ]

    async def fake_fetch(url, *, query, title):
        assert "example.com" not in url
        return {
            "title": "국립경주박물관 - 천마총",
            "category": "공식 웹자료",
            "overview": "천마총은 피장자의 신원이 명확하게 밝혀지지 않은 고분이다.",
            "homepage": url,
            "source_url": url,
            "source_name": "국립경주박물관",
        }

    monkeypatch.setattr(service.openai, "embeddings", fake_embeddings)
    monkeypatch.setattr(service.openai, "answer_with_context", fake_answer)
    monkeypatch.setattr(service.heritage, "contexts", fake_heritage_contexts)
    monkeypatch.setattr(service.daum, "web_documents", fake_daum_documents)
    monkeypatch.setattr(service.trusted_web, "fetch_document", fake_fetch)

    result = asyncio.run(service.search("천마총은 누구 무덤이야?", 5))

    assert result.grounded is True
    assert "피장자" in result.answer
    assert len(answer_calls) == 2
    assert any("국가유산청" in c["title"] for c in answer_calls[1])
    assert any("국립경주박물관" in c["title"] for c in answer_calls[1])
    assert all("example.com" not in (hit.homepage or "") for hit in result.hits)
    assert any("국립경주박물관" in hit.title for hit in result.hits)

def test_external_subject_variants_remove_parenthetical_place_parent():
    variants = RagService._external_subject_variants(
        "천마총(대릉원)",
        "천마총은 누구 무덤이야?",
    )
    assert "천마총" in variants
    assert "대릉원" in variants


def test_trusted_daum_snippet_is_used_when_original_fetch_fails(monkeypatch):
    db = _session()
    db.add(
        PlaceRecord(
            place_id="tm-2",
            title="천마총(대릉원)",
            category="관광지",
            latitude=35.0,
            longitude=129.0,
            data={
                "place_id": "tm-2",
                "title": "천마총(대릉원)",
                "category": "관광지",
                "latitude": 35.0,
                "longitude": 129.0,
                "overview": "경주 대릉원에 있는 고분",
            },
            embedding=[0.0, 1.0],
        )
    )
    db.commit()

    service = RagService(_settings(), db)
    answer_calls = []
    heritage_calls = []
    search_calls = []

    async def fake_embeddings(texts):
        return [[0.0, 1.0]]

    async def fake_answer(query, contexts, history=None):
        answer_calls.append(contexts)
        if len(answer_calls) == 1:
            return "확인할 수 있는 자료가 부족합니다."
        assert any("무덤의 주인공이 누구인지는 알 수 없" in c.get("overview", "") for c in contexts)
        return "국립경주박물관 자료에 따르면 천마총의 무덤 주인공은 정확히 알 수 없습니다."

    async def fake_heritage_contexts(title, limit=2):
        heritage_calls.append(title)
        return []

    async def fake_daum_documents(query, limit=10):
        search_calls.append(query)
        if "천마총" not in query:
            return []
        return [
            {
                "title": "천마총 금관 - 국립경주박물관",
                "contents": "무덤의 주인공이 누구인지는 알 수 없지만 발견된 문화를 통해 신라의 황금 문화를 알 수 있어요.",
                "url": "https://gyeongju.museum.go.kr/kor/html/sub03/0305.html?file_id=7679&mode=D&no=4968",
            }
        ]

    async def fake_fetch(url, *, query, title):
        return None

    monkeypatch.setattr(service.openai, "embeddings", fake_embeddings)
    monkeypatch.setattr(service.openai, "answer_with_context", fake_answer)
    monkeypatch.setattr(service.heritage, "contexts", fake_heritage_contexts)
    monkeypatch.setattr(service.daum, "web_documents", fake_daum_documents)
    monkeypatch.setattr(service.trusted_web, "fetch_document", fake_fetch)

    result = asyncio.run(service.search("천마총은 누구 무덤이야?", 5))

    assert result.grounded is True
    assert "정확히 알 수 없습니다" in result.answer
    assert "천마총" in heritage_calls
    assert any("국립경주박물관" in query for query in search_calls)
    assert any(hit.category == "공식 웹검색 요약" for hit in result.hits)

