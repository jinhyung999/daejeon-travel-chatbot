import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import bus_graph
import transit
from geo import CAR_SPEED_KMH, haversine_km


class BusGraphTest(unittest.TestCase):
    def setUp(self):
        bus_graph.clear_bus_graph_cache()
        self.db_path = Path("in-memory-bus-graph.db")

        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(
            """
            CREATE TABLE transport (
                stop_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                lat REAL NOT NULL,
                lng REAL NOT NULL
            );
            CREATE TABLE bus_route (
                route_id TEXT PRIMARY KEY,
                route_no TEXT NOT NULL,
                route_type TEXT
            );
            CREATE TABLE bus_route_stop (
                route_id TEXT NOT NULL,
                updowncd INTEGER NOT NULL,
                node_order INTEGER NOT NULL,
                stop_id TEXT NOT NULL
            );
            """
        )
        self.connection.executemany(
            "INSERT INTO transport VALUES (?, ?, ?, ?)",
            [
                ("S1", "첫 정류장", 36.3500, 127.3800),
                ("S2", "둘째 정류장", 36.3510, 127.3810),
                ("S3", "셋째 정류장", 36.3520, 127.3830),
            ],
        )
        self.connection.execute("INSERT INTO bus_route VALUES (?, ?, ?)", ("R1", "107", "간선버스"))
        self.connection.executemany(
            "INSERT INTO bus_route_stop VALUES (?, ?, ?, ?)",
            [("R1", 0, 1, "S1"), ("R1", 0, 2, "S2"), ("R1", 0, 3, "S3")],
        )
        self.connection.commit()

        class SharedConnection:
            def __init__(inner_self):
                inner_self.close_calls = 0

            def cursor(inner_self):
                return self.connection.cursor()

            def close(inner_self):
                inner_self.close_calls += 1

        self.shared_connection = SharedConnection()
        self.connect_patcher = patch.object(
            bus_graph.sqlite3, "connect", return_value=self.shared_connection
        )
        self.connect_mock = self.connect_patcher.start()

    def tearDown(self):
        bus_graph.clear_bus_graph_cache()
        self.connect_patcher.stop()
        self.connection.close()

    def test_loads_routes_stops_coordinates_names_and_metadata(self):
        graph = bus_graph.get_bus_graph(self.db_path)

        self.assertEqual([(1, "S1"), (2, "S2"), (3, "S3")], graph.by_route[("R1", 0)])
        self.assertEqual([("R1", 0, 2)], graph.by_stop["S2"])
        self.assertEqual((36.3510, 127.3810), graph.coords["S2"])
        self.assertEqual("둘째 정류장", graph.stop_names["S2"])
        self.assertEqual(("107", "간선버스"), graph.route_meta["R1"])

    def test_static_segment_is_sum_of_positive_adjacent_edge_minutes(self):
        graph = bus_graph.get_bus_graph(self.db_path)

        first = bus_graph.static_segment_minutes(graph, "R1", 0, 1, 2)
        second = bus_graph.static_segment_minutes(graph, "R1", 0, 2, 3)
        whole = bus_graph.static_segment_minutes(graph, "R1", 0, 1, 3)
        expected_first = haversine_km(*graph.coords["S1"], *graph.coords["S2"]) / CAR_SPEED_KMH * 60

        self.assertGreater(first, 0)
        self.assertGreater(second, 0)
        self.assertAlmostEqual(expected_first, first)
        self.assertAlmostEqual(first + second, whole)

    def test_same_path_returns_same_graph_and_closes_single_connection(self):
        first = bus_graph.get_bus_graph(self.db_path)
        second = bus_graph.get_bus_graph(self.db_path)

        self.assertIs(first, second)
        self.assertEqual(1, self.connect_mock.call_count)
        self.assertEqual(1, self.shared_connection.close_calls)

    def test_cache_holds_at_most_four_database_paths(self):
        self.assertEqual(4, bus_graph.get_bus_graph.cache_parameters()["maxsize"])

    def test_connection_closes_when_loading_raises(self):
        class BrokenConnection:
            def __init__(inner_self):
                inner_self.closed = False

            def cursor(inner_self):
                raise RuntimeError("query failed")

            def close(inner_self):
                inner_self.closed = True

        connection = BrokenConnection()
        with patch.object(bus_graph.sqlite3, "connect", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "query failed"):
                bus_graph.get_bus_graph(Path("broken.db"))

        self.assertTrue(connection.closed)

    def test_transit_static_leg_delegates_to_graph_segment_minutes(self):
        graph = bus_graph.get_bus_graph(self.db_path)
        leg = {
            "route_id": "R1",
            "updowncd": 0,
            "board_order": 1,
            "alight_order": 3,
        }

        with patch.object(transit, "static_segment_minutes", return_value=12.5) as segment_minutes:
            result = transit._static_leg_minutes({}, {}, leg, graph=graph)

        self.assertEqual(12.5, result)
        segment_minutes.assert_called_once_with(graph, "R1", 0, 1, 3)


if __name__ == "__main__":
    unittest.main()
