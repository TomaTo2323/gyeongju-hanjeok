from app.config import get_settings

s = get_settings()
checks = {
    "PUBLIC_DATA_SERVICE_KEY": bool(s.public_data_service_key),
    "KAKAO_REST_API_KEY": bool(s.kakao_rest_api_key),
    "KMA_SERVICE_KEY": bool(s.kma_service_key),
    "OPENAI_API_KEY": bool(s.openai_api_key),
    "NAVER_CLIENT_ID/SECRET": bool(s.naver_client_id and s.naver_client_secret),
    "YOUTUBE_API_KEY": bool(s.youtube_api_key),
}
print("\n[경주한적 API 설정 확인]")
for name, ok in checks.items():
    print(f"- {name}: {'OK' if ok else '미설정'}")
print("\n미설정 연동은 해당 기능 호출 시 503 또는 빈 콘텐츠로 표시됩니다. 서버 자체는 실행됩니다.\n")
