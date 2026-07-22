import sqlite3
import unittest

from collectors import blog_concept_enrich as enrich_mod


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


class EnsureGiftshopEnrichmentSchemaTest(unittest.TestCase):
    def test_adds_missing_columns(self):
        conn = make_place_db()
        self.addCleanup(conn.close)

        enrich_mod.ensure_giftshop_enrichment_schema(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(place)")}
        for expected in (
            "concept_tag", "photo_spot", "has_workshop",
            "blog_url_1", "blog_url_2", "blog_url_3",
        ):
            self.assertIn(expected, columns)

    def test_is_idempotent(self):
        conn = make_place_db()
        self.addCleanup(conn.close)

        enrich_mod.ensure_giftshop_enrichment_schema(conn)
        enrich_mod.ensure_giftshop_enrichment_schema(conn)  # 두 번째도 에러 없어야 함

        columns = [row[1] for row in conn.execute("PRAGMA table_info(place)")]
        self.assertEqual(columns.count("concept_tag"), 1)


class FakeNaverBlogClient:
    def __init__(self, payloads: dict):
        self.payloads = payloads  # {(query, sort): payload}
        self.calls = []

    def search_blog(self, query, sort):
        self.calls.append((query, sort))
        return self.payloads.get((query, sort), {"items": [], "total": 0})


def fake_extract_fn(place_name, snippets, **kwargs):
    if not snippets:
        return {k: None for k in (
            "concept_tag", "open_time", "close_day", "parking", "photo_spot", "has_workshop"
        )}
    return {
        "concept_tag": "빈티지",
        "open_time": "12:00-19:00",
        "close_day": "매주 월요일",
        "parking": "불가",
        "photo_spot": True,
        "has_workshop": None,
    }


class EnrichTest(unittest.TestCase):
    def _seed_place(self, conn, place_id, **overrides):
        row = {
            "place_id": place_id, "name": "다구로잉", "category": "giftshop",
            "address": "대전 중구", "lat": 36.0, "lng": 127.0,
            "open_time": None, "close_day": None, "fee": None, "has_parking": None,
            "tel": None, "source_api": "naver_search", "extra_json": "{}",
            "overview": None, "homepage": None, "recommend": "추천",
        }
        row.update(overrides)
        conn.execute(
            """
            INSERT INTO place (place_id, name, category, address, lat, lng, open_time,
                close_day, fee, has_parking, tel, source_api, extra_json, overview,
                homepage, recommend)
            VALUES (:place_id, :name, :category, :address, :lat, :lng, :open_time,
                :close_day, :fee, :has_parking, :tel, :source_api, :extra_json, :overview,
                :homepage, :recommend)
            """,
            row,
        )
        conn.commit()

    def test_only_targets_recommend_rows(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        enrich_mod.ensure_giftshop_enrichment_schema(conn)
        self._seed_place(conn, "p1", recommend="추천")
        self._seed_place(conn, "p2", recommend=None)
        naver_client = FakeNaverBlogClient({
            ("다구로잉 대전 중구", "sim"): {"items": [{"link": "u1", "description": "빈티지 소품샵"}], "total": 1},
            ("다구로잉 대전 중구", "date"): {"items": [{"link": "u1", "description": "빈티지 소품샵"}], "total": 1},
        })

        enrich_mod.enrich(conn=conn, naver_client=naver_client, extract_fn=fake_extract_fn)

        self.assertEqual(len(naver_client.calls), 2)  # p2는 대상이 아니므로 호출 없음

    def test_fills_concept_fields_and_blog_url(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        enrich_mod.ensure_giftshop_enrichment_schema(conn)
        self._seed_place(conn, "p1")
        naver_client = FakeNaverBlogClient({
            ("다구로잉 대전 중구", "sim"): {"items": [{"link": "u1", "description": "빈티지 소품샵"}], "total": 1},
            ("다구로잉 대전 중구", "date"): {"items": [{"link": "u1", "description": "빈티지 소품샵"}], "total": 1},
        })

        enrich_mod.enrich(conn=conn, naver_client=naver_client, extract_fn=fake_extract_fn)

        row = conn.execute(
            "SELECT concept_tag, open_time, close_day, has_parking, photo_spot, has_workshop, blog_url_1 "
            "FROM place WHERE place_id='p1'"
        ).fetchone()
        self.assertEqual(row, ("빈티지", "12:00-19:00", "매주 월요일", 0, 1, None, "u1"))

    def test_does_not_overwrite_existing_open_time(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        enrich_mod.ensure_giftshop_enrichment_schema(conn)
        self._seed_place(conn, "p1", open_time="이미 확인된 시간")
        naver_client = FakeNaverBlogClient({
            ("다구로잉 대전 중구", "sim"): {"items": [{"link": "u1", "description": "빈티지 소품샵"}], "total": 1},
            ("다구로잉 대전 중구", "date"): {"items": [{"link": "u1", "description": "빈티지 소품샵"}], "total": 1},
        })

        enrich_mod.enrich(conn=conn, naver_client=naver_client, extract_fn=fake_extract_fn)

        row = conn.execute("SELECT open_time FROM place WHERE place_id='p1'").fetchone()
        self.assertEqual(row[0], "이미 확인된 시간")

    def test_skips_when_no_snippets_found(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        enrich_mod.ensure_giftshop_enrichment_schema(conn)
        self._seed_place(conn, "p1")
        naver_client = FakeNaverBlogClient({})  # 검색 결과 없음

        enrich_mod.enrich(conn=conn, naver_client=naver_client, extract_fn=fake_extract_fn)

        row = conn.execute("SELECT concept_tag FROM place WHERE place_id='p1'").fetchone()
        self.assertIsNone(row[0])


if __name__ == "__main__":
    unittest.main()
