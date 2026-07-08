# =====================================================
# common.py
# 공공데이터 API 수집 시 공통으로 사용하는 유틸리티 모듈
#
# 주요 기능
# 1. API 요청 및 재시도(request_with_retry)
# 2. 원본 JSON/XML 저장(save_raw)
# 3. 페이지 자동 수집(paginate)
# 4. SQLite DB 저장(Upsert)
# =====================================================


import json                  # JSON 데이터 저장/읽기용 모듈
import time                  # 대기(sleep) 기능 사용
import sqlite3               # SQLite 데이터베이스 사용
from pathlib import Path     # 운영체제와 상관없이 경로 관리

import requests              # HTTP API 요청 라이브러리

# 현재 파일 기준으로 DB 파일 경로 설정
DB_PATH = Path(__file__).parent.parent / "db" / "travel.db"

# 원본 API 데이터를 저장할 폴더 경로
RAW_DIR = Path(__file__).parent.parent / "data" / "raw"

# raw 폴더가 없으면 자동 생성
RAW_DIR.mkdir(parents=True, exist_ok=True)


# SQLite 데이터베이스 연결 객체 반환
def get_conn():
    return sqlite3.connect(DB_PATH)


# API 요청 함수 (실패 시 자동 재시도)
def request_with_retry(url, params, headers=None, max_retry=3, timeout=10):

    # 최대 max_retry 횟수만큼 반복
    for attempt in range(max_retry):
        try:
            # GET 방식으로 API 요청
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)

            # HTTP 오류 발생 시 예외 발생
            resp.raise_for_status()

            # 요청 성공 시 Response 반환
            return resp

        # 요청 실패 시
        except requests.RequestException as e:

            # 재시도 대기시간 (1→2→4초)
            wait = 2 ** attempt

            # 재시도 안내 출력
            print(f"[retry {attempt + 1}/{max_retry}] {e} → {wait}s 대기")

            # 잠시 대기 후 다시 요청
            time.sleep(wait)

    # 최대 재시도 후에도 실패하면 예외 발생
    raise RuntimeError(f"요청 실패(최대 재시도 초과): {url}")


# API 원본 JSON 저장
def save_raw(name, page_no, payload: dict):

    # 저장 파일명 생성
    path = RAW_DIR / f"{name}_page{page_no}.json"

    # UTF-8 형식으로 저장
    with open(path, "w", encoding="utf-8") as f:

        # 한글이 깨지지 않도록 JSON 저장
        json.dump(payload, f, ensure_ascii=False)


# 여러 페이지의 데이터를 자동으로 수집하는 함수
def paginate(fetch_page_fn, num_of_rows=100, max_pages=200, sleep_sec=0.3):
    """
    fetch_page_fn(page_no, num_of_rows) -> (items: list[dict], total_count: int)
    total_count에 도달할 때까지 자동으로 페이지 반복.
    """

    # 시작 페이지
    page_no = 1

    # 전체 데이터를 저장할 리스트
    all_items = []

    # 전체 데이터 개수
    total_count = None

    while True:

        # 현재 페이지 데이터 가져오기
        items, total_count = fetch_page_fn(page_no, num_of_rows)

        # 데이터가 없으면 종료
        if not items:
            break

        # 현재 페이지 데이터를 전체 리스트에 추가
        all_items.extend(items)

        # 전체 데이터를 모두 가져오면 종료
        if total_count is not None and len(all_items) >= total_count:
            break

        # 다음 페이지
        page_no += 1

        # 최대 페이지를 넘으면 종료
        if page_no > max_pages:
            print("max_pages 도달, 중단")
            break

        # API 과부하 방지를 위해 잠시 대기
        time.sleep(sleep_sec)

    # 전체 데이터 반환
    return all_items


# place 테이블에 데이터 저장 (없으면 INSERT, 있으면 UPDATE)
def upsert_place(rows: list[dict]):

    # DB 연결
    conn = get_conn()

    # Cursor 생성
    cur = conn.cursor()

    # 여러 행을 한 번에 실행
    cur.executemany("""
        INSERT INTO place (place_id, name, category, address, lat, lng,
                            open_time, close_day, fee, has_parking, tel,
                            source_api, extra_json)
        VALUES (:place_id, :name, :category, :address, :lat, :lng,
                :open_time, :close_day, :fee, :has_parking, :tel,
                :source_api, :extra_json)

        -- place_id가 이미 존재하면 UPDATE 수행
        ON CONFLICT(place_id) DO UPDATE SET
            name=excluded.name, category=excluded.category,
            address=excluded.address, lat=excluded.lat, lng=excluded.lng,
            open_time=excluded.open_time, close_day=excluded.close_day,
            fee=excluded.fee, has_parking=excluded.has_parking,
            tel=excluded.tel, source_api=excluded.source_api,
            extra_json=excluded.extra_json
    """, rows)

    # DB 반영
    conn.commit()

    # 연결 종료
    conn.close()

    # 저장 결과 출력
    print(f"place upsert: {len(rows)}건")


# event 테이블에 데이터 저장 (없으면 INSERT, 있으면 UPDATE)
def upsert_event(rows: list[dict]):

    conn = get_conn()
    cur = conn.cursor()

    cur.executemany("""
        INSERT INTO event (event_id, name, place_name, address, lat, lng,
                            start_date, end_date, fee, source_api)
        VALUES (:event_id, :name, :place_name, :address, :lat, :lng,
                :start_date, :end_date, :fee, :source_api)

        -- event_id가 이미 존재하면 UPDATE 수행
        ON CONFLICT(event_id) DO UPDATE SET
            name=excluded.name, place_name=excluded.place_name,
            address=excluded.address, lat=excluded.lat, lng=excluded.lng,
            start_date=excluded.start_date, end_date=excluded.end_date,
            fee=excluded.fee, source_api=excluded.source_api
    """, rows)

    conn.commit()
    conn.close()

    print(f"event upsert: {len(rows)}건")


# parking 테이블에 데이터 저장 (없으면 INSERT, 있으면 UPDATE)
def upsert_parking(rows: list[dict]):

    conn = get_conn()
    cur = conn.cursor()

    cur.executemany("""
        INSERT INTO parking (parking_id, name, address, lat, lng,
                              operate_time, fee, capacity)
        VALUES (:parking_id, :name, :address, :lat, :lng,
                :operate_time, :fee, :capacity)

        -- parking_id가 이미 존재하면 UPDATE 수행
        ON CONFLICT(parking_id) DO UPDATE SET
            name=excluded.name, address=excluded.address,
            lat=excluded.lat, lng=excluded.lng,
            operate_time=excluded.operate_time, fee=excluded.fee,
            capacity=excluded.capacity
    """, rows)

    conn.commit()
    conn.close()

    print(f"parking upsert: {len(rows)}건")


# transport 테이블에 데이터 저장 (없으면 INSERT, 있으면 UPDATE)
def upsert_transport(rows: list[dict]):

    conn = get_conn()
    cur = conn.cursor()

    cur.executemany("""
        INSERT INTO transport (stop_id, name, type, lat, lng, routes)
        VALUES (:stop_id, :name, :type, :lat, :lng, :routes)

        -- stop_id가 이미 존재하면 UPDATE 수행
        ON CONFLICT(stop_id) DO UPDATE SET
            name=excluded.name, type=excluded.type,
            lat=excluded.lat, lng=excluded.lng, routes=excluded.routes
    """, rows)

    conn.commit()
    conn.close()

    print(f"transport upsert: {len(rows)}건")