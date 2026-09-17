import asyncio
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base, KnowledgeDocument, PlaceRecord
from app.services import RagService


def _settings():
    return Settings(_env_file=None, openai_api_key="test-openai", kakao_rest_api_key="test-kakao")


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine, tables=[PlaceRecord.__table__, KnowledgeDocument.__table__])
    return Session(engine)


def test_external_searches_are_parallel_and_limited(monkeypatch):
    service = RagService(_settings(), _session())

    async def slow_heritage(title, limit=2):
        await asyncio.sleep(0.15)
        return []

    async def slow_daum(query, limit=12):
        await asyncio.sleep(0.15)
        return [{
            "title": "석굴암 - 국가유산청",
            "contents": "석굴암은 신라 경덕왕 때 김대성이 창건을 시작하였다.",
            "url": "https://www.khs.go.kr/test/seokguram",
        }]

    async def slow_fetch(url, *, query, title):
        await asyncio.sleep(0.15)
        return {
            "title": "국가유산청 - 석굴암",
            "category": "공식 웹자료",
            "overview": "석굴암은 751년에 김대성이 창건을 시작하여 774년에 완성하였다.",
            "homepage": url,
            "source_url": url,
            "source_name": "국가유산청",
        }

    monkeypatch.setattr(service.heritage, "contexts", slow_heritage)
    monkeypatch.setattr(service.daum, "web_documents", slow_daum)
    monkeypatch.setattr(service.trusted_web, "fetch_document", slow_fetch)

    started = time.perf_counter()
    contexts = asyncio.run(service._external_official_contexts("석굴암은 언제 만들어졌어?", None))
    elapsed = time.perf_counter() - started

    assert contexts
    # 2 heritage + 3 Daum이 순차라면 0.75초 이상 + fetch가 필요하지만,
    # 병렬 구조에서는 검색 0.15초 + fetch 0.15초 수준이어야 합니다.
    assert elapsed < 0.65
    assert len(contexts) <= 3


def test_history_question_prefetches_external_while_internal_answer_runs(monkeypatch):
    db = _session()
    db.add(PlaceRecord(
        place_id="sg-1",
        title="석굴암",
        category="관광지",
        latitude=35.79,
        longitude=129.35,
        data={
            "place_id": "sg-1",
            "title": "석굴암",
            "category": "관광지",
            "latitude": 35.79,
            "longitude": 129.35,
            "overview": "통일신라 석굴 사원",
        },
        embedding=[0.0, 1.0],
    ))
    db.commit()
    service = RagService(_settings(), db)
    answer_calls = 0

    async def embeddings(texts):
        await asyncio.sleep(0.05)
        return [[0.0, 1.0]]

    async def answer(query, contexts, history=None):
        nonlocal answer_calls
        answer_calls += 1
        await asyncio.sleep(0.15)
        if answer_calls == 1:
            return "확인할 수 있는 자료가 부족합니다."
        return "국가유산청 자료에 따르면 석굴암은 751년에 창건을 시작해 774년에 완성되었습니다."

    async def heritage(title, limit=2):
        await asyncio.sleep(0.15)
        return [{
            "title": "국가유산청 - 석굴암",
            "category": "국가유산 공식자료",
            "overview": "석굴암은 751년에 김대성이 창건을 시작하여 774년에 완성하였다.",
            "homepage": "https://www.khs.go.kr/test/seokguram",
            "source_url": "https://www.khs.go.kr/test/seokguram",
            "source_name": "국가유산청",
        }]

    async def daum(query, limit=12):
        await asyncio.sleep(0.15)
        return []

    monkeypatch.setattr(service.openai, "embeddings", embeddings)
    monkeypatch.setattr(service.openai, "answer_with_context", answer)
    monkeypatch.setattr(service.heritage, "contexts", heritage)
    monkeypatch.setattr(service.daum, "web_documents", daum)

    started = time.perf_counter()
    result = asyncio.run(service.search("석굴암은 언제 만들어졌어?", 5))
    elapsed = time.perf_counter() - started

    assert result.grounded is True
    assert "751년" in result.answer
    assert answer_calls == 2
    assert elapsed < 0.8
