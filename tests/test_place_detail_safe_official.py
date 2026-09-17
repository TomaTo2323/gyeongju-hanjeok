from app.clients import GyeongjuOfficialTourClient
from app.compat_api import _place_detail_facts_from_official_text


def test_official_parser_does_not_treat_prose_as_parking():
    facts = _place_detail_facts_from_official_text(
        "테스트 장소",
        "테스트 장소 소개\n주차장에서 정문으로 향하는 길의 양쪽에 벚나무 숲이 조성되어 있습니다.",
    )
    assert "parking" not in facts


def test_official_parser_accepts_explicit_parking_label():
    facts = _place_detail_facts_from_official_text(
        "신라왕경숲",
        "신라왕경숲 정보\n이용시간 : 이용시간 제한 없음\n편의시설 : 무료 주차장, 공중화장실",
    )
    assert facts["operating_hours"] == "이용시간 제한 없음"
    assert facts["parking"] == "무료 주차장 이용"


def test_client_label_parser_requires_line_start():
    lines = [
        "주차장에서 정문으로 향하는 길에 벚나무가 있습니다.",
        "주차시설 : 무료 주차장",
    ]
    value = GyeongjuOfficialTourClient._extract_labeled_value(
        lines,
        GyeongjuOfficialTourClient.FIELD_LABELS["parking"],
    )
    assert value == "무료 주차장"


def test_silla_forest_verified_snapshot_exists():
    facts = GyeongjuOfficialTourClient.VERIFIED_OFFICIAL_FACTS["신라왕경숲"]
    assert facts["operating_hours"] == "이용시간 제한 없음"
    assert facts["rest_date"] == "연중무휴"
    assert "주차장" in facts["parking"]
