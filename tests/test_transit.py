import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import transit


class NearestStopsBoundingBoxTest(unittest.TestCase):
    def test_longitude_search_covers_full_radius_at_daejeon_latitude(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE transport (stop_id TEXT, name TEXT, lat REAL, lng REAL)"
        )
        conn.execute(
            "INSERT INTO transport VALUES (?, ?, ?, ?)",
            ("EAST_STOP", "동쪽 정류장", 36.35, 127.01),
        )

        class SharedConnection:
            def cursor(self):
                return conn.cursor()

            def close(self):
                pass

        try:
            with patch.object(transit, "_get_conn", return_value=SharedConnection()):
                stops = transit.nearest_stops(36.35, 127.0, k=10, max_km=1.0)
        finally:
            conn.close()

        self.assertIn("EAST_STOP", {stop["stop_id"] for stop in stops})


class RealtimeCandidateLimitTest(unittest.TestCase):
    def test_default_recommendation_limits_live_api_calls_to_output_count(self):
        calls = []

        def fake_arrival(*args):
            calls.append(args)
            return None

        with patch.object(transit, "get_arrival_minutes", side_effect=fake_arrival):
            result = transit.recommend_bus_routes("성심당", "대전시립박물관")

        self.assertEqual(3, len(result.get("routes", [])))
        self.assertLessEqual(len(calls), 3)


class LiveCheckpointCalculationTest(unittest.TestCase):
    def test_selects_unique_vehicle_closest_before_board_order(self):
        vehicles = [
            {"vehicle_no": "far", "node_order": 3},
            {"vehicle_no": "target", "node_order": 8},
            {"vehicle_no": "after", "node_order": 15},
        ]

        selected = transit._select_boarding_vehicle(vehicles, {3, 8, 10, 15}, 10)

        self.assertEqual("target", selected["vehicle_no"])

    def test_ambiguous_vehicles_at_same_closest_order_returns_none(self):
        vehicles = [
            {"vehicle_no": "first", "node_order": 8},
            {"vehicle_no": "second", "node_order": 8},
        ]

        selected = transit._select_boarding_vehicle(vehicles, {8, 10}, 10)

        self.assertIsNone(selected)

    def test_checkpoint_is_one_order_before_leading_vehicle(self):
        target = {"vehicle_no": "target", "node_order": 8}
        vehicles = [target, {"vehicle_no": "front", "node_order": 15}]

        checkpoint = transit._select_live_checkpoint(
            target, vehicles, set(range(1, 19)), board_order=10, alight_order=18
        )

        self.assertEqual(14, checkpoint)

    def test_checkpoint_is_alight_when_leading_vehicle_is_beyond_alight(self):
        target = {"vehicle_no": "target", "node_order": 8}
        vehicles = [target, {"vehicle_no": "front", "node_order": 19}]

        checkpoint = transit._select_live_checkpoint(
            target, vehicles, set(range(1, 20)), board_order=10, alight_order=18
        )

        self.assertEqual(18, checkpoint)

    def test_live_ride_adds_checkpoint_delta_and_static_remainder(self):
        result = transit._calculate_live_ride(
            {"minutes": 5, "arrprevstationcnt": 2},
            {"minutes": 14, "arrprevstationcnt": 6},
            board_order=10,
            checkpoint_order=14,
            static_live_minutes=8,
            static_remainder_minutes=7,
        )

        self.assertEqual(
            {
                "ride_minutes": 16,
                "live_segment_minutes": 9,
                "static_remainder_minutes": 7,
            },
            result,
        )

    def test_live_ride_rejects_ratio_outside_allowed_range(self):
        result = transit._calculate_live_ride(
            {"minutes": 5, "arrprevstationcnt": 2},
            {"minutes": 35, "arrprevstationcnt": 6},
            board_order=10,
            checkpoint_order=14,
            static_live_minutes=8,
            static_remainder_minutes=7,
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
