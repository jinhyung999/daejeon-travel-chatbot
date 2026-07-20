import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import requests


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import car_route


START = {"name": "대전역", "lat": 36.3324, "lng": 127.4348}
END = {"name": "국립중앙과학관", "lat": 36.3741, "lng": 127.3751}


def tmap_response(properties):
    response = Mock()
    response.json.return_value = {"features": [{"properties": properties}]}
    return response


class CarRouteSuccessTest(unittest.TestCase):
    def test_resolves_places_posts_coordinates_and_normalizes_summary(self):
        response = tmap_response({
            "totalDistance": 8827,
            "totalTime": 1104,
            "taxiFare": 10200,
        })

        with patch.dict(os.environ, {"TMAP_API_KEY": "test-key"}), patch.object(
            car_route, "resolve_place", side_effect=[START, END]
        ) as resolve, patch.object(
            car_route.requests, "post", return_value=response
        ) as request_post:
            result = car_route.get_car_route(" 대전역 ", "국립중앙과학관")

        self.assertTrue(result["success"])
        self.assertEqual(8827, result["distance_meters"])
        self.assertEqual(8.8, result["distance_km"])
        self.assertEqual(1104, result["duration_seconds"])
        self.assertEqual(19, result["duration_minutes"])
        self.assertEqual(10200, result["taxi_fare_won"])
        self.assertEqual("TMAP", result["source"])
        self.assertIn("calculated_at", result)
        self.assertEqual(
            {"name": "대전역", "lat": 36.3324, "lng": 127.4348},
            result["from_place"],
        )
        resolve.assert_has_calls([call("대전역"), call("국립중앙과학관")])
        _, kwargs = request_post.call_args
        self.assertEqual("test-key", kwargs["headers"]["appKey"])
        self.assertEqual("127.4348", kwargs["json"]["startX"])
        self.assertEqual("36.3324", kwargs["json"]["startY"])
        self.assertEqual("127.3751", kwargs["json"]["endX"])
        self.assertEqual("36.3741", kwargs["json"]["endY"])
        self.assertEqual(10, kwargs["timeout"])
        response.raise_for_status.assert_called_once_with()


class CarRouteFailureTest(unittest.TestCase):
    def test_rejects_empty_input_before_place_lookup(self):
        with patch.object(car_route, "resolve_place") as resolve:
            result = car_route.get_car_route(" ", "국립중앙과학관")

        self.assertEqual("invalid_input", result["reason"])
        resolve.assert_not_called()

    def test_reports_which_place_could_not_be_found(self):
        with patch.object(car_route, "resolve_place", side_effect=[START, None]):
            result = car_route.get_car_route("대전역", "없는 목적지")

        self.assertEqual("place_not_found", result["reason"])
        self.assertIn("도착", result["message"])

    def test_missing_key_skips_http_request(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(
            car_route, "resolve_place", side_effect=[START, END]
        ), patch.object(car_route.requests, "post") as request_post:
            result = car_route.get_car_route("대전역", "국립중앙과학관")

        self.assertEqual("missing_api_key", result["reason"])
        request_post.assert_not_called()

    def test_request_error_has_stable_failure_contract(self):
        with patch.dict(os.environ, {"TMAP_API_KEY": "test-key"}), patch.object(
            car_route, "resolve_place", side_effect=[START, END]
        ), patch.object(
            car_route.requests, "post", side_effect=requests.Timeout("secret body")
        ):
            result = car_route.get_car_route("대전역", "국립중앙과학관")

        self.assertEqual("api_request_failed", result["reason"])
        self.assertNotIn("secret body", str(result))

    def test_missing_or_non_numeric_summary_is_invalid(self):
        response = tmap_response({"totalDistance": "broken", "totalTime": 100})
        with patch.dict(os.environ, {"TMAP_API_KEY": "test-key"}), patch.object(
            car_route, "resolve_place", side_effect=[START, END]
        ), patch.object(car_route.requests, "post", return_value=response):
            result = car_route.get_car_route("대전역", "국립중앙과학관")

        self.assertEqual("invalid_api_response", result["reason"])

    def test_non_finite_summary_is_invalid(self):
        response = tmap_response({
            "totalDistance": float("inf"),
            "totalTime": 100,
            "taxiFare": 1000,
        })
        with patch.dict(os.environ, {"TMAP_API_KEY": "test-key"}), patch.object(
            car_route, "resolve_place", side_effect=[START, END]
        ), patch.object(car_route.requests, "post", return_value=response):
            result = car_route.get_car_route("대전역", "국립중앙과학관")

        self.assertEqual("invalid_api_response", result["reason"])


if __name__ == "__main__":
    unittest.main()
