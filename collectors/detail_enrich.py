import html
import json
import os
import re
import time

from dotenv import load_dotenv

from common import get_conn, request_with_retry, save_raw

load_dotenv()
TOUR_API_KEY = os.getenv("TOUR_API_KEY")
DETAIL_INTRO_URL = "http://apis.data.go.kr/B551011/KorService2/detailIntro2"

# contentTypeId별 detailIntro2 응답 필드명이 서로 다름 (관광지/레포츠 vs 문화시설)
FIELD_MAP = {
    "12": {"open_time": "usetime", "close_day": "restdate", "parking": "parking", "fee": None},
    "28": {"open_time": "usetimeleports", "close_day": "restdateleports", "parking": "parkingleports", "fee": "usefeeleports"},
    "14": {"open_time": "usetimeculture", "close_day": "restdateculture", "parking": "parkingculture", "fee": "usefee"},
}


_TAG_RE = re.compile(r"<[^>]+>")


def _clean_text(value: str | None) -> str | None:
    """TourAPI 텍스트에 섞여있는 <br> 등 HTML 태그/엔티티를 정리"""
    if not value:
        return None
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = "\n".join(line.strip() for line in text.split("\n") if line.strip())
    return text or None


def _parse_parking(value: str | None) -> int | None:
    if not value:
        return None
    if "불가" in value:
        return 0
    if "가능" in value:
        return 1
    return None


def fetch_detail_intro(content_id: str, content_type_id: str) -> dict:
    params = {
        "serviceKey": TOUR_API_KEY,
        "contentId": content_id,
        "contentTypeId": content_type_id,
        "MobileOS": "ETC",
        "MobileApp": "DaejeonTravelBot",
        "_type": "json",
    }
    resp = request_with_retry(DETAIL_INTRO_URL, params)
    data = resp.json()
    save_raw(f"detail_{content_type_id}", content_id, data)
    items = data.get("response", {}).get("body", {}).get("items", {})
    if not items or isinstance(items, str):
        return {}
    item = items.get("item", {})
    if isinstance(item, list):
        item = item[0] if item else {}
    return item


def enrich(categories=("attraction", "culture")):
    conn = get_conn()
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in categories)
    targets = cur.execute(f"""
        SELECT place_id, extra_json FROM place
        WHERE source_api='tourapi' AND category IN ({placeholders})
    """, categories).fetchall()

    updated, skipped = 0, 0
    for place_id, extra_json in targets:
        content_type_id = json.loads(extra_json or "{}").get("contentTypeId")
        field_map = FIELD_MAP.get(content_type_id)
        if not field_map:
            skipped += 1
            continue

        detail = fetch_detail_intro(place_id, content_type_id)
        if not detail:
            skipped += 1
            time.sleep(0.3)
            continue

        open_time = _clean_text(detail.get(field_map["open_time"]))
        close_day = _clean_text(detail.get(field_map["close_day"]))
        has_parking = _parse_parking(detail.get(field_map["parking"]))
        fee = _clean_text(detail.get(field_map["fee"])) if field_map["fee"] else None

        cur.execute("""
            UPDATE place SET open_time=?, close_day=?, has_parking=?, fee=?
            WHERE place_id=?
        """, (open_time, close_day, has_parking, fee, place_id))
        updated += 1
        time.sleep(0.3)

    conn.commit()
    conn.close()
    print(f"detailIntro2 보강 완료: {updated}건 갱신, {skipped}건 스킵(정보없음)")


if __name__ == "__main__":
    enrich()
