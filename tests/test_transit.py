import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
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

        def fake_vehicle_locations(route_id):
            calls.append(route_id)
            return []

        with patch.object(transit, "get_route_vehicle_locations", side_effect=fake_vehicle_locations), \
             patch.object(
                 transit,
                 "get_arrival_info",
                 return_value={"minutes": 3, "arrprevstationcnt": 2},
             ):
            result = transit.recommend_bus_routes("성심당", "대전시립박물관")

        self.assertEqual(3, len(result.get("routes", [])))
        self.assertEqual(3, len(calls))
        self.assertEqual(
            {"vehicle_locations_unavailable"},
            {
                route["legs"][0]["realtime_failure_reason"]
                for route in result["routes"]
            },
        )


class FirstLegRealtimeIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.by_route = {
            ("R1", 0): [(1, "S1"), (2, "S2"), (3, "S3"), (4, "S4")],
            ("R2", 0): [(1, "S4"), (2, "S5")],
        }
        self.coords = {
            "S1": (36.35, 127.36),
            "S2": (36.35, 127.38),
            "S3": (36.35, 127.40),
            "S4": (36.35, 127.42),
            "S5": (36.35, 127.44),
        }
        edge_minutes = {}
        for (route_id, updowncd), stops in self.by_route.items():
            for (left_order, left_stop), (right_order, right_stop) in zip(stops, stops[1:]):
                minutes = (
                    transit.haversine_km(*self.coords[left_stop], *self.coords[right_stop])
                    / transit.CAR_SPEED_KMH
                    * 60
                )
                edge_minutes[(route_id, updowncd, left_order, right_order)] = minutes
        self.graph = SimpleNamespace(
            by_route=self.by_route,
            coords=self.coords,
            edge_minutes=edge_minutes,
            stop_names={stop_id: f"{stop_id} 정류장" for stop_id in self.coords},
        )
        self.legs = [
            {
                "route_id": "R1", "updowncd": 0,
                "board_stop_id": "S2", "board_order": 2,
                "alight_stop_id": "S4", "alight_order": 4,
            },
            {
                "route_id": "R2", "updowncd": 0,
                "board_stop_id": "S4", "board_order": 1,
                "alight_stop_id": "S5", "alight_order": 2,
            },
        ]

    def test_only_first_leg_uses_vehicle_and_checkpoint_eta(self):
        vehicles = [
            {"vehicle_no": "BUS-107", "node_order": 1},
            {"vehicle_no": "LEADER", "node_order": 4},
        ]

        def fake_arrival(stop_id, route_id):
            values = {
                ("S2", "R1"): {"minutes": 5, "arrprevstationcnt": 2},
                ("S3", "R1"): {"minutes": 10, "arrprevstationcnt": 3},
            }
            return values.get((stop_id, route_id))

        with patch.object(transit, "get_route_vehicle_locations", return_value=vehicles) as locations, \
             patch.object(transit, "get_arrival_info", side_effect=fake_arrival) as arrivals:
            refined = transit._refine_legs_realtime(
                self.by_route, self.coords, self.legs, graph=self.graph
            )

        first, second = refined
        self.assertEqual("live_checkpoint_plus_static", first["ride_time_source"])
        self.assertEqual("BUS-107", first["vehicle_no"])
        self.assertEqual("S3", first["live_checkpoint_stop_id"])
        self.assertEqual("S3 정류장", first["live_checkpoint_stop"])
        self.assertEqual(5, first["wait_minutes"])
        self.assertFalse(first["wait_estimated"])
        self.assertGreater(first["live_segment_minutes"], 0)
        self.assertGreater(first["static_remainder_minutes"], 0)
        self.assertEqual("medium", first["confidence"])
        self.assertTrue(first["ride_estimated"])
        self.assertIsNone(first["realtime_failure_reason"])

        self.assertEqual("static_stop_distance", second["ride_time_source"])
        self.assertIsNone(second["vehicle_no"])
        self.assertEqual(5, second["wait_minutes"])
        self.assertTrue(second["wait_estimated"])
        self.assertEqual(0.0, second["live_segment_minutes"])
        self.assertGreater(second["ride_minutes"], 0)
        self.assertEqual("low", second["confidence"])
        self.assertEqual("first_bus_leg_only", second["realtime_failure_reason"])
        locations.assert_called_once_with("R1")
        self.assertEqual([("S2", "R1"), ("S3", "R1")], [call.args for call in arrivals.call_args_list])

    def test_no_vehicle_data_keeps_static_result_shape(self):
        with patch.object(transit, "get_route_vehicle_locations", return_value=[]), \
             patch.object(transit, "get_arrival_info", return_value=None):
            refined = transit._refine_legs_realtime(
                self.by_route, self.coords, self.legs, graph=self.graph
            )

        for leg in refined:
            self.assertEqual("static_stop_distance", leg["ride_time_source"])
            self.assertIsNone(leg["vehicle_no"])
            self.assertIsNone(leg["live_checkpoint_stop_id"])
            self.assertIsNone(leg["live_checkpoint_stop"])
            self.assertEqual(0.0, leg["live_segment_minutes"])
            self.assertEqual(leg["ride_minutes"], leg["static_remainder_minutes"])
            self.assertGreater(leg["ride_minutes"], 0)
            self.assertEqual("low", leg["confidence"])
        self.assertEqual("board_arrival_unavailable", refined[0]["realtime_failure_reason"])
        self.assertEqual("first_bus_leg_only", refined[1]["realtime_failure_reason"])

    def test_vehicle_location_failure_reason_is_preserved(self):
        with patch.object(
            transit,
            "get_arrival_info",
            return_value={"minutes": 3, "arrprevstationcnt": 2},
        ), patch.object(transit, "get_route_vehicle_locations", return_value=[]):
            refined = transit._refine_legs_realtime(
                self.by_route, self.coords, self.legs, graph=self.graph
            )

        self.assertEqual(
            "vehicle_locations_unavailable",
            refined[0]["realtime_failure_reason"],
        )

    def test_checkpoint_failure_reason_is_preserved(self):
        vehicles = [
            {"vehicle_no": "TARGET", "node_order": 1},
            {"vehicle_no": "AT-BOARD", "node_order": 2},
        ]
        with patch.object(
            transit,
            "get_arrival_info",
            return_value={"minutes": 3, "arrprevstationcnt": 2},
        ), patch.object(transit, "get_route_vehicle_locations", return_value=vehicles):
            refined = transit._refine_legs_realtime(
                self.by_route, self.coords, self.legs, graph=self.graph
            )

        self.assertEqual(
            "live_checkpoint_unavailable",
            refined[0]["realtime_failure_reason"],
        )

    def test_boarding_vehicle_unmatched_reason_is_preserved(self):
        vehicles = [{"vehicle_no": "AFTER-BOARD", "node_order": 3}]
        with patch.object(
            transit,
            "get_arrival_info",
            return_value={"minutes": 3, "arrprevstationcnt": 2},
        ), patch.object(transit, "get_route_vehicle_locations", return_value=vehicles):
            refined = transit._refine_legs_realtime(
                self.by_route, self.coords, self.legs, graph=self.graph
            )

        self.assertEqual(
            "boarding_vehicle_unmatched",
            refined[0]["realtime_failure_reason"],
        )

    def test_checkpoint_arrival_unavailable_reason_is_preserved(self):
        vehicles = [
            {"vehicle_no": "TARGET", "node_order": 1},
            {"vehicle_no": "LEADER", "node_order": 4},
        ]

        def arrival(stop_id, _route_id):
            if stop_id == "S2":
                return {"minutes": 3, "arrprevstationcnt": 2}
            return None

        with patch.object(transit, "get_arrival_info", side_effect=arrival), patch.object(
            transit, "get_route_vehicle_locations", return_value=vehicles
        ):
            refined = transit._refine_legs_realtime(
                self.by_route, self.coords, self.legs, graph=self.graph
            )

        self.assertEqual(
            "checkpoint_arrival_unavailable",
            refined[0]["realtime_failure_reason"],
        )

    def test_checkpoint_validation_failure_reason_is_preserved(self):
        vehicles = [
            {"vehicle_no": "TARGET", "node_order": 1},
            {"vehicle_no": "LEADER", "node_order": 4},
        ]

        def arrival(stop_id, _route_id):
            if stop_id == "S2":
                return {"minutes": 3, "arrprevstationcnt": 2}
            return {"minutes": 60, "arrprevstationcnt": 3}

        with patch.object(transit, "get_arrival_info", side_effect=arrival), patch.object(
            transit, "get_route_vehicle_locations", return_value=vehicles
        ):
            refined = transit._refine_legs_realtime(
                self.by_route, self.coords, self.legs, graph=self.graph
            )

        self.assertEqual(
            "checkpoint_validation_failed",
            refined[0]["realtime_failure_reason"],
        )

    def test_invalid_board_eta_keeps_static_wait(self):
        for invalid_eta in (-1, float("nan"), float("inf")):
            with self.subTest(invalid_eta=invalid_eta), patch.object(
                transit,
                "get_arrival_info",
                return_value={"minutes": invalid_eta, "arrprevstationcnt": 2},
            ), patch.object(transit, "get_route_vehicle_locations", return_value=[]):
                refined = transit._refine_legs_realtime(
                    self.by_route, self.coords, self.legs, graph=self.graph
                )

            self.assertEqual(5, refined[0]["wait_minutes"])
            self.assertTrue(refined[0]["wait_estimated"])


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

    def test_checkpoint_ignores_duplicate_location_for_target_vehicle(self):
        target = {"vehicle_no": "target", "node_order": 8}
        vehicles = [
            target,
            {"vehicle_no": "target", "node_order": 15},
            {"vehicle_no": "front", "node_order": 17},
        ]

        checkpoint = transit._select_live_checkpoint(
            target, vehicles, set(range(1, 19)), board_order=10, alight_order=18
        )

        self.assertEqual(16, checkpoint)

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

    def test_live_ride_rejects_negative_and_non_finite_eta(self):
        for invalid_eta in (-1, float("nan"), float("inf")):
            with self.subTest(invalid_eta=invalid_eta):
                result = transit._calculate_live_ride(
                    {"minutes": invalid_eta, "arrprevstationcnt": 2},
                    {"minutes": 8, "arrprevstationcnt": 6},
                    board_order=10,
                    checkpoint_order=14,
                    static_live_minutes=8,
                    static_remainder_minutes=7,
                )

                self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
