# =====================================================
# daejeon_food.py
# 대전 맛집 공공데이터 API를 이용하여 음식점 정보를 수집하는 모듈
#
# 주요 기능
# 1. 대전 맛집 API 호출
# 2. 페이지별 데이터 자동 수집
# 3. API에 없는 고유 ID 생성(SHA1 해시)
# 4. SQLite place 테이블에 저장(Upsert)
# =====================================================

import hashlib              # 문자열을 해시값으로 변환하기 위한 모듈
import json                 # JSON 데이터 처리
import os                   # 환경변수 읽기

from dotenv import load_dotenv   # .env 파일의 환경변수 로드

# 공통 함수 가져오기
from common import paginate, request_with_retry, save_raw, upsert_place

# .env 파일 읽기
load_dotenv()

# 대전 맛집 API 인증키 가져오기
DAEJEON_FOOD_API_KEY = os.getenv("DAEJEON_FOOD_API_KEY")

# 대전 맛집 공공데이터 API 주소
BASE_URL = "https://apis.data.go.kr/6300000/openapi2022/restrnt/getrestrnt"


# place_id 생성 함수
def _make_place_id(name: str, address: str) -> str:

    # 이 API는 고유 ID를 제공하지 않기 때문에
    # 이름 + 주소를 SHA1 해시로 변환하여 고유 ID 생성
    digest = hashlib.sha1(
        f"{name}|{address}".encode("utf-8")
    ).hexdigest()[:16]

    # DB에서 사용할 place_id 반환
    return f"daejeon_food_{digest}"


# 한 페이지의 맛집 데이터를 가져오는 함수
def fetch_page(page_no, num_of_rows):

    # API 요청 파라미터
    params = {
        "serviceKey": DAEJEON_FOOD_API_KEY,
        "pageNo": page_no,
        "numOfRows": num_of_rows,
    }

    # API 요청
    resp = request_with_retry(BASE_URL, params)

    # JSON 형태로 변환
    data = resp.json()

    # 원본 JSON 저장
    save_raw("daejeon_food", page_no, data)

    # response > body 추출
    body = data.get("response", {}).get("body", {})

    # 맛집 목록 추출 (없으면 빈 리스트)
    items = body.get("items") or []

    # (데이터 목록, 전체 개수) 반환
    return items, body.get("totalCount", 0)


# 전체 맛집 데이터를 수집하는 함수
def collect():

    # 모든 페이지 데이터 가져오기
    items = paginate(fetch_page)

    # DB에 저장할 데이터 리스트
    rows = []

    # 맛집 하나씩 처리
    for it in items:

        # 맛집 이름
        name = it.get("restrntNm")

        # 주소
        address = it.get("restrntAddr")

        # DB 형식으로 변환
        rows.append({

            # 생성한 고유 ID
            "place_id": _make_place_id(name, address),

            # 맛집 이름
            "name": name,

            # 장소 종류
            "category": "restaurant",

            # 주소
            "address": address,

            # 위도
            "lat": float(it["mapLat"]) if it.get("mapLat") else None,

            # 경도
            "lng": float(it["mapLot"]) if it.get("mapLot") else None,

            # 영업시간
            "open_time": it.get("salsTime"),

            # 휴무일
            "close_day": it.get("hldyGuid"),

            # 가격 정보 없음
            "fee": None,

            # 주차 정보 없음
            "has_parking": None,

            # 전화번호
            "tel": it.get("restrntInqrTel"),

            # 어떤 API에서 가져왔는지
            "source_api": "daejeon_food",

            # 기타 정보를 JSON 형태로 저장
            "extra_json": json.dumps({

                # 대표 음식
                "rprsFod": it.get("rprsFod"),

                # 음식점 소개
                "restrntSumm": it.get("restrntSumm"),

                # 상세 주소
                "restrntDtlAddr": it.get("restrntDtlAddr"),

                # 우편번호
                "restrntZip": it.get("restrntZip"),

            }, ensure_ascii=False),
        })

    # place 테이블에 저장
    upsert_place(rows)

    # 저장한 데이터 반환
    return rows


# 현재 파일을 직접 실행했을 때만 실행
if __name__ == "__main__":
    collect()