import sqlite3
import sys
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import transit_graph
from geo import CAR_SPEED_KMH, haversine_km


class TransitGraphTest(unittest.TestCase):
    def setUp(self):
        transit_graph.clear_transit_graph_cache()
        self.extra_db_paths = []
        self.db_path = Path(f".tmp_transit_graph_{uuid.uuid4().hex}.db")
        self._create_database(self.db_path)

    def tearDown(self):
        transit_graph.clear_transit_graph_cache()
        self.db_path.unlink(missing_ok=True)
        for path in self.extra_db_paths:
            path.unlink(missing_ok=True)

    @staticmethod
    def _create_database(path):
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                """
                CREATE TABLE transport (
                    stop_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    lat REAL NOT NULL, lng REAL NOT NULL
                );
                CREATE TABLE bus_route (
                    route_id TEXT PRIMARY KEY, route_no TEXT NOT NULL, route_type TEXT
                );
                CREATE TABLE bus_route_stop (
                    route_id TEXT NOT NULL, updowncd INTEGER NOT NULL,
                    node_order INTEGER NOT NULL, stop_id TEXT NOT NULL
                );
                CREATE TABLE subway_line (
                    line_id TEXT PRIMARY KEY, name_ko TEXT NOT NULL, name_en TEXT NOT NULL
                );
                CREATE TABLE subway_station (
                    station_id TEXT PRIMARY KEY, line_id TEXT NOT NULL,
                    station_no INTEGER NOT NULL, name_ko TEXT NOT NULL, name_en TEXT NOT NULL,
                    address TEXT, lat REAL NOT NULL, lng REAL NOT NULL,
                    coordinate_source TEXT NOT NULL
                );
                CREATE TABLE subway_edge (
                    line_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    from_station_id TEXT NOT NULL, to_station_id TEXT NOT NULL,
                    travel_seconds INTEGER NOT NULL, distance_km REAL NOT NULL
                );
                CREATE TABLE subway_schedule (
                    station_id TEXT NOT NULL, day_type TEXT NOT NULL,
                    direction TEXT NOT NULL, train_no TEXT NOT NULL,
                    arrival_time TEXT, departure_time TEXT NOT NULL
                );
                CREATE TABLE transit_transfer (
                    station_id TEXT NOT NULL, stop_id TEXT NOT NULL,
                    distance_m REAL NOT NULL, walking_minutes REAL NOT NULL
                );
                """
            )
            connection.executemany(
                "INSERT INTO transport VALUES (?, ?, ?, ?)",
                [("S1", "첫 정류장", 36.3500, 127.3800),
                 ("S2", "둘째 정류장", 36.3510, 127.3810),
                 ("S3", "셋째 정류장", 36.3520, 127.3830)],
            )
            connection.execute("INSERT INTO bus_route VALUES (?, ?, ?)", ("R1", "107", "간선버스"))
            connection.executemany(
                "INSERT INTO bus_route_stop VALUES (?, ?, ?, ?)",
                [("R1", 0, 1, "S1"), ("R1", 0, 2, "S2"), ("R1", 0, 3, "S3")],
            )
            connection.execute(
                "INSERT INTO subway_line VALUES (?, ?, ?)",
                ("DJM1", "대전도시철도 1호선", "Daejeon Metro Line 1"),
            )
            connection.executemany(
                "INSERT INTO subway_station VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [("DJM101", "DJM1", 101, "판암", "Panam", None, 36.316, 127.459, "derived_bus_stops"),
                 ("DJM102", "DJM1", 102, "신흥", "Sinheung", None, 36.320, 127.448, "derived_bus_stops"),
                 ("DJM103", "DJM1", 103, "대동", "Daedong", None, 36.329, 127.442, "derived_bus_stops")],
            )
            connection.executemany(
                "INSERT INTO subway_edge VALUES (?, ?, ?, ?, ?, ?)",
                [("DJM1", 1, "DJM101", "DJM102", 90, 1.2),
                 ("DJM1", 2, "DJM102", "DJM103", 150, 1.5)],
            )
            connection.executemany(
                "INSERT INTO subway_schedule VALUES (?, ?, ?, ?, ?, ?)",
                [("DJM101", "01", "up", "W1", None, "083000"),
                 ("DJM101", "01", "up", "W2", None, "084500"),
                 ("DJM101", "02", "up", "S1", None, "090000"),
                 ("DJM101", "03", "up", "H1", None, "100000")],
            )
            connection.execute(
                "INSERT INTO transit_transfer VALUES (?, ?, ?, ?)",
                ("DJM102", "S2", 123.4, 2.75),
            )
            connection.commit()
        finally:
            connection.close()

    def test_loads_typed_services_memberships_edges_metadata_and_places(self):
        graph = transit_graph.get_transit_graph(self.db_path)
        bus_service = ("bus", "R1", "0")
        up_service = ("subway", "DJM1", "up")
        down_service = ("subway", "DJM1", "down")

        self.assertEqual(["bus:S1", "bus:S2", "bus:S3"], graph.service_sequences[bus_service])
        self.assertEqual(
            ["subway:DJM101", "subway:DJM102", "subway:DJM103"],
            graph.service_sequences[up_service],
        )
        self.assertEqual(list(reversed(graph.service_sequences[up_service])), graph.service_sequences[down_service])
        self.assertIn((bus_service, 1), graph.node_services["bus:S2"])
        self.assertIn((up_service, 1), graph.node_services["subway:DJM102"])
        self.assertIn((down_service, 1), graph.node_services["subway:DJM102"])

        expected_bus = haversine_km(36.3500, 127.3800, 36.3510, 127.3810) / CAR_SPEED_KMH * 60
        self.assertAlmostEqual(expected_bus, graph.adjacent_minutes[(bus_service, 0, 1)])
        self.assertEqual(1.5, graph.adjacent_minutes[(up_service, 0, 1)])
        self.assertEqual(1.5, graph.adjacent_minutes[(down_service, 1, 2)])
        self.assertEqual(2.5, graph.adjacent_minutes[(up_service, 1, 2)])
        self.assertEqual({"route_no": "107", "route_type": "간선버스"}, graph.service_meta[bus_service])
        self.assertEqual("대전도시철도 1호선", graph.service_meta[up_service]["name_ko"])
        self.assertEqual("Daejeon Metro Line 1", graph.service_meta[down_service]["name_en"])
        self.assertEqual("둘째 정류장", graph.names["bus:S2"])
        self.assertEqual("신흥", graph.names["subway:DJM102"])
        self.assertEqual((36.320, 127.448), graph.coords["subway:DJM102"])
        self.assertIsNotNone(graph.bus_graph)

    def test_transfer_adjacency_is_symmetric_and_preserves_values(self):
        graph = transit_graph.get_transit_graph(self.db_path)
        expected = {"node": "subway:DJM102", "distance_m": 123.4, "walking_minutes": 2.75}
        self.assertIn(expected, graph.transfer_adjacency["bus:S2"])
        self.assertIn(
            {"node": "bus:S2", "distance_m": 123.4, "walking_minutes": 2.75},
            graph.transfer_adjacency["subway:DJM102"],
        )

    def test_wait_uses_weekday_and_saturday_schedules_then_falls_back(self):
        graph = transit_graph.get_transit_graph(self.db_path)

        self.assertEqual((10.0, False), transit_graph.subway_wait_minutes(
            graph, "DJM101", "up", datetime(2026, 7, 16, 8, 20)
        ))
        self.assertEqual((30.0, False), transit_graph.subway_wait_minutes(
            graph, "DJM101", "up", datetime(2026, 7, 18, 8, 30)
        ))
        self.assertEqual((30.0, False), transit_graph.subway_wait_minutes(
            graph, "DJM101", "up", datetime(2026, 7, 19, 9, 30)
        ))
        self.assertEqual((5.0, True), transit_graph.subway_wait_minutes(
            graph, "DJM101", "up", datetime(2026, 7, 16, 23, 0)
        ))
        self.assertEqual((5.0, True), transit_graph.subway_wait_minutes(
            graph, "DJM103", "down", datetime(2026, 7, 19, 8, 0)
        ))

    def test_cache_identity_and_maxsize(self):
        self.assertIs(
            transit_graph.get_transit_graph(self.db_path),
            transit_graph.get_transit_graph(self.db_path),
        )
        self.assertEqual(4, transit_graph.get_transit_graph.cache_parameters()["maxsize"])

        for _ in range(5):
            path = Path(f".tmp_transit_graph_{uuid.uuid4().hex}.db")
            self.extra_db_paths.append(path)
            self._create_database(path)
            transit_graph.get_transit_graph(path)
        self.assertEqual(4, transit_graph.get_transit_graph.cache_info().currsize)

    def test_connection_closes_on_success_and_failure(self):
        real_connect = sqlite3.connect
        connections = []

        class TrackingConnection:
            def __init__(self, connection):
                self.connection = connection
                self.closed = False

            def cursor(self):
                return self.connection.cursor()

            def close(self):
                self.closed = True
                self.connection.close()

        def tracking_connect(*args, **kwargs):
            wrapped = TrackingConnection(real_connect(*args, **kwargs))
            connections.append(wrapped)
            return wrapped

        with patch.object(transit_graph.sqlite3, "connect", side_effect=tracking_connect):
            transit_graph.get_transit_graph(self.db_path)
        self.assertTrue(connections[-1].closed)

        class BrokenConnection:
            closed = False

            def cursor(self):
                raise RuntimeError("query failed")

            def close(self):
                self.closed = True

        transit_graph.clear_transit_graph_cache()
        broken = BrokenConnection()
        with patch.object(transit_graph.sqlite3, "connect", return_value=broken):
            with self.assertRaisesRegex(RuntimeError, "query failed"):
                transit_graph.get_transit_graph(Path("broken.db"))
        self.assertTrue(broken.closed)


if __name__ == "__main__":
    unittest.main()
