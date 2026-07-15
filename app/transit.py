# =====================================================
# transit.py
# 두 좌표 사이의 버스 이동 방법을 최대 3회 환승까지 탐색하고,
# 총 예상소요시간이 짧은 순으로 상위 경로를 추천하는 모듈
#
# 데이터 근거: bus_route_stop(노선×방향별 정류소 순서) — TAGO
#             getRouteAcctoThrghSttnList 원본을 build_route_stops.py로 적재
#
# 소요시간 계산은 2단계로 나뉜다
# (docs/superpowers/specs/2026-07-14-bus-route-multi-transfer-design.md v2 참고):
#   1단계(_search_candidate_paths/_score_path_static): 정류소 거리×평균속도로
#     빠른 근사치 계산 → 탐색 가지치기 + 1차 순위
#   2단계(Task 4, realtime_bus.py 사용): 첫 승차 구간의 대기시간에만
#     TAGO 실시간 도착예측을 적용. 승차시간(ride_minutes)은 서로 다른 차량을
#     교차검증할 방법이 없어(범위 밖) 항상 정적 근사치를 사용한다.
# =====================================================

import math
import sqlite3
from pathlib import Path

from geo import CAR_SPEED_KMH, estimate_minutes, haversine_km, road_distance_km
from place_lookup import resolve_place
from realtime_bus import get_arrival_minutes

DB_PATH = Path(__file__).parent.parent / "db" / "travel.db"

NEAREST_STOP_K = 10            # 출발/도착 근처 정류소 후보 개수 (k=2였을 때 실제 경로를 놓치는 사례 확인됨)
MAX_NEAREST_STOP_KM = 1.0      # 이보다 먼 정류소는 "근처"로 취급하지 않음
MAX_TRANSFERS = 3
MAX_BEAM_PER_STEP = 200        # 매 단계 종료 후 복합점수 기준으로 남기는 후보 상태 수 상한
MAX_CANDIDATE_PATHS = 200      # 완성된 경로 수집 상한(안전장치, 보통 이 값에 도달하지 않음)
STATIC_PRUNE_KEEP = 15         # 1단계 근사치로 추린 뒤 2단계 실시간 계산을 수행할 후보 수
STATIC_WAIT_ESTIMATE_MIN = 5.0  # 실시간 데이터가 없는 구간에 사용하는 추정 대기시간(분)
ALLOWED_DETOUR_KM = 1.5        # 다음 정류소가 목적지에서 이만큼 더 멀어지는 것까지는 탐색 허용(하드 "가까워져야만" 필터 완화)
TRANSFER_PENALTY_KM_EQUIV = 0.3  # 빔 정렬 시 구간 수에 곱해 더하는 페널티(거리 환산)
TRANSFER_WALK_RADIUS_M = 200   # 서로 다른 nodeId 사이 도보환승을 허용하는 반경
MAX_NEARBY_TRANSFER_STOPS = 4  # 도보환승 후보로 추가하는 인근 정류소 최대 개수

# 도보환승 검색용 공간 격자 한 칸 크기(도 단위).
# _nearby_stop_ids는 ±1 이웃 셀만 검색하므로, 반경 radius_m 이내의 두 점이
# 서로 다른 두 칸 이상 떨어져 배치되면 놓친다(회귀 버그 — 위도/경도에 동일한
# 각도 크기를 쓰면 경도 1도의 실제 거리가 cos(위도)만큼 줄어들어, 위도 기준으로
# 정한 칸 크기가 경도 방향으로는 반경보다 작아질 수 있음).
# round() 기반 격자 키에서 "반경 이내 두 점은 항상 1칸 이내"가 성립하려면
# 각 칸의 실제 폭(위도·경도 방향 모두)이 radius_m 이상이어야 한다. 그래서
# 위도/경도 칸 크기를 따로 계산하고, 경도 칸 크기는 대전 지역 대표 위도의
# cos 보정을 적용한다. 여유 마진을 곱해 부동소수점 경계 문제도 방지한다.
_GRID_REF_LAT_DEG = 36.35        # 대전 지역 대표 위도(경도 격자 크기 cos 보정용)
_METERS_PER_LAT_DEG = 111320.0   # 위도 1도의 대략적인 실제 거리(m)
_GRID_SAFETY_MARGIN = 1.2        # 칸 크기를 반경보다 여유 있게 키우는 마진
STOP_GRID_CELL_LAT_DEG = (TRANSFER_WALK_RADIUS_M * _GRID_SAFETY_MARGIN) / _METERS_PER_LAT_DEG
STOP_GRID_CELL_LNG_DEG = (TRANSFER_WALK_RADIUS_M * _GRID_SAFETY_MARGIN) / (
    _METERS_PER_LAT_DEG * math.cos(math.radians(_GRID_REF_LAT_DEG))
)


def _get_conn():
    return sqlite3.connect(DB_PATH)


def nearest_stops(lat: float, lng: float, k: int = NEAREST_STOP_K, max_km: float = MAX_NEAREST_STOP_KM) -> list[dict]:
    """주어진 좌표에서 가까운 버스정류소 k개 (직선거리 기준)"""
    conn = _get_conn()
    cur = conn.cursor()
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


def _load_stop_coords(cur):
    return {stop_id: (lat, lng) for stop_id, lat, lng in cur.execute(
        "SELECT stop_id, lat, lng FROM transport WHERE lat IS NOT NULL"
    )}


def _load_tago_node_ids(cur):
    return dict(cur.execute(
        "SELECT stop_id, tago_node_id FROM transport WHERE tago_node_id IS NOT NULL"
    ))


def _load_route_meta(cur):
    """route_id -> (route_no, route_type)"""
    return {rid: (no, typ) for rid, no, typ in cur.execute(
        "SELECT route_id, route_no, route_type FROM bus_route"
    )}


def _text_name(cur, stop_id):
    row = cur.execute("SELECT name FROM transport WHERE stop_id=?", (stop_id,)).fetchone()
    return row[0] if row else stop_id


def _grid_key(lat, lng):
    return (round(lat / STOP_GRID_CELL_LAT_DEG), round(lng / STOP_GRID_CELL_LNG_DEG))


def _load_stop_grid(coords):
    """도보환승 반경 검색용 공간 격자. grid[(gy,gx)] = [(stop_id, lat, lng), ...]"""
    grid = {}
    for stop_id, (lat, lng) in coords.items():
        grid.setdefault(_grid_key(lat, lng), []).append((stop_id, lat, lng))
    return grid


def _nearby_stop_ids(coords, grid, stop_id, lat, lng, radius_m=TRANSFER_WALK_RADIUS_M, limit=MAX_NEARBY_TRANSFER_STOPS):
    """stop_id 자신을 제외하고, 반경 radius_m 이내의 가까운 정류소를 거리순 최대 limit개 반환."""
    gy, gx = _grid_key(lat, lng)
    candidates = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            for cand_id, clat, clng in grid.get((gy + dy, gx + dx), []):
                if cand_id == stop_id:
                    continue
                d_km = haversine_km(lat, lng, clat, clng)
                if d_km * 1000 <= radius_m:
                    candidates.append((d_km, cand_id))
    candidates.sort(key=lambda x: x[0])
    return candidates[:limit]


def _boardable_options(by_stop, coords, grid, stop_id, lat, lng):
    """현재 물리적 위치(stop_id)에서 탑승 가능한 (board_stop_id, route_id, updowncd, node_order) 목록.
    stop_id 자신의 노선뿐 아니라, 도보환승 반경 이내의 다른 stop_id가 제공하는 노선도 포함한다
    (문제점 4.2: 동일 stop_id에서만 환승 가능했던 제약 제거)."""
    options = [
        (stop_id, route_id, ud, order)
        for route_id, ud, order in by_stop.get(stop_id, [])
    ]
    for _d_km, near_id in _nearby_stop_ids(coords, grid, stop_id, lat, lng):
        for route_id, ud, order in by_stop.get(near_id, []):
            options.append((near_id, route_id, ud, order))
    return options


def _search_candidate_paths(by_route, by_stop, coords, grid, from_stops, to_stops, max_transfers):
    """출발지 근처 정류소들에서 시작해 최대 (max_transfers+1)구간까지 BFS로 경로 후보를 찾는다.

    매 구간마다 직접 탑승 + 도보환승 탑승 옵션을 모두 고려하고, 목적지 방향으로
    "어느 정도" 가까워지는 정류소(ALLOWED_DETOUR_KM 이내로 멀어지는 것까지 허용)를
    다음 단계 후보로 유지한 뒤, 매 단계 종료 시점에 복합점수(목적지까지 거리 +
    구간 수 페널티) 기준으로 정렬해 상위 MAX_BEAM_PER_STEP개만 남긴다.

    ("목적지에 무조건 가까워져야만 확장"이라는 하드 필터는 실제 환승 경로를
    놓치는 사례가 있어 채택하지 않았다 — 스펙 문서 4-3절 참고.)

    to_stops가 빈 리스트인 경우(목적지 근처에 정류소가 전혀 없는 정상적인
    상황 — nearest_stops가 MAX_NEAREST_STOP_KM 이내에서 아무것도 못 찾을 수
    있음) dist_to_dest가 빈 시퀀스에 min()을 호출해 ValueError가 나는 것을
    막기 위해 즉시 빈 결과를 반환한다. from_stops가 비어 있을 때 frontier가
    비어 루프가 곧바로 끝나는 것과 대칭되는 처리다.
    """
    if not to_stops:
        return []

    to_stop_ids = {s["stop_id"] for s in to_stops}
    to_coords = [(s["lat"], s["lng"]) for s in to_stops]

    def dist_to_dest(lat, lng):
        return min(haversine_km(lat, lng, tlat, tlng) for tlat, tlng in to_coords)

    frontier = [
        {"stop_id": s["stop_id"], "lat": s["lat"], "lng": s["lng"],
         "legs": [], "routes_used": frozenset(), "origin_stop": s}
        for s in from_stops
    ]

    completed = []

    for _ in range(max_transfers + 1):
        if len(completed) >= MAX_CANDIDATE_PATHS or not frontier:
            break

        scored_next = []
        for state in frontier:
            cur_dist = dist_to_dest(state["lat"], state["lng"])

            for board_stop_id, route_id, ud, order in _boardable_options(
                by_stop, coords, grid, state["stop_id"], state["lat"], state["lng"]
            ):
                if (route_id, ud) in state["routes_used"]:
                    continue

                downstream = [(o, sid) for o, sid in by_route.get((route_id, ud), []) if o > order]
                if not downstream:
                    continue

                dest_hit = next(((o, sid) for o, sid in downstream if sid in to_stop_ids), None)
                if dest_hit:
                    o, sid = dest_hit
                    completed.append({
                        "origin_stop": state["origin_stop"],
                        "legs": state["legs"] + [{
                            "route_id": route_id, "updowncd": ud,
                            "board_stop_id": board_stop_id, "board_order": order,
                            "alight_stop_id": sid, "alight_order": o,
                        }],
                    })
                    if len(completed) >= MAX_CANDIDATE_PATHS:
                        break
                    continue

                for o, sid in downstream:
                    scoord = coords.get(sid)
                    if scoord is None:
                        continue
                    d = dist_to_dest(*scoord)
                    if d > cur_dist + ALLOWED_DETOUR_KM:
                        continue
                    score = d + TRANSFER_PENALTY_KM_EQUIV * (len(state["legs"]) + 1)
                    scored_next.append((score, {
                        "stop_id": sid, "lat": scoord[0], "lng": scoord[1],
                        "legs": state["legs"] + [{
                            "route_id": route_id, "updowncd": ud,
                            "board_stop_id": board_stop_id, "board_order": order,
                            "alight_stop_id": sid, "alight_order": o,
                        }],
                        "routes_used": state["routes_used"] | {(route_id, ud)},
                        "origin_stop": state["origin_stop"],
                    }))
            if len(completed) >= MAX_CANDIDATE_PATHS:
                break

        scored_next.sort(key=lambda x: x[0])
        frontier = [s for _, s in scored_next[:MAX_BEAM_PER_STEP]]

    return completed


def _static_leg_minutes(by_route, coords, leg):
    """구간의 정류소 순서대로 좌표 거리를 누적해 평균속도(CAR_SPEED_KMH)로 환산한 근사 소요시간(분).
    실제 도로 굴곡·신호대기 등은 반영하지 않는 근사치다(문제점 4.5) — 최종 결과에서
    항상 ride_estimated=True로 표시해 신뢰도 오인을 방지한다."""
    stops = [sid for o, sid in by_route[(leg["route_id"], leg["updowncd"])]
             if leg["board_order"] <= o <= leg["alight_order"]]
    total_km = 0.0
    for a, b in zip(stops, stops[1:]):
        if a in coords and b in coords:
            total_km += haversine_km(*coords[a], *coords[b])
    return total_km / CAR_SPEED_KMH * 60


def _walk_minutes(lat1, lng1, lat2, lng2):
    """도로 우회계수(road_distance_km)를 적용한 도보 소요시간 근사치.
    직선거리를 그대로 쓰면 도로 반대편 정류소 등에서 과소평가된다(문제점 4.7)."""
    if None in (lat1, lng1, lat2, lng2):
        return 0.0
    return estimate_minutes(road_distance_km(lat1, lng1, lat2, lng2), "walk")


def _score_path_static(by_route, coords, from_stop, to_lat, to_lng, path):
    """1단계 근사치 총소요시간(분). 실시간 API 호출 없이 가지치기/1차 순위용으로만 사용."""
    legs = path["legs"]
    first_board_coord = coords.get(legs[0]["board_stop_id"])
    total = _walk_minutes(from_stop["lat"], from_stop["lng"], *(first_board_coord or (None, None)))

    prev_alight = None
    for leg in legs:
        if prev_alight is not None and prev_alight != leg["board_stop_id"]:
            total += _walk_minutes(*(coords.get(prev_alight) or (None, None)),
                                    *(coords.get(leg["board_stop_id"]) or (None, None)))
        total += STATIC_WAIT_ESTIMATE_MIN
        total += _static_leg_minutes(by_route, coords, leg)
        prev_alight = leg["alight_stop_id"]

    last_alight_coord = coords.get(legs[-1]["alight_stop_id"])
    total += _walk_minutes(*(last_alight_coord or (None, None)), to_lat, to_lng)
    return total


def _cached_arrival(cache, tago_node_id, route_id):
    key = (tago_node_id, route_id)
    if key not in cache:
        cache[key] = get_arrival_minutes(tago_node_id, route_id)
    return cache[key]


def _refine_legs_realtime(by_route, coords, tago_ids, arrival_cache, legs):
    """각 구간의 ride_minutes/wait_minutes를 계산한다.

    - wait_minutes: legs[0](첫 승차 구간)만 실시간 도착예측을 조회한다. 사용자가
      "지금" 그 정류소에 있다는 전제가 성립하므로 미래 시각 문제가 없다(문제점 4.4).
      두 번째 이후 구간은 사용자가 아직 그 정류소에 도착하지 않았으므로 "지금" 조회한
      값이 무의미할 수 있어(4.4 예시) 항상 정적 추정치를 사용한다.
    - ride_minutes: 모든 구간에서 항상 정적 근사치를 사용한다. 서로 다른 정류장의
      실시간 ETA를 빼서 승차시간을 구하는 방식은 차량 단위 교차검증 없이는 같은
      차량인지 보장할 수 없어(문제점 4.3) 이 코드에 그 경로 자체가 없다.
    """
    refined = []
    for i, leg in enumerate(legs):
        static_ride = _static_leg_minutes(by_route, coords, leg)
        board_tago = tago_ids.get(leg["board_stop_id"])

        wait_minutes = STATIC_WAIT_ESTIMATE_MIN
        wait_estimated = True
        if i == 0 and board_tago:
            live_wait = _cached_arrival(arrival_cache, board_tago, leg["route_id"])
            if live_wait is not None:
                wait_minutes = live_wait
                wait_estimated = False

        refined.append({
            **leg,
            "wait_minutes": wait_minutes,
            "wait_estimated": wait_estimated,
            "ride_minutes": static_ride,
            "ride_estimated": True,
        })
    return refined


def recommend_bus_routes(from_place: str, to_place: str, max_transfers: int = MAX_TRANSFERS, max_results: int = 3) -> dict:
    """두 장소 사이의 버스 동선을 최대 max_transfers회 환승까지 탐색해,
    총 예상소요시간이 짧은 순으로 상위 max_results개를 반환한다.
    예외를 던지지 않고 항상 dict를 반환한다."""
    import datetime

    origin = resolve_place(from_place)
    if origin is None:
        return {"error": "place_not_found", "query": from_place}
    dest = resolve_place(to_place)
    if dest is None:
        return {"error": "place_not_found", "query": to_place}

    from_stops = nearest_stops(origin["lat"], origin["lng"])
    to_stops = nearest_stops(dest["lat"], dest["lng"])
    if not from_stops:
        return {"error": "no_nearby_stop", "which": "from"}
    if not to_stops:
        return {"error": "no_nearby_stop", "which": "to"}

    conn = _get_conn()
    cur = conn.cursor()
    by_route, by_stop = _load_route_graph(cur)
    coords = _load_stop_coords(cur)
    grid = _load_stop_grid(coords)
    route_meta = _load_route_meta(cur)
    tago_ids = _load_tago_node_ids(cur)

    from_place_out = {"name": origin["name"], "lat": origin["lat"], "lng": origin["lng"]}
    to_place_out = {"name": dest["name"], "lat": dest["lat"], "lng": dest["lng"]}
    calculated_at = datetime.datetime.now().isoformat(timespec="seconds")

    candidates = _search_candidate_paths(by_route, by_stop, coords, grid, from_stops, to_stops, max_transfers)
    if not candidates:
        conn.close()
        return {"from_place": from_place_out, "to_place": to_place_out, "calculated_at": calculated_at,
                 "routes": [], "reason": "no_route_found"}

    scored = sorted(
        candidates,
        key=lambda p: _score_path_static(by_route, coords, p["origin_stop"], dest["lat"], dest["lng"], p),
    )

    deduped, seen_sequences = [], set()
    for p in scored:
        seq = tuple((leg["route_id"], leg["updowncd"], leg["board_stop_id"], leg["alight_stop_id"]) for leg in p["legs"])
        if seq in seen_sequences:
            continue
        seen_sequences.add(seq)
        deduped.append(p)

    arrival_cache = {}
    finalized = []
    for p in deduped[:STATIC_PRUNE_KEEP]:
        legs = _refine_legs_realtime(by_route, coords, tago_ids, arrival_cache, p["legs"])

        walk_to_board = _walk_minutes(p["origin_stop"]["lat"], p["origin_stop"]["lng"],
                                       *(coords.get(legs[0]["board_stop_id"]) or (None, None)))
        last_alight_coord = coords.get(legs[-1]["alight_stop_id"])
        walk_from_last = _walk_minutes(*(last_alight_coord or (None, None)), dest["lat"], dest["lng"])

        total = walk_to_board
        total_walk = walk_to_board + walk_from_last
        prev_alight = None
        for leg in legs:
            if prev_alight is not None and prev_alight != leg["board_stop_id"]:
                leg["walk_transfer_minutes"] = _walk_minutes(
                    *(coords.get(prev_alight) or (None, None)),
                    *(coords.get(leg["board_stop_id"]) or (None, None)),
                )
            else:
                leg["walk_transfer_minutes"] = 0.0
            total += leg["walk_transfer_minutes"]
            total_walk += leg["walk_transfer_minutes"]
            total += leg["wait_minutes"]
            total += leg["ride_minutes"]
            prev_alight = leg["alight_stop_id"]
        total += walk_from_last

        realtime_components = sum(1 for leg in legs if not leg["wait_estimated"])
        total_components = 2 * len(legs)  # 구간마다 wait+ride 두 요소
        realtime_coverage = realtime_components / total_components if total_components else 0.0
        estimated = any(leg["wait_estimated"] or leg["ride_estimated"] for leg in legs)

        finalized.append({
            "total_minutes": total, "transfer_count": len(legs) - 1,
            "legs": legs, "walk_to_board_minutes": walk_to_board,
            "walk_from_last_stop_minutes": walk_from_last,
            "total_walk_minutes": total_walk,
            "estimated": estimated, "realtime_coverage": realtime_coverage,
        })

    finalized.sort(key=lambda r: (
        round(r["total_minutes"] / 3), r["transfer_count"], r["total_walk_minutes"], r["total_minutes"]
    ))

    routes_out = []
    for r in finalized[:max_results]:
        legs_out = []
        for i, leg in enumerate(r["legs"]):
            route_no, route_type = route_meta.get(leg["route_id"], (leg["route_id"], None))
            legs_out.append({
                "route_id": leg["route_id"], "route_no": route_no, "route_type": route_type,
                "updowncd": leg["updowncd"],
                "board_stop_id": leg["board_stop_id"], "board_stop": _text_name(cur, leg["board_stop_id"]),
                "board_order": leg["board_order"],
                "wait_minutes": round(leg["wait_minutes"], 1), "wait_estimated": leg["wait_estimated"],
                "alight_stop_id": leg["alight_stop_id"], "alight_stop": _text_name(cur, leg["alight_stop_id"]),
                "alight_order": leg["alight_order"],
                "ride_minutes": round(leg["ride_minutes"], 1), "ride_estimated": leg["ride_estimated"],
                "walk_transfer_minutes": round(leg["walk_transfer_minutes"], 1),
            })

        routes_out.append({
            "total_minutes": round(r["total_minutes"], 1),
            "transfer_count": r["transfer_count"],
            "estimated": r["estimated"],
            "realtime_coverage": round(r["realtime_coverage"], 2),
            "walk_to_board_minutes": round(r["walk_to_board_minutes"], 1),
            "walk_from_last_stop_minutes": round(r["walk_from_last_stop_minutes"], 1),
            "legs": legs_out,
        })

    conn.close()
    return {"from_place": from_place_out, "to_place": to_place_out, "calculated_at": calculated_at, "routes": routes_out}


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) == 3:
        result = recommend_bus_routes(sys.argv[1], sys.argv[2])
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("사용법: python transit.py <출발지명> <도착지명>")
