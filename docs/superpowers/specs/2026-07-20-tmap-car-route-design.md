# T map 자동차 경로 조회 설계

## 목표

LLM이 두 장소명을 전달하면 T map 자동차 경로 API를 이용해 실제 도로 거리, 예상 자동차 이동시간, 예상 택시비를 구조화된 데이터로 받을 수 있게 한다.

## 범위

- `get_car_route(from_place, to_place)` 공개 함수를 제공한다.
- 기존 `app/place_lookup.py`의 `resolve_place()`로 장소명을 좌표로 변환한다.
- T map 자동차 경로안내 API를 호출한다.
- 거리, 소요시간, 택시비를 LLM이 바로 사용할 수 있는 `dict`로 정규화한다.
- 환경변수 누락, 장소 검색 실패, API 통신 및 응답 오류를 구조화된 실패 결과로 반환한다.
- 경로 상세 좌표를 저장하거나 지도 UI를 구현하는 작업은 포함하지 않는다.

## 구조

새 모듈 `app/car_route.py`가 장소 조회, T map 요청, 응답 정규화를 담당한다. 기존 대중교통 계산 모듈과 분리해 각 이동수단의 책임을 명확히 하고, 향후 LLM 도구 등록 시 이 함수만 독립적으로 노출할 수 있게 한다.

외부 HTTP 호출은 작은 내부 함수로 분리한다. 공개 함수는 장소명 기반 인터페이스만 제공하고 내부 호출 함수는 좌표와 주입 가능한 HTTP 세션을 받아 네트워크 없이 단위 테스트할 수 있게 한다.

## 공개 인터페이스

```python
def get_car_route(from_place: str, to_place: str) -> dict:
    ...
```

성공 결과 예시:

```json
{
  "success": true,
  "from_place": {
    "name": "대전역",
    "lat": 36.3324,
    "lng": 127.4348
  },
  "to_place": {
    "name": "국립중앙과학관",
    "lat": 36.3741,
    "lng": 127.3751
  },
  "distance_meters": 8827,
  "distance_km": 8.8,
  "duration_seconds": 1104,
  "duration_minutes": 19,
  "taxi_fare_won": 10200,
  "calculated_at": "2026-07-20T12:00:00+09:00",
  "source": "TMAP"
}
```

`distance_meters`와 `duration_seconds`는 API 원본 정수값을 보존한다. `distance_km`는 소수 첫째 자리로 반올림하고, `duration_minutes`는 실제보다 짧게 안내하지 않도록 초 단위를 분 단위로 올림한다.

실패 결과 예시:

```json
{
  "success": false,
  "reason": "place_not_found",
  "message": "출발 장소를 찾을 수 없습니다.",
  "from_place": "알 수 없는 장소",
  "to_place": "국립중앙과학관"
}
```

실패 사유는 `invalid_input`, `place_not_found`, `missing_api_key`, `api_request_failed`, `invalid_api_response` 중 하나다. API 키와 외부 서비스의 원문 오류 본문은 결과에 포함하지 않는다.

## 설정과 보안

- API 키는 `TMAP_API_KEY` 환경변수에서 읽는다.
- `.env.example`에는 값이 없는 예시 항목만 추가한다.
- 실제 키를 소스, 테스트 픽스처, 로그, 오류 메시지에 기록하지 않는다.
- HTTP 요청 제한시간은 10초로 설정한다.

## 데이터 흐름

1. 입력이 비어 있지 않은 문자열인지 검증한다.
2. 출발지와 도착지를 `resolve_place()`로 조회한다.
3. `TMAP_API_KEY`를 확인한다.
4. 장소 좌표를 WGS84 경도·위도 순서로 T map 자동차 경로 API에 전송한다.
5. 첫 번째 경로 요약의 `totalDistance`, `totalTime`, `taxiFare`를 검증한다.
6. 원본 단위와 사용자 친화 단위를 함께 담아 반환한다.

## 오류 처리

외부 API의 타임아웃, 연결 오류, 비정상 HTTP 상태는 `api_request_failed`로 통합한다. 성공 HTTP 응답이어도 필수 요약 필드가 없거나 숫자로 해석할 수 없으면 `invalid_api_response`를 반환한다. 공개 함수는 예상 가능한 운영 오류를 예외로 노출하지 않아 LLM 호출 흐름을 중단시키지 않는다.

## 테스트

`tests/test_car_route.py`에서 다음 동작을 검증한다.

- 두 장소를 찾아 T map 요청 파라미터를 올바른 좌표 순서로 구성한다.
- T map 요약을 거리, 올림한 분 단위 시간, 택시비로 변환한다.
- 빈 입력을 거부한다.
- 출발지 또는 도착지를 찾지 못한 경우를 구분한다.
- API 키가 없을 때 네트워크 요청 없이 실패한다.
- HTTP 오류와 잘못된 응답을 안정적인 실패 계약으로 변환한다.

단위 테스트는 모의 HTTP 응답을 사용한다. 전체 테스트 통과 후 실제 키를 환경변수로 주입해 대전 내 두 장소에 대한 통합 호출을 한 번 실행하고, 키나 전체 응답을 출력하지 않은 채 성공 여부와 정규화된 요약만 확인한다.
