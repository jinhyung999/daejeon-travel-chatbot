import json
import os

from dotenv import load_dotenv

from common import paginate, request_with_retry, save_raw, upsert_place

load_dotenv()
TOUR_API_KEY = os.getenv("TOUR_API_KEY")
BASE_URL = "http://apis.data.go.kr/B551011/KorService2/areaBasedList2"
DETAIL_INTRO_URL = "http://apis.data.go.kr/B551011/KorService2/detailIntro2"

# 관광지(12) / 문화시설(14) / 레포츠(28) - 대전 areaCode=3
CONTENT_TYPES = {"12": "attraction", "14": "culture", "28": "attraction"}


def _extract_items(data: dict) -> tuple[list[dict], int]:
    body = data.get("response", {}).get("body", {})
    items = body.get("items", {})
    # 결과 0건일 때 items가 "" 로 오는 경우가 있어 방어적으로 처리
    if not items or isinstance(items, str):
        return [], body.get("totalCount", 0)
    item = items.get("item", [])
    if isinstance(item, dict):
        item = [item]
    return item, body.get("totalCount", 0)


def fetch_page(content_type_id, page_no, num_of_rows):
    params = {
        "serviceKey": TOUR_API_KEY,
        "numOfRows": num_of_rows,
        "pageNo": page_no,
        "MobileOS": "ETC",
        "MobileApp": "DaejeonTravelBot",
        "areaCode": 3,
        "contentTypeId": content_type_id,
        "_type": "json",
    }
    resp = request_with_retry(BASE_URL, params)
    data = resp.json()
    save_raw(f"tour_{content_type_id}", page_no, data)
    return _extract_items(data)


def fetch_detail_intro(content_id, content_type_id):
    """우선순위 상위 장소에 한해 개장시간·휴무일·주차 등 상세정보 보강 시 사용."""
    params = {
        "serviceKey": TOUR_API_KEY,
        "contentId": content_id,
        "contentTypeId": content_type_id,
        "MobileOS": "ETC",
        "MobileApp": "DaejeonTravelBot",
        "_type": "json",
    }
    resp = request_with_retry(DETAIL_INTRO_URL, params)
    item = resp.json()["response"]["body"]["items"].get("item", {})
    if isinstance(item, list):
        item = item[0] if item else {}
    return item


def collect():
    all_rows = []
    for content_type_id, category in CONTENT_TYPES.items():
        items = paginate(lambda p, n, cid=content_type_id: fetch_page(cid, p, n))
        for it in items:
            all_rows.append({
                "place_id": it.get("contentid"),
                "name": it.get("title"),
                "category": category,
                "address": it.get("addr1"),
                "lat": float(it["mapy"]) if it.get("mapy") else None,
                "lng": float(it["mapx"]) if it.get("mapx") else None,
                "open_time": None,
                "close_day": None,
                "fee": None,
                "has_parking": None,
                "tel": it.get("tel"),
                "source_api": "tourapi",
                "extra_json": json.dumps({"firstimage": it.get("firstimage")}, ensure_ascii=False),
            })
    upsert_place(all_rows)
    return all_rows


if __name__ == "__main__":
    collect()
