import hashlib
import os
import xml.etree.ElementTree as ET

from dotenv import load_dotenv

from common import paginate, request_with_retry, save_raw, upsert_parking

load_dotenv()
DAEJEON_PARKING_API_KEY = os.getenv("DAEJEON_PARKING_API_KEY")
BASE_URL = "http://apis.data.go.kr/6300000/pis/parkinglotIF"
MAX_NUM_OF_ROWS = 50  # API 제한


def _make_parking_id(name: str, address: str) -> str:
    # 이 API는 응답에 고유 ID가 없어 name+address 해시로 안정적인 PK를 만든다
    digest = hashlib.sha1(f"{name}|{address}".encode("utf-8")).hexdigest()[:16]
    return f"daejeon_parking_{digest}"


def _text(item: ET.Element, tag: str) -> str | None:
    el = item.find(tag)
    if el is None or el.text is None:
        return None
    return el.text.strip()


def _build_fee(item: ET.Element) -> str | None:
    base_time, base_rate = _text(item, "baseTime"), _text(item, "baseRate")
    add_time, add_rate = _text(item, "addTime"), _text(item, "addRate")
    if not (base_time and base_rate):
        return None
    fee = f"기본 {base_time}분 {base_rate}원"
    if add_time and add_rate:
        fee += f", 추가 {add_time}분당 {add_rate}원"
    return fee


def _build_operate_time(item: ET.Element) -> str | None:
    parts = []
    for label, open_tag, close_tag in [
        ("평일", "weekdayOpenTime", "weekdayCloseTime"),
        ("토요일", "satOpenTime", "satCloseTime"),
        ("공휴일", "holidayOpenTime", "holidayCloseTime"),
    ]:
        open_t, close_t = _text(item, open_tag), _text(item, close_tag)
        if open_t and close_t:
            parts.append(f"{label} {open_t}~{close_t}")
    return " / ".join(parts) if parts else None


def fetch_page(page_no, num_of_rows):
    params = {
        "ServiceKey": DAEJEON_PARKING_API_KEY,
        "numOfRows": num_of_rows,
        "pageNo": page_no,
    }
    resp = request_with_retry(BASE_URL, params)
    save_raw("daejeon_parking", page_no, {"raw_xml": resp.text})
    root = ET.fromstring(resp.text)
    items = root.findall("./body/item")
    total_count_el = root.find("./body/totalCount")
    total_count = int(total_count_el.text) if total_count_el is not None and total_count_el.text else 0
    return items, total_count


def collect():
    items = paginate(fetch_page, num_of_rows=MAX_NUM_OF_ROWS)
    rows = []
    for it in items:
        name = _text(it, "name")
        address = _text(it, "address")
        total_qty = _text(it, "totalQty")
        rows.append({
            "parking_id": _make_parking_id(name, address),
            "name": name,
            "address": address,
            "lat": float(_text(it, "lat")) if _text(it, "lat") else None,
            "lng": float(_text(it, "lon")) if _text(it, "lon") else None,
            "operate_time": _build_operate_time(it),
            "fee": _build_fee(it),
            "capacity": int(total_qty) if total_qty and total_qty.isdigit() else None,
        })
    upsert_parking(rows)
    return rows


if __name__ == "__main__":
    collect()
