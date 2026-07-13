# =====================================================
# tago_bus_routes.py
# 국토교통부 TAGO(국가대중교통정보센터) 버스노선정보로
# transport.routes(경유노선) 결측을 보강하는 모듈
#
# 흐름:
#   1. BusRouteInfoInqireService/getRouteNoList (cityCode=25=대전)
#      로 전체 노선 목록(routeid, routeno, routetp) 수집
#   2. 노선별로 BusRouteInfoInqireService/getRouteAcctoThrghSttnList
#      호출 → 그 노선이 지나가는 정류소 목록(이름+좌표) 수집
#   3. TAGO 정류소는 기존 transport 테이블(대전시 자체 BIS API 수집,
#      NODEID 체계가 달라 ID로 직접 매칭 불가)과 이름+좌표 근접(30m)으로
#      매칭 → 매칭된 stop_id에 경유노선 번호들을 콤마로 묶어 UPDATE
#   4. TAGO 전체 정류소(3,061개)가 기존 transport(1,425개)보다 훨씬 많음이
#      확인됨 — 매칭 안 된 TAGO 정류소는 "기존 데이터의 결측"이 아니라
#      "애초에 기존 DB에 없던 정류소"이므로, 신규 transport 행으로 추가
#      (stop_id는 원본 출처가 구분되게 tago_{nodeid} 사용, 기존 ID 체계와
#      형식이 달라 충돌 없음)
#
# TAGO 버스도착정보(ArvlInfoInqireService)/버스위치정보(BusLcInfoInqireService)는
# 실시간으로 계속 바뀌는 데이터라 이 스크립트에서 다루지 않음
# (DB에 정적으로 쌓아둘 대상이 아니라, 챗봇 응답 시점에 실시간 조회해야 하는 영역)
#
# 사용법:
#   python tago_bus_routes.py           # dry-run (매칭 통계만 출력)
#   python tago_bus_routes.py --apply   # 실제 DB 반영
# =====================================================

import math
import os
import re
import sys
import time

from dotenv import load_dotenv

from common import get_conn, request_with_retry, save_raw, upsert_transport

load_dotenv()
TAGO_API_KEY = os.getenv("TAGO_API_KEY")

CITY_CODE = 25  # TAGO 도시코드: 대전광역시
ROUTE_LIST_URL = "https://apis.data.go.kr/1613000/BusRouteInfoInqireService/getRouteNoList"
ROUTE_STOPS_URL = "https://apis.data.go.kr/1613000/BusRouteInfoInqireService/getRouteAcctoThrghSttnList"

MATCH_RADIUS_M = 30

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_name(name: str) -> str:
    return _WHITESPACE_RE.sub("", name or "").strip().lower()


def _haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _grid_key(lat, lng):
    return (round(lat / 0.001), round(lng / 0.001))


def fetch_all_routes() -> list[dict]:
    routes, page = [], 1
    while True:
        resp = request_with_retry(ROUTE_LIST_URL, {
            "serviceKey": TAGO_API_KEY, "cityCode": CITY_CODE,
            "pageNo": page, "numOfRows": 100, "_type": "json",
        })
        data = resp.json()
        save_raw("tago_route_list", page, data)
        items = data.get("response", {}).get("body", {}).get("items", {})
        item = items.get("item", []) if items and not isinstance(items, str) else []
        if isinstance(item, dict):
            item = [item]
        if not item:
            break
        routes.extend(item)
        total = data["response"]["body"].get("totalCount", 0)
        if len(routes) >= total:
            break
        page += 1
        time.sleep(0.2)
    return routes


def fetch_route_stops(route_id: str, max_retry=5) -> list[dict]:
    # "가용한 세션이 존재하지 않습니다" 등 TAGO 쪽 동시 요청 제한(resultCode!='00')은
    # request_with_retry가 잡아내는 HTTP/네트워크 오류가 아니라 응답 본문 안의
    # 애플리케이션 레벨 오류라 여기서 별도로 재시도 처리
    for attempt in range(1, max_retry + 1):
        resp = request_with_retry(ROUTE_STOPS_URL, {
            "serviceKey": TAGO_API_KEY, "cityCode": CITY_CODE, "routeId": route_id,
            "pageNo": 1, "numOfRows": 200, "_type": "json",
        })
        data = resp.json()
        save_raw("tago_route_stops", route_id, data)

        result_code = str(data.get("response", {}).get("header", {}).get("resultCode", ""))
        if result_code not in ("0", "00"):
            msg = data.get("response", {}).get("header", {}).get("resultMsg", "알 수 없는 오류")
            wait = 1.5 * attempt
            print(f"    [세션제한 재시도 {attempt}/{max_retry}] {route_id}: {msg} → {wait}s 대기")
            time.sleep(wait)
            continue

        body = data.get("response", {}).get("body", {})
        items = body.get("items", {}) if isinstance(body, dict) else {}
        item = items.get("item", []) if items and not isinstance(items, str) else []
        if isinstance(item, dict):
            item = [item]
        return item

    print(f"    [포기] {route_id}: {max_retry}회 재시도 후에도 실패, 이 노선은 스킵")
    return []


def _build_transport_index(cur):
    rows = cur.execute("SELECT stop_id, name, lat, lng FROM transport WHERE lat IS NOT NULL").fetchall()
    index = {}
    for stop_id, name, lat, lng in rows:
        index.setdefault(_grid_key(lat, lng), []).append((stop_id, _normalize_name(name), lat, lng))
    return index


def _match_stop(index, name, lat, lng):
    norm = _normalize_name(name)
    gy, gx = _grid_key(lat, lng)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            for stop_id, cand_norm, cand_lat, cand_lng in index.get((gy + dy, gx + dx), []):
                if cand_norm != norm:
                    continue
                if _haversine_m(lat, lng, cand_lat, cand_lng) <= MATCH_RADIUS_M:
                    return stop_id
    return None


def _process_route(route, index, stop_routes, new_stops):
    route_id = route.get("routeid")
    route_label = f"{route.get('routeno')}({route.get('routetp')})"
    try:
        stops = fetch_route_stops(route_id)
    except RuntimeError as e:
        print(f"    [실패] {route_label} 요청 자체 실패: {e}")
        return False
    if not stops:
        return False  # 실패(포기) 표시 — 상위에서 재시도 대상으로 기록

    for s in stops:
        lat, lng = s.get("gpslati"), s.get("gpslong")
        name, nodeid = s.get("nodenm"), s.get("nodeid")
        if lat is None or lng is None or not name or not nodeid:
            continue
        stop_id = _match_stop(index, name, lat, lng)
        if stop_id:
            stop_routes.setdefault(stop_id, set()).add(route_label)
        else:
            # 기존 DB에 없는 정류소 → 신규 행 후보로 누적
            tago_stop_id = f"tago_{nodeid}"
            entry = new_stops.setdefault(tago_stop_id, {"name": name, "lat": lat, "lng": lng, "routes": set()})
            entry["routes"].add(route_label)
    return True


def collect(apply=False):
    conn = get_conn()
    cur = conn.cursor()
    index = _build_transport_index(cur)

    routes = fetch_all_routes()
    print(f"대전 버스노선 {len(routes)}개 확인")

    stop_routes = {}   # 기존 transport.stop_id -> set(routeno 라벨)
    new_stops = {}      # tago_{nodeid} -> {name, lat, lng, routes:set()}
    failed_routes = []

    for i, route in enumerate(routes, 1):
        ok = _process_route(route, index, stop_routes, new_stops)
        if not ok:
            failed_routes.append(route)
        if i % 20 == 0:
            print(f"  진행: {i}/{len(routes)}개 노선 처리")
        time.sleep(0.5)

    # 세션제한 등으로 완전히 포기했던 노선은 한 번 더 몰아서 재시도
    if failed_routes:
        print(f"\n1차 실패 노선 {len(failed_routes)}개 재시도 중...")
        still_failed = []
        for route in failed_routes:
            time.sleep(2)
            if not _process_route(route, index, stop_routes, new_stops):
                still_failed.append(route.get("routeno"))
        if still_failed:
            print(f"  재시도 후에도 실패: {still_failed} (수동 재실행 필요)")

    total_existing = cur.execute("SELECT COUNT(*) FROM transport").fetchone()[0]
    print(f"\n기존 정류소 중 routes 매칭: {len(stop_routes)}건 / {total_existing}건")
    print(f"기존 DB에 없던 신규 정류소(TAGO에만 존재): {len(new_stops)}건")

    if apply:
        for stop_id, labels in stop_routes.items():
            cur.execute(
                "UPDATE transport SET routes=? WHERE stop_id=?",
                (",".join(sorted(labels)), stop_id),
            )
        conn.commit()

        new_rows = [
            {
                "stop_id": sid, "name": e["name"], "type": "bus",
                "lat": e["lat"], "lng": e["lng"], "routes": ",".join(sorted(e["routes"])),
            }
            for sid, e in new_stops.items()
        ]
        conn.close()
        if new_rows:
            upsert_transport(new_rows)

        conn = get_conn()
        cur = conn.cursor()
        filled = cur.execute("SELECT COUNT(*) FROM transport WHERE routes IS NOT NULL AND routes<>''").fetchone()[0]
        total = cur.execute("SELECT COUNT(*) FROM transport").fetchone()[0]
        print(f"\n반영 완료. transport 총 {total}건, routes 채움률 {filled}/{total}")
        conn.close()
    else:
        print("\n[dry-run] 실제 반영하려면: python tago_bus_routes.py --apply")
        conn.close()

    return stop_routes, new_stops


if __name__ == "__main__":
    collect(apply="--apply" in sys.argv)
