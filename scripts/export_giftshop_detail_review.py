import argparse
import csv
import json
import os
from pathlib import Path
import sqlite3
import sys

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from collectors.kakao_giftshop_detail import KakaoLocalClient, classify_candidate


DEFAULT_DB_PATH = REPO_ROOT / "db" / "travel.db"
DEFAULT_OUTPUT_PATH = REPO_ROOT / "data" / "curation" / "giftshop_detail_review.csv"
FIELDNAMES = [
    "place_id", "name", "address", "lat", "lng",
    "kakao_name", "kakao_address", "kakao_distance_m", "kakao_tel",
    "kakao_place_url", "match_status", "match_error",
    "tel", "open_time", "close_day", "tel_source_url", "hours_source_url",
    "verified_at", "review_status", "review_note",
]


def _giftshops(conn):
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(
        "SELECT place_id, name, address, lat, lng, tel, open_time, close_day, extra_json "
        "FROM place WHERE category='giftshop' ORDER BY place_id"
    )]


def _detail_enrichment(place):
    try:
        extra = json.loads(place["extra_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    detail = extra.get("detail_enrichment") if isinstance(extra, dict) else None
    return detail if isinstance(detail, dict) else {}


def collect_review_rows(conn, client):
    output = []
    for place in _giftshops(conn):
        try:
            documents = client.search_keyword(
                f"대전 {place['name']}", lat=place["lat"], lng=place["lng"]
            )
            candidate = classify_candidate(place, documents)
        except Exception as error:
            candidate = {
                "kakao_name": "", "kakao_address": "", "kakao_distance_m": "",
                "kakao_tel": "", "kakao_place_url": "", "match_status": "error",
                "match_error": str(error),
            }
        detail = _detail_enrichment(place)
        existing_tel_source = detail.get("tel_source_url") or ""
        hours_source = detail.get("hours_source_url") or ""
        if place["tel"] and existing_tel_source:
            suggested_tel = place["tel"]
            tel_source = existing_tel_source
        elif candidate["kakao_tel"] and candidate["kakao_place_url"]:
            suggested_tel = candidate["kakao_tel"]
            tel_source = candidate["kakao_place_url"]
        else:
            suggested_tel = ""
            tel_source = ""
        output.append({
            "place_id": place["place_id"], "name": place["name"],
            "address": place["address"] or "", "lat": place["lat"], "lng": place["lng"],
            **candidate,
            "tel": suggested_tel or "",
            "open_time": (place["open_time"] or "") if hours_source else "",
            "close_day": (place["close_day"] or "") if hours_source else "",
            "tel_source_url": tel_source,
            "hours_source_url": hours_source if (place["open_time"] or place["close_day"]) else "",
            "verified_at": "", "review_status": "pending",
            "review_note": "",
        })
    return output


def export_review_csv(db_path, output_path, client):
    conn = sqlite3.connect(db_path)
    try:
        rows = collect_review_rows(conn, client)
    finally:
        conn.close()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(description="Export giftshop detail review CSV")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)
    client = KakaoLocalClient(os.getenv("KAKAO_REST_API_KEY"))
    count = export_review_csv(args.db, args.output, client)
    print(f"exported={count} output={args.output}")


if __name__ == "__main__":
    main()
