import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base, KnowledgeDocument, PlaceRecord
from app.services import RagService


def _settings():
    return Settings(_env_file=None, openai_api_key="test", kakao_rest_api_key="test")


def _session():
    RagService._external_context_cache.clear()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[PlaceRecord.__table__, KnowledgeDocument.__table__])
    return Session(engine)


def test_tomb_owner_accepts_official_royal_tomb_wording():
    service = RagService(_settings(), _session())
    context = {
        "title": "국립경주박물관 - 천마총 특별전",
        "overview": "전시는 1부 왕(족)의 무덤, 천마총과 2부 천마문 말다래로 구성됩니다.",
        "source_name": "국립경주박물관",
    }
    assert service._context_directly_answers_query("천마총은 누구 무덤이야?", context) is True
    answer = service._extractive_context_answer("천마총은 누구 무덤이야?", [context])
    assert "왕(족)의 무덤" in answer
    assert "특정 피장자의 이름" in answer


def test_tomb_owner_queries_combine_intent_and_institution(monkeypatch):
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
    assert any("왕족 무덤" in q and "국립경주박물관" in q for q in calls)
    assert any("피장자" in q and "국립문화유산연구원" in q for q in calls)


def test_subject_match_without_snippet_evidence_is_still_fetched(monkeypatch):
    service = RagService(_settings(), _session())

    async def no_heritage(title, limit=2):
        return []

    docs = []
    for i in range(9):
        docs.append({
            "title": f"기타 공식자료 {i}",
            "url": f"https://khs.go.kr/example/{i}",
            "contents": "천마총 관련 일반 소개 자료입니다.",
        })
    docs.append({
        "title": "국립경주박물관 천마총 특별전",
        "url": "https://gyeongju.museum.go.kr/tomb-owner",
        "contents": "천마총 특별전 안내입니다.",
    })

    async def fake_daum(query, limit=12):
        return docs

    fetched_urls = []

    async def fake_fetch(url, query, title):
        fetched_urls.append(url)
        if url.endswith("/tomb-owner"):
            return {
                "title": "국립경주박물관 - 천마총 특별전",
                "category": "공식 웹자료",
                "overview": "전시는 1부 왕(족)의 무덤, 천마총으로 구성됩니다.",
                "source_url": url,
                "homepage": url,
                "source_name": "국립경주박물관",
            }
        return None

    monkeypatch.setattr(service.heritage, "contexts", no_heritage)
    monkeypatch.setattr(service.daum, "web_documents", fake_daum)
    monkeypatch.setattr(service.trusted_web, "fetch_document", fake_fetch)

    contexts = asyncio.run(service._external_official_contexts("천마총은 누구 무덤이야?", None))
    assert "https://gyeongju.museum.go.kr/tomb-owner" in fetched_urls
    assert contexts
    assert contexts[0]["source_name"] == "국립경주박물관"
