import copy
import csv
import math
import sqlite3
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from scripts import build_subway_data as builder
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

    def _write_edge_times(self, header, values):
        rows = []
        for sequence, value in enumerate(values, start=1):
            from_no = 100 + sequence
            to_no = from_no + 1
            if sequence == 20:
                to_no = 120
            rows.append(
                {
                    "sequence": sequence,
                    "from_station_no": from_no,
                    "to_station_no": to_no,
                    header: value,
                    "distance_km": 1.0 + sequence / 100,
                }
            )
        self._write_cp949(
            self.edge_csv,
            ["sequence", "from_station_no", "to_station_no", header, "distance_km"],
            rows,
        )

    def test_ambiguous_korean_travel_time_header_parses_compact_mmss(self):
        compact_times = [
            200, 220, 150, 150, 150, 210, 150, 210, 210, 140, 200,
            140, 140, 200, 200, 200, 140, 200, 140, 200, 130,
        ]
        self._write_edge_times("소요시간", compact_times)

        snapshot = build_snapshot(self.station_csv, self.edge_csv, self.db_path)

        parsed = [edge["travel_seconds"] for edge in snapshot["edges"]]
        self.assertEqual([120, 140, 110], parsed[:3])
        self.assertEqual(90, parsed[-1])
        self.assertEqual(2400, sum(parsed))
        self.assertEqual(40, sum(parsed) / 60)

    def test_ambiguous_korean_travel_time_rejects_invalid_compact_mmss(self):
        for invalid in (160, -130, "not-a-number", "1.5"):
            with self.subTest(invalid=invalid):
                self._write_edge_times("소요시간", [invalid] + [130] * 20)
                with self.assertRaisesRegex(ValueError, "invalid edge row|MMSS"):
                    build_snapshot(self.station_csv, self.edge_csv, self.db_path)

    def test_explicit_seconds_headers_remain_literal_seconds(self):
        for header in ("travel_seconds", "소요시간초"):
            with self.subTest(header=header):
                self._write_edge_times(header, [200] * 21)
                snapshot = build_snapshot(self.station_csv, self.edge_csv, self.db_path)
                self.assertEqual([200] * 21, [edge["travel_seconds"] for edge in snapshot["edges"]])

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

    def test_snapshot_missing_transfer_coverage_is_rejected_before_mutation(self):
        snapshot = build_snapshot(self.station_csv, self.edge_csv, self.db_path)
        apply_snapshot(snapshot, self.db_path)
        snapshot["transfers"] = [
            row for row in snapshot["transfers"] if row["station_id"] != "DJM122"
        ]

        with self.assertRaisesRegex(ValueError, "transfer.*cover|coverage"):
            apply_snapshot(snapshot, self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            self.assertGreater(
                connection.execute(
                    "SELECT count(*) FROM transit_transfer WHERE station_id = 'DJM122'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_snapshot_transfer_for_unknown_station_is_rejected(self):
        snapshot = build_snapshot(self.station_csv, self.edge_csv, self.db_path)
        snapshot["transfers"].append(
            {
                **snapshot["transfers"][0],
                "station_id": "DJM999",
            }
        )

        with self.assertRaisesRegex(ValueError, "unknown.*station|station.*unknown"):
            apply_snapshot(snapshot, self.db_path)

    def test_apply_rolls_back_if_database_has_station_without_transfer(self):
        snapshot = build_snapshot(self.station_csv, self.edge_csv, self.db_path)
        apply_snapshot(snapshot, self.db_path)
        invalid = copy.deepcopy(snapshot)
        invalid["transfers"] = [
            row for row in invalid["transfers"] if row["station_id"] != "DJM122"
        ]

        with patch.object(builder, "_validate_snapshot", return_value=None):
            with self.assertRaisesRegex(ValueError, "without.*transfer|transfer.*coverage"):
                apply_snapshot(invalid, self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            self.assertGreater(
                connection.execute(
                    "SELECT count(*) FROM transit_transfer WHERE station_id = 'DJM122'"
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_rejects_malformed_schedule_times(self):
        snapshot = build_snapshot(self.station_csv, self.edge_csv, self.db_path)
        base = {
            "station_id": "DJM101",
            "day_type": "01",
            "direction": "up",
            "train_no": "T1",
            "arrival_time": None,
            "departure_time": "080100",
        }
        cases = [
            ("departure_time", "8:01"),
            ("departure_time", "246000"),
            ("departure_time", "08AA00"),
            ("arrival_time", "236060"),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                invalid = copy.deepcopy(snapshot)
                invalid["schedules"] = [{**base, field: value}]
                with self.assertRaisesRegex(ValueError, "schedule.*time|HHMMSS"):
                    apply_snapshot(invalid, self.db_path)

    def test_missing_database_dry_run_does_not_create_a_file(self):
        missing_db = self.root / f"{self.prefix}_missing.db"

        with self.assertRaisesRegex(ValueError, "bus stops|database|transport"):
            build_snapshot(self.station_csv, self.edge_csv, missing_db)

        self.assertFalse(missing_db.exists())

    def test_backup_uses_live_sqlite_snapshot_including_uncheckpointed_wal(self):
        writer = sqlite3.connect(self.db_path)
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("CREATE TABLE preapply_marker (value TEXT NOT NULL)")
        writer.execute("INSERT INTO preapply_marker VALUES ('from-wal')")
        writer.commit()
        try:
            snapshot = build_snapshot(self.station_csv, self.edge_csv, self.db_path)
            backup = apply_snapshot(snapshot, self.db_path)
        finally:
            writer.close()

        backup_connection = sqlite3.connect(backup)
        try:
            self.assertEqual(
                "from-wal",
                backup_connection.execute("SELECT value FROM preapply_marker").fetchone()[0],
            )
            self.assertEqual(
                0,
                backup_connection.execute(
                    "SELECT count(*) FROM sqlite_master WHERE name = 'subway_line'"
                ).fetchone()[0],
            )
        finally:
            backup_connection.close()

    def test_runtime_and_schema_file_create_identical_subway_objects(self):
        schema_path = Path(__file__).parents[1] / "db" / "schema.sql"
        runtime_db = self.root / f"{self.prefix}_runtime-schema.db"
        file_db = self.root / f"{self.prefix}_file-schema.db"

        def create_objects(path, sql, needs_transport):
            connection = sqlite3.connect(path)
            try:
                if needs_transport:
                    connection.execute("CREATE TABLE transport (stop_id TEXT PRIMARY KEY)")
                connection.executescript(sql)
                rows = connection.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "WHERE (tbl_name LIKE 'subway_%' OR tbl_name = 'transit_transfer') "
                    "AND type IN ('table', 'index') "
                    "ORDER BY type, name"
                ).fetchall()
            finally:
                connection.close()
            return [
                (
                    kind,
                    name,
                    table_name,
                    None if sql_text is None else ''.join(sql_text.lower().split()),
                )
                for kind, name, table_name, sql_text in rows
            ]

        runtime_objects = create_objects(runtime_db, builder.SUBWAY_SCHEMA_SQL, True)
        file_objects = create_objects(file_db, schema_path.read_text(encoding="utf-8"), False)

        self.assertEqual(file_objects, runtime_objects)

    def test_apply_migrates_legacy_subway_tables_to_current_runtime_schema(self):
        snapshot = build_snapshot(self.station_csv, self.edge_csv, self.db_path)
        legacy_sql = builder.SUBWAY_SCHEMA_SQL.replace(
            "arrival_time TEXT CHECK (\n"
            "    arrival_time IS NULL OR (length(arrival_time) = 6 AND arrival_time NOT GLOB '*[^0-9]*')\n"
            "  ),\n"
            "  departure_time TEXT NOT NULL CHECK (\n"
            "    length(departure_time) = 6 AND departure_time NOT GLOB '*[^0-9]*'\n"
            "  ),",
            "arrival_time TEXT,\n  departure_time TEXT NOT NULL,",
        )
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript(legacy_sql)
            legacy_schedule_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'subway_schedule'"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertNotIn("length(departure_time)", legacy_schedule_sql)

        apply_snapshot(snapshot, self.db_path)

        expected_db = self.root / f"{self.prefix}_expected-schema.db"
        expected_connection = sqlite3.connect(expected_db)
        try:
            expected_connection.execute("CREATE TABLE transport (stop_id TEXT PRIMARY KEY)")
            expected_connection.executescript(builder.SUBWAY_SCHEMA_SQL)
            expected = expected_connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('subway_line', 'subway_station', 'subway_edge', "
                "'subway_schedule', 'transit_transfer') ORDER BY name"
            ).fetchall()
        finally:
            expected_connection.close()

        connection = sqlite3.connect(self.db_path)
        try:
            actual = connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('subway_line', 'subway_station', 'subway_edge', "
                "'subway_schedule', 'transit_transfer') ORDER BY name"
            ).fetchall()
            schedule_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'subway_schedule'"
            ).fetchone()[0]
        finally:
            connection.close()

        normalize = lambda sql: "".join(sql.lower().split())
        self.assertEqual(
            [(name, normalize(sql)) for name, sql in expected],
            [(name, normalize(sql)) for name, sql in actual],
        )
        self.assertIn("length(departure_time) = 6", schedule_sql)

    def test_database_failure_rolls_back_all_replacement_rows(self):
        snapshot = build_snapshot(self.station_csv, self.edge_csv, self.db_path)
        apply_snapshot(snapshot, self.db_path)
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "INSERT INTO subway_schedule VALUES "
            "('DJM101', '01', 'up', 'T1', '080000', '080100')"
        )
        connection.commit()
        tables = (
            "subway_line",
            "subway_station",
            "subway_edge",
            "subway_schedule",
            "transit_transfer",
        )
        before = {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in tables
        }
        connection.close()
        invalid = copy.deepcopy(snapshot)
        invalid["transfers"][0]["stop_id"] = "MISSING-BUS-STOP"

        with self.assertRaises(sqlite3.IntegrityError):
            apply_snapshot(invalid, self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            after = {
                table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in tables
            }
            self.assertEqual(before, after)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
