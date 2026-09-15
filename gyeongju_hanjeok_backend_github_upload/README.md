# 경주한적 Backend

혼잡도를 코스 생성의 제약조건으로 사용하는 경주 실시간 맞춤 관광 코스 백엔드입니다. **더미 관광 데이터는 포함하지 않으며**, 한국관광공사·기상청·카카오·네이버·YouTube·OpenAI의 실제 API를 호출합니다.

## 1. API 활용신청

공공데이터포털에서 아래 API를 각각 활용신청해야 합니다. 같은 계정의 일반 인증키를 `PUBLIC_DATA_SERVICE_KEY`에 입력하더라도, 각 서비스 활용승인이 없으면 해당 호출은 거부될 수 있습니다.

- 한국관광공사 국문 관광정보 서비스 `KorService1`
- 관광지 집중률 방문자 추이 예측 정보 `TatsCnctrRateService`
- 관광지별 연관 관광지 정보 `TarRlteTarService1`
- 기초지자체 중심 관광지 정보 `LocgoHubTarService1`
- 관광사진 정보 `PhotoGalleryService1`
- 기상청 단기예보 조회서비스 `VilageFcstInfoService_2.0`

별도 발급:

- 카카오디벨로퍼스 REST API 키
- OpenAI API 키
- 네이버 검색 API 및 DataLab API Client ID/Secret
- YouTube Data API v3 키

> 카카오 도보 길찾기는 제휴 파트너 승인이 필요한 규격일 수 있습니다. 기본 설정은 자동차 길찾기에 카카오 API를 사용하고, 도보는 좌표 기반 실제 거리와 보행속도로 계산합니다. 제휴 승인을 받았다면 `.env`에서 `ENABLE_KAKAO_WALKING_API=true`로 바꾸세요.

## 2. Windows 실행

1. `.env.example`을 `.env`로 복사합니다.
2. `.env`의 키 항목을 채웁니다.
3. `run_backend.bat`을 더블클릭합니다.
4. 브라우저에서 `http://127.0.0.1:8000/docs`를 엽니다.

PowerShell에서 직접 실행할 수도 있습니다.

```powershell
Copy-Item .env.example .env
.\run_backend.ps1
```

## 3. 주요 API

- `GET /health`: 키 설정 및 DB 상태
- `GET /api/v1/places`: 현 위치 주변 실제 관광공사 장소 조회
- `GET /api/v1/places/{place_id}`: 관광지 상세
- `POST /api/v1/courses/recommend`: 혼잡도·날씨·거리·선호 기반 NSGA-II 코스 1~3개 생성
- `POST /api/v1/courses/modify`: 자연어 장소 제외·추가·교체·순서/조건 변경
- `POST /api/v1/courses/recalculate`: 다음 관광지 혼잡도 8점 초과 시 대체지 탐색
- `GET /api/v1/content/{place_id}?title=첨성대`: 네이버 블로그·YouTube 콘텐츠
- `GET /api/v1/congestion/{place_name}`: 관광지 집중률 원본 조회
- `GET /api/v1/weather/current`: 현재 기상청 실황 조회
- `GET /api/v1/insights/related`: 연관 관광지 데이터
- `GET /api/v1/insights/hubs`: 중심 관광지 데이터
- `POST /api/v1/rag/search`: 동기화된 공식 관광정보 기반 의미 검색·답변
- `POST /api/v1/etiquette/nearby`: 문화재 인근 위치 기반 에티켓
- `POST /api/v1/journeys`: 코스 시작
- `POST /api/v1/journeys/{id}/visits`: 50m 이내 10분 체류 방문 완료
- `POST /api/v1/admin/sync`: 경주 관광지 DB 및 RAG 임베딩 갱신

## 4. Flutter 연결 주소

- Android 에뮬레이터: `http://10.0.2.2:8000`
- iOS 시뮬레이터: `http://127.0.0.1:8000`
- 실제 휴대전화: `http://PC의-같은-WiFi-IP:8000`

## 5. 검증

```powershell
pip install -r requirements-dev.txt
pytest
python scripts/smoke_test.py
```

`smoke_test.py`는 서버를 먼저 실행한 뒤 사용하세요. 실제 API 승인이 완료되어야 코스 추천 통합 테스트가 성공합니다.

## 6. 중요한 제한

- API 키를 입력해도 **해당 API 활용신청 승인**이 없으면 호출되지 않습니다.
- 관광공사 집중률 데이터에 없는 관광지는 혼잡도 미확인으로 유지하며 임의의 실시간 값을 만들지 않습니다.
- 공식 API에서 리뷰/별점을 제공하지 않으므로 현재 추천식의 리뷰 항목은 중립값 0.5를 적용합니다. 실제 리뷰 공급자가 정해지면 해당 클라이언트만 교체하면 됩니다.
- 운영시간·입장료는 관광공사 상세 데이터 필드가 관광지 유형별로 다르므로 가능한 필드를 통합해서 처리합니다.

## Flutter 프론트 호환 API

통합본에는 `app/compat_api.py`가 추가되어 Flutter가 사용하는 `/places`, `/routes/recommend`, `/routes/recommend/refresh`, `/chat/modify-course`, `/contents/place/{id}` 등의 경로를 지원합니다. 기존 `/api/v1/...` 경로도 그대로 유지됩니다. 전체 실행 순서는 상위 폴더의 `README_MERGED.md`를 확인하세요.
