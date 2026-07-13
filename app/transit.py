# =====================================================
# transit.py
# 두 좌표 사이의 버스 이동 방법(직행/환승 1회)을 안내하는 모듈
#
# 범위: 소요시간/배차간격 계산은 하지 않음(원본 데이터에 없음).
#       "출발지 근처 정류소 → 몇 번 버스 → (환승 시) 어느 정류소에서
#       몇 번으로 갈아타서 → 도착지 근처 정류소"까지의 노선 안내만 제공.
#
# 데이터 근거: bus_route_stop(노선×방향별 정류소 순서) — TAGO
#             getRouteAcctoThrghSttnList 원본을 build_route_stops.py로 적재
# =====================================================

import sqlite3
from pathlib import Path

from geo import haversine_km

DB_PATH = Path(__file__).parent.parent / "db" / "travel.db"

MAX_NEAREST_STOP_KM = 1.0   # 이보다 먼 정류소는 "근처"로 취급하지 않음
MAX_DIRECT_RESULTS = 5
MAX_TRANSFER_RESULTS = 5


def _get_conn():
    return sqlite3.connect(DB_PATH)


def nearest_stops(lat: float, lng: float, k: int = 3, max_km: float = MAX_NEAREST_STOP_KM) -> list[dict]:
    """주어진 좌표에서 가까운 버스정류소 k개 (직선거리 기준)"""
    conn = _get_conn()
    cur = conn.cursor()
    # 대략적인 위경도 박스로 1차 필터링 후 haversine으로 정확히 정렬
    deg = max_km / 111.0
    rows = cur.execute(
        """SELECT stop_id, name, lat, lng FROM transport
           WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?""",
        (lat - deg, lat + deg, lng - deg, lng + deg),
    ).fetchall()
    conn.close()

    scored = []
    for stop_id, name, slat, slng in rows:
        dist = haversine_km(lat, lng, slat, slng)
        if dist <= max_km:
            scored.append({"stop_id": stop_id, "name": name, "lat": slat, "lng": slng, "dist_km": round(dist, 3)})
    scored.sort(key=lambda r: r["dist_km"])
    return scored[:k]


def _load_route_graph(cur):
    """by_route[(route_id, updowncd)] = [(node_order, stop_id), ...] (순서 정렬)
       by_stop[stop_id] = [(route_id, updowncd, node_order), ...]"""
    by_route, by_stop = {}, {}
    for route_id, updowncd, node_order, stop_id in cur.execute(
        "SELECT route_id, updowncd, node_order, stop_id FROM bus_route_stop ORDER BY route_id, updowncd, node_order"
    ):
        by_route.setdefault((route_id, updowncd), []).append((node_order, stop_id))
        by_stop.setdefault(stop_id, []).append((route_id, updowncd, node_order))
    return by_route, by_stop


def _route_label(cur, route_id):
    row = cur.execute("SELECT route_no, route_type FROM bus_route WHERE route_id=?", (route_id,)).fetchone()
    return f"{row[0]}번({row[1]})" if row else route_id


def find_bus_directions(from_lat: float, from_lng: float, to_lat: float, to_lng: float) -> dict:
    """출발/도착 좌표 근처 정류소를 찾고, 그 사이 직행/1회 환승 버스 노선을 찾음"""
    conn = _get_conn()
    cur = conn.cursor()
    by_route, by_stop = _load_route_graph(cur)

    from_stops = nearest_stops(from_lat, from_lng, k=2)
    to_stops = nearest_stops(to_lat, to_lng, k=2)

    if not from_stops or not to_stops:
        conn.close()
        return {"from_stops": from_stops, "to_stops": to_stops, "direct": [], "transfer": []}

    direct, transfer = [], []
    seen_direct, seen_transfer = set(), set()

    for fs in from_stops:
        for entry_a in by_stop.get(fs["stop_id"], []):
            route_a, ud_a, order_a = entry_a

            # 직행: 같은 (route_id, updowncd)가 도착지 정류소도 지나가고, 순서가 뒤쪽인지
            for ts in to_stops:
                for entry_b in by_stop.get(ts["stop_id"], []):
                    route_b, ud_b, order_b = entry_b
                    if route_a == route_b and ud_a == ud_b and order_a < order_b:
                        key = (fs["stop_id"], route_a, ud_a, ts["stop_id"])
                        if key not in seen_direct and len(direct) < MAX_DIRECT_RESULTS:
                            seen_direct.add(key)
                            direct.append({
                                "from_stop": fs["name"], "to_stop": ts["name"],
                                "route": _route_label(cur, route_a),
                                "stops_between": order_b - order_a,
                            })

            if len(transfer) >= MAX_TRANSFER_RESULTS:
                continue

            # 환승 1회: route_a를 타고 order_a 이후에 내리는 모든 정류소 X를 후보로,
            # X에서 다른 노선으로 갈아타 도착지 정류소에 닿는지 확인
            for order_x, stop_x in by_route.get((route_a, ud_a), []):
                if order_x <= order_a or stop_x == fs["stop_id"]:
                    continue
                for entry_x in by_stop.get(stop_x, []):
                    route_c, ud_c, order_xc = entry_x
                    if route_c == route_a and ud_c == ud_a:
                        continue  # 같은 노선/방향이면 환승이 아님
                    for ts in to_stops:
                        for entry_b in by_stop.get(ts["stop_id"], []):
                            route_b, ud_b, order_b = entry_b
                            if route_b == route_c and ud_b == ud_c and order_xc < order_b:
                                key = (fs["stop_id"], route_a, stop_x, route_b, ts["stop_id"])
                                if key not in seen_transfer and len(transfer) < MAX_TRANSFER_RESULTS:
                                    seen_transfer.add(key)
                                    transfer.append({
                                        "from_stop": fs["name"],
                                        "first_route": _route_label(cur, route_a),
                                        "transfer_stop": stop_x,
                                        "transfer_stop_name": _text_name(cur, stop_x),
                                        "second_route": _route_label(cur, route_b),
                                        "to_stop": ts["name"],
                                    })
                if len(transfer) >= MAX_TRANSFER_RESULTS:
                    break

    conn.close()
    return {"from_stops": from_stops, "to_stops": to_stops, "direct": direct, "transfer": transfer}


def _text_name(cur, stop_id):
    row = cur.execute("SELECT name FROM transport WHERE stop_id=?", (stop_id,)).fetchone()
    return row[0] if row else stop_id


if __name__ == "__main__":
    import sys
    # 간단한 수동 테스트: python transit.py <lat1> <lng1> <lat2> <lng2>
    if len(sys.argv) == 5:
        a = list(map(float, sys.argv[1:3]))
        b = list(map(float, sys.argv[3:5]))
        import json
        print(json.dumps(find_bus_directions(*a, *b), ensure_ascii=False, indent=2))
