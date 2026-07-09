# =====================================================
# mois_lodging.py
# 행정안전부_문화_숙박업 조회서비스(전국 인허가데이터)에서
# 대전 지역 영업중인 숙박업소만 걸러 place 테이블에 추가하는 모듈
#
# - 이 API는 지역 필터 파라미터가 없어 전국(약 58,000건)을 페이지네이션하며
#   주소가 "대전광역시"로 시작하는 행만 골라낸다
# - 좌표는 EPSG:5174(TM 중부원점)로 내려오므로 WGS84(위경도)로 변환한다
# - 기존 place(TourAPI/상가정보 등) 데이터와 이름+좌표 근접도로 중복 검사 후 스킵
# =====================================================

import os
import time

from dotenv import load_dotenv
from pyproj import Transformer

from common import request_with_retry, save_raw, upsert_place
from sbiz_market import _load_existing_index, _is_duplicate, _grid_key, _normalize_name

load_dotenv()
MOIS_LODGING_API_KEY = os.getenv("MOIS_LODGING_API_KEY")
BASE_URL = "https://apis.data.go.kr/1741000/lodgings/info"

_transformer = Transformer.from_crs("EPSG:5174", "EPSG:4326", always_xy=True)


def _to_latlng(x_str, y_str):
    if not x_str or not y_str:
        return None, None
    try:
        x, y = float(x_str), float(y_str)
    except ValueError:
        return None, None
    if x == y:
        # 관측된 일부 레코드는 X/Y가 동일값으로 들어오는 오류 데이터라 신뢰 불가
        return None, None
    lng, lat = _transformer.transform(x, y)
    return lat, lng


def fetch_page(page_no, num_of_rows=100):
    params = {
        "serviceKey": MOIS_LODGING_API_KEY,
        "pageNo": page_no,
        "numOfRows": num_of_rows,
        "type": "json",
    }
    resp = request_with_retry(BASE_URL, params)
    data = resp.json()
    save_raw("mois_lodging", page_no, data)
    body = data.get("response", {}).get("body", {})
    items = body.get("items", {})
    if not items or isinstance(items, str):
        return [], body.get("totalCount", 0)
    item = items.get("item", [])
    if isinstance(item, dict):
        item = [item]
    return item, body.get("totalCount", 0)


def _make_place_id(mng_no: str) -> str:
    return f"mois_lodging_{mng_no}"


def _fetch_page_with_retry(page_no, max_page_retry=5):
    for attempt in range(max_page_retry):
        try:
            return fetch_page(page_no)
        except RuntimeError as e:
            wait = min(30, 2 ** attempt)
            print(f"[page {page_no}] 요청 실패({attempt + 1}/{max_page_retry}): {e} → {wait}s 대기")
            time.sleep(wait)
    print(f"[page {page_no}] 최종 실패, 이 페이지는 건너뜀")
    return None, None


def collect():
    existing_index = _load_existing_index()

    rows = []
    page_no = 1
    total_count = None
    scanned = 0
    skipped_dupe = 0
    skipped_closed = 0
    skipped_no_coord = 0
    skipped_pages = 0
    inserted_total = 0

    while True:
        items, page_total_count = _fetch_page_with_retry(page_no)
        if items is None:
            # 이 페이지는 재시도 끝에 실패 -> 스킵하고 다음 페이지로 계속 진행
            skipped_pages += 1
            page_no += 1
            if total_count is not None and (page_no - 1) * 100 >= total_count:
                break
            time.sleep(0.2)
            continue

        total_count = page_total_count
        if not items:
            break
        scanned += len(items)

        for it in items:
            addr = it.get("ROAD_NM_ADDR") or it.get("LOTNO_ADDR") or ""
            if not addr.startswith("대전광역시"):
                continue

            if it.get("SALS_STTS_CD") != "01":
                skipped_closed += 1
                continue

            lat, lng = _to_latlng(it.get("CRD_INFO_X"), it.get("CRD_INFO_Y"))
            if lat is None or lng is None:
                skipped_no_coord += 1
                continue

            name = it.get("BPLC_NM")
            if _is_duplicate(existing_index, name, lat, lng):
                skipped_dupe += 1
                continue

            rows.append({
                "place_id": _make_place_id(it.get("MNG_NO")),
                "name": name,
                "category": "lodging",
                "address": addr,
                "lat": lat,
                "lng": lng,
                "open_time": None,
                "close_day": None,
                "fee": None,
                "has_parking": None,
                "tel": it.get("TELNO") or None,
                "source_api": "mois_lodging",
                "extra_json": __import__("json").dumps({
                    "업태구분": it.get("BZSTAT_SE_NM"),
                    "영업상태": it.get("DTL_SALS_STTS_NM"),
                    "객실수": it.get("KSRM_CNT"),
                }, ensure_ascii=False),
            })

            key = _grid_key(lat, lng)
            existing_index.setdefault(key, []).append((_normalize_name(name), lat, lng))

        # 50페이지마다 중간 저장 (중간에 실패해도 여기까지는 DB에 남음)
        if page_no % 50 == 0 and rows:
            upsert_place(rows)
            inserted_total += len(rows)
            rows = []

        if scanned >= total_count:
            break
        page_no += 1
        time.sleep(0.2)

    if rows:
        upsert_place(rows)
        inserted_total += len(rows)

    print(f"전국 {total_count}건 중 {scanned}건 스캔(스킵된 페이지 {skipped_pages}개), "
          f"대전 신규 {inserted_total}건, "
          f"폐업/휴업 스킵 {skipped_closed}건, 좌표없음 스킵 {skipped_no_coord}건, 중복 스킵 {skipped_dupe}건")

    return inserted_total


if __name__ == "__main__":
    collect()
