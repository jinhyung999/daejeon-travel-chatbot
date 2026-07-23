import json
import sqlite3
import unittest

from collectors import signature_menu_backfill as backfill_mod


def make_place_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE place (
          place_id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          category TEXT NOT NULL,
          source_api TEXT,
          extra_json TEXT,
          overview TEXT,
          recommend TEXT
        )
        """
    )
    return conn


def seed_place(conn, place_id, **overrides):
    row = {
        "place_id": place_id, "name": "개천식당", "category": "restaurant",
        "source_api": "daejeon_food", "recommend": "추천",
        "extra_json": json.dumps({"rprsFod": "만둣국 / 7,000원", "restrntSumm": "60년 전통 만둣국집"}, ensure_ascii=False),
        "overview": None,
    }
    row.update(overrides)
    conn.execute(
        """
        INSERT INTO place (place_id, name, category, source_api, extra_json, overview, recommend)
        VALUES (:place_id, :name, :category, :source_api, :extra_json, :overview, :recommend)
        """,
        row,
    )
    conn.commit()


class EnsureSignatureMenuSchemaTest(unittest.TestCase):
    def test_adds_missing_column(self):
        conn = make_place_db()
        self.addCleanup(conn.close)

        backfill_mod.ensure_signature_menu_schema(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(place)")}
        self.assertIn("signature_menu", columns)

    def test_is_idempotent(self):
        conn = make_place_db()
        self.addCleanup(conn.close)

        backfill_mod.ensure_signature_menu_schema(conn)
        backfill_mod.ensure_signature_menu_schema(conn)

        columns = [row[1] for row in conn.execute("PRAGMA table_info(place)")]
        self.assertEqual(columns.count("signature_menu"), 1)


class BackfillTest(unittest.TestCase):
    def test_fills_signature_menu_and_overview_from_extra_json(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        backfill_mod.ensure_signature_menu_schema(conn)
        seed_place(conn, "p1")

        stats = backfill_mod.backfill(conn=conn)

        row = conn.execute(
            "SELECT signature_menu, overview FROM place WHERE place_id='p1'"
        ).fetchone()
        self.assertEqual(row, ("만둣국 / 7,000원", "60년 전통 만둣국집"))
        self.assertEqual(stats.updated, 1)

    def test_strips_double_quotes(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        backfill_mod.ensure_signature_menu_schema(conn)
        seed_place(conn, "p1", extra_json=json.dumps(
            {"rprsFod": '"만둣국" / 7,000원', "restrntSumm": '"60년 전통" 만둣국집'},
            ensure_ascii=False,
        ))

        backfill_mod.backfill(conn=conn)

        row = conn.execute(
            "SELECT signature_menu, overview FROM place WHERE place_id='p1'"
        ).fetchone()
        self.assertEqual(row, ("만둣국 / 7,000원", "60년 전통 만둣국집"))

    def test_only_targets_daejeon_food_recommend_rows(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        backfill_mod.ensure_signature_menu_schema(conn)
        seed_place(conn, "p1", source_api="tourapi")
        seed_place(conn, "p2", recommend=None)

        backfill_mod.backfill(conn=conn)

        row1 = conn.execute("SELECT signature_menu FROM place WHERE place_id='p1'").fetchone()
        row2 = conn.execute("SELECT signature_menu FROM place WHERE place_id='p2'").fetchone()
        self.assertIsNone(row1[0])
        self.assertIsNone(row2[0])

    def test_leaves_null_when_source_field_missing(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        backfill_mod.ensure_signature_menu_schema(conn)
        seed_place(conn, "p1", extra_json=json.dumps({"restrntSumm": None}, ensure_ascii=False))

        backfill_mod.backfill(conn=conn)

        row = conn.execute("SELECT signature_menu FROM place WHERE place_id='p1'").fetchone()
        self.assertIsNone(row[0])

    def test_does_not_overwrite_existing_values(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        backfill_mod.ensure_signature_menu_schema(conn)
        seed_place(conn, "p1", overview="이미 있는 소개글")
        conn.execute("UPDATE place SET signature_menu='기존 대표메뉴' WHERE place_id='p1'")
        conn.commit()

        backfill_mod.backfill(conn=conn)

        row = conn.execute(
            "SELECT signature_menu, overview FROM place WHERE place_id='p1'"
        ).fetchone()
        self.assertEqual(row, ("기존 대표메뉴", "이미 있는 소개글"))

    def test_is_idempotent_across_repeated_runs(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        backfill_mod.ensure_signature_menu_schema(conn)
        seed_place(conn, "p1")

        backfill_mod.backfill(conn=conn)
        stats = backfill_mod.backfill(conn=conn)

        row = conn.execute(
            "SELECT signature_menu, overview FROM place WHERE place_id='p1'"
        ).fetchone()
        self.assertEqual(row, ("만둣국 / 7,000원", "60년 전통 만둣국집"))
        self.assertEqual(stats.updated, 0)


if __name__ == "__main__":
    unittest.main()
