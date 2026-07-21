import csv
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import re
import sqlite3
import unittest
import uuid

from scripts.import_restaurant_recommendations import ensure_recommend_schema
from scripts.init_db import init_db


REPO_ROOT = Path(__file__).resolve().parents[1]


def make_place_db(database=":memory:"):
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
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
          homepage TEXT
        )
        """
    )
    return conn


class RecommendSchemaTest(unittest.TestCase):
    def test_schema_migration_is_idempotent(self):
        conn = make_place_db()
        self.addCleanup(conn.close)

        ensure_recommend_schema(conn)
        ensure_recommend_schema(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(place)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(place)")}
        self.assertIn("recommend", columns)
        self.assertIn("idx_place_category_recommend", indexes)

    def test_init_db_migrates_legacy_place_table_before_schema_index(self):
        db_path = REPO_ROOT / f".tmp_recommend_{uuid.uuid4().hex}.db"
        try:
            conn = make_place_db(db_path)
            conn.close()

            with redirect_stdout(StringIO()):
                init_db(db_path=db_path)

            conn = sqlite3.connect(db_path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(place)")}
                indexes = {row[1] for row in conn.execute("PRAGMA index_list(place)")}
                self.assertIn("recommend", columns)
                self.assertIn("idx_place_category_recommend", indexes)
            finally:
                conn.close()
        finally:
            db_path.unlink(missing_ok=True)


class ApprovedCandidateDataTest(unittest.TestCase):
    def test_approved_candidate_snapshot_has_only_required_fields(self):
        path = REPO_ROOT / "data" / "curation" / "restaurant_candidates_approved.csv"
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))

        self.assertEqual(len(rows), 438)
        self.assertEqual(
            set(rows[0]),
            {
                "district", "name", "category", "address", "road_address",
                "latitude", "longitude", "naver_link",
                "recommendation_score", "recommendation_reason",
            },
        )
        self.assertNotIn("recent_blog_count", rows[0])
        self.assertTrue(all(row["name"].strip() for row in rows))

    def test_approved_candidate_reasons_exclude_blog_count_wording(self):
        path = REPO_ROOT / "data" / "curation" / "restaurant_candidates_approved.csv"
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))

        count_or_period_pattern = re.compile(
            r"최근\s*\d+\s*(?:일|개월|년)|(?:블로그|게시글|글)\s*\d+\s*건"
        )
        self.assertFalse(
            any(count_or_period_pattern.search(row["recommendation_reason"]) for row in rows)
        )
        self.assertIn("지역성: 칼국수", {row["recommendation_reason"] for row in rows})
