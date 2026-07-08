# =====================================================
# detail_common.py
# TourAPI detailCommon2로 장소 소개글(overview) 보강
#
# - 대상: place 테이블에서 source_api='tourapi'인 장소
# - overview/homepage -> extra_json에 추가
# - tel -> place.tel이 비어있을 때만 채움
# =====================================================

import json
import os
import time

from dotenv import load_dotenv

from common import get_conn, request_with_retry, save_raw
from detail_enrich import _clean_text

load_dotenv()
TOUR_API_KEY = os.getenv("TOUR_API_KEY")
DETAIL_COMMON_URL = "http://apis.data.go.kr/B551011/KorService2/detailCommon2"


def fetch_detail_common(content_id: str) -> dict:
    params = {
        "serviceKey": TOUR_API_KEY,
        "contentId": content_id,
        "MobileOS": "ETC",
        "MobileApp": "DaejeonTravelBot",
        "_type": "json",
    }
    resp = request_with_retry(DETAIL_COMMON_URL, params)
    data = resp.json()
    save_raw("common", content_id, data)
    items = data.get("response", {}).get("body", {}).get("items", {})
    if not items or isinstance(items, str):
        return {}
    item = items.get("item", {})
    if isinstance(item, list):
        item = item[0] if item else {}
    return item


def _extract_homepage(value: str | None) -> str | None:
    """homepage 필드는 <a href="...">...</a> 형태라 URL만 추출"""
    if not value:
        return None
    import re
    m = re.search(r'href="([^"]+)"', value)
    if m:
        return m.group(1)
    return _clean_text(value)


def enrich_overview():
    conn = get_conn()
    cur = conn.cursor()
    targets = cur.execute("""
        SELECT place_id, tel, extra_json FROM place
        WHERE source_api='tourapi'
    """).fetchall()

    updated, skipped = 0, 0
    for place_id, tel, extra_json in targets:
        extra = json.loads(extra_json or "{}")

        # 이미 overview가 있으면 재호출하지 않음 (중단 후 재실행 대비)
        if extra.get("overview"):
            skipped += 1
            continue

        detail = fetch_detail_common(place_id)
        if not detail:
            skipped += 1
            time.sleep(0.3)
            continue

        overview = _clean_text(detail.get("overview"))
        homepage = _extract_homepage(detail.get("homepage"))
        new_tel = _clean_text(detail.get("tel"))

        if overview:
            extra["overview"] = overview
        if homepage:
            extra["homepage"] = homepage

        # tel은 기존 값이 없을 때만 채움
        tel_to_save = tel if tel else new_tel

        cur.execute("""
            UPDATE place SET extra_json=?, tel=?
            WHERE place_id=?
        """, (json.dumps(extra, ensure_ascii=False), tel_to_save, place_id))
        updated += 1
        time.sleep(0.3)

    conn.commit()
    conn.close()
    print(f"detailCommon2 보강 완료: {updated}건 갱신, {skipped}건 스킵")


if __name__ == "__main__":
    enrich_overview()
