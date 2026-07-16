# =====================================================
# realtime_bus.py
# TAGO 실시간 버스 도착예측 조회 (ArvlInfoInqireService)
#
# 배치 수집 대상이 아니라 호출 시점에 실시간으로 조회하는 용도.
# (참고: collectors/tago_bus_routes.py 상단 주석 — 실시간 도착/위치정보는
#  계속 바뀌는 데이터라 DB에 쌓아두지 않고 필요할 때 조회하기로 함)
# =====================================================

import os
import threading
import time

import requests
from dotenv import load_dotenv

load_dotenv()
TAGO_API_KEY = os.getenv("TAGO_API_KEY")

CITY_CODE = 25  # 대전광역시
ARRIVAL_URL = "https://apis.data.go.kr/1613000/ArvlInfoInqireService/getSttnAcctoArvlPrearngeInfoList"
VEHICLE_LOCATION_URL = "https://apis.data.go.kr/1613000/BusLcInfoInqireService/getRouteAcctoBusLcList"
SUCCESS_CACHE_TTL = 20
FAILURE_CACHE_TTL = 5

_realtime_cache = {}
_cache_guard = threading.Lock()
_cache_key_locks = {}


def _request_with_retry(url, params, max_retry=1, timeout=2):
    """실시간 조회는 대화형 상황에서 호출되므로, collectors/common.py의
    request_with_retry(최대 3회, 지수백오프)보다 짧은 타임아웃/재시도로 빠르게 실패한다."""
    for attempt in range(max_retry):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException:
            if attempt + 1 == max_retry:
                return None
            time.sleep(1)
    return None


def clear_realtime_cache():
    """테스트와 명시적 새로고침을 위해 실시간 조회 캐시를 비운다."""
    with _cache_guard:
        _realtime_cache.clear()
        _cache_key_locks.clear()


def _response_items(resp):
    if resp is None:
        return False, []
    try:
        data = resp.json()
    except (TypeError, ValueError):
        return False, []
    if not isinstance(data, dict):
        return False, []
    response = data.get("response")
    if not isinstance(response, dict):
        return False, []
    header = response.get("header", {})
    if not isinstance(header, dict):
        return False, []
    if str(header.get("resultCode")) not in ("0", "00"):
        return False, []

    body = response.get("body", {})
    items = body.get("items", {}) if isinstance(body, dict) else {}
    item = items.get("item", []) if items and not isinstance(items, str) else []
    if isinstance(item, dict):
        item = [item]
    if not isinstance(item, list):
        return False, []
    return True, item


def _cached_fetch(cache_key, empty_value, fetch):
    now = time.monotonic()
    with _cache_guard:
        cached = _realtime_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
        key_lock = _cache_key_locks.setdefault(cache_key, threading.Lock())

    with key_lock:
        now = time.monotonic()
        with _cache_guard:
            cached = _realtime_cache.get(cache_key)
            if cached and cached[0] > now:
                return cached[1]

        try:
            success, value = fetch()
        except Exception:
            success, value = False, empty_value
        if not success:
            value = empty_value
        ttl = SUCCESS_CACHE_TTL if success else FAILURE_CACHE_TTL
        with _cache_guard:
            _realtime_cache[cache_key] = (time.monotonic() + ttl, value)
        return value


def get_stop_arrivals(tago_node_id: str) -> dict:
    """정류소의 노선별 다음 도착 정보와 차량 유형을 반환한다."""
    if not TAGO_API_KEY:
        return {}

    def fetch():
        resp = _request_with_retry(ARRIVAL_URL, {
            "serviceKey": TAGO_API_KEY,
            "cityCode": CITY_CODE,
            "nodeId": tago_node_id,
            "numOfRows": 30,
            "pageNo": 1,
            "_type": "json",
        })
        success, items = _response_items(resp)
        if not success:
            return False, {}

        arrivals = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                route_id = item["routeid"]
                arrivals[route_id] = {
                    "minutes": float(item["arrtime"]) / 60,
                    "arrprevstationcnt": int(item["arrprevstationcnt"]),
                    "vehicle_type": item.get("vehicletp"),
                }
            except (KeyError, TypeError, ValueError):
                continue
        return True, arrivals

    return _cached_fetch(("arrivals", tago_node_id), {}, fetch)


def get_arrival_info(tago_node_id: str, route_id: str) -> dict | None:
    """특정 노선의 다음 도착 메타데이터를 반환한다."""
    return get_stop_arrivals(tago_node_id).get(route_id)


def get_arrival_minutes(tago_node_id: str, route_id: str) -> float | None:
    """기존 호출자를 위한 도착 예정 분 단위 호환 wrapper."""
    info = get_arrival_info(tago_node_id, route_id)
    return info.get("minutes") if info else None


def get_route_vehicle_locations(route_id: str) -> list:
    """노선에서 운행 중인 차량의 현재 정류소 순서와 위치를 반환한다."""
    if not TAGO_API_KEY:
        return []

    def fetch():
        resp = _request_with_retry(VEHICLE_LOCATION_URL, {
            "serviceKey": TAGO_API_KEY,
            "cityCode": CITY_CODE,
            "routeId": route_id,
            "numOfRows": 100,
            "pageNo": 1,
            "_type": "json",
        })
        success, items = _response_items(resp)
        if not success:
            return False, []

        locations = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                locations.append({
                    "vehicle_no": item["vehicleno"],
                    "node_order": int(item["nodeord"]),
                    "stop_id": item.get("nodeid"),
                    "lat": item.get("gpslati"),
                    "lng": item.get("gpslong"),
                })
            except (KeyError, TypeError, ValueError):
                continue
        return True, locations

    return _cached_fetch(("vehicles", route_id), [], fetch)


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3:
        print(get_arrival_minutes(sys.argv[1], sys.argv[2]))
    else:
        print("사용법: python realtime_bus.py <TAGO nodeId> <TAGO routeId>")
