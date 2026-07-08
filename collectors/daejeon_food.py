import hashlib
import json
import os

from dotenv import load_dotenv

from common import paginate, request_with_retry, save_raw, upsert_place

load_dotenv()
DAEJEON_FOOD_API_KEY = os.getenv("DAEJEON_FOOD_API_KEY")
BASE_URL = "https://apis.data.go.kr/6300000/openapi2022/restrnt/getrestrnt"


def _make_place_id(name: str, address: str) -> str:
    # 이 API는 응답에 고유 ID가 없어 name+address 해시로 안정적인 PK를 만든다
    digest = hashlib.sha1(f"{name}|{address}".encode("utf-8")).hexdigest()[:16]
    return f"daejeon_food_{digest}"


def fetch_page(page_no, num_of_rows):
    params = {
        "serviceKey": DAEJEON_FOOD_API_KEY,
        "pageNo": page_no,
        "numOfRows": num_of_rows,
    }
    resp = request_with_retry(BASE_URL, params)
    data = resp.json()
    save_raw("daejeon_food", page_no, data)
    body = data.get("response", {}).get("body", {})
    items = body.get("items") or []
    return items, body.get("totalCount", 0)


def collect():
    items = paginate(fetch_page)
    rows = []
    for it in items:
        name = it.get("restrntNm")
        address = it.get("restrntAddr")
        rows.append({
            "place_id": _make_place_id(name, address),
            "name": name,
            "category": "restaurant",
            "address": address,
            "lat": float(it["mapLat"]) if it.get("mapLat") else None,
            "lng": float(it["mapLot"]) if it.get("mapLot") else None,
            "open_time": it.get("salsTime"),
            "close_day": it.get("hldyGuid"),
            "fee": None,
            "has_parking": None,
            "tel": it.get("restrntInqrTel"),
            "source_api": "daejeon_food",
            "extra_json": json.dumps({
                "rprsFod": it.get("rprsFod"),
                "restrntSumm": it.get("restrntSumm"),
                "restrntDtlAddr": it.get("restrntDtlAddr"),
                "restrntZip": it.get("restrntZip"),
            }, ensure_ascii=False),
        })
    upsert_place(rows)
    return rows


if __name__ == "__main__":
    collect()
