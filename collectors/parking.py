# =====================================================
# parking.py
# 대전 공영주차장 공공데이터 API를 이용하여 주차장 정보를 수집하는 모듈
#
# 주요 기능
# 1. 대전 공영주차장 API 호출(XML)
# 2. 페이지별 데이터 자동 수집
# 3. API에 없는 고유 ID 생성(SHA1 해시)
# 4. 운영시간 및 요금 정보 가공
# 5. SQLite parking 테이블에 저장(Upsert)
# =====================================================

import hashlib                    # 문자열을 해시값으로 변환하기 위한 모듈
import os                         # 환경변수 읽기
import xml.etree.ElementTree as ET  # XML 데이터 파싱

from dotenv import load_dotenv    # .env 파일 읽기

# 공통 함수 가져오기
from common import paginate, request_with_retry, save_raw, upsert_parking

# .env 파일 로드
load_dotenv()

# 대전 공영주차장 API 인증키
DAEJEON_PARKING_API_KEY = os.getenv("DAEJEON_PARKING_API_KEY")

# 공영주차장 API 주소
BASE_URL = "http://apis.data.go.kr/6300000/pis/parkinglotIF"

# API에서 한 번에 가져올 최대 데이터 개수
MAX_NUM_OF_ROWS = 50


# 주차장 고유 ID 생성
def _make_parking_id(name: str, address: str) -> str:

    # API에서 고유 ID를 제공하지 않아
    # 이름 + 주소를 이용하여 SHA1 해시 생성
    digest = hashlib.sha1(
        f"{name}|{address}".encode("utf-8")
    ).hexdigest()[:16]

    # parking_id 반환
    return f"daejeon_parking_{digest}"


# XML 태그 값을 읽는 함수
def _text(item: ET.Element, tag: str) -> str | None:

    # 해당 태그 찾기
    el = item.find(tag)

    # 태그가 없거나 값이 없으면 None 반환
    if el is None or el.text is None:
        return None

    # 앞뒤 공백 제거 후 반환
    return el.text.strip()


# 주차요금 문자열 생성
def _build_fee(item: ET.Element) -> str | None:

    # 기본 시간
    base_time = _text(item, "baseTime")

    # 기본 요금
    base_rate = _text(item, "baseRate")

    # 추가 시간
    add_time = _text(item, "addTime")

    # 추가 요금
    add_rate = _text(item, "addRate")

    # 기본 정보가 없으면 요금 생성 불가
    if not (base_time and base_rate):
        return None

    # 기본요금 문자열 생성
    fee = f"기본 {base_time}분 {base_rate}원"

    # 추가요금 정보가 있으면 이어 붙임
    if add_time and add_rate:
        fee += f", 추가 {add_time}분당 {add_rate}원"

    return fee


# 운영시간 문자열 생성
def _build_operate_time(item: ET.Element) -> str | None:

    # 운영시간 저장 리스트
    parts = []

    # 평일 / 토요일 / 공휴일 운영시간 처리
    for label, open_tag, close_tag in [

        ("평일", "weekdayOpenTime", "weekdayCloseTime"),

        ("토요일", "satOpenTime", "satCloseTime"),

        ("공휴일", "holidayOpenTime", "holidayCloseTime"),

    ]:

        # 시작시간
        open_t = _text(item, open_tag)

        # 종료시간
        close_t = _text(item, close_tag)

        # 둘 다 존재하면 리스트에 추가
        if open_t and close_t:
            parts.append(f"{label} {open_t}~{close_t}")

    # "평일 ... / 토요일 ..." 형태로 반환
    return " / ".join(parts) if parts else None


# 한 페이지의 주차장 데이터를 가져오는 함수
def fetch_page(page_no, num_of_rows):

    # API 요청 파라미터
    params = {
        "ServiceKey": DAEJEON_PARKING_API_KEY,
        "numOfRows": num_of_rows,
        "pageNo": page_no,
    }

    # API 요청
    resp = request_with_retry(BASE_URL, params)

    # XML 원본 저장
    save_raw("daejeon_parking", page_no, {
        "raw_xml": resp.text
    })

    # XML 파싱
    root = ET.fromstring(resp.text)

    # 주차장 목록 추출
    items = root.findall("./body/item")

    # 전체 데이터 개수 추출
    total_count_el = root.find("./body/totalCount")

    total_count = (
        int(total_count_el.text)
        if total_count_el is not None and total_count_el.text
        else 0
    )

    # (데이터 목록, 전체 개수) 반환
    return items, total_count


# 전체 주차장 데이터를 수집하는 함수
def collect():

    # 모든 페이지 데이터 가져오기
    items = paginate(
        fetch_page,
        num_of_rows=MAX_NUM_OF_ROWS
    )

    # DB에 저장할 리스트
    rows = []

    # 주차장 하나씩 처리
    for it in items:

        # 주차장 이름
        name = _text(it, "name")

        # 주소
        address = _text(it, "address")

        # 총 주차 가능 대수
        total_qty = _text(it, "totalQty")

        rows.append({

            # 생성한 고유 ID
            "parking_id": _make_parking_id(name, address),

            # 주차장 이름
            "name": name,

            # 주소
            "address": address,

            # 위도
            "lat": float(_text(it, "lat")) if _text(it, "lat") else None,

            # 경도
            "lng": float(_text(it, "lon")) if _text(it, "lon") else None,

            # 운영시간
            "operate_time": _build_operate_time(it),

            # 요금
            "fee": _build_fee(it),

            # 주차 가능 대수
            "capacity": int(total_qty)
            if total_qty and total_qty.isdigit()
            else None,

        })

    # parking 테이블 저장
    upsert_parking(rows)

    # 저장한 데이터 반환
    return rows


# 현재 파일을 직접 실행했을 때만 실행
if __name__ == "__main__":
    collect()