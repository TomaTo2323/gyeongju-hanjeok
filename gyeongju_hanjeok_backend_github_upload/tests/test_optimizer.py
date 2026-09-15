from app.optimizer import optimize_courses
from app.schemas import Place, RecommendRequest


def places():
    return [
        Place(place_id=str(i), title=f"장소{i}", category="관광지" if i % 2 else "문화시설", latitude=35.85 + i * 0.002, longitude=129.22 + i * 0.002, congestion_score=float(i % 8), overview="역사 문화")
        for i in range(1, 9)
    ]


def test_optimizer_returns_pareto_courses():
    request = RecommendRequest(latitude=35.8562, longitude=129.2247, available_minutes=300, preferences=["역사"], seed=1)
    result = optimize_courses(places(), request, population_size=20, generations=5, max_places=5)
    assert 1 <= len(result) <= 3
    assert all(2 <= len(item.genes) <= 5 for item in result)
    assert all(item.total_minutes > 0 for item in result)
