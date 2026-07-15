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


if __name__ == "__main__":
    unittest.main()
