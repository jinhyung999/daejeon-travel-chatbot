import math
import os
from datetime import datetime, timedelta

from dotenv import load_dotenv

from common import get_conn, request_with_retry

load_dotenv()
KMA_API_KEY = os.getenv("KMA_API_KEY")
AIRKOREA_API_KEY = os.getenv("AIRKOREA_API_KEY")

KMA_NOWCAST_URL = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst"
KMA_FORECAST_URL = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
AIRKOREA_STATION_LIST_URL = "http://apis.data.go.kr/B552584/MsrstnInfoInqireSvc/getMsrstnList"
AIRKOREA_REALTIME_URL = "http://apis.data.go.kr/B552584/ArpltnInforInqireSvc/getMsrstnAcctoRltmMesureDnsty"

# --- 기상청 위경도 -> 격자좌표(nx, ny) 변환 (KMA 공식 LCC DFS 변환식) ---
_RE = 6371.00877  # 지구 반경(km)
_GRID = 5.0       # 격자 간격(km)
_SLAT1 = 30.0     # 투영 위도1
_SLAT2 = 60.0     # 투영 위도2
_OLON = 126.0     # 기준점 경도
_OLAT = 38.0      # 기준점 위도
_XO = 43          # 기준점 X좌표(격자)
_YO = 136         # 기준점 Y좌표(격자)


def latlon_to_grid(lat: float, lon: float) -> tuple[int, int]:
    deg_rad = math.pi / 180.0
    re = _RE / _GRID
    slat1 = _SLAT1 * deg_rad
    slat2 = _SLAT2 * deg_rad
    olon = _OLON * deg_rad
    olat = _OLAT * deg_rad

    sn = math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(sn)
    sf = math.tan(math.pi * 0.25 + slat1 * 0.5)
    sf = math.pow(sf, sn) * math.cos(slat1) / sn
    ro = math.tan(math.pi * 0.25 + olat * 0.5)
    ro = re * sf / math.pow(ro, sn)

    ra = math.tan(math.pi * 0.25 + lat * deg_rad * 0.5)
    ra = re * sf / math.pow(ra, sn)
    theta = lon * deg_rad - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn

    x = ra * math.sin(theta) + _XO + 0.5
    y = ro - ra * math.cos(theta) + _YO + 0.5
    return int(x), int(y)


# 실제 수집된 place 테이블의 대표 장소 좌표를 기준점으로 사용
# (격자값을 직접 하드코딩하지 않고, 검증된 실좌표를 변환식에 넣어 산출)
_DISTRICT_ANCHORS = {
    "dunsan":           (36.3504850621, 127.3849680306),  # 대전광역시청 시민공원 (둔산동)
    "yuseong":          (36.3628471396, 127.3626169166),  # 갑천 (유성구 구성동)
    "daejeon_station":  (36.3341266098, 127.4293367291),  # 인쇄거리 (동구 정동, 대전역 인근)
    "jung_gu":          (36.3291465966, 127.4278175564),  # 스카이로드 (중구 은행동)
    "seo_gu":           (36.301292, 127.33754),            # 카페멜리사 (서구 관저동)
}

GRID_MAP = {
    key: dict(zip(("nx", "ny"), latlon_to_grid(lat, lon)))
    for key, (lat, lon) in _DISTRICT_ANCHORS.items()
}


def _nearest_forecast_datetime(now: datetime | None = None) -> tuple[str, str]:
    """단기예보(getVilageFcst) 발표시각(3시간 간격) 중 가장 최근 발표 base_date/base_time"""
    base_hours = [2, 5, 8, 11, 14, 17, 20, 23]
    now = now or datetime.now()
    candidate = now - timedelta(minutes=10)  # 발표 후 API 반영 지연 고려
    for hour in reversed(base_hours):
        if candidate.hour >= hour:
            return candidate.strftime("%Y%m%d"), f"{hour:02d}00"
    prev_day = candidate - timedelta(days=1)
    return prev_day.strftime("%Y%m%d"), "2300"


def _nearest_nowcast_datetime(now: datetime | None = None) -> tuple[str, str]:
    """초단기실황(getUltraSrtNcst) 발표시각(매시 정각) 중 가장 최근 발표 base_date/base_time
    (매시 40분 이후 제공되므로 40분 이내면 이전 시각을 사용)"""
    now = now or datetime.now()
    candidate = now - timedelta(minutes=40)
    return candidate.strftime("%Y%m%d"), candidate.strftime("%H00")


def fetch_weather(location_key: str) -> dict:
    nx, ny = GRID_MAP[location_key]["nx"], GRID_MAP[location_key]["ny"]
    result = {"temp": None, "pop": None, "pty": None}

    # 초단기실황: 기온(T1H), 강수형태(PTY) - 지금 시각 실측값
    ncst_date, ncst_time = _nearest_nowcast_datetime()
    ncst_resp = request_with_retry(KMA_NOWCAST_URL, {
        "serviceKey": KMA_API_KEY, "numOfRows": 20, "pageNo": 1, "dataType": "JSON",
        "base_date": ncst_date, "base_time": ncst_time, "nx": nx, "ny": ny,
    })
    for it in ncst_resp.json()["response"]["body"]["items"]["item"]:
        if it.get("category") == "T1H" and it.get("obsrValue") is not None:
            result["temp"] = float(it["obsrValue"])
        elif it.get("category") == "PTY" and it.get("obsrValue") is not None:
            result["pty"] = int(it["obsrValue"])

    # 단기예보: 강수확률(POP) - 실황에는 없어 예보값으로 보완
    fcst_date, fcst_time = _nearest_forecast_datetime()
    fcst_resp = request_with_retry(KMA_FORECAST_URL, {
        "serviceKey": KMA_API_KEY, "numOfRows": 300, "pageNo": 1, "dataType": "JSON",
        "base_date": fcst_date, "base_time": fcst_time, "nx": nx, "ny": ny,
    })
    for it in fcst_resp.json()["response"]["body"]["items"]["item"]:
        if it.get("category") == "POP" and it.get("fcstValue") is not None:
            result["pop"] = int(it["fcstValue"])
            break  # 가장 가까운 예보시각의 첫 POP 값만 사용

    return result


def list_daejeon_stations() -> list[str]:
    """AirKorea 측정소 목록에서 대전 지역 측정소명만 추출 (이름을 하드코딩하지 않고 실조회로 확인)"""
    params = {
        "serviceKey": AIRKOREA_API_KEY,
        "numOfRows": 100,
        "pageNo": 1,
        "returnType": "json",
    }
    resp = request_with_retry(AIRKOREA_STATION_LIST_URL, params)
    items = resp.json()["response"]["body"]["items"]
    return [it["stationName"] for it in items if "대전" in (it.get("addr") or "")]


def fetch_air_quality(station_name: str) -> dict:
    params = {
        "serviceKey": AIRKOREA_API_KEY,
        "stationName": station_name,
        "dataTerm": "DAILY",
        "pageNo": 1,
        "numOfRows": 1,
        "returnType": "json",
        "ver": "1.3",
    }
    resp = request_with_retry(AIRKOREA_REALTIME_URL, params)
    item = resp.json()["response"]["body"]["items"][0]

    def _to_int(value):
        return int(value) if value and value.isdigit() else None

    return {
        "pm10": _to_int(item.get("pm10Value")),
        "pm25": _to_int(item.get("pm25Value")),
        "khai_grade": _to_int(item.get("khaiGrade")),
    }


def collect_snapshot(location_key: str, station_name: str | None = None):
    weather = fetch_weather(location_key)
    # 대기질 API 키가 아직 없어 station_name 없이 호출하면 대기질은 비워둔다
    air = fetch_air_quality(station_name) if station_name else {"pm10": None, "pm25": None, "khai_grade": None}
    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO weather (location_key, fetched_at, temp, pop, pty, pm10, pm25, khai_grade)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(location_key, fetched_at) DO UPDATE SET
            temp=excluded.temp, pop=excluded.pop, pty=excluded.pty,
            pm10=excluded.pm10, pm25=excluded.pm25, khai_grade=excluded.khai_grade
    """, (location_key, fetched_at, weather["temp"], weather["pop"], weather["pty"],
          air["pm10"], air["pm25"], air["khai_grade"]))
    conn.commit()
    conn.close()
    print(f"weather snapshot 저장: {location_key} @ {fetched_at}")


if __name__ == "__main__":
    print("GRID_MAP:", GRID_MAP)
