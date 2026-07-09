# =====================================================
# sbiz_market.py
# 소상공인시장진흥공단 상가(상권)정보 CSV(대전)를 읽어 place 테이블에 추가하는 모듈
#
# - API 호출이 아니라 로컬 CSV 파일을 읽는다 (data.go.kr 파일데이터, 분기별 갱신본)
# - 대분류/중분류를 프로젝트의 place.category로 매핑
# - 기존 place(TourAPI/대전시 음식점 API 등)와 이름+좌표 근접도로 중복 검사 후 스킵
# =====================================================

import csv
import json
import math
import re
from pathlib import Path

from common import get_conn, upsert_place, upsert_medical

CSV_PATH = (
    Path(__file__).parent.parent
    / "csv"
    / "소상공인시장진흥공단_상가(상권)정보_20260331"
    / "소상공인시장진흥공단_상가(상권)정보_대전_202603.csv"
)

# 중분류명 -> place.category 매핑 (여기 없는 중분류는 수집 대상 아님)
MID_CATEGORY_MAP = {
    "비알코올 ": "cafe",
    "한식": "restaurant",
    "기타 간이": "restaurant",
    "주점": "restaurant",
    "중식": "restaurant",
    "일식": "restaurant",
    "서양식": "restaurant",
    "구내식당·뷔페": "restaurant",
    "동남아시아": "restaurant",
    "일반 숙박": "lodging",
    "기타 숙박": "lodging",
    "유원지·오락": "attraction",
    "도서관·사적지": "attraction",
}

# 의원/병원/기타보건 중분류는 place가 아니라 별도 medical 테이블로 저장
MEDICAL_MID_CATEGORIES = {"의원", "병원", "기타 보건"}

# 중복 판정 반경(m) — 이 거리 안에 이름이 비슷한 기존 place가 있으면 스킵
DEDUPE_RADIUS_M = 30

_BRANCH_SUFFIX_RE = re.compile(r"\(.*?\)|점$|점포|지점")
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_name(name: str) -> str:
    if not name:
        return ""
    name = _BRANCH_SUFFIX_RE.sub("", name)
    name = _WHITESPACE_RE.sub("", name)
    return name.strip().lower()


def _haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _grid_key(lat: float, lng: float) -> tuple[int, int]:
    # 약 0.001도 ~ 111m 격자. 근접 검사 시 인접 8칸까지 같이 확인.
    return (round(lat / 0.001), round(lng / 0.001))


def _load_existing_index():
    """기존 place 테이블 데이터를 격자 인덱스로 로드"""
    conn = get_conn()
    cur = conn.cursor()
    rows = cur.execute("SELECT name, lat, lng FROM place WHERE lat IS NOT NULL AND lng IS NOT NULL").fetchall()
    conn.close()

    index = {}
    for name, lat, lng in rows:
        key = _grid_key(lat, lng)
        index.setdefault(key, []).append((_normalize_name(name), lat, lng))
    return index


def _is_duplicate(index, name, lat, lng) -> bool:
    norm = _normalize_name(name)
    gy, gx = _grid_key(lat, lng)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            for cand_norm, cand_lat, cand_lng in index.get((gy + dy, gx + dx), []):
                if not cand_norm or not norm:
                    continue
                if cand_norm != norm:
                    continue
                if _haversine_m(lat, lng, cand_lat, cand_lng) <= DEDUPE_RADIUS_M:
                    return True
    return False


def _make_place_id(row_no: str) -> str:
    return f"sbiz_{row_no}"


def collect():
    if not CSV_PATH.exists():
        raise FileNotFoundError(f"CSV 파일을 찾을 수 없습니다: {CSV_PATH}")

    existing_index = _load_existing_index()

    place_rows = []
    medical_rows = []
    skipped_dupe = 0
    skipped_unmapped = 0
    total = 0

    with open(CSV_PATH, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            total += 1
            mid = r["상권업종중분류명"]
            is_medical = mid in MEDICAL_MID_CATEGORIES
            category = "medical" if is_medical else MID_CATEGORY_MAP.get(mid)
            if not category:
                skipped_unmapped += 1
                continue

            try:
                lat = float(r["위도"]) if r["위도"] else None
                lng = float(r["경도"]) if r["경도"] else None
            except ValueError:
                lat = lng = None

            name = r["상호명"]
            if r.get("지점명"):
                name = f"{name}({r['지점명']})"

            address = r["도로명주소"] or r["지번주소"]
            extra_json = json.dumps({
                "대분류": r["상권업종대분류명"],
                "중분류": mid,
                "소분류": r["상권업종소분류명"],
                "건물명": r.get("건물명") or None,
            }, ensure_ascii=False)

            # medical은 place와 성격이 달라 중복 검사 없이 별도 테이블로 저장
            if is_medical:
                medical_rows.append({
                    "medical_id": _make_place_id(r["상가업소번호"]),
                    "name": name,
                    "category": r["상권업종소분류명"],
                    "address": address,
                    "lat": lat,
                    "lng": lng,
                    "tel": None,
                    "source_api": "sbiz",
                    "extra_json": extra_json,
                })
                continue

            if lat is not None and lng is not None and _is_duplicate(existing_index, name, lat, lng):
                skipped_dupe += 1
                continue

            place_rows.append({
                "place_id": _make_place_id(r["상가업소번호"]),
                "name": name,
                "category": category,
                "address": address,
                "lat": lat,
                "lng": lng,
                "open_time": None,
                "close_day": None,
                "fee": None,
                "has_parking": None,
                "tel": None,
                "source_api": "sbiz",
                "extra_json": extra_json,
            })

            # 같은 실행 중 방금 추가한 행끼리도 중복 검사에 걸리도록 인덱스에 반영
            if lat is not None and lng is not None:
                key = _grid_key(lat, lng)
                existing_index.setdefault(key, []).append((_normalize_name(name), lat, lng))

    print(f"CSV 전체 {total}건 중 매핑대상 {total - skipped_unmapped}건 "
          f"(place {len(place_rows)}건 + medical {len(medical_rows)}건), "
          f"기존 place와 중복 스킵 {skipped_dupe}건")

    place_result = upsert_place(place_rows)
    upsert_medical(medical_rows)
    return place_result


if __name__ == "__main__":
    collect()
