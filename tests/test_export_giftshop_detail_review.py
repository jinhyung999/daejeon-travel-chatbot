import csv
import sqlite3
import unittest
import uuid
from pathlib import Path

from scripts.export_giftshop_detail_review import (
    FIELDNAMES,
    collect_review_rows,
    export_review_csv,
)


def make_db(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE place (place_id TEXT PRIMARY KEY, name TEXT, category TEXT, "
        "address TEXT, lat REAL, lng REAL, tel TEXT, open_time TEXT, close_day TEXT, extra_json TEXT)"
    )
    conn.executemany(
        "INSERT INTO place VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, '{}')",
        [
            ("g1", "도시상점", "giftshop", "대전 서구 둔산로 1", 36.35, 127.37),
            ("r1", "식당", "restaurant", "대전 서구 둔산로 2", 36.35, 127.37),
        ],
    )
    conn.commit()
    return conn


class FakeClient:
    def __init__(self, error=False):
        self.error = error
        self.queries = []

    def search_keyword(self, query, *, lat, lng):
        self.queries.append(query)
        if self.error:
            raise RuntimeError("network down")
        return [{
            "place_name": "도시상점",
            "road_address_name": "대전 서구 둔산로 1",
            "address_name": "",
            "x": str(lng),
            "y": str(lat),
            "phone": "042-111-2222",
            "place_url": "https://place.map.kakao.com/1",
        }]


class GiftshopReviewExportTest(unittest.TestCase):
    def test_exports_only_giftshops_with_pending_status(self):
        conn = make_db()
        self.addCleanup(conn.close)
        client = FakeClient()
        rows = collect_review_rows(conn, client)
        self.assertEqual(["g1"], [row["place_id"] for row in rows])
        self.assertEqual("pending", rows[0]["review_status"])
        self.assertEqual("042-111-2222", rows[0]["tel"])
        self.assertEqual(rows[0]["kakao_place_url"], rows[0]["tel_source_url"])
        self.assertEqual(["대전 도시상점"], client.queries)

    def test_api_error_becomes_error_row_and_collection_continues(self):
        conn = make_db()
        self.addCleanup(conn.close)
        row = collect_review_rows(conn, FakeClient(error=True))[0]
        self.assertEqual("error", row["match_status"])
        self.assertIn("network down", row["match_error"])

    def test_writes_utf8_csv_with_exact_header_without_mutating_database(self):
        unique = uuid.uuid4().hex
        db_path = Path.cwd() / f"test-export-{unique}.db"
        output = Path.cwd() / f"test-export-{unique}.csv"
        self.addCleanup(db_path.unlink, missing_ok=True)
        self.addCleanup(output.unlink, missing_ok=True)
        try:
            conn = make_db(db_path)
            conn.close()
            count = export_review_csv(db_path, output, FakeClient())
            with output.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                rows = list(reader)
            self.assertEqual(1, count)
            self.assertEqual(FIELDNAMES, reader.fieldnames)
            self.assertEqual("도시상점", rows[0]["name"])
            self.assertEqual("", rows[0]["verified_at"])
            check = sqlite3.connect(db_path)
            try:
                stored = check.execute(
                    "SELECT tel, open_time, close_day FROM place WHERE place_id='g1'"
                ).fetchone()
            finally:
                check.close()
            self.assertEqual((None, None, None), stored)
        finally:
            db_path.unlink(missing_ok=True)
            output.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
