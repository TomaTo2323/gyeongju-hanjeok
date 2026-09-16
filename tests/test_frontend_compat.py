from datetime import datetime, timezone

from app.compat_api import FrontRecommendRequest, _course_to_front, _to_backend_request
from app.schemas import Course, CoursePlace, CourseType, TransportMode


def test_front_request_maps_to_backend_contract():
    body = FrontRecommendRequest(
        start_latitude=35.85,
        start_longitude=129.22,
        available_hours=3.5,
        transport_type="walk",
        preferred_categories=["문화유산"],
        avoid_paid=True,
        expected_include="첨성대, 월정교",
        expected_exclude="불국사",
        memo="부모님과 천천히",
    )

    mapped = _to_backend_request(body)

    assert mapped.latitude == 35.85
    assert mapped.available_minutes == 210
    assert mapped.transport == TransportMode.walking
    assert mapped.free_only is True
    assert mapped.required_place_names == ["첨성대", "월정교"]
    assert mapped.excluded_place_names == ["불국사"]
    assert "부모님과 천천히" in mapped.preferences


def test_backend_course_serializes_for_flutter():
    place = CoursePlace(
        place_id="123",
        content_type_id="12",
        title="첨성대",
        category="관광지",
        address="경주시",
        latitude=35.8347,
        longitude=129.2191,
        congestion_score=2.0,
        order=1,
        arrival_time=datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc),
        stay_minutes=45,
        travel_minutes_from_previous=10,
    )
    course = Course(
        course_id="course-1",
        title="한적 코스",
        type=CourseType.congestion_avoidance,
        total_minutes=55,
        total_distance_km=2.3,
        objective_values={},
        places=[place],
    )

    payload = _course_to_front(course)
    route = payload["route"]

    assert route["id"] == "course-1"
    assert route["average_quiet_score"] == 80
    assert route["stops"][0]["travel_minutes"] == 10
    assert route["stops"][0]["place"]["name"] == "첨성대"
