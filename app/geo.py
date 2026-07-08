import math

EARTH_RADIUS_KM = 6371.0
WALK_SPEED_KMH = 4.0
CAR_SPEED_KMH = 20.0  # 대전 시내 평균 주행속도 근사치
CIRCUITY_FACTOR = 1.3  # 실제 도로거리는 직선거리보다 통상 1.2~1.4배라는 근사치 보정값


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """두 좌표 간 직선거리(km). 실제 도로 경로가 아닌 근사치."""
    rad = math.pi / 180.0
    dlat = (lat2 - lat1) * rad
    dlng = (lng2 - lng1) * rad
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * rad) * math.cos(lat2 * rad) * math.sin(dlng / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_KM * c


def road_distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """직선거리에 도로 우회 보정(circuity factor)을 곱한 근사 도로거리(km)."""
    return haversine_km(lat1, lng1, lat2, lng2) * CIRCUITY_FACTOR


def estimate_minutes(distance_km: float, mode: str = "walk") -> int:
    """도로거리(circuity factor 반영된 값) 기준 대략적인 소요시간(분). mode: 'walk' 또는 'car'."""
    speed = WALK_SPEED_KMH if mode == "walk" else CAR_SPEED_KMH
    return round(distance_km / speed * 60)
