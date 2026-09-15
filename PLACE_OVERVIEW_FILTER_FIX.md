# 장소 소개 필터 수정

## 원인
기존 `_looks_like_location_description()`이 `위치한`, `위치해` 같은 표현을 포함한 정상 관광 소개까지 주소성 문장으로 오인해 제거할 수 있었습니다.
특히 금리단길처럼 공식 소개에 "상점/음식점이 위치해 있으며"라는 표현이 들어간 경우 소개가 비어 보일 수 있었습니다.

## 수정
- 위치 표현 하나만으로 소개문을 버리지 않음
- 관광/상권/역사/유적/볼거리/즐길거리/체험 등 실질적인 설명어가 있으면 정상 소개로 유지
- 짧은 주소 자체 설명만 제거
- `/places/{id}`의 `place` 객체 안에 `description`과 같은 값의 `overview` 별칭도 함께 반환

## 확인
PowerShell:
`$r.place.description`
`$r.place.overview`
`$r.place.homepage`
`$r.place.content_links | ConvertTo-Json -Depth 10`
