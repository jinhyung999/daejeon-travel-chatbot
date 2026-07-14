# =====================================================
# tago_bus_routes.py
#
# TAGO 대전 버스 노선 목록과 노선별 경유 정류소를 raw JSON으로 수집한다.
# 이 수집기는 DB를 직접 수정하지 않는다. 전체 수집이 성공한 뒤
# scripts/build_route_stops.py가 원천 캐시를 검증하고 TAGO 단일 스냅샷으로
# DB를 교체한다.
#
# 사용법:
#   python collectors/tago_bus_routes.py
#   python scripts/build_route_stops.py          # dry-run 검증
#   python scripts/build_route_stops.py --apply  # 백업 후 DB 반영
# =====================================================

import os
import time

from dotenv import load_dotenv

from common import request_with_retry, save_raw


load_dotenv()
TAGO_API_KEY = os.getenv("TAGO_API_KEY")

CITY_CODE = 25
ROUTE_LIST_URL = "https://apis.data.go.kr/1613000/BusRouteInfoInqireService/getRouteNoList"
ROUTE_STOPS_URL = "https://apis.data.go.kr/1613000/BusRouteInfoInqireService/getRouteAcctoThrghSttnList"


def _items_from_payload(data: dict) -> tuple[list[dict], dict]:
    response = data.get("response", {})
    header = response.get("header", {})
    result_code = str(header.get("resultCode", ""))
    if result_code not in ("0", "00"):
        raise RuntimeError(
            f"TAGO 응답 오류: {result_code} {header.get('resultMsg', '')}"
        )

    body = response.get("body", {})
    if not isinstance(body, dict):
        raise RuntimeError("TAGO 응답 body 형식 오류")
    items = body.get("items", {})
    item = items.get("item", []) if items and not isinstance(items, str) else []
    if isinstance(item, dict):
        item = [item]
    return item, body


def fetch_all_routes() -> list[dict]:
    if not TAGO_API_KEY:
        raise RuntimeError("TAGO_API_KEY가 설정되지 않았습니다.")

    routes = []
    page = 1
    total_count = None
    while True:
        response = request_with_retry(
            ROUTE_LIST_URL,
            {
                "serviceKey": TAGO_API_KEY,
                "cityCode": CITY_CODE,
                "pageNo": page,
                "numOfRows": 100,
                "_type": "json",
            },
        )
        data = response.json()
        items, body = _items_from_payload(data)
        save_raw("tago_route_list", page, data)
        routes.extend(items)
        total_count = int(body.get("totalCount", 0))
        if len(routes) >= total_count:
            break
        if not items:
            raise RuntimeError(
                f"노선 목록 조기 종료: collected={len(routes)}, totalCount={total_count}"
            )
        page += 1
        time.sleep(0.2)

    unique_ids = {str(route.get("routeid")) for route in routes}
    if len(unique_ids) != total_count:
        raise RuntimeError(
            f"노선 목록 중복/누락: unique={len(unique_ids)}, totalCount={total_count}"
        )
    return routes


def _fetch_route_stop_page(route_id: str, page: int, max_retry: int) -> tuple[dict, list[dict], dict]:
    for attempt in range(1, max_retry + 1):
        response = request_with_retry(
            ROUTE_STOPS_URL,
            {
                "serviceKey": TAGO_API_KEY,
                "cityCode": CITY_CODE,
                "routeId": route_id,
                "pageNo": page,
                "numOfRows": 200,
                "_type": "json",
            },
        )
        data = response.json()
        try:
            items, body = _items_from_payload(data)
            return data, items, body
        except RuntimeError as error:
            if attempt == max_retry:
                raise RuntimeError(
                    f"{route_id} page={page}: {max_retry}회 실패"
                ) from error
            wait = 1.5 * attempt
            print(
                f"    [TAGO 재시도 {attempt}/{max_retry}] "
                f"{route_id} page={page} → {wait:.1f}초 대기"
            )
            time.sleep(wait)
    raise RuntimeError(f"도달할 수 없는 재시도 상태: {route_id}")


def fetch_route_stops(route_id: str, max_retry: int = 5) -> list[dict]:
    all_items = []
    page = 1
    total_count = None
    first_payload = None

    while True:
        payload, items, body = _fetch_route_stop_page(route_id, page, max_retry)
        if first_payload is None:
            first_payload = payload
        all_items.extend(items)
        total_count = int(body.get("totalCount", 0))
        if len(all_items) >= total_count:
            break
        if not items:
            raise RuntimeError(
                f"정류소 목록 조기 종료: routeId={route_id}, "
                f"collected={len(all_items)}, totalCount={total_count}"
            )
        page += 1
        time.sleep(0.2)

    keys = {
        (str(item.get("routeid")), int(item.get("updowncd", 0)), int(item.get("nodeord")))
        for item in all_items
    }
    if len(keys) != total_count:
        raise RuntimeError(
            f"정류소 목록 중복/누락: routeId={route_id}, "
            f"unique={len(keys)}, totalCount={total_count}"
        )

    # build_route_stops.py가 파일 하나만 읽어도 전체 목록을 얻도록 페이지를 합친다.
    combined = {
        "response": {
            "header": first_payload.get("response", {}).get("header", {}),
            "body": {
                "items": {"item": all_items},
                "numOfRows": max(total_count, len(all_items)),
                "pageNo": 1,
                "totalCount": total_count,
            },
        }
    }
    save_raw("tago_route_stops", route_id, combined)
    return all_items


def collect() -> list[dict]:
    routes = fetch_all_routes()
    print(f"대전 TAGO 버스노선 {len(routes)}개 확인")

    failed = []
    total_stops = 0
    for index, route in enumerate(routes, 1):
        route_id = str(route.get("routeid"))
        try:
            total_stops += len(fetch_route_stops(route_id))
        except RuntimeError as error:
            failed.append((route_id, str(error)))
        if index % 20 == 0 or index == len(routes):
            print(f"  진행: {index}/{len(routes)}개 노선")
        time.sleep(0.5)

    if failed:
        details = "; ".join(f"{route_id}: {error}" for route_id, error in failed)
        raise RuntimeError(
            "일부 노선 수집에 실패하여 DB 빌드를 중단합니다. "
            f"실패 {len(failed)}개: {details}"
        )

    print(f"TAGO raw 수집 완료: 노선 {len(routes)}개, 경유 레코드 {total_stops}개")
    print("DB 검증: python scripts/build_route_stops.py")
    print("DB 반영: python scripts/build_route_stops.py --apply")
    return routes


if __name__ == "__main__":
    collect()
