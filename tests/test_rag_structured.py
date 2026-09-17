import asyncio

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Base, KnowledgeDocument, PlaceRecord
from app.schemas import ChatTurn
from app.services import RagService


def _session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[PlaceRecord.__table__, KnowledgeDocument.__table__],
    )
    return Session(engine)


def _settings():
    return Settings(_env_file=None, openai_api_key="test-key")


def test_structured_hours_uses_exact_place_without_embedding_or_llm(monkeypatch):
    db = _session()
    db.add(
        PlaceRecord(
            place_id="126160",
            title="경주 첨성대",
            category="관광지",
            latitude=35.8347,
            longitude=129.2191,
            data={
                "place_id": "126160",
                "title": "경주 첨성대",
                "category": "관광지",
                "latitude": 35.8347,
                "longitude": 129.2191,
                "operating_hours": "09:00~22:00",
                "address": "경상북도 경주시 첨성로 140-25",
            },
            embedding=None,
        )
    )
    db.commit()

    service = RagService(_settings(), db)

    async def no_refresh(record, fields):
        return record

    async def embeddings_must_not_run(texts):  # pragma: no cover - failure path
        raise AssertionError("structured query must not call OpenAI embeddings")

    monkeypatch.setattr(service, "_refresh_place_record", no_refresh)
    monkeypatch.setattr(service.openai, "embeddings", embeddings_must_not_run)

    result = asyncio.run(service.search("첨성대 몇 시까지 관람할 수 있어?", 5))

    assert result.grounded is True
    assert "09:00~22:00" in result.answer
    assert len(result.hits) == 1
    assert result.hits[0].title == "경주 첨성대"
    assert result.hits[0].similarity == 1.0


def test_structured_followup_resolves_place_from_recent_user_history(monkeypatch):
    db = _session()
    db.add(
        PlaceRecord(
            place_id="126160",
            title="첨성대",
            category="관광지",
            latitude=35.8347,
            longitude=129.2191,
            data={
                "place_id": "126160",
                "title": "첨성대",
                "category": "관광지",
                "latitude": 35.8347,
                "longitude": 129.2191,
                "parking": "인근 공영주차장 이용",
            },
            embedding=None,
        )
    )
    db.commit()

    service = RagService(_settings(), db)

    async def no_refresh(record, fields):
        return record

    monkeypatch.setattr(service, "_refresh_place_record", no_refresh)

    history = [
        ChatTurn(role="user", content="첨성대에 대해 알려줘"),
        ChatTurn(role="assistant", content="첨성대 안내입니다."),
    ]
    result = asyncio.run(service.search("거기 주차는 돼?", 5, history=history))

    assert result.grounded is True
    assert "인근 공영주차장 이용" in result.answer
    assert result.hits[0].title == "첨성대"


def test_general_rag_prioritizes_named_place_and_limits_knowledge_docs(monkeypatch):
    db = _session()
    db.add(
        PlaceRecord(
            place_id="126160",
            title="첨성대",
            category="관광지",
            latitude=35.8347,
            longitude=129.2191,
            data={
                "place_id": "126160",
                "title": "첨성대",
                "category": "관광지",
                "latitude": 35.8347,
                "longitude": 129.2191,
                "overview": "신라시대 천문 관측과 관련된 석조 건축물",
            },
            embedding=[0.0, 1.0],
        )
    )
    for index in range(10):
        db.add(
            KnowledgeDocument(
                doc_id=f"doc-{index}",
                title=f"일반 관광 안내 {index}",
                category="지식",
                text=f"경주 관광 일반 안내 문서 {index}",
                embedding=[1.0, 0.0],
            )
        )
    db.commit()

    service = RagService(_settings(), db)
    captured = {}

    async def fake_embeddings(texts):
        return [[0.0, 1.0]]

    async def fake_answer(query, contexts, history=None):
        captured["contexts"] = contexts
        return "첨성대 관련 답변"

    monkeypatch.setattr(service.openai, "embeddings", fake_embeddings)
    monkeypatch.setattr(service.openai, "answer_with_context", fake_answer)

    result = asyncio.run(service.search("첨성대 역사 알려줘", 5))

    assert result.hits[0].title == "첨성대"
    # top_k=5일 때 장소 최대 5 + 지식문서 최대 6개만 LLM에 전달한다.
    assert len(captured["contexts"]) == 7
    assert captured["contexts"][0]["title"] == "첨성대"


def test_structured_hours_falls_back_to_gyeongju_official_when_tourapi_missing(monkeypatch):
    db = _session()
    db.add(
        PlaceRecord(
            place_id="126160",
            title="경주 첨성대",
            category="관광지",
            latitude=35.8347,
            longitude=129.2191,
            data={
                "place_id": "126160",
                "title": "경주 첨성대",
                "category": "관광지",
                "latitude": 35.8347,
                "longitude": 129.2191,
                "operating_hours": None,
                "address": "경상북도 경주시 인왕동 839-1",
            },
            embedding=None,
        )
    )
    db.commit()

    service = RagService(_settings(), db)

    async def tour_detail_still_missing(place):
        return place

    async def official_info(title, requested_fields):
        assert "operating_hours" in requested_fields
        return {
            "operating_hours": "09:00-22:00(동절기 21:00까지), 연중무휴",
            "source_name": "경주시 경주문화관광",
            "source_url": "https://www.gyeongju.go.kr/tour/page.do?area_uid=47&cmd=2&mnu_uid=2292",
        }

    async def embeddings_must_not_run(texts):  # pragma: no cover - failure path
        raise AssertionError("structured query must not call OpenAI embeddings")

    monkeypatch.setattr(service.tour, "detail", tour_detail_still_missing)
    monkeypatch.setattr(service.official_tour, "place_info", official_info)
    monkeypatch.setattr(service.openai, "embeddings", embeddings_must_not_run)

    result = asyncio.run(service.search("첨성대 운영시간 알려줘", 5))

    assert result.grounded is True
    assert "경주시 경주문화관광 기준" in result.answer
    assert "09:00-22:00" in result.answer

    saved = db.get(PlaceRecord, "126160")
    assert saved is not None
    assert saved.data["operating_hours"].startswith("09:00-22:00")
    assert "operating_hours" in saved.data["official_fallback_fields"]
    assert saved.data["official_source_url"].startswith("https://www.gyeongju.go.kr/tour/")
