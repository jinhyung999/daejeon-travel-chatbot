# =====================================================
# place_lookup.py
# 장소명 문자열을 place 테이블에서 조회해 좌표로 변환하는 모듈
#
# 완전일치를 우선 시도하고, 없으면 부분일치(LIKE)로 넓힌다.
# 여러 건이 걸리면 scripts/dedupe_place.py와 동일한 소스 우선순위
# (tourapi > daejeon_food > daejeon_shopping > mois_lodging > sbiz)로
# 1건을 결정적으로 선택한다.
# =====================================================

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "travel.db"

SOURCE_PRIORITY = {
    "tourapi": 0,
    "daejeon_food": 1,
    "daejeon_shopping": 2,
    "mois_lodging": 3,
    "sbiz": 4,
}


def _get_conn():
    return sqlite3.connect(DB_PATH)


def _pick_best(rows):
    """rows: list of (place_id, name, source_api, lat, lng).
    소스 우선순위가 가장 높은(숫자가 작은) 행을 선택하고,
    동순위면 place_id 사전순으로 결정적으로 고른다."""
    return sorted(rows, key=lambda r: (SOURCE_PRIORITY.get(r[2], 9), r[0]))[0]


def resolve_place(name: str) -> dict | None:
    """장소명으로 place 테이블을 조회해 좌표를 반환한다.
    완전일치 -> 부분일치 순으로 시도하고, 못 찾으면 None을 반환한다."""
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name:
        return None

    conn = _get_conn()
    cur = conn.cursor()

    rows = cur.execute(
        "SELECT place_id, name, source_api, lat, lng FROM place WHERE name = ? AND lat IS NOT NULL",
        (name,),
    ).fetchall()

    if not rows:
        rows = cur.execute(
            "SELECT place_id, name, source_api, lat, lng FROM place WHERE name LIKE ? AND lat IS NOT NULL",
            (f"%{name}%",),
        ).fetchall()

    conn.close()

    if not rows:
        return None

    place_id, matched_name, source_api, lat, lng = _pick_best(rows)
    return {"place_id": place_id, "name": matched_name, "lat": lat, "lng": lng, "source_api": source_api}


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 2:
        print(resolve_place(sys.argv[1]))
    else:
        print("사용법: python place_lookup.py <장소명>")
