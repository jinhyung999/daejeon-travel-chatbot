# 소품샵 핵심 정보 보강 설계

## 목적

`place.category='giftshop'`인 소품샵 33건에 대해 전화번호, 영업시간, 휴무일을 한 번 정확하게 보강한다. OpenAI API는 사용하지 않는다. 자동 수집 결과를 바로 DB에 기록하지 않고 사람이 출처를 확인한 승인 데이터만 반영한다.

기존 `2026-07-22-giftshop-collection-and-recommend-enrichment-design.md`는 수정하지 않는다. 이 문서는 현재 남아 있는 소품샵의 핵심 정보 보강만 다룬다.

## 현재 상태

- 오분류 8건을 삭제해 소품샵은 33건이다.
- 33건 모두 `tel`, `open_time`, `close_day`가 NULL이다.
- 모든 소품샵에 이름, 주소, 좌표가 있어 Kakao Local API 결과와 교차 검증할 수 있다.
- `.env`에 `KAKAO_REST_API_KEY`가 설정되어 있다.
- 기존 `extra_json.naver_link`는 Instagram 16건, 스마트스토어 6건, 독립 홈페이지 3건, YouTube 1건이며 링크가 없는 곳은 6건이다.

## 범위

### 핵심 필드

1. `tel`
2. `open_time`
3. `close_day`

### 선택 정보

검수 중 명확하게 확인되는 홈페이지나 참고 메모는 `extra_json.detail_enrichment`에 저장할 수 있다. 주차, 콘셉트 태그, 포토스팟, 체험 여부 및 블로그 기반 추정은 이번 범위에서 제외한다.

## 선택한 접근법

Kakao Local API와 수동 검수 CSV를 결합한다.

Kakao Local API는 장소명, 주소, 좌표, 전화번호와 Kakao 장소 URL을 제공하므로 장소 식별과 전화번호 후보 수집에 사용한다. 영업시간과 휴무일은 공식 API 응답에 없으므로 공식 홈페이지, 사업자가 운영하는 SNS, 지도 상세 화면을 사람이 확인한다.

지도 상세 페이지의 비공개 내부 API나 HTML 구조를 크롤링하지 않는다. 33건의 일회성 작업에서는 크롤러 구현과 변경 대응 비용이 크고, 잘못 매칭된 영업시간을 자동 반영할 위험이 있기 때문이다.

## 구성 요소

### 1. Kakao 후보 수집기

`collectors/kakao_giftshop_detail.py`를 추가한다.

- DB에서 `category='giftshop'`인 행만 읽는다.
- 각 장소를 `대전 {상호명}`으로 Kakao 키워드 검색한다.
- 기존 좌표를 `x`, `y`로 전달해 가까운 결과를 우선한다.
- 후보의 이름, 주소, 좌표, 거리, 전화번호, Kakao 장소 URL을 반환한다.
- API 결과는 DB에 직접 쓰지 않는다.
- 429 및 5xx 응답은 기존 요청 재시도 규칙에 맞춰 제한적으로 재시도한다.

### 2. 검수 CSV 내보내기

`scripts/export_giftshop_detail_review.py`를 추가한다.

CSV는 다음 열을 가진다.

| 열 | 용도 |
|---|---|
| `place_id` | DB 대상 행 식별자 |
| `name`, `address`, `lat`, `lng` | 현재 DB 정보 |
| `kakao_name`, `kakao_address`, `kakao_distance_m` | 후보 일치 여부 검토 |
| `kakao_tel`, `kakao_place_url` | Kakao 후보 정보 |
| `match_status` | `matched`, `ambiguous`, `not_found`, `error` |
| `tel`, `open_time`, `close_day` | 최종 반영 값 |
| `tel_source_url` | 전화번호 확인 출처 |
| `hours_source_url` | 영업시간·휴무일 확인 출처 |
| `verified_at` | 확인 날짜, `YYYY-MM-DD` |
| `review_status` | `pending`, `approved`, `rejected` |
| `review_note` | 충돌이나 예외 기록 |

내보내기 시 Kakao 후보 전화번호는 `tel`에 제안값으로 복사할 수 있지만 `review_status`는 항상 `pending`으로 둔다.

### 3. 사람 검수

검수자는 다음 순서로 확인한다.

1. 상호명과 주소 또는 좌표가 같은 장소인지 확인한다.
2. 전화번호는 Kakao 후보와 사업자 운영 채널을 비교한다.
3. 영업시간과 휴무일은 사업자 공식 홈페이지 또는 사업자가 운영하는 SNS를 우선 확인한다.
4. 공식 채널에 정보가 없으면 Kakao 또는 Naver 지도 상세 화면을 확인한다.
5. 출처가 충돌하면 더 최근에 사업자가 게시한 정보를 선택하고 `review_note`에 충돌 내용을 남긴다.
6. 확인할 수 없는 값은 추정하지 않고 비워 둔다.
7. 장소와 값이 확인된 행만 `review_status='approved'`로 바꾼다.

영업시간은 장소별 표현을 보존하는 TEXT로 저장한다. 예를 들어 `매일 12:00~20:00`, `화~일 13:00~19:00`처럼 기록하며, 정보가 없는 요일을 임의로 보충하지 않는다.

### 4. 승인 CSV 가져오기

`scripts/import_giftshop_detail_review.py`를 추가한다.

- `review_status='approved'`인 행만 처리한다.
- `place_id`가 존재하고 현재 카테고리가 `giftshop`인지 확인한다.
- 승인 행은 `verified_at`이 반드시 있어야 하며 `YYYY-MM-DD` 형식인지 검증한다.
- `open_time` 또는 `close_day`가 있으면 `hours_source_url`이 반드시 있어야 한다.
- `tel`이 있으면 `tel_source_url`이 반드시 있어야 한다. Kakao 전화번호를 사용한 경우 Kakao 장소 URL을 기록한다.
- 빈 CSV 값으로 기존 DB 값을 지우지 않는다.
- 전체 파일을 하나의 트랜잭션으로 처리하고 한 행이라도 유효하지 않으면 모두 롤백한다.
- 반영 전 `travel.db`의 타임스탬프 백업을 만든다.

## DB 반영 방식

새 컬럼은 추가하지 않는다. 검수된 핵심 값은 기존 컬럼에 저장하고 출처 메타데이터는 기존 `extra_json` 객체에 병합한다.

```json
{
  "detail_enrichment": {
    "tel_source_url": "https://place.map.kakao.com/...",
    "hours_source_url": "https://...",
    "verified_at": "2026-07-22",
    "verification": "manual"
  }
}
```

기존 `extra_json` 키는 보존한다. 가져오기를 다시 실행하더라도 승인된 동일 입력으로 결과가 달라지지 않아야 한다.

## 후보 매칭 규칙

자동 매칭은 최종 승인 대신 검수 우선순위를 정하는 용도로만 사용한다.

- 이름을 공백, 괄호, `점`, `지점` 같은 지점 접미사를 제거해 정규화한다.
- 정규화한 이름이 같고 기존 좌표에서 200m 이내면 `matched`로 제안한다.
- 이름은 같지만 거리가 200m를 넘거나 주소의 구가 다르면 `ambiguous`로 둔다.
- 이름이 다르거나 적절한 후보가 없으면 `not_found`로 둔다.
- `matched`라도 사람의 `approved` 표시 없이는 DB에 반영하지 않는다.

## 오류 처리

- Kakao API 키가 없으면 실행 전에 종료한다.
- 개별 장소 검색 실패는 CSV에 오류 상태와 이유를 남기고 나머지 장소를 계속 처리한다.
- 후보가 여러 개면 첫 결과를 자동 확정하지 않고 `ambiguous`로 표시한다.
- CSV의 중복 `place_id`, 알 수 없는 상태값, 잘못된 날짜, 비어 있는 출처는 가져오기 오류로 처리한다.
- 가져오기 실패 시 DB 트랜잭션을 롤백하고 백업 위치와 오류 행을 출력한다.

## 테스트

### 후보 수집기

- 좌표와 검색어가 Kakao 요청에 전달된다.
- 정규화 이름과 200m 거리 기준으로 `matched`가 된다.
- 동명 장소, 다른 구, 먼 거리 후보는 `ambiguous`가 된다.
- 결과가 없으면 `not_found`가 된다.
- 후보 수집만으로 DB가 변경되지 않는다.

### CSV 내보내기

- giftshop 33건만 내보낸다.
- 각 `place_id`가 한 번만 존재한다.
- 모든 행의 초기 `review_status`가 `pending`이다.
- Kakao 후보와 현재 DB 필드를 구분해 기록한다.

### CSV 가져오기

- `approved` 행만 갱신한다.
- `pending`과 `rejected` 행은 변경하지 않는다.
- 출처 없는 영업시간 또는 휴무일을 거부한다.
- 빈 입력이 기존 값을 지우지 않는다.
- 기존 `extra_json` 키를 보존한다.
- 유효하지 않은 한 행이 있으면 전체 변경을 롤백한다.
- 같은 승인 CSV를 두 번 가져와도 결과가 동일하다.
- 반영 후 `PRAGMA integrity_check` 결과가 `ok`다.

## 완료 기준

- 33개 소품샵 모두 검수 CSV에 포함된다.
- 모든 행이 `approved` 또는 `rejected`로 최종 분류된다.
- 확인 가능한 전화번호, 영업시간, 휴무일이 출처와 함께 DB에 반영된다.
- 확인할 수 없는 정보는 NULL로 남고 추정값은 없다.
- 기존 설계 문서와 기존 `extra_json` 데이터가 보존된다.
- 가져오기 전 백업과 반영 요약이 남는다.
- 관련 단위 테스트와 DB 무결성 검사가 통과한다.
