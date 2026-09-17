import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base, KnowledgeDocument, PlaceRecord
from app.schemas import RagHit, RagSearchResponse
from app.services import RagService


def _settings():
    return Settings(_env_file=None, openai_api_key="test", kakao_rest_api_key="test")


def _session():
    RagService._external_context_cache.clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[PlaceRecord.__table__, KnowledgeDocument.__table__])
    return Session(engine)


def test_external_official_response_is_preferred_over_local_direct_evidence(monkeypatch):
    db = _session()
    db.add(PlaceRecord(
        place_id="sg", title="석굴암 [유네스코 세계유산]", category="관광지",
        latitude=35.79, longitude=129.35,
        data={"place_id":"sg","title":"석굴암 [유네스코 세계유산]","category":"관광지","latitude":35.79,"longitude":129.35},
        embedding=[0.0, 1.0],
    ))
    db.add(KnowledgeDocument(
        doc_id="local", title="석굴암 창건 설화", category="역사·정체성",
        text="751년 김대성이 석굴암을 짓기 시작했다.", embedding=[0.0, 1.0],
    ))
    db.commit()
    service = RagService(_settings(), db)

    calls = []
    async def external(*args, **kwargs):
        calls.append(True)
        return RagSearchResponse(
            query="석굴암은 언제 만들어졌어?",
            answer="국가유산포털 기준 석굴암은 751년에 시작해 774년에 완공되었습니다.",
            grounded=True,
            hits=[RagHit(
                source_type="etiquette", place_id="https://heritage.go.kr/x",
                title="국가유산포털 - 석굴암", category="공식 웹자료", similarity=1.0,
                overview="751년 시작, 774년 완공", homepage="https://heritage.go.kr/x",
            )],
        )
    monkeypatch.setattr(service, "_external_fallback_response", external)

    result = asyncio.run(service.search("석굴암은 언제 만들어졌어?", 5))
    assert calls == [True]
    assert "774년" in result.answer
    assert result.hits[0].title.startswith("국가유산포털")


def test_daum_original_query_and_intent_query_are_both_called(monkeypatch):
    service = RagService(_settings(), _session())
    calls = []
    async def no_heritage(title, limit=2):
        return []
    async def fake_daum(query, limit=12):
        calls.append(query)
        return []
    monkeypatch.setattr(service.heritage, "contexts", no_heritage)
    monkeypatch.setattr(service.daum, "web_documents", fake_daum)

    asyncio.run(service._external_official_contexts("천마총은 누구 무덤이야?", None))
    assert any("피장자" in q or "무덤 주인" in q for q in calls)
    assert "천마총은 누구 무덤이야?" in calls
    assert len(calls) >= 4
