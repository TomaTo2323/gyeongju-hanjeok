import asyncio

from app.clients import GyeongjuOfficialTourClient
from app.config import Settings


def _settings():
    return Settings(
        _env_file=None,
        naver_client_id="test-id",
        naver_client_secret="test-secret",
    )


def test_official_parser_extracts_cheomseongdae_visit_fields():
    client = GyeongjuOfficialTourClient(_settings())
    sample = """
    <html><body>
      <h1>첨성대</h1>
      <ul>
        <li>주소 경북 경주시 인왕동 839-1</li>
        <li>관람시간 : 09:00 -22:00(동절기 21:00까지), 연중무휴</li>
        <li>관람료 : 무료</li>
        <li>주차정보 : 천마총 노상주차장, 쪽샘임시주차장(무료)</li>
      </ul>
    </body></html>
    """
    fields = client._parse_fields(
        sample,
        {"operating_hours", "rest_date", "fee_text", "parking", "address"},
    )

    assert fields["operating_hours"].startswith("09:00 -22:00")
    assert fields["rest_date"] == "연중무휴"
    assert fields["fee_text"] == "무료"
    assert "쪽샘임시주차장" in fields["parking"]
    assert "인왕동 839-1" in fields["address"]


def test_official_client_uses_only_current_gyeongju_tour_pages(monkeypatch):
    client = GyeongjuOfficialTourClient(_settings())

    async def fake_web_documents(query, limit=10):
        return [
            {
                "title": "첨성대 - 경주문화관광",
                "url": "https://www.gyeongju.go.kr/tour/page.do?area_uid=47&cmd=2&mnu_uid=2292",
                "description": "첨성대 관람시간 관람료 주차정보",
            },
            {
                "title": "오래된 백업 페이지",
                "url": "https://www.gyeongju.go.kr/tour_bak/page.do?mnu_uid=2297",
                "description": "첨성대",
            },
            {
                "title": "블로그",
                "url": "https://example.com/cheomseongdae",
                "description": "첨성대",
            },
        ]

    async def fake_get_text(url):
        assert "/tour_bak/" not in url
        assert "gyeongju.go.kr/tour/" in url
        return """
        <html><body><h1>첨성대</h1>
        <li>관람시간 : 09:00-22:00(동절기 21:00까지), 연중무휴</li>
        <li>관람료 : 무료</li>
        </body></html>
        """

    monkeypatch.setattr(client.naver, "web_documents", fake_web_documents)
    monkeypatch.setattr(client, "_get_text", fake_get_text)

    result = asyncio.run(
        client.place_info("경주 첨성대", {"operating_hours", "fee_text"})
    )

    assert result["operating_hours"].replace(" ", "").startswith("09:00-22:00")
    assert result["fee_text"] == "무료"
    assert result["source_name"] == "경주시 경주문화관광"
    assert result["source_url"].startswith("https://www.gyeongju.go.kr/tour/")


def test_official_client_direct_seed_works_without_naver_results(monkeypatch):
    client = GyeongjuOfficialTourClient(_settings())

    async def empty_web_documents(query, limit=10):
        return []

    async def fake_get_text(url):
        assert url == (
            "https://www.gyeongju.go.kr/tour/"
            "page.do?area_uid=47&cmd=2&mnu_uid=2292"
        )
        return """
        <html><body><h1>첨성대</h1>
        <li>관람시간 : 09:00 -22:00(동절기 21:00까지), 연중무휴</li>
        <li>관람료 : 무료</li>
        <li>주차정보 : 천마총 노상주차장, 쪽샘임시주차장(무료)</li>
        </body></html>
        """

    monkeypatch.setattr(client.naver, "web_documents", empty_web_documents)
    monkeypatch.setattr(client, "_get_text", fake_get_text)

    result = asyncio.run(
        client.place_info("경주 첨성대", {"operating_hours", "fee_text", "parking"})
    )

    assert result["operating_hours"].replace(" ", "").startswith("09:00-22:00")
    assert result["fee_text"] == "무료"
    assert "쪽샘임시주차장" in result["parking"]
    assert result["source_url"].startswith("https://www.gyeongju.go.kr/tour/")


def test_official_verified_snapshot_works_when_network_is_unavailable(monkeypatch):
    client = GyeongjuOfficialTourClient(_settings())

    async def fail_web_documents(query, limit=10):
        raise AssertionError("verified snapshot should return before NAVER search")

    async def fail_get_text(url):
        raise AssertionError("verified snapshot should return before page fetch")

    monkeypatch.setattr(client.naver, "web_documents", fail_web_documents)
    monkeypatch.setattr(client, "_get_text", fail_get_text)

    result = asyncio.run(
        client.place_info(
            "경주 첨성대",
            {"operating_hours", "rest_date", "fee_text", "parking", "address"},
        )
    )

    assert result["operating_hours"].replace(" ", "").startswith("09:00-22:00")
    assert result["rest_date"] == "연중무휴"
    assert result["fee_text"] == "무료"
    assert "쪽샘임시주차장" in result["parking"]
    assert result["address"] == "경북 경주시 인왕동 839-1"
    assert result["source_name"] == "경주시 경주문화관광"
    assert result["source_mode"] == "verified_official_snapshot"


def test_official_domain_search_snippet_is_last_resort_when_page_fetch_fails(monkeypatch):
    client = GyeongjuOfficialTourClient(_settings())

    async def fake_web_documents(query, limit=10):
        return [{
            "title": "테스트명소 - 경주문화관광",
            "url": "https://www.gyeongju.go.kr/tour/page.do?area_uid=999&cmd=2&mnu_uid=2292",
            "description": "테스트명소 관람시간: 09:00-18:00 관람료: 무료",
        }]

    async def fail_get_text(url):
        from app.clients import IntegrationError
        raise IntegrationError("gyeongju_official", "blocked")

    monkeypatch.setattr(client.naver, "web_documents", fake_web_documents)
    monkeypatch.setattr(client, "_get_text", fail_get_text)

    result = asyncio.run(
        client.place_info("테스트명소", {"operating_hours", "fee_text"})
    )

    assert result["operating_hours"].replace(" ", "").startswith("09:00-18:00")
    assert result["source_url"].startswith("https://www.gyeongju.go.kr/tour/")
