import os
from datetime import datetime, timedelta

from dotenv import load_dotenv

from common import paginate, request_with_retry, save_raw, upsert_event

load_dotenv()
TOUR_API_KEY = os.getenv("TOUR_API_KEY")
BASE_URL = "http://apis.data.go.kr/B551011/KorService2/searchFestival2"

# 최근 1년 기준으로 조회 (eventStartDate 이후 종료되는 행사만 반환됨)
EVENT_START_DATE = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")


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
        "eventStartDate": EVENT_START_DATE,
        "_type": "json",
    }
    resp = request_with_retry(BASE_URL, params)
    data = resp.json()
    save_raw("tour_festival", page_no, data)
    return _extract_items(data)


def collect():
    items = paginate(fetch_page)
    rows = []
    for it in items:
        rows.append({
            "event_id": it.get("contentid"),
            "name": it.get("title"),
            "place_name": it.get("addr2") or it.get("sponsor1"),
            "address": it.get("addr1"),
            "lat": float(it["mapy"]) if it.get("mapy") else None,
            "lng": float(it["mapx"]) if it.get("mapx") else None,
            "start_date": it.get("eventstartdate"),
            "end_date": it.get("eventenddate"),
            "fee": None,
            "source_api": "tourapi",
        })
    upsert_event(rows)
    return rows


if __name__ == "__main__":
    collect()
