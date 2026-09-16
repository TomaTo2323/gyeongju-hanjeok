from app.geo import estimate_travel_minutes, haversine_km, latlon_to_kma_grid


def test_haversine_zero():
    assert haversine_km(35.8, 129.2, 35.8, 129.2) == 0


def test_estimate_minutes():
    assert estimate_travel_minutes(4, "walking") == 60
    assert estimate_travel_minutes(28, "car") == 60


def test_kma_grid_gyeongju_reasonable():
    x, y = latlon_to_kma_grid(35.8562, 129.2247)
    assert 80 <= x <= 110
    assert 70 <= y <= 110
