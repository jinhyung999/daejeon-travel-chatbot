import json
import time
import sqlite3
from pathlib import Path

import requests

DB_PATH = Path(__file__).parent.parent / "db" / "travel.db"
RAW_DIR = Path(__file__).parent.parent / "data" / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)


def get_conn():
    return sqlite3.connect(DB_PATH)


def request_with_retry(url, params, headers=None, max_retry=3, timeout=10):
    for attempt in range(max_retry):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            wait = 2 ** attempt
            print(f"[retry {attempt + 1}/{max_retry}] {e} → {wait}s 대기")
            time.sleep(wait)
    raise RuntimeError(f"요청 실패(최대 재시도 초과): {url}")


def save_raw(name, page_no, payload: dict):
    path = RAW_DIR / f"{name}_page{page_no}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def paginate(fetch_page_fn, num_of_rows=100, max_pages=200, sleep_sec=0.3):
    """
    fetch_page_fn(page_no, num_of_rows) -> (items: list[dict], total_count: int)
    total_count에 도달할 때까지 자동으로 페이지 반복.
    """
    page_no = 1
    all_items = []
    total_count = None
    while True:
        items, total_count = fetch_page_fn(page_no, num_of_rows)
        if not items:
            break
        all_items.extend(items)
        if total_count is not None and len(all_items) >= total_count:
            break
        page_no += 1
        if page_no > max_pages:
            print("max_pages 도달, 중단")
            break
        time.sleep(sleep_sec)
    return all_items


def upsert_place(rows: list[dict]):
    conn = get_conn()
    cur = conn.cursor()
    cur.executemany("""
        INSERT INTO place (place_id, name, category, address, lat, lng,
                            open_time, close_day, fee, has_parking, tel,
                            source_api, extra_json)
        VALUES (:place_id, :name, :category, :address, :lat, :lng,
                :open_time, :close_day, :fee, :has_parking, :tel,
                :source_api, :extra_json)
        ON CONFLICT(place_id) DO UPDATE SET
            name=excluded.name, category=excluded.category,
            address=excluded.address, lat=excluded.lat, lng=excluded.lng,
            open_time=excluded.open_time, close_day=excluded.close_day,
            fee=excluded.fee, has_parking=excluded.has_parking,
            tel=excluded.tel, source_api=excluded.source_api,
            extra_json=excluded.extra_json
    """, rows)
    conn.commit()
    conn.close()
    print(f"place upsert: {len(rows)}건")


def upsert_event(rows: list[dict]):
    conn = get_conn()
    cur = conn.cursor()
    cur.executemany("""
        INSERT INTO event (event_id, name, place_name, address, lat, lng,
                            start_date, end_date, fee, source_api)
        VALUES (:event_id, :name, :place_name, :address, :lat, :lng,
                :start_date, :end_date, :fee, :source_api)
        ON CONFLICT(event_id) DO UPDATE SET
            name=excluded.name, place_name=excluded.place_name,
            address=excluded.address, lat=excluded.lat, lng=excluded.lng,
            start_date=excluded.start_date, end_date=excluded.end_date,
            fee=excluded.fee, source_api=excluded.source_api
    """, rows)
    conn.commit()
    conn.close()
    print(f"event upsert: {len(rows)}건")


def upsert_parking(rows: list[dict]):
    conn = get_conn()
    cur = conn.cursor()
    cur.executemany("""
        INSERT INTO parking (parking_id, name, address, lat, lng,
                              operate_time, fee, capacity)
        VALUES (:parking_id, :name, :address, :lat, :lng,
                :operate_time, :fee, :capacity)
        ON CONFLICT(parking_id) DO UPDATE SET
            name=excluded.name, address=excluded.address,
            lat=excluded.lat, lng=excluded.lng,
            operate_time=excluded.operate_time, fee=excluded.fee,
            capacity=excluded.capacity
    """, rows)
    conn.commit()
    conn.close()
    print(f"parking upsert: {len(rows)}건")


def upsert_transport(rows: list[dict]):
    conn = get_conn()
    cur = conn.cursor()
    cur.executemany("""
        INSERT INTO transport (stop_id, name, type, lat, lng, routes)
        VALUES (:stop_id, :name, :type, :lat, :lng, :routes)
        ON CONFLICT(stop_id) DO UPDATE SET
            name=excluded.name, type=excluded.type,
            lat=excluded.lat, lng=excluded.lng, routes=excluded.routes
    """, rows)
    conn.commit()
    conn.close()
    print(f"transport upsert: {len(rows)}건")
