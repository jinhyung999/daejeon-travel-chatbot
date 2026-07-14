# =====================================================
# backfill_transport_tago_node_id.py
# transport.tago_node_id 컬럼을 채우는 1회성 마이그레이션 스크립트
#
# 1. stop_id가 'tago_'로 시작하는 행: 접두사를 떼면 바로 TAGO nodeId
# 2. 그 외(BIS 원본 id로 매칭된 행): data/raw/tago_route_stops_*.json 캐시에서
#    이름+좌표(30m) 재매칭으로 원본 nodeId를 복원 (tago_bus_routes.py와 동일한 매칭 로직)
#
# 이 스크립트는 API를 재호출하지 않는다 — 이미 받아온 캐시 파일만 사용한다.
# =====================================================

import glob
import json
import math
import re
import sqlite3
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


def _load_tago_node_index():
    """data/raw/tago_route_stops_*.json 캐시에서 nodeid -> (name, lat, lng) 맵 구성"""
    index = {}
    for f in sorted(glob.glob(str(RAW_DIR / "tago_route_stops_*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        body = d.get("response", {}).get("body", "")
        if not isinstance(body, dict):
            continue
        items = body.get("items", {})
        item = items.get("item", []) if items and not isinstance(items, str) else []
        if isinstance(item, dict):
            item = [item]
        for s in item:
            nodeid = s.get("nodeid")
            lat, lng = s.get("gpslati"), s.get("gpslong")
            name = s.get("nodenm")
            if nodeid and lat is not None and lng is not None and name:
                index[nodeid] = (name, lat, lng)
    return index


def run():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    columns = [row[1] for row in cur.execute("PRAGMA table_info(transport)")]
    if "tago_node_id" not in columns:
        cur.execute("ALTER TABLE transport ADD COLUMN tago_node_id TEXT")
        conn.commit()
        print("transport.tago_node_id 컬럼 추가")

    cur.execute("""
        UPDATE transport SET tago_node_id = substr(stop_id, 6)
        WHERE stop_id LIKE 'tago_%' AND tago_node_id IS NULL
    """)
    direct_filled = cur.rowcount
    conn.commit()

    node_index = _load_tago_node_index()
    print(f"TAGO 노드 캐시 {len(node_index)}개 로드")

    grid = {}
    for nodeid, (name, lat, lng) in node_index.items():
        grid.setdefault(_grid_key(lat, lng), []).append((nodeid, _normalize_name(name), lat, lng))

    targets = cur.execute("""
        SELECT stop_id, name, lat, lng FROM transport
        WHERE stop_id NOT LIKE 'tago_%' AND tago_node_id IS NULL AND lat IS NOT NULL
    """).fetchall()

    matched = 0
    for stop_id, name, lat, lng in targets:
        norm = _normalize_name(name)
        gy, gx = _grid_key(lat, lng)
        best = None
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for nodeid, cand_norm, cand_lat, cand_lng in grid.get((gy + dy, gx + dx), []):
                    if cand_norm != norm:
                        continue
                    d = _haversine_m(lat, lng, cand_lat, cand_lng)
                    if d <= MATCH_RADIUS_M and (best is None or d < best[0]):
                        best = (d, nodeid)
        if best:
            cur.execute("UPDATE transport SET tago_node_id=? WHERE stop_id=?", (best[1], stop_id))
            matched += 1

    conn.commit()

    total = cur.execute("SELECT COUNT(*) FROM transport").fetchone()[0]
    filled = cur.execute("SELECT COUNT(*) FROM transport WHERE tago_node_id IS NOT NULL").fetchone()[0]
    print(f"tago_ 접두사 직접 채움: {direct_filled}건")
    print(f"BIS 원본 이름+좌표 재매칭: {matched}건 / {len(targets)}건 대상")
    print(f"전체 transport {total}건 중 tago_node_id 확보 {filled}건")

    conn.close()


if __name__ == "__main__":
    run()
