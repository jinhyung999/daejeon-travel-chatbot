import copy
import csv
import math
import sqlite3
import unittest
import uuid
from pathlib import Path

from scripts.build_subway_data import apply_snapshot, build_snapshot


class SubwayDataBuilderTest(unittest.TestCase):
    def setUp(self):
        # SQLite cannot open files below TemporaryDirectory in the Windows sandbox,
        # so use uniquely named files directly in the writable worktree.
        self.prefix = f".tmp_subway_{uuid.uuid4().hex}"
        self.root = Path.cwd()
        self.station_csv = self.root / f"{self.prefix}_stations.csv"
        self.edge_csv = self.root / f"{self.prefix}_edges.csv"
        self.db_path = self.root / f"{self.prefix}_travel.db"
        self._create_bus_db()
        self._write_sources()

    def tearDown(self):
        for path in self.root.glob(f"{self.prefix}*"):
            path.unlink(missing_ok=True)

    def _create_bus_db(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "CREATE TABLE transport ("
            "stop_id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL, "
            "lat REAL NOT NULL, lng REAL NOT NULL)"
        )
        for station_no in range(101, 123):
            offset = (station_no - 101) * 0.01
            base_name = "신흥" if station_no == 102 else f"역{station_no}"
            connection.execute(
                "INSERT INTO transport VALUES (?, ?, 'bus', ?, ?)",
                (f"B{station_no}", base_name, 36.30 + offset, 127.30 + offset),
            )
        # Prefer the group containing '<base>역' when both exact and station groups exist.
        connection.execute(
            "INSERT INTO transport VALUES ('B101E', '역101역 1번출구', 'bus', 36.301, 127.301)"
        )
        # Roughly 599m north and over 600m north of station 101's derived coordinate.
        station_lat = 36.301
        connection.execute(
            "INSERT INTO transport VALUES ('NEAR', '환승 정류장', 'bus', ?, 127.301)",
            (station_lat + 0.599 / 111.195,),
        )
        connection.execute(
            "INSERT INTO transport VALUES ('FAR', '먼 정류장', 'bus', ?, 127.301)",
            (station_lat + 0.601 / 111.195,),
        )
        connection.commit()
        connection.close()

    def _write_cp949(self, path, fieldnames, rows):
        with path.open("w", encoding="cp949", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def _write_sources(self, missing_station=None, bad_edge=None):
        stations = []
        for station_no in range(101, 123):
            if station_no == missing_station:
                continue
            korean = "신 흥(대전대입구)" if station_no == 102 else f"역 {station_no}"
            stations.append(
                {
                    "역번호": station_no,
                    "주소": f"대전 {station_no}",
                    "한 글": korean,
                    "한 자": f"漢字{station_no}",
                    "로 마 자": f"Station {station_no}",
                }
            )
        edges = []
        for sequence in range(1, 22):
            from_no = 100 + sequence
            to_no = from_no + 1
            if sequence == 20:
                to_no = 120  # The sole source defect accepted by the loader.
            if bad_edge and sequence == bad_edge[0]:
                from_no, to_no = bad_edge[1], bad_edge[2]
            edges.append(
                {
                    "sequence": sequence,
                    "from_station_no": from_no,
                    "to_station_no": to_no,
                    "travel_seconds": 90 + sequence,
                    "distance_km": 1.0 + sequence / 100,
                }
            )
        self._write_cp949(
            self.station_csv,
            ["역번호", "주소", "한 글", "한 자", "로 마 자"],
            stations,
        )
        self._write_cp949(
            self.edge_csv,
            [
                "sequence",
                "from_station_no",
                "to_station_no",
                "travel_seconds",
                "distance_km",
            ],
            edges,
        )

    def test_build_snapshot_parses_normalizes_and_corrects_known_edge(self):
        snapshot = build_snapshot(self.station_csv, self.edge_csv, self.db_path)

        self.assertEqual("DJM1", snapshot["lines"][0]["line_id"])
        self.assertEqual(22, len(snapshot["stations"]))
        self.assertEqual(21, len(snapshot["edges"]))
        station = snapshot["stations"][1]
        self.assertEqual("DJM102", station["station_id"])
        self.assertEqual("신흥(대전대입구)", station["name_ko"])
        self.assertEqual("Station 102", station["name_en"])
        self.assertEqual("derived_bus_stops", station["coordinate_source"])
        corrected = next(edge for edge in snapshot["edges"] if edge["sequence"] == 20)
        self.assertEqual(("DJM120", "DJM121"), (corrected["from_station_id"], corrected["to_station_id"]))

    def test_station_coordinates_prefer_station_suffix_group_and_use_median(self):
        snapshot = build_snapshot(self.station_csv, self.edge_csv, self.db_path)
        station = snapshot["stations"][0]
        self.assertAlmostEqual(36.301, station["lat"])
        self.assertAlmostEqual(127.301, station["lng"])

    def test_rejects_any_other_non_adjacent_edge(self):
        self._write_sources(bad_edge=(3, 103, 103))
        with self.assertRaisesRegex(ValueError, "adjacent"):
            build_snapshot(self.station_csv, self.edge_csv, self.db_path)

    def test_rejects_missing_station_number(self):
        self._write_sources(missing_station=110)
        with self.assertRaisesRegex(ValueError, "22|101.*122|station"):
            build_snapshot(self.station_csv, self.edge_csv, self.db_path)

    def test_transfers_include_599m_exclude_over_600m_and_use_walking_formula(self):
        snapshot = build_snapshot(self.station_csv, self.edge_csv, self.db_path)
        transfers = {row["stop_id"]: row for row in snapshot["transfers"] if row["station_id"] == "DJM101"}

        self.assertIn("NEAR", transfers)
        self.assertNotIn("FAR", transfers)
        transfer = transfers["NEAR"]
        self.assertLessEqual(transfer["distance_m"], 600.0)
        expected_minutes = transfer["distance_m"] * 1.25 / 4500 * 60
        self.assertTrue(math.isclose(expected_minutes, transfer["walking_minutes"], rel_tol=1e-9))

    def test_apply_creates_tables_replaces_rows_and_makes_backup(self):
        snapshot = build_snapshot(self.station_csv, self.edge_csv, self.db_path)
        backup = apply_snapshot(snapshot, self.db_path)

        self.assertTrue(backup.exists())
        self.assertEqual(self.db_path.parent, backup.parent)
        connection = sqlite3.connect(self.db_path)
        try:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertTrue(
                {"subway_line", "subway_station", "subway_edge", "subway_schedule", "transit_transfer"}
                <= tables
            )
            self.assertEqual(22, connection.execute("SELECT count(*) FROM subway_station").fetchone()[0])
            self.assertEqual(21, connection.execute("SELECT count(*) FROM subway_edge").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            connection.close()

    def test_invalid_snapshot_does_not_mutate_existing_rows(self):
        snapshot = build_snapshot(self.station_csv, self.edge_csv, self.db_path)
        apply_snapshot(snapshot, self.db_path)
        invalid = copy.deepcopy(snapshot)
        invalid["edges"].pop()

        with self.assertRaises(ValueError):
            apply_snapshot(invalid, self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(22, connection.execute("SELECT count(*) FROM subway_station").fetchone()[0])
            self.assertEqual(21, connection.execute("SELECT count(*) FROM subway_edge").fetchone()[0])
        finally:
            connection.close()

    def test_database_failure_rolls_back_all_replacement_rows(self):
        snapshot = build_snapshot(self.station_csv, self.edge_csv, self.db_path)
        apply_snapshot(snapshot, self.db_path)
        invalid = copy.deepcopy(snapshot)
        invalid["transfers"][0]["stop_id"] = "MISSING-BUS-STOP"

        with self.assertRaises(sqlite3.IntegrityError):
            apply_snapshot(invalid, self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(22, connection.execute("SELECT count(*) FROM subway_station").fetchone()[0])
            self.assertEqual(21, connection.execute("SELECT count(*) FROM subway_edge").fetchone()[0])
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT count(*) FROM transit_transfer WHERE stop_id = 'MISSING-BUS-STOP'"
                ).fetchone()[0],
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
