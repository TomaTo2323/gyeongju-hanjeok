# 장소 소개 검색 보강

## 문제
TourAPI overview가 비어 있는 장소에서 NAVER 웹문서 검색만으로 공식 소개 페이지를 찾지 못하면 프론트에 장소 소개가 비어 있었다.

## 수정
- 기존 TourAPI overview 최우선 유지
- NAVER 지역검색 description 보조 유지
- Kakao/Daum 웹문서 검색 API를 공식 페이지 검색에 추가
- 경주문화관광, 국가유산청, 대한민국 구석구석, 한국민족문화대백과사전 등 신뢰 도메인만 소개 후보로 사용
- Kakao/Daum과 NAVER 검색 결과를 합쳐 공식 설명 스니펫 선택
- 검색 로그 `[PLACE OVERVIEW SEARCH]` 추가
- 정확한 근거가 없으면 유형명만으로 소개문을 생성하지 않는 원칙 유지

## 기대 흐름
TourAPI overview -> NAVER 지역검색 -> Kakao/Daum + NAVER 공식 웹문서 -> 블로그 복수 합의 -> 미확인
