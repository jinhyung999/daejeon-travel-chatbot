import sqlite3
import unittest

from collectors import common


def make_place_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE place (
          place_id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          category TEXT NOT NULL,
          address TEXT,
          lat REAL,
          lng REAL,
          open_time TEXT,
          close_day TEXT,
          fee TEXT,
          has_parking INTEGER,
          tel TEXT,
          source_api TEXT,
          extra_json TEXT
        )
        """
    )
    return conn


ROW = {
    "place_id": "p1", "name": "테스트", "category": "giftshop", "address": "주소",
    "lat": 36.0, "lng": 127.0, "open_time": None, "close_day": None, "fee": None,
    "has_parking": None, "tel": None, "source_api": "naver_search", "extra_json": "{}",
}


class UpsertPlaceInjectedConnTest(unittest.TestCase):
    def test_writes_to_injected_conn_and_leaves_it_open(self):
        conn = make_place_db()
        self.addCleanup(conn.close)

        common.upsert_place([ROW], conn=conn)

        row = conn.execute("SELECT name FROM place WHERE place_id='p1'").fetchone()
        self.assertEqual(row[0], "테스트")
        # 커넥션이 닫히지 않았어야 추가 쿼리가 가능하다
        conn.execute("SELECT 1")

    def test_empty_rows_returns_zero_without_touching_conn(self):
        conn = make_place_db()
        self.addCleanup(conn.close)

        result = common.upsert_place([], conn=conn)

        self.assertEqual(result, {"total": 0, "inserted": 0, "updated": 0})


if __name__ == "__main__":
    unittest.main()
