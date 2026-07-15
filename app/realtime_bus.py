# =====================================================
# realtime_bus.py
# TAGO 실시간 버스 도착예측 조회 (ArvlInfoInqireService)
#
# 배치 수집 대상이 아니라 호출 시점에 실시간으로 조회하는 용도.
# (참고: collectors/tago_bus_routes.py 상단 주석 — 실시간 도착/위치정보는
#  계속 바뀌는 데이터라 DB에 쌓아두지 않고 필요할 때 조회하기로 함)
# =====================================================

import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()
TAGO_API_KEY = os.getenv("TAGO_API_KEY")

CITY_CODE = 25  # 대전광역시
ARRIVAL_URL = "https://apis.data.go.kr/1613000/ArvlInfoInqireService/getSttnAcctoArvlPrearngeInfoList"


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


def get_arrival_minutes(tago_node_id: str, route_id: str) -> float | None:
    """특정 정류소(TAGO nodeId)에서 특정 노선(TAGO routeId)의 다음 버스가
    몇 분 후 도착 예정인지 조회한다. 실시간 데이터가 없거나(운행 종료 등)
    요청 자체가 실패하면 None을 반환한다."""
    if not TAGO_API_KEY:
        return None

    resp = _request_with_retry(ARRIVAL_URL, {
        "serviceKey": TAGO_API_KEY,
        "cityCode": CITY_CODE,
        "nodeId": tago_node_id,
        "numOfRows": 30,
        "pageNo": 1,
        "_type": "json",
    })
    if resp is None:
        return None

    try:
        data = resp.json()
    except ValueError:
        return None
    header = data.get("response", {}).get("header", {})
    if str(header.get("resultCode")) not in ("0", "00"):
        return None

    body = data.get("response", {}).get("body", {})
    items = body.get("items", {}) if isinstance(body, dict) else {}
    item = items.get("item", []) if items and not isinstance(items, str) else []
    if isinstance(item, dict):
        item = [item]

    for it in item:
        if it.get("routeid") == route_id:
            arrtime = it.get("arrtime")
            if arrtime is not None:
                try:
                    return float(arrtime) / 60
                except (TypeError, ValueError):
                    return None

    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3:
        print(get_arrival_minutes(sys.argv[1], sys.argv[2]))
    else:
        print("사용법: python realtime_bus.py <TAGO nodeId> <TAGO routeId>")
