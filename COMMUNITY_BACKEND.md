# 경주한적 커뮤니티 백엔드

## 적용 범위

커뮤니티를 단순 게시판이 아니라 코스/관광지/혼잡도와 연결하기 위해 다음 3종 게시물을 지원합니다.

- `course`: 완료 코스 후기. 게시 당시 `course_snapshot`을 보존하고 별점/여행일을 저장합니다.
- `live`: 특정 관광지의 현재 혼잡/한적 현장 제보. `place_id`, `crowd_percent`, `observed_at`을 저장합니다.
- `travel`: 일반 여행 후기. 장소는 `related_place_ids`로 선택 연결할 수 있습니다.

모든 `/community/*` API는 로그인 후 `Authorization: Bearer <access_token>`이 필요합니다.

## DB 테이블

- `community_posts`: 공통 게시물 + 유형별 데이터
- `community_recommendations`: 사용자별 게시물 추천 1회
- `community_comments`: 댓글
- `community_saved_courses`: 코스 후기에서 저장한 코스 스냅샷

기존 `init_db()` 실행 시 새 테이블이 자동 생성됩니다. 기존 테이블의 컬럼은 변경하지 않았습니다.

## 주요 API

### 피드/게시글

- `POST /community/posts` 글 작성
- `GET /community/posts?post_type=course|live|travel&sort=latest|recommended` 피드
- `GET /community/posts/mine` 내가 쓴 글
- `GET /community/posts/{post_id}` 상세
- `PATCH /community/posts/{post_id}` 내 글 수정
- `DELETE /community/posts/{post_id}` 내 글 삭제

### 추천

- `POST /community/posts/{post_id}/recommend` 추천
- `DELETE /community/posts/{post_id}/recommend` 추천 취소

### 댓글

- `GET /community/posts/{post_id}/comments`
- `POST /community/posts/{post_id}/comments`
- `PATCH /community/comments/{comment_id}`
- `DELETE /community/comments/{comment_id}`

### 코스 후기 연결

- `POST /community/posts/{post_id}/save-course` 내 코스에 저장
- `DELETE /community/posts/{post_id}/save-course` 저장 취소
- `GET /community/saved-courses` 저장한 코스 목록
- `GET /community/posts/{post_id}/course-copy` 다른 사용자의 코스를 따라가기 위한 데이터 조회

`course-copy`는 과거 코스를 그대로 실행시키지 않습니다. 게시 당시 코스 순서(`course_snapshot`)와 현재 DB의 장소 정보(`current_places`)를 함께 반환하며, 응답의 `requires_live_refresh=true`를 기준으로 프론트가 기존 코스 재계산/추천 API를 호출해 현재 혼잡도와 이동조건을 다시 반영하도록 설계했습니다.

### 현장 혼잡 제보

- `GET /community/places/{place_id}/live-summary?hours=2`

최근 제보 평균과 최근 최대 5개 제보를 반환합니다. 기존 공식/계산 혼잡도는 덮어쓰지 않고 `official_congestion_score`와 커뮤니티 평균을 별도로 반환합니다.

혼잡도 구간은 홈 화면 기준과 동일합니다.

- 0 이상 20 미만: 청색 / 매우 한적
- 20 이상 40 미만: 녹색 / 한적
- 40 이상 60 미만: 황색 / 보통
- 60 이상 80 미만: 홍색 / 혼잡
- 80 이상 100 이하: 적색 / 매우 혼잡

## 요청 예시

### 코스 후기

```json
{
  "post_type": "course",
  "title": "야경 중심 한적 코스 후기",
  "content": "저녁에 다녀왔는데 이동하기 편했고 월정교가 특히 좋았어요.",
  "image_urls": [],
  "course_rating": 4.5,
  "travel_date": "2026-09-15",
  "course_snapshot": {
    "course_id": "course-123",
    "title": "경주 야경 코스",
    "places": [
      {"place_id": "123", "title": "대릉원"},
      {"place_id": "456", "title": "월정교"}
    ]
  }
}
```

기존 `journeys`를 사용하는 경우 완료한 `source_journey_id`만 보내도 백엔드가 당시 코스를 스냅샷으로 저장할 수 있습니다.

### 지금 여기

```json
{
  "post_type": "live",
  "content": "지금 사진 찍기 편할 정도로 여유 있어요.",
  "place_id": "123",
  "crowd_percent": 25
}
```

`live` 제보는 최근 6시간 이내 관측만 등록할 수 있고, 피드 응답의 `live_is_recent`는 최근 2시간 기준입니다.

### 일반 여행 후기

```json
{
  "post_type": "travel",
  "title": "비 오는 날 경주 여행",
  "content": "교촌마을 쪽이 조용해서 좋았어요.",
  "related_place_ids": ["123", "456"],
  "tags": ["비오는날", "야경"]
}
```

## 프론트 연결 기준

- 커뮤니티 상단: `GET /community/posts`의 `post_type` 필터 사용
- 게시물 추천/댓글: 각 전용 API 사용
- 코스 탭의 완료 코스 → 평가하기: `POST /community/posts` (`course`)
- 코스 후기의 `코스 저장`: `POST .../save-course`
- 코스 후기의 `이 코스 따라가기`: `GET .../course-copy` 후 기존 코스 재계산 API 연결
- 지도/관광지 상세의 현장 제보: `POST /community/posts` (`live`)
- 관광지 상세의 최근 현장 정보: `GET /community/places/{place_id}/live-summary`

## 테스트

추가 테스트: `tests/test_community.py`

검증 항목:

- 혼잡도 5단계 경계값
- 백엔드 Course 형태와 Flutter route/stops 형태에서 장소 ID 추출
- 코스 후기 작성 → 추천 → 댓글 → 저장 → 코스 따라가기 데이터
- 현장 제보 작성 → 최근 혼잡 요약

원본 프로젝트의 `tests/test_frontend_compat.py` 2개는 이번 수정 전 원본 ZIP에서도 동일하게 실패합니다. 이번 커뮤니티 작업에서는 기존 코스 변환 동작을 임의로 변경하지 않았습니다.

## 2026-09-15 혼잡도·코스 추천 통합 추가

이번 버전에서는 커뮤니티의 `지금 여기` 제보를 기존 혼잡도/코스 추천과 연결했습니다.

### 원칙
- `congestion_score`는 기존 V2.4.4 결과를 그대로 유지합니다.
- 최근 커뮤니티 제보가 있어도 V2.4.4의 가중치 자체는 변경하지 않습니다.
- 최근 2시간 제보는 별도의 `community_congestion_score`로 집계합니다.
- 코스 추천에서만 `routing_congestion_score`를 사용합니다.
- 커뮤니티 제보 영향은 신선도와 제보 수에 따라 증가하지만 최대 20%로 제한됩니다.

### 장소 응답에 추가된 필드
- `community_congestion_score`
- `community_report_count`
- `community_latest_observed_at`
- `routing_congestion_score`
- 프론트 호환 응답에는 `routing_quiet_score`도 포함됩니다.

### 코스 추천 반영
- 한적함 45% + 취향 30% + 출발지 거리 25% 비율은 그대로입니다.
- 한적함 45% 계산에만 `routing_congestion_score`가 사용됩니다.
- 혼잡도 우선 후보, 맛집/카페 보조 평가, 실시간 재조정에도 같은 코스용 혼잡도를 사용합니다.
- 탐색 반경은 전역 하드필터로 사용하지 않습니다. 코스 후반 비정상 장거리 이동은 이동수단별 안전 상한으로만 방지합니다.

### 코스 후기 → 같은 코스 재추천
`GET /community/posts/{post_id}/course-copy` 응답에 다음 값이 추가됩니다.
- `source_place_names`
- `include_food`
- `include_cafe`
- `suggested_available_minutes`
- `same_course_request_patch`

프론트는 `same_course_request_patch`에 현재 출발 좌표와 이동수단을 추가해 기존 `/routes/recommend`에 전달할 수 있습니다. 이렇게 하면 게시 당시 관광지는 `required_place_names`로 보존하면서 현재 V2.4.4 혼잡도, 최근 커뮤니티 제보, 현재 이동조건을 다시 반영해 코스를 계산할 수 있습니다.
