# =====================================================
# build_route_stops.py
# tago_bus_routes.py가 남겨둔 캐시(data/raw/tago_route_list_*.json,
# tago_route_stops_*.json)로 bus_route / bus_route_stop 테이블을 채움
# (API 재호출 없음 — 이미 받아온 원본을 재가공만 함)
#
# 목적: transport.routes는 "이 정류소를 지나는 노선 목록"만 알려주고
#       "그 노선이 정류소를 어떤 순서로 지나가는지"는 없어서, 직행/환승
#       경로 탐색이 불가능했음. bus_route_stop에 노선별 정류소 순서를
#       저장해서 동선 탐색(app/transit.py)의 기반 데이터로 삼음
#
# 정류소 매칭: TAGO nodeid는 신규 정류소면 transport.stop_id='tago_{nodeid}'로
#              이미 저장되어 있고, 기존 대전시 BIS 정류소에 매칭됐던 것은
#              당시 매칭 결과가 저장 안 됐으므로 이름+좌표(30m)로 재매칭
#              (tago_bus_routes.py와 동일한 로직 재사용)
#
# 사용법: python scripts/build_route_stops.py
# =====================================================

import glob
import json
import math
import re
import sqlite3
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "travel.db"
RAW_DIR = Path(__file__).parent.parent / "data" / "raw"

MATCH_RADIUS_M = 30
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_name(name):
    return _WHITESPACE_RE.sub("", name or "").strip().lower()


def _haversine_m(lat1, lng1, lat2, lng2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _grid_key(lat, lng):
    return (round(lat / 0.001), round(lng / 0.001))


def _load_route_meta():
    meta = {}
    for f in sorted(glob.glob(str(RAW_DIR / "tago_route_list_page*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        items = d.get("response", {}).get("body", {}).get("items", {})
        item = items.get("item", []) if items and not isinstance(items, str) else []
        if isinstance(item, dict):
            item = [item]
        for r in item:
            meta[r["routeid"]] = (str(r.get("routeno")), r.get("routetp"))
    return meta


def run():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # BIS 원본 정류소만으로 매칭 인덱스 구성 (tago_ 신규행은 이름 재매칭 대상에서 제외)
    bis_index = {}
    for stop_id, name, lat, lng in cur.execute(
        "SELECT stop_id, name, lat, lng FROM transport WHERE lat IS NOT NULL AND stop_id NOT LIKE 'tago_%'"
    ):
        bis_index.setdefault(_grid_key(lat, lng), []).append((stop_id, _normalize_name(name), lat, lng))

    known_tago_ids = {
        row[0] for row in cur.execute("SELECT stop_id FROM transport WHERE stop_id LIKE 'tago_%'")
    }

    def resolve_stop_id(nodeid, name, lat, lng):
        tago_id = f"tago_{nodeid}"
        if tago_id in known_tago_ids:
            return tago_id
        norm = _normalize_name(name)
        gy, gx = _grid_key(lat, lng)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for sid, cand_norm, cand_lat, cand_lng in bis_index.get((gy + dy, gx + dx), []):
                    if cand_norm == norm and _haversine_m(lat, lng, cand_lat, cand_lng) <= MATCH_RADIUS_M:
                        return sid
        return None

    route_meta = _load_route_meta()
    print(f"노선 메타 {len(route_meta)}건 로드")

    route_rows, route_stop_rows = [], []
    unresolved = 0
    stop_files = sorted(glob.glob(str(RAW_DIR / "tago_route_stops_*.json")))
    print(f"노선별 정류소 캐시 파일 {len(stop_files)}개 처리 중...")

    for f in stop_files:
        d = json.load(open(f, encoding="utf-8"))
        body = d.get("response", {}).get("body", "")
        if not isinstance(body, dict):
            continue  # 세션제한 등으로 실패했던 캐시(빈 응답)는 스킵
        items = body.get("items", {})
        item = items.get("item", []) if items and not isinstance(items, str) else []
        if isinstance(item, dict):
            item = [item]
        if not item:
            continue

        route_id = item[0]["routeid"]
        route_no, route_type = route_meta.get(route_id, (None, None))
        route_rows.append((route_id, route_no, route_type, date.today().isoformat()))

        for s in item:
            stop_id = resolve_stop_id(s["nodeid"], s.get("nodenm"), s.get("gpslati"), s.get("gpslong"))
            if not stop_id:
                unresolved += 1
                continue
            route_stop_rows.append((route_id, s.get("updowncd", 0), s["nodeord"], stop_id))

    cur.executemany(
        "INSERT INTO bus_route (route_id, route_no, route_type, collected_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(route_id) DO UPDATE SET route_no=excluded.route_no, route_type=excluded.route_type, "
        "collected_at=excluded.collected_at",
        route_rows,
    )
    cur.executemany(
        "INSERT INTO bus_route_stop (route_id, updowncd, node_order, stop_id) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(route_id, updowncd, node_order) DO UPDATE SET stop_id=excluded.stop_id",
        route_stop_rows,
    )
    conn.commit()

    print(f"\nbus_route: {len(route_rows)}건")
    print(f"bus_route_stop: {len(route_stop_rows)}건 저장 (정류소 매칭 실패로 스킵 {unresolved}건)")
    conn.close()


if __name__ == "__main__":
    run()
