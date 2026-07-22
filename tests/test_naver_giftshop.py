import sqlite3
import unittest
from io import StringIO
from contextlib import redirect_stdout

from collectors import naver_giftshop


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
          extra_json TEXT,
          overview TEXT,
          homepage TEXT,
          recommend TEXT
        )
        """
    )
    return conn


class FakeNaverClient:
    """query별로 미리 정해둔 지역검색 결과를 돌려주는 가짜 클라이언트."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls = []

    def search_local(self, query, sort):
        self.calls.append((query, sort))
        return self.responses.get(query, [])


GIFTSHOP_ITEM = {
    "title": "<b>소품샵</b> 소소로와",
    "category": "가구,인테리어>인테리어소품",
    "address": "대전광역시 중구 대흥동 178-2 2층",
    "roadAddress": "대전광역시 중구 대종로 451 2층",
    "mapx": "1273550890",
    "mapy": "363371579",
    "telephone": "",
    "link": "https://blog.naver.com/example",
}

NON_GIFTSHOP_ITEM = {
    "title": "소품샵 흉내내는 철물점",
    "category": "생활,편의>철물점",
    "address": "대전광역시 중구 아무데 1",
    "roadAddress": "대전광역시 중구 아무로 1",
    "mapx": "1273550890",
    "mapy": "363371579",
    "telephone": "",
    "link": "https://blog.naver.com/other",
}

# "서구"는 대전뿐 아니라 부산·인천·광주에도 있는 흔한 구 이름이라, 검색어에 "대전"이
# 없으면 다른 도시 결과가 섞여 들어온다 (실제로 겪은 문제: 49건 중 14건이 타 지역).
NON_DAEJEON_ITEM = {
    "title": "소품샵 흉내내는 부산가게",
    "category": "가구,인테리어>인테리어소품",
    "address": "부산광역시 서구 아무데 1",
    "roadAddress": "부산광역시 서구 아무로 1",
    "mapx": "1290550890",
    "mapy": "351071579",
    "telephone": "",
    "link": "https://blog.naver.com/busan",
}


class StablePlaceIdTest(unittest.TestCase):
    def test_deterministic_for_same_input(self):
        id1 = naver_giftshop.stable_place_id("소소로와", "대전광역시 중구 대종로 451 2층", 36.3371579, 127.3550890)
        id2 = naver_giftshop.stable_place_id("소소로와", "대전광역시 중구 대종로 451 2층", 36.3371579, 127.3550890)
        self.assertEqual(id1, id2)
        self.assertTrue(id1.startswith("naver_giftshop_"))

    def test_different_for_different_address(self):
        id1 = naver_giftshop.stable_place_id("소소로와", "주소A", 36.0, 127.0)
        id2 = naver_giftshop.stable_place_id("소소로와", "주소B", 36.0, 127.0)
        self.assertNotEqual(id1, id2)


class IsDuplicateTest(unittest.TestCase):
    def test_same_name_within_30m_is_duplicate(self):
        existing = [("소소로와", 36.3371579, 127.3550890)]
        self.assertTrue(
            naver_giftshop.is_duplicate("소소로와", 36.3371600, 127.3550900, existing)
        )

    def test_same_name_far_away_is_not_duplicate(self):
        existing = [("소소로와", 36.3371579, 127.3550890)]
        self.assertFalse(
            naver_giftshop.is_duplicate("소소로와", 36.40, 127.40, existing)
        )

    def test_different_name_is_not_duplicate(self):
        existing = [("소소로와", 36.3371579, 127.3550890)]
        self.assertFalse(
            naver_giftshop.is_duplicate("잠시다락", 36.3371579, 127.3550890, existing)
        )


class CollectTest(unittest.TestCase):
    def test_filters_out_non_giftshop_category(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        client = FakeNaverClient({"대전 대흥동 소품샵": [NON_GIFTSHOP_ITEM]})

        rows = naver_giftshop.collect(target_count=100, conn=conn, client=client)

        self.assertEqual(rows, [])

    def test_collects_giftshop_item_with_recommend_flag(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        client = FakeNaverClient({"대전 대흥동 소품샵": [GIFTSHOP_ITEM]})

        rows = naver_giftshop.collect(target_count=1, conn=conn, client=client)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category"], "giftshop")
        stored = conn.execute(
            "SELECT recommend, category FROM place WHERE place_id=?", (rows[0]["place_id"],)
        ).fetchone()
        self.assertEqual(stored, ("추천", "giftshop"))

    def test_stops_once_target_reached(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        item2 = dict(GIFTSHOP_ITEM, title="소품샵 잠시다락", mapx="1274550890", mapy="363471579")
        client = FakeNaverClient({
            "대전 대덕구 소품샵": [GIFTSHOP_ITEM],
            "대전 신탄진 소품샵": [item2],
        })

        rows = naver_giftshop.collect(target_count=1, conn=conn, client=client)

        self.assertEqual(len(rows), 1)

    def test_prints_shortfall_when_target_not_reached(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        client = FakeNaverClient({})  # 모든 검색어에 결과 없음

        out = StringIO()
        with redirect_stdout(out):
            naver_giftshop.collect(target_count=100, conn=conn, client=client)

        self.assertIn("목표 100건 중 0건", out.getvalue())

    def test_search_query_includes_daejeon(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        client = FakeNaverClient({})

        naver_giftshop.collect(target_count=100, conn=conn, client=client)

        self.assertTrue(client.calls)
        for query, _sort in client.calls:
            self.assertIn("대전", query)

    def test_filters_out_non_daejeon_address(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        client = FakeNaverClient({"대전 대흥동 소품샵": [NON_DAEJEON_ITEM]})

        rows = naver_giftshop.collect(target_count=100, conn=conn, client=client)

        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
