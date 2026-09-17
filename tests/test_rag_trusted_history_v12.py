from app.clients import TrustedWebSourceClient
from app.services import RagService


def test_aks_is_trusted_history_source():
    url = "https://encykorea.aks.ac.kr/Article/E0055857"
    assert TrustedWebSourceClient.trusted_source_name(url) == "한국민족문화대백과사전"


def test_tomb_owner_accepts_scholarly_inference_sentence():
    context = {
        "title": "한국민족문화대백과사전 - 천마총",
        "source_name": "한국민족문화대백과사전",
        "overview": (
            "천마총의 피장자와 연대에 관해서는 여러 견해가 있다. "
            "발굴보고서에서는 소지마립간과 지증왕을 이 고분의 피장자로 추정하고 있다. "
            "따라서 잠정적으로 지증왕의 왕릉으로 추정하여 둔다."
        ),
    }
    evidence = RagService._direct_evidence_sentences("천마총은 누구 무덤이야?", context)
    assert evidence
    answer = RagService._extractive_context_answer("천마총은 누구 무덤이야?", [context])
    assert "확정" in answer
    assert "지증왕" in answer
    assert "추정" in answer


def test_museum_royal_tomb_wording_is_direct_owner_evidence():
    context = {
        "title": "국립경주박물관 - 신라능묘 특별전 3 천마총",
        "source_name": "국립경주박물관",
        "overview": "전시는 1부 왕(족)의 무덤, 천마총과 2부 천마문 말다래와 장식 마구로 구성되어 있습니다.",
    }
    evidence = RagService._direct_evidence_sentences("천마총은 누구 무덤이야?", context)
    assert evidence
    answer = RagService._extractive_context_answer("천마총은 누구 무덤이야?", [context])
    assert "왕(족)의 무덤" in answer
    assert "특정 피장자의 이름" in answer
