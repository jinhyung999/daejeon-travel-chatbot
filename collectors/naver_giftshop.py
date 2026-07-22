import hashlib
import json
import math
import os
import re

from dotenv import load_dotenv

try:
    from common import get_conn, upsert_place
    from naver_search import NaverSearchClient
except ModuleNotFoundError:
    from collectors.common import get_conn, upsert_place
    from collectors.naver_search import NaverSearchClient

load_dotenv()

TARGET_COUNT = 100
CATEGORY = "giftshop"
DEDUPE_RADIUS_M = 30

LOCATION_SEEDS = {
    "대덕구": ["대덕구", "신탄진", "송촌동", "비래동", "오정동", "중리동"],
    "유성구": ["유성구", "봉명동", "궁동", "어은동", "관평동", "전민동", "노은동", "지족동", "원내동"],
    "동구": ["동구", "대전역", "소제동", "가양동", "용운동", "판암동", "산내"],
    "서구": ["서구", "둔산동", "갈마동", "월평동", "도마동", "관저동", "만년동", "탄방동"],
    "중구": ["중구", "대흥동", "은행동", "선화동", "오류동", "유천동", "산성동", "보문산"],
}

_TAG_RE = re.compile(r"<[^>]+>")
_BRANCH_SUFFIX_RE = re.compile(r"\(.*?\)|점$|점포|지점")
_WHITESPACE_RE = re.compile(r"\s+")


def _clean_html(text: str | None) -> str:
    return _TAG_RE.sub("", text or "").strip()


def _normalize_name(name: str) -> str:
    name = _BRANCH_SUFFIX_RE.sub("", name or "")
    name = _WHITESPACE_RE.sub("", name)
    return name.strip().lower()


def _haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def is_duplicate(name, lat, lng, existing) -> bool:
    """existing: (normalized_name, lat, lng) 튜플 리스트"""
    norm = _normalize_name(name)
    for cand_norm, cand_lat, cand_lng in existing:
        if cand_norm != norm:
            continue
        if _haversine_m(lat, lng, cand_lat, cand_lng) <= DEDUPE_RADIUS_M:
            return True
    return False


def stable_place_id(name, road_address, lat, lng) -> str:
    identity = "|".join((
        _normalize_name(name),
        (road_address or "").strip(),
        f"{lat:.7f}",
        f"{lng:.7f}",
    ))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return "naver_giftshop_" + digest


def _load_existing_index(conn):
    rows = conn.execute(
        "SELECT name, lat, lng FROM place WHERE lat IS NOT NULL AND lng IS NOT NULL"
    ).fetchall()
    return [(_normalize_name(name), lat, lng) for name, lat, lng in rows]


def _parse_item(item):
    category = item.get("category") or ""
    if "인테리어소품" not in category:
        return None

    mapx = item.get("mapx")
    mapy = item.get("mapy")
    if not mapx or not mapy:
        return None

    lat = float(mapy) / 10_000_000
    lng = float(mapx) / 10_000_000
    name = _clean_html(item.get("title"))
    road_address = _clean_html(item.get("roadAddress"))
    address = _clean_html(item.get("address"))

    return {
        "name": name,
        "address": road_address or address,
        "road_address": road_address,
        "lat": lat,
        "lng": lng,
        "tel": item.get("telephone") or None,
        "naver_category": category,
        "naver_link": item.get("link") or None,
    }


def collect(target_count=TARGET_COUNT, conn=None, client=None):
    owns_conn = conn is None
    if conn is None:
        conn = get_conn()
    if client is None:
        client = NaverSearchClient(os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET"))

    existing = _load_existing_index(conn)
    place_rows = []
    seen_ids = set()
    queries_tried = 0

    for neighborhoods in LOCATION_SEEDS.values():
        for neighborhood in neighborhoods:
            if len(place_rows) >= target_count:
                break
            queries_tried += 1
            items = client.search_local(f"{neighborhood} 소품샵", sort="random")
            for item in items:
                parsed = _parse_item(item)
                if not parsed:
                    continue
                if is_duplicate(parsed["name"], parsed["lat"], parsed["lng"], existing):
                    continue

                place_id = stable_place_id(
                    parsed["name"], parsed["road_address"], parsed["lat"], parsed["lng"]
                )
                if place_id in seen_ids:
                    continue
                seen_ids.add(place_id)

                place_rows.append({
                    "place_id": place_id,
                    "name": parsed["name"],
                    "category": CATEGORY,
                    "address": parsed["address"],
                    "lat": parsed["lat"],
                    "lng": parsed["lng"],
                    "open_time": None,
                    "close_day": None,
                    "fee": None,
                    "has_parking": None,
                    "tel": parsed["tel"],
                    "source_api": "naver_search",
                    "extra_json": json.dumps(
                        {
                            "naver_category": parsed["naver_category"],
                            "naver_link": parsed["naver_link"],
                        },
                        ensure_ascii=False,
                    ),
                })
                existing.append((_normalize_name(parsed["name"]), parsed["lat"], parsed["lng"]))
        if len(place_rows) >= target_count:
            break

    upsert_place(place_rows, conn=conn)

    if place_rows:
        placeholders = ",".join("?" for _ in place_rows)
        conn.execute(
            f"UPDATE place SET recommend='추천' WHERE place_id IN ({placeholders})",
            [row["place_id"] for row in place_rows],
        )
        conn.commit()

    if len(place_rows) < target_count:
        print(
            f"소품샵 수집 완료: 목표 {target_count}건 중 {len(place_rows)}건 반영 "
            f"(검색 조합 {queries_tried}개 소진)"
        )
    else:
        print(f"소품샵 수집 완료: {len(place_rows)}건 반영 (목표 {target_count}건 달성)")

    if owns_conn:
        conn.close()

    return place_rows


if __name__ == "__main__":
    collect()
