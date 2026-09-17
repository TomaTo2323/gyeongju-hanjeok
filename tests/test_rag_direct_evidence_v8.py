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


def test_seokguram_when_uses_local_direct_history_without_external(monkeypatch):
    db = _session()
    db.add(PlaceRecord(
        place_id="sg", title="석굴암 [유네스코 세계유산]", category="관광지",
        latitude=35.79, longitude=129.35,
        data={"place_id":"sg","title":"석굴암 [유네스코 세계유산]","category":"관광지","latitude":35.79,"longitude":129.35},
        embedding=[0.0, 1.0],
    ))
    db.add(KnowledgeDocument(
        doc_id="history-kimdaeseong", title="불국사·석굴암 창건 설화: 김대성의 두 부모 이야기",
        category="역사·정체성",
        text="751년(경덕왕 10년) 김대성이 전생의 부모를 위해 석굴암을 짓기 시작했다고 전합니다. 774년 김대성이 완성을 보지 못하고 세상을 떠나자 나라에서 공사를 마무리했습니다.",
        embedding=[0.0, 1.0],
    ))
    db.commit()
    service = RagService(_settings(), db)

    async def fail_external(*args, **kwargs):
        raise AssertionError("local direct evidence should answer before external API")
    monkeypatch.setattr(service, "_external_fallback_response", fail_external)

    result = asyncio.run(service.search("석굴암은 언제 만들어졌어?", 5))
    assert result.grounded is True
    assert "751년" in result.answer
    assert "774년" in result.answer


def test_external_direct_evidence_bypasses_llm(monkeypatch):
    service = RagService(_settings(), _session())

    async def fake_contexts(query, exact_place):
        return [{
            "title":"국립경주박물관 - 천마총",
            "category":"공식 웹자료",
            "overview":"천마총은 무덤의 주인공이 누구인지는 알 수 없는 신라시대 고분입니다.",
            "source_url":"https://gyeongju.museum.go.kr/tomb",
            "homepage":"https://gyeongju.museum.go.kr/tomb",
            "source_name":"국립경주박물관",
        }]
    async def fail_answer(*args, **kwargs):
        raise AssertionError("LLM should not be called when direct official evidence exists")
    monkeypatch.setattr(service, "_external_official_contexts", fake_contexts)
    monkeypatch.setattr(service.openai, "answer_with_context", fail_answer)

    result = asyncio.run(service._external_fallback_response("천마총은 누구 무덤이야?", [], None, budget_seconds=4.0))
    assert result is not None
    assert result.grounded is True
    assert "알 수 없" in result.answer


def test_intent_specific_daum_query_is_sent_first(monkeypatch):
    service = RagService(_settings(), _session())
    calls=[]
    async def no_heritage(title, limit=2):
        return []
    async def fake_daum(query, limit=12):
        calls.append(query)
        return []
    monkeypatch.setattr(service.heritage, "contexts", no_heritage)
    monkeypatch.setattr(service.daum, "web_documents", fake_daum)
    asyncio.run(service._external_official_contexts("천마총은 누구 무덤이야?", None))
    assert calls
    assert "피장자" in calls[0] or "무덤 주인" in calls[0]


def test_unrelated_subject_mention_is_rejected():
    service = RagService(_settings(), _session())
    context={
        "title":"경주시 문화도시 위원회 회의록",
        "overview":"2023년도 문화도시 사업을 논의했다. 관광지 목록에는 석굴암도 포함된다.",
    }
    assert service._context_directly_answers_query("석굴암은 언제 만들어졌어?", context) is False


def test_hwangnyongsa_builder_sentence_is_direct_evidence():
    service = RagService(_settings(), _session())
    context={
        "title":"경주 황룡사지",
        "overview":"선덕여왕 12년 자장의 권유로 9층 목탑을 짓기 시작했으며 백제의 장인 아비지에 의해 645년에 완공되었다.",
    }
    answer=service._extractive_context_answer("황룡사 9층 목탑은 누가 세웠어?", [context])
    assert "아비지" in answer
