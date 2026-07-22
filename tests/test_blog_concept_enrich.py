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


if __name__ == "__main__":
    unittest.main()
