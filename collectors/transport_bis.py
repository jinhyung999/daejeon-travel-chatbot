import os

from dotenv import load_dotenv

from common import paginate, request_with_retry, save_raw, upsert_transport

load_dotenv()
DAEJEON_BIS_API_KEY = os.getenv("DAEJEON_BIS_API_KEY")
BASE_URL = "https://apis.data.go.kr/6300000/GetStatListService/getStatList"


def fetch_page(page_no, num_of_rows):
    params = {
        "serviceKey": DAEJEON_BIS_API_KEY,
        "pageNo": page_no,
        "numOfRows": num_of_rows,
        "type": "json",
    }
    resp = request_with_retry(BASE_URL, params)
    data = resp.json()
    save_raw("daejeon_bis", page_no, data)
    body = data.get("response", {}).get("body", {})
    items = body.get("items", {})
    if not items or isinstance(items, str):
        return [], body.get("totalCount", 0)
    item = items.get("item", [])
    if isinstance(item, dict):
        item = [item]
    return item, body.get("totalCount", 0)


def collect():
    items = paginate(fetch_page)
    rows = []
    for it in items:
        rows.append({
            "stop_id": it.get("NODEID"),
            "name": it.get("NODENM"),
            "type": "bus",
            "lat": float(it["LATITUDE"]) if it.get("LATITUDE") else None,
            "lng": float(it["LONGITUDE"]) if it.get("LONGITUDE") else None,
            "routes": None,  # 이 API는 정류소 목록만 제공, 경유노선은 별도 조회 필요
        })
    upsert_transport(rows)
    return rows


if __name__ == "__main__":
    collect()
