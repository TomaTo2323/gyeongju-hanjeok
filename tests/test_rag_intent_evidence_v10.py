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


def test_tomb_owner_rejects_artifact_page():
    context = {
        "title": "국가유산청 - 천마총 금관",
        "source_name": "국가유산청",
        "overview": "이 금관은 천마총에서 출토된 신라시대 금관으로 왕족 무덤에서 나온 대표 유물이다.",
    }
    assert RagService._context_directly_answers_query("천마총은 누구 무덤이야?", context) is False


def test_tomb_owner_accepts_explicit_unknown_owner():
    context = {
        "title": "국립경주박물관 - 천마총",
        "source_name": "국립경주박물관",
        "overview": "천마총은 대릉원의 고분으로, 무덤의 주인인 피장자는 아직 밝혀지지 않았다.",
    }
    answer = RagService._extractive_context_answer("천마총은 누구 무덤이야?", [context])
    assert "피장자" in answer
    assert "밝혀지지" in answer


def test_builder_requires_actual_builder_relation():
    irrelevant = {
        "title": "국립문화유산연구원 - 황룡사 9층 목탑 복원 연구",
        "source_name": "국립문화유산연구원",
        "overview": "황룡사 9층 목탑의 구조와 복원기술을 분석한 연구 자료이다.",
    }
    direct = {
        "title": "경주시 - 황룡사 9층 목탑",
        "source_name": "경주시",
        "overview": "황룡사 9층 목탑은 자장의 건의로 선덕여왕 때 추진되었으며 백제 장인 아비지에 의해 건립되었다.",
    }
    assert RagService._context_directly_answers_query("황룡사 9층 목탑은 누가 세웠어?", irrelevant) is False
    assert RagService._context_directly_answers_query("황룡사 9층 목탑은 누가 세웠어?", direct) is True


def test_date_answer_keeps_start_and_completion_years():
    context = {
        "title": "국가유산포털 - 석굴암",
        "source_name": "국가유산포털",
        "overview": "석굴암은 751년에 김대성이 창건을 시작하였다. 774년에 나라에서 공사를 마무리하여 완성하였다.",
    }
    answer = RagService._extractive_context_answer("석굴암은 언제 만들어졌어?", [context])
    assert "751년" in answer
    assert "774년" in answer


def test_daum_page_fetch_is_compared_against_khs_artifact(monkeypatch):
    service = RagService(_settings(), _session())

    async def heritage(title, limit=4):
        return [{
            "title": "국가유산청 - 천마총 금관",
            "category": "국가유산 공식자료",
            "overview": "천마총에서 출토된 금관이다.",
            "source_url": "https://www.khs.go.kr/artifact",
            "source_name": "국가유산청",
        }]

    async def daum(query, limit=15):
        return [{
            "title": "천마총 발굴과 피장자 - 국립경주박물관",
            "contents": "천마총 피장자",
            "url": "https://gyeongju.museum.go.kr/tomb-owner",
        }]

    async def fetch(url, *, query, title):
        return {
            "title": "국립경주박물관 - 천마총",
            "category": "공식 웹자료",
            "overview": "천마총은 대릉원의 고분이며 무덤의 주인인 피장자는 현재까지 밝혀지지 않았다.",
            "source_url": url,
            "homepage": url,
            "source_name": "국립경주박물관",
        }

    monkeypatch.setattr(service.heritage, "contexts", heritage)
    monkeypatch.setattr(service.daum, "web_documents", daum)
    monkeypatch.setattr(service.trusted_web, "fetch_document", fetch)
    contexts = asyncio.run(service._external_official_contexts("천마총은 누구 무덤이야?", None))
    assert contexts
    assert contexts[0]["source_name"] == "국립경주박물관"
    assert "피장자" in contexts[0]["overview"]
