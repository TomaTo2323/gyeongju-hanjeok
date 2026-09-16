from app.services import _rule_based_command


def test_remove_by_ordinal():
    result = _rule_based_command("두 번째 장소 빼줘", ["첨성대", "월정교", "교촌마을"])
    assert result["action"] == "remove"
    assert result["target"] == "월정교"


def test_condition():
    result = _rule_based_command("무료 장소와 야경 위주로 다시 짜줘", ["첨성대"])
    assert result["action"] == "set_condition"
    assert "무료" in result["conditions"]
    assert "야경" in result["conditions"]
