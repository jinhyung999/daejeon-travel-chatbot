import json
import os

from dotenv import load_dotenv

from common import paginate, request_with_retry, save_raw, upsert_place

load_dotenv()
TOUR_API_KEY = os.getenv("TOUR_API_KEY")
BASE_URL = "http://apis.data.go.kr/B551011/KorService2/areaBasedList2"

CONTENT_TYPE_ID = "32"  # 숙박


def _extract_items(data: dict) -> tuple[list[dict], int]:
    body = data.get("response", {}).get("body", {})
    items = body.get("items", {})
    if not items or isinstance(items, str):
        return [], body.get("totalCount", 0)
    item = items.get("item", [])
    if isinstance(item, dict):
        item = [item]
    return item, body.get("totalCount", 0)


def fetch_page(page_no, num_of_rows):
    params = {
        "serviceKey": TOUR_API_KEY,
        "numOfRows": num_of_rows,
        "pageNo": page_no,
        "MobileOS": "ETC",
        "MobileApp": "DaejeonTravelBot",
        "areaCode": 3,
        "contentTypeId": CONTENT_TYPE_ID,
        "_type": "json",
    }
    resp = request_with_retry(BASE_URL, params)
    data = resp.json()
    save_raw(f"tour_{CONTENT_TYPE_ID}", page_no, data)
    return _extract_items(data)


def collect():
    items = paginate(fetch_page)
    rows = []
    for it in items:
        rows.append({
            "place_id": it.get("contentid"),
            "name": it.get("title"),
            "category": "lodging",
            "address": it.get("addr1"),
            "lat": float(it["mapy"]) if it.get("mapy") else None,
            "lng": float(it["mapx"]) if it.get("mapx") else None,
            "open_time": None,
            "close_day": None,
            "fee": None,
            "has_parking": None,
            "tel": it.get("tel"),
            "source_api": "tourapi",
            "extra_json": json.dumps({
                "firstimage": it.get("firstimage"),
                "contentTypeId": CONTENT_TYPE_ID,
            }, ensure_ascii=False),
        })
    upsert_place(rows)
    return rows


if __name__ == "__main__":
    collect()
