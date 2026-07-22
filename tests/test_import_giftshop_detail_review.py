import csv
import json
import sqlite3
import unittest
import uuid
from pathlib import Path

from scripts.export_giftshop_detail_review import FIELDNAMES
from scripts.import_giftshop_detail_review import apply_review_rows, import_review_file


def make_db(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE place (place_id TEXT PRIMARY KEY, name TEXT, category TEXT, "
        "address TEXT, lat REAL, lng REAL, tel TEXT, open_time TEXT, close_day TEXT, extra_json TEXT)"
    )
    conn.execute(
        "INSERT INTO place VALUES ('g1','Sample Shop','giftshop','Daejeon',36.35,127.37,NULL,NULL,NULL,?)",
        (json.dumps({
            "naver_link": "https://old.example",
            "detail_enrichment": {"legacy": "kept"},
        }),),
    )
    conn.commit()
    return conn


def approved_row(**overrides):
    row = {field: "" for field in FIELDNAMES}
    row.update({
        "place_id": "g1",
        "name": "Sample Shop",
        "tel": "042-111-2222",
        "open_time": "Daily 12:00-20:00",
        "close_day": "Every Monday",
        "tel_source_url": "https://place.map.kakao.com/1",
        "hours_source_url": "https://official.example/hours",
        "verified_at": "2026-07-22",
        "review_status": "approved",
    })
    row.update(overrides)
    return row


class GiftshopReviewImportTest(unittest.TestCase):
    def test_updates_approved_and_preserves_extra_json(self):
        conn = make_db()
        self.addCleanup(conn.close)

        stats = apply_review_rows(conn, [approved_row()])

        tel, hours, closed, raw = conn.execute(
            "SELECT tel, open_time, close_day, extra_json FROM place WHERE place_id='g1'"
        ).fetchone()
        self.assertEqual(
            ("042-111-2222", "Daily 12:00-20:00", "Every Monday"),
            (tel, hours, closed),
        )
        extra = json.loads(raw)
        self.assertEqual("https://old.example", extra["naver_link"])
        self.assertEqual("kept", extra["detail_enrichment"]["legacy"])
        self.assertEqual("manual", extra["detail_enrichment"]["verification"])
        self.assertEqual(1, stats.updated)

    def test_pending_and_rejected_do_not_update(self):
        conn = make_db()
        self.addCleanup(conn.close)

        stats = apply_review_rows(conn, [approved_row(review_status="pending")])

        self.assertEqual(0, stats.updated)
        self.assertIsNone(
            conn.execute("SELECT tel FROM place WHERE place_id='g1'").fetchone()[0]
        )

    def test_rejects_missing_sources_bad_date_and_non_giftshop(self):
        cases = [
            approved_row(tel_source_url=""),
            approved_row(hours_source_url=""),
            approved_row(verified_at="22-07-2026"),
            approved_row(place_id="missing"),
        ]
        for row in cases:
            with self.subTest(row=row):
                conn = make_db()
                try:
                    with self.assertRaises(ValueError):
                        apply_review_rows(conn, [row])
                    self.assertIsNone(
                        conn.execute("SELECT tel FROM place WHERE place_id='g1'").fetchone()[0]
                    )
                finally:
                    conn.close()

    def test_invalid_second_row_rolls_back_first(self):
        conn = make_db()
        self.addCleanup(conn.close)

        with self.assertRaises(ValueError):
            apply_review_rows(conn, [approved_row(), approved_row(place_id="missing")])

        self.assertIsNone(
            conn.execute("SELECT tel FROM place WHERE place_id='g1'").fetchone()[0]
        )

    def test_blank_cells_do_not_erase_and_repeat_is_idempotent(self):
        conn = make_db()
        self.addCleanup(conn.close)
        apply_review_rows(conn, [approved_row()])

        apply_review_rows(
            conn,
            [approved_row(open_time="", close_day="", hours_source_url="")],
        )

        self.assertEqual(
            ("Daily 12:00-20:00", "Every Monday"),
            conn.execute(
                "SELECT open_time, close_day FROM place WHERE place_id='g1'"
            ).fetchone(),
        )

    def test_live_apply_creates_backup_and_dry_run_does_not_mutate(self):
        unique = uuid.uuid4().hex
        db_path = Path.cwd() / f"test-import-{unique}.db"
        csv_path = Path.cwd() / f"test-import-{unique}.csv"
        backup_path = None
        try:
            make_db(db_path).close()
            with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDNAMES)
                writer.writeheader()
                writer.writerow(approved_row())

            _, dry_backup = import_review_file(db_path, csv_path, apply=False)
            self.assertIsNone(dry_backup)
            conn = sqlite3.connect(db_path)
            try:
                self.assertIsNone(
                    conn.execute("SELECT tel FROM place WHERE place_id='g1'").fetchone()[0]
                )
            finally:
                conn.close()

            _, backup_path = import_review_file(db_path, csv_path, apply=True)
            self.assertTrue(backup_path.is_file())
        finally:
            if backup_path is not None:
                backup_path.unlink(missing_ok=True)
            db_path.unlink(missing_ok=True)
            csv_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
