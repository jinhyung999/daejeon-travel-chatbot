import csv
from contextlib import closing
import json
import sqlite3
import unittest
import uuid
from pathlib import Path

from scripts.export_giftshop_detail_review import FIELDNAMES
from scripts.import_giftshop_detail_review import (
    apply_review_rows,
    import_review_file,
    read_review_rows,
)


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
    def _csv_path(self):
        path = Path.cwd() / f"test-import-{uuid.uuid4().hex}.csv"
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def test_requires_exact_header_contract(self):
        cases = {
            "missing": FIELDNAMES[:-1],
            "extra": [*FIELDNAMES, "unexpected"],
            "reordered": [FIELDNAMES[1], FIELDNAMES[0], *FIELDNAMES[2:]],
            "duplicate": [*FIELDNAMES[:-1], FIELDNAMES[0]],
        }
        for label, header in cases.items():
            with self.subTest(label=label):
                path = self._csv_path()
                with path.open("w", encoding="utf-8-sig", newline="") as stream:
                    csv.writer(stream).writerow(header)
                with self.assertRaises(ValueError):
                    read_review_rows(path)

    def test_rejects_malformed_row_widths(self):
        for label, values in {
            "surplus": [*approved_row().values(), "surplus"],
            "short": list(approved_row().values())[:-1],
        }.items():
            with self.subTest(label=label):
                path = self._csv_path()
                with path.open("w", encoding="utf-8-sig", newline="") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(FIELDNAMES)
                    writer.writerow(values)
                with self.assertRaises(ValueError):
                    read_review_rows(path)

    def test_rejects_duplicate_place_id(self):
        path = self._csv_path()
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows([approved_row(), approved_row()])

        with self.assertRaises(ValueError):
            read_review_rows(path)

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

        stats = apply_review_rows(conn, [
            approved_row(review_status="pending"),
            approved_row(review_status="rejected"),
        ])

        self.assertEqual(0, stats.updated)
        self.assertEqual(1, stats.pending)
        self.assertEqual(1, stats.rejected)
        self.assertIsNone(
            conn.execute("SELECT tel FROM place WHERE place_id='g1'").fetchone()[0]
        )

    def test_rejects_unknown_status(self):
        conn = make_db()
        self.addCleanup(conn.close)

        with self.assertRaises(ValueError):
            apply_review_rows(conn, [approved_row(review_status="ready")])

        self.assertIsNone(
            conn.execute("SELECT tel FROM place WHERE place_id='g1'").fetchone()[0]
        )

    def test_rejects_missing_sources_bad_date_and_non_giftshop(self):
        cases = [
            approved_row(tel_source_url=""),
            approved_row(hours_source_url=""),
            approved_row(verified_at="22-07-2026"),
            approved_row(verified_at="2026-7-2"),
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

    def test_identical_approved_import_is_fully_idempotent(self):
        conn = make_db()
        self.addCleanup(conn.close)
        row = approved_row(review_note="Checked in person")

        apply_review_rows(conn, [row])
        first = conn.execute("SELECT * FROM place WHERE place_id='g1'").fetchone()
        apply_review_rows(conn, [row])
        second = conn.execute("SELECT * FROM place WHERE place_id='g1'").fetchone()

        self.assertEqual(first, second)

    def test_live_apply_creates_backup_and_dry_run_does_not_mutate(self):
        unique = uuid.uuid4().hex
        db_path = Path.cwd() / f"test-import-{unique}.db"
        csv_path = Path.cwd() / f"test-import-{unique}.csv"
        backup_dir = db_path.parent / "backups"
        backup_dir_existed = backup_dir.exists()
        backup_paths = []
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

            _, first_backup = import_review_file(db_path, csv_path, apply=True)
            backup_paths.append(first_backup)
            self.assertTrue(first_backup.is_file())
            with closing(sqlite3.connect(first_backup)) as backup_conn:
                self.assertIsNone(
                    backup_conn.execute(
                        "SELECT tel FROM place WHERE place_id='g1'"
                    ).fetchone()[0]
                )
            with closing(sqlite3.connect(db_path)) as live_conn:
                self.assertEqual(
                    "042-111-2222",
                    live_conn.execute(
                        "SELECT tel FROM place WHERE place_id='g1'"
                    ).fetchone()[0],
                )

            with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDNAMES)
                writer.writeheader()
                writer.writerow(approved_row(tel="042-999-0000"))
            _, second_backup = import_review_file(db_path, csv_path, apply=True)
            backup_paths.append(second_backup)

            self.assertNotEqual(first_backup, second_backup)
            self.assertTrue(second_backup.is_file())
            with closing(sqlite3.connect(second_backup)) as backup_conn:
                self.assertEqual(
                    "042-111-2222",
                    backup_conn.execute(
                        "SELECT tel FROM place WHERE place_id='g1'"
                    ).fetchone()[0],
                )
            with closing(sqlite3.connect(db_path)) as live_conn:
                self.assertEqual(
                    "042-999-0000",
                    live_conn.execute(
                        "SELECT tel FROM place WHERE place_id='g1'"
                    ).fetchone()[0],
                )
        finally:
            for backup_path in backup_paths:
                backup_path.unlink(missing_ok=True)
            db_path.unlink(missing_ok=True)
            csv_path.unlink(missing_ok=True)
            if not backup_dir_existed:
                backup_dir.rmdir()


if __name__ == "__main__":
    unittest.main()
