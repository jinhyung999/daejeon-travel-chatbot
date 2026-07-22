# =====================================================
# tour_course.py
# 한국관광공사 TourAPI를 이용하여 대전 여행코스(contentTypeId=25) 예시 데이터를
# 미리 확인하기 위한 스크립트
#
# 주요 기능
# 1. 여행코스 목록 조회 (areaBasedList2)
# 2. 코스별 세부 경유지 조회 (detailInfo2)
# 3. 콘솔 요약 출력 + data/raw/tour_course_sample.json 저장
#
# DB에는 저장하지 않는다 (예시 데이터 확인 전용 스크립트).
# =====================================================

import json
import os
from pathlib import Path

from dotenv import load_dotenv

from common import request_with_retry, save_raw

load_dotenv()

TOUR_API_KEY = os.getenv("TOUR_API_KEY")

# 여행코스 목록 조회 API
BASE_URL = "http://apis.data.go.kr/B551011/KorService2/areaBasedList2"

# 여행코스 세부 경유지 조회 API
DETAIL_INFO_URL = "http://apis.data.go.kr/B551011/KorService2/detailInfo2"

# 여행코스 콘텐츠타입ID
CONTENT_TYPE_ID = "25"

SAMPLE_OUTPUT_PATH = (
    Path(__file__).parent.parent / "data" / "raw" / "tour_course_sample.json"
)


def _extract_items(data: dict) -> list[dict]:
    body = data.get("response", {}).get("body", {})
    items = body.get("items", {})

    if not items or isinstance(items, str):
        return []

    item = items.get("item", [])

    if isinstance(item, dict):
        item = [item]

    return item


def fetch_course_list(num_of_rows: int) -> list[dict]:
    """대전 여행코스 목록을 상위 num_of_rows개 조회한다."""

    params = {
        "serviceKey": TOUR_API_KEY,
        "numOfRows": num_of_rows,
        "pageNo": 1,
        "MobileOS": "ETC",
        "MobileApp": "DaejeonTravelBot",
        "areaCode": 3,
        "contentTypeId": CONTENT_TYPE_ID,
        "_type": "json",
    }

    resp = request_with_retry(BASE_URL, params)
    data = resp.json()

    save_raw(f"tour_{CONTENT_TYPE_ID}_list", 1, data)

    return _extract_items(data)


def fetch_course_stops(content_id: str) -> list[dict]:
    """여행코스 하나의 순서별 경유지 목록을 조회한다."""

    params = {
        "serviceKey": TOUR_API_KEY,
        "contentId": content_id,
        "contentTypeId": CONTENT_TYPE_ID,
        "MobileOS": "ETC",
        "MobileApp": "DaejeonTravelBot",
        "_type": "json",
    }

    resp = request_with_retry(DETAIL_INFO_URL, params)
    data = resp.json()

    save_raw(f"tour_{CONTENT_TYPE_ID}_detail_{content_id}", 1, data)

    return _extract_items(data)


def preview(limit: int = 5) -> list[dict]:
    """여행코스 상위 limit개와 각 코스의 경유지를 조회해 콘솔에 요약 출력하고
    data/raw/tour_course_sample.json에 저장한다. DB에는 저장하지 않는다."""

    courses = fetch_course_list(limit)

    sample = []

    for course in courses:
        content_id = course.get("contentid")
        title = course.get("title")
        address = course.get("addr1")

        stops = fetch_course_stops(content_id) if content_id else []

        stop_names = [
            stop.get("subname") for stop in stops if stop.get("subname")
        ]

        print(f"\n[{title}] ({address})")
        print(f"  경유지 {len(stop_names)}곳: {' -> '.join(stop_names)}")

        sample.append({
            "contentid": content_id,
            "title": title,
            "address": address,
            "firstimage": course.get("firstimage"),
            "stops": [
                {
                    "subnum": stop.get("subnum"),
                    "subname": stop.get("subname"),
                    "subdetailoverview": stop.get("subdetailoverview"),
                    "subdetailimg": stop.get("subdetailimg"),
                }
                for stop in stops
            ],
        })

    SAMPLE_OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SAMPLE_OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(sample, f, ensure_ascii=False, indent=2)

    print(f"\n예시 데이터 저장 완료: {SAMPLE_OUTPUT_PATH}")

    return sample


if __name__ == "__main__":
    preview()
