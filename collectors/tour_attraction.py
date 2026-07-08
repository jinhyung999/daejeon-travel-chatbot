# =====================================================
# tour_attraction.py
# 한국관광공사 TourAPI를 이용하여 대전 관광지 정보를 수집하는 모듈
#
# 주요 기능
# 1. 관광지/문화시설/레포츠 API 호출
# 2. 페이지별 데이터 자동 수집
# 3. 관광지 정보 추출 및 가공
# 4. SQLite place 테이블에 저장(Upsert)
# =====================================================

import json                 # JSON 데이터 처리
import os                   # 환경변수 읽기

from dotenv import load_dotenv   # .env 파일의 환경변수 로드

# 공통 함수 가져오기
from common import paginate, request_with_retry, save_raw, upsert_place

# .env 파일 읽기
load_dotenv()

# TourAPI 인증키 가져오기
TOUR_API_KEY = os.getenv("TOUR_API_KEY")

# 관광지 목록 조회 API
BASE_URL = "http://apis.data.go.kr/B551011/KorService2/areaBasedList2"

# 관광지 상세정보 조회 API
DETAIL_INTRO_URL = "http://apis.data.go.kr/B551011/KorService2/detailIntro2"

# 수집할 콘텐츠 종류
# 관광지(12), 문화시설(14), 레포츠(28)
CONTENT_TYPES = {
    "12": "attraction",
    "14": "culture",
    "28": "attraction"
}


# API 응답에서 실제 관광지 목록을 추출하는 함수
def _extract_items(data: dict) -> tuple[list[dict], int]:

    # response > body 추출
    body = data.get("response", {}).get("body", {})

    # items 추출
    items = body.get("items", {})

    # 결과가 없으면 빈 리스트 반환
    if not items or isinstance(items, str):
        return [], body.get("totalCount", 0)

    # 관광지 목록 추출
    item = items.get("item", [])

    # 데이터가 1개일 경우 리스트로 변환
    if isinstance(item, dict):
        item = [item]

    # (관광지 목록, 전체 개수) 반환
    return item, body.get("totalCount", 0)


# 관광지 데이터를 한 페이지씩 가져오는 함수
def fetch_page(content_type_id, page_no, num_of_rows):

    # API 요청 파라미터 설정
    params = {
        "serviceKey": TOUR_API_KEY,
        "numOfRows": num_of_rows,
        "pageNo": page_no,
        "MobileOS": "ETC",
        "MobileApp": "DaejeonTravelBot",
        "areaCode": 3,                   # 대전
        "contentTypeId": content_type_id,
        "_type": "json",
    }

    # API 요청
    resp = request_with_retry(BASE_URL, params)

    # JSON으로 변환
    data = resp.json()

    # 원본 JSON 저장
    save_raw(f"tour_{content_type_id}", page_no, data)

    # 관광지 목록 반환
    return _extract_items(data)


# 관광지 상세정보 조회 함수
def fetch_detail_intro(content_id, content_type_id):
    """개장시간, 휴무일, 주차 여부 등의 상세정보 조회"""

    # API 요청 파라미터
    params = {
        "serviceKey": TOUR_API_KEY,
        "contentId": content_id,
        "contentTypeId": content_type_id,
        "MobileOS": "ETC",
        "MobileApp": "DaejeonTravelBot",
        "_type": "json",
    }

    # API 요청
    resp = request_with_retry(DETAIL_INTRO_URL, params)

    # 상세정보 추출
    item = resp.json()["response"]["body"]["items"].get("item", {})

    # 결과가 리스트일 경우 첫 번째 데이터 사용
    if isinstance(item, list):
        item = item[0] if item else {}

    # 상세정보 반환
    return item


# 전체 관광지 데이터를 수집하는 함수
def collect():

    # DB에 저장할 리스트
    all_rows = []

    # 관광지 종류별(관광지, 문화시설, 레포츠) 반복
    for content_type_id, category in CONTENT_TYPES.items():

        # paginate()를 이용하여 모든 페이지 데이터 수집
        items = paginate(
            lambda p, n, cid=content_type_id:
            fetch_page(cid, p, n)
        )

        # 관광지 데이터를 하나씩 처리
        for it in items:

            # DB 저장 형식으로 변환
            all_rows.append({

                # 관광지 고유 ID(API 제공)
                "place_id": it.get("contentid"),

                # 관광지 이름
                "name": it.get("title"),

                # 장소 종류
                "category": category,

                # 주소
                "address": it.get("addr1"),

                # 위도
                "lat": float(it["mapy"]) if it.get("mapy") else None,

                # 경도
                "lng": float(it["mapx"]) if it.get("mapx") else None,

                # 운영시간(현재 미수집)
                "open_time": None,

                # 휴무일(현재 미수집)
                "close_day": None,

                # 이용요금(현재 미수집)
                "fee": None,

                # 주차 여부(현재 미수집)
                "has_parking": None,

                # 전화번호
                "tel": it.get("tel"),

                # 데이터 출처
                "source_api": "tourapi",

                # 추가 정보를 JSON 형태로 저장
                "extra_json": json.dumps({

                    # 대표 이미지 주소
                    "firstimage": it.get("firstimage"),

                    # detailIntro2 호출 시 필요한 원본 콘텐츠타입ID
                    "contentTypeId": content_type_id,

                }, ensure_ascii=False),
            })

    # place 테이블에 저장 (없으면 INSERT, 있으면 UPDATE)
    upsert_place(all_rows)

    # 저장한 데이터 반환
    return all_rows


# 현재 파일을 직접 실행했을 때만 collect() 실행
if __name__ == "__main__":
    collect()