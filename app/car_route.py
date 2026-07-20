import math
import os
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

from place_lookup import resolve_place


TMAP_ROUTE_URL = "https://apis.openapi.sk.com/tmap/routes?version=1&format=json"
REQUEST_TIMEOUT_SECONDS = 10
KST = timezone(timedelta(hours=9))

load_dotenv()


def _failure(reason, message, from_place, to_place):
    return {
        "success": False,
        "reason": reason,
        "message": message,
        "from_place": from_place,
        "to_place": to_place,
    }


def _place_result(place):
    return {
        "name": place["name"],
        "lat": place["lat"],
        "lng": place["lng"],
    }


def _nonnegative_int(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not a route total")
    result = int(value)
    if result < 0:
        raise ValueError("route total cannot be negative")
    return result


def _request_summary(start, end, api_key):
    try:
        response = requests.post(
            TMAP_ROUTE_URL,
            headers={"appKey": api_key, "Accept": "application/json"},
            json={
                "startX": str(start["lng"]),
                "startY": str(start["lat"]),
                "endX": str(end["lng"]),
                "endY": str(end["lat"]),
                "reqCoordType": "WGS84GEO",
                "resCoordType": "WGS84GEO",
                "searchOption": "0",
                "trafficInfo": "Y",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException:
        return None, "api_request_failed"

    try:
        payload = response.json()
        properties = payload["features"][0]["properties"]
        return {
            "distance_meters": _nonnegative_int(properties["totalDistance"]),
            "duration_seconds": _nonnegative_int(properties["totalTime"]),
            "taxi_fare_won": _nonnegative_int(properties["taxiFare"]),
        }, None
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return None, "invalid_api_response"


def get_car_route(from_place: str, to_place: str) -> dict:
    if not isinstance(from_place, str) or not isinstance(to_place, str):
        return _failure(
            "invalid_input",
            "출발지와 도착지는 장소명 문자열이어야 합니다.",
            from_place,
            to_place,
        )

    from_name = from_place.strip()
    to_name = to_place.strip()
    if not from_name or not to_name:
        return _failure(
            "invalid_input",
            "출발지와 도착지 장소명을 입력해 주세요.",
            from_name,
            to_name,
        )

    start = resolve_place(from_name)
    if start is None:
        return _failure(
            "place_not_found",
            "출발 장소를 찾을 수 없습니다.",
            from_name,
            to_name,
        )

    end = resolve_place(to_name)
    if end is None:
        return _failure(
            "place_not_found",
            "도착 장소를 찾을 수 없습니다.",
            from_name,
            to_name,
        )

    api_key = os.getenv("TMAP_API_KEY")
    if not api_key:
        return _failure(
            "missing_api_key",
            "T map API 키가 설정되지 않았습니다.",
            from_name,
            to_name,
        )

    summary, error = _request_summary(start, end, api_key)
    if error == "api_request_failed":
        return _failure(
            error,
            "T map 경로 API 요청에 실패했습니다.",
            from_name,
            to_name,
        )
    if error:
        return _failure(
            error,
            "T map 경로 API 응답을 해석할 수 없습니다.",
            from_name,
            to_name,
        )

    distance_meters = summary["distance_meters"]
    duration_seconds = summary["duration_seconds"]
    return {
        "success": True,
        "from_place": _place_result(start),
        "to_place": _place_result(end),
        "distance_meters": distance_meters,
        "distance_km": round(distance_meters / 1000, 1),
        "duration_seconds": duration_seconds,
        "duration_minutes": math.ceil(duration_seconds / 60),
        "taxi_fare_won": summary["taxi_fare_won"],
        "calculated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "source": "TMAP",
    }
