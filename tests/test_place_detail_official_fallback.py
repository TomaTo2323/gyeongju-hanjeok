from app.compat_api import _place_detail_facts_from_official_text


def test_gyeongju_forest_official_text_parser():
    text = (
        "#8 신라왕경숲\n"
        "신라시대 북촌의 범람을 막기 위해 자연 치수 장치로 오리수라는 숲을 조성했다. "
        "보문관광단지 진입로 인근에 있는 신라왕경숲은 바로 그 오리수를 재현해 조성한 숲이다.\n"
        "신라왕경숲 정보\n"
        "위치 : 경주시 구황동 885-6\n"
        "이용시간 : 이용시간 제한 없음\n"
        "이용료 : 무료\n"
        "주차정보 : 신라왕경숲 주차장(무료) 이용\n"
    )
    facts = _place_detail_facts_from_official_text("신라왕경숲", text)
    assert facts["operating_hours"] == "이용시간 제한 없음"
    assert facts["fee_text"] == "무료"
    assert "주차장" in facts["parking"]
    assert "오리수" in facts["overview"]


def test_no_invented_visit_info():
    facts = _place_detail_facts_from_official_text(
        "테스트장소",
        "테스트장소는 산책하기 좋은 장소입니다.",
    )
    assert "operating_hours" not in facts
    assert "fee_text" not in facts
    assert "parking" not in facts
