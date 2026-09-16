from app.compat_api import _front_overview_text, _looks_like_location_description
from app.schemas import Place


def test_rich_tourism_overview_with_location_words_is_not_rejected():
    text = (
        "경주 내 대표적인 번화가로 영화관과 다수의 상점, 음식점이 위치해 있으며 "
        "버스킹과 퍼포먼스 공연 등 다양한 즐길거리와 볼거리가 있는 관광에 특화된 상권이다."
    )
    assert _looks_like_location_description(text) is False


def test_short_address_only_text_is_rejected():
    text = "경상북도 경주시 원효로 127에 위치한 곳입니다."
    assert _looks_like_location_description(text) is True


def test_front_overview_keeps_real_gold_street_description():
    text = (
        "경주 내 대표적인 번화가로 다수의 상점과 음식점이 자리하며 "
        "버스킹 등 다양한 즐길거리와 볼거리를 만날 수 있는 관광 상권이다."
    )
    place = Place(
        place_id="2992067",
        content_type_id="12",
        title="경주 금리단길",
        latitude=35.8420235954,
        longitude=129.2148312586,
        overview=text,
    )
    assert _front_overview_text(place) == text
