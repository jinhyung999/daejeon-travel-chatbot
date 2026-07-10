# =====================================================
# 배재은 
# 대전광역시 문화관광(쇼핑) 공공데이터 API 주소 : https://www.data.go.kr/data/15000867/openapi.do
# daejeon_shopping.py
# 대전광역시 문화관광(쇼핑) 공공데이터 API를 이용하여 쇼핑 정보를 수집하는 모듈
#
# API 응답 필드와 place 테이블 컬럼 매핑 구조
#
#| API 응답 필드      | `place` 저장 위치                   |
#| -------------- | ------------------------------- |
#| `shppgNm`      | `name`                          |
#| `shppgAddr`    | `address`                       |
#| `mapLat`       | `lat`                           |
#| `mapLot`       | `lng`                           |
#| `salsTime`     | `open_time`                     |
#| `shppgInqrTel` | `tel`                           |
#| `pkgFclt`      | `has_parking` 판단 + `extra_json` |
#| `shppgDtlAddr` | `extra_json`                    |
#| `shppgHmpgUrl` | `extra_json`                    |
#| `shppgIntrd`   | `extra_json`                    |
#| `shppgZip`     | `extra_json`                    |
# =====================================================

# 스키마 place 테이블 
#-- 관광지, 맛집, 카페, 숙박, 쇼핑 정보 저장
#CREATE TABLE IF NOT EXISTS place (
#  place_id      TEXT PRIMARY KEY,      -- 장소 고유 ID
#  name          TEXT NOT NULL,         -- 장소 이름
#  category      TEXT NOT NULL,         -- 장소 종류
#  address       TEXT,                  -- 주소
#  lat           REAL,                  -- 위도
#  lng           REAL,                  -- 경도
# open_time     TEXT,                  -- 운영시간
#  close_day     TEXT,                  -- 휴무일
#  fee           TEXT,                  -- 이용요금
#  has_parking   INTEGER,               -- 주차 가능 여부(1/0)
#  tel           TEXT,                  -- 전화번호
#  source_api    TEXT,                  -- 데이터 출처 API
#  extra_json    TEXT                   -- 기타 상세 정보(JSON)
#);

import hashlib              # 문자열을 SHA1 해시로 변환
import json                 # JSON 데이터 처리
import os                   # 환경변수 읽기

from dotenv import load_dotenv   # .env 파일의 환경변수 로드

# common.py의 공통 함수 가져오기
from common import paginate, request_with_retry, save_raw, upsert_place

# .env 파일 읽기
load_dotenv()

# 쇼핑 API 인증키 가져오기
# .env 파일에 DAEJEON_SHOPPING_API_KEY=실제_API키 형식으로 작성
DAEJEON_SHOPPING_API_KEY = os.getenv("DAEJEON_SHOPPING_API_KEY")

# 대전광역시 문화관광(쇼핑) API 실제 호출 주소
BASE_URL = "https://apis.data.go.kr/6300000/openapi2022/shppg/getshppg"


# place_id 생성 함수
def _make_place_id(name: str, address: str) -> str:

    # API에서 별도의 고유 ID를 제공하지 않기 때문에
    # 쇼핑 장소 이름과 주소를 결합하여 SHA1 해시 생성
    digest = hashlib.sha1(
        f"{name}|{address}".encode("utf-8")
    ).hexdigest()[:16]

    # place 테이블에서 사용할 고유 ID 반환
    return f"daejeon_shopping_{digest}"


# 문자열 값을 실수로 변환하는 함수
def _to_float(value) -> float | None:

    # 값이 없으면 None 반환
    if value in (None, ""):
        return None

    try:
        # 위도와 경도를 float 자료형으로 변환
        return float(value)

    # 숫자로 변환할 수 없는 값이면 None 반환
    except (TypeError, ValueError):
        return None


# 편의시설 정보를 이용하여 주차 가능 여부를 판단하는 함수
def _parse_parking(value: str | None) -> int | None:

    # 편의시설 정보가 없으면 판단할 수 없음
    if not value:
        return None

    # 주차 불가라는 문구가 있으면 0
    if "주차 불가" in value or "주차불가" in value:
        return 0

    # 주차 관련 문구가 있으면 주차 가능으로 판단
    if "주차" in value or "주차장" in value:
        return 1

    # 주차 여부를 판단할 수 없으면 None
    return None


# API 응답에서 쇼핑 목록과 전체 데이터 개수를 추출하는 함수
def _extract_items(data: dict) -> tuple[list[dict], int]:

    # response > body 추출
    body = data.get("response", {}).get("body", {})

    # items 추출
    items = body.get("items") or []

    # items가 {"item": [...]} 형태인 경우 처리
    if isinstance(items, dict):
        items = items.get("item") or []

    # 데이터가 1건이라서 dict로 반환된 경우 리스트로 변환
    if isinstance(items, dict):
        items = [items]

    # 예상하지 못한 형식이면 빈 리스트 사용
    if not isinstance(items, list):
        items = []

    # 전체 데이터 개수 가져오기
    total_count = body.get("totalCount", 0)

    # 전체 개수를 정수로 변환
    try:
        total_count = int(total_count)
    except (TypeError, ValueError):
        total_count = 0

    # 쇼핑 목록과 전체 개수 반환
    return items, total_count


# 한 페이지의 쇼핑 데이터를 가져오는 함수
def fetch_page(page_no, num_of_rows):

    # API 요청 파라미터
    params = {
        "serviceKey": DAEJEON_SHOPPING_API_KEY,
        "pageNo": page_no,
        "numOfRows": num_of_rows,
    }

    # 공통 요청 함수를 이용하여 API 호출
    resp = request_with_retry(BASE_URL, params)

    # API 응답을 JSON 형태로 변환
    data = resp.json()

    # API 원본 JSON 저장
    save_raw("daejeon_shopping", page_no, data)

    # 쇼핑 목록과 전체 데이터 개수 반환
    return _extract_items(data)


# 전체 쇼핑 데이터를 수집하는 함수
def collect():

    # API 인증키가 설정되어 있는지 확인
    if not DAEJEON_SHOPPING_API_KEY:
        raise RuntimeError(
            ".env 파일에 DAEJEON_SHOPPING_API_KEY가 설정되어 있지 않습니다."
        )

    # 모든 페이지의 쇼핑 데이터 수집
    items = paginate(fetch_page)

    # DB에 저장할 데이터 리스트
    rows = []

    # 쇼핑 데이터 하나씩 처리
    for it in items:

        # 쇼핑 장소 이름
        name = it.get("shppgNm")

        # 기본 주소
        address = it.get("shppgAddr")

        # 장소 이름이 없으면 저장하지 않음
        # place 테이블의 name 컬럼이 NOT NULL이기 때문
        if not name:
            continue

        # 편의시설 정보
        package_facility = it.get("pkgFclt")

        # place 테이블 안에 넣을 형식으로 변환
        rows.append({

            # 이름과 주소로 만든 고유 ID
            "place_id": _make_place_id(name, address or ""),

            # 쇼핑 장소 이름
            "name": name,

            # 장소 종류
            "category": "shopping",

            # 주소
            "address": address,

            # 위도
            "lat": _to_float(it.get("mapLat")),

            # 경도
            "lng": _to_float(it.get("mapLot")),

            # 영업시간
            "open_time": it.get("salsTime"),

            # API 응답에 휴무일 필드가 없으므로 None
            "close_day": None,

            # API 응답에 이용요금 필드가 없으므로 None
            "fee": None,

            # 편의시설 정보에서 주차 가능 여부 판단
            "has_parking": _parse_parking(package_facility),

            # 문의 전화번호
            "tel": it.get("shppgInqrTel"),

            # 데이터 출처
            "source_api": "daejeon_shopping",

            # place 테이블에 별도 컬럼이 없는 부가정보를 JSON으로 저장
            "extra_json": json.dumps({

                # 상세 주소
                "shppgDtlAddr": it.get("shppgDtlAddr"),

                # 홈페이지 주소
                "shppgHmpgUrl": it.get("shppgHmpgUrl"),

                # 쇼핑 장소 소개
                "shppgIntrd": it.get("shppgIntrd"),

                # 우편번호
                "shppgZip": it.get("shppgZip"),

                # 편의시설 정보
                "pkgFclt": package_facility,

            }, ensure_ascii=False),
        })

    # place 테이블에 저장
    upsert_place(rows)

    # 저장한 데이터 반환
    return rows


# 현재 파일을 직접 실행했을 때만 collect() 실행
if __name__ == "__main__":
    collect()