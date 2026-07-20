import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import realtime_bus


def tago_response(items, result_code="00"):
    response = Mock()
    response.json.return_value = {
        "response": {
            "header": {"resultCode": result_code},
            "body": {"items": {"item": items}},
        }
    }
    return response


class RealtimeBusParsingAndCacheTest(unittest.TestCase):
    def setUp(self):
        realtime_bus.clear_realtime_cache()

    def test_stop_arrivals_preserve_metadata_and_are_cached(self):
        response = tago_response(
            [
                {
                    "routeid": "DJB30300001",
                    "arrtime": 150,
                    "arrprevstationcnt": "3",
                    "vehicletp": "일반버스",
                },
                {"routeid": "broken", "arrtime": "not-a-number"},
            ]
        )

        with patch.object(realtime_bus, "TAGO_API_KEY", "test-key"), patch.object(
            realtime_bus, "_request_with_retry", return_value=response
        ) as request:
            first = realtime_bus.get_stop_arrivals("DJB8000001")
            second = realtime_bus.get_stop_arrivals("DJB8000001")

        expected = {
            "DJB30300001": {
                "minutes": 2.5,
                "arrprevstationcnt": 3,
                "vehicle_type": "일반버스",
            }
        }
        self.assertEqual(expected, first)
        self.assertEqual(expected, second)
        self.assertEqual(
            expected["DJB30300001"],
            realtime_bus.get_arrival_info("DJB8000001", "DJB30300001"),
        )
        self.assertEqual(
            2.5,
            realtime_bus.get_arrival_minutes("DJB8000001", "DJB30300001"),
        )
        request.assert_called_once()

    def test_route_vehicle_locations_parse_vehicle_number_and_node_order(self):
        response = tago_response(
            {
                "vehicleno": "대전75자1234",
                "nodeord": "7",
                "nodeid": "DJB8000007",
                "gpslati": "36.3501",
                "gpslong": "127.3849",
            }
        )

        with patch.object(realtime_bus, "TAGO_API_KEY", "test-key"), patch.object(
            realtime_bus, "_request_with_retry", return_value=response
        ):
            locations = realtime_bus.get_route_vehicle_locations("DJB30300001")

        self.assertEqual(
            [
                {
                    "vehicle_no": "대전75자1234",
                    "node_order": 7,
                    "stop_id": "DJB8000007",
                    "lat": "36.3501",
                    "lng": "127.3849",
                }
            ],
            locations,
        )

    def test_failed_response_is_cached(self):
        response = tago_response([], result_code="99")

        with patch.object(realtime_bus, "TAGO_API_KEY", "test-key"), patch.object(
            realtime_bus, "_request_with_retry", return_value=response
        ) as request:
            self.assertEqual({}, realtime_bus.get_stop_arrivals("DJB8000001"))
            self.assertEqual({}, realtime_bus.get_stop_arrivals("DJB8000001"))

        request.assert_called_once()

    def test_stop_arrivals_skip_negative_and_non_finite_arrival_times(self):
        response = tago_response([
            {
                "routeid": route_id,
                "arrtime": arrtime,
                "arrprevstationcnt": "3",
                "vehicletp": "일반버스",
            }
            for route_id, arrtime in (
                ("negative", -60),
                ("nan", float("nan")),
                ("infinite", float("inf")),
            )
        ])

        with patch.object(realtime_bus, "TAGO_API_KEY", "test-key"), patch.object(
            realtime_bus, "_request_with_retry", return_value=response
        ):
            arrivals = realtime_bus.get_stop_arrivals("DJB8000001")

        self.assertEqual({}, arrivals)

    def test_route_vehicle_locations_skip_missing_or_empty_vehicle_numbers(self):
        response = tago_response([
            {"vehicleno": None, "nodeord": "5"},
            {"vehicleno": "", "nodeord": "6"},
            {"vehicleno": "BUS-7", "nodeord": "7"},
        ])

        with patch.object(realtime_bus, "TAGO_API_KEY", "test-key"), patch.object(
            realtime_bus, "_request_with_retry", return_value=response
        ):
            locations = realtime_bus.get_route_vehicle_locations("DJB30300001")

        self.assertEqual(["BUS-7"], [item["vehicle_no"] for item in locations])


class RealtimeBusFailureTest(unittest.TestCase):
    def setUp(self):
        realtime_bus.clear_realtime_cache()

    def test_missing_api_key_skips_http_request(self):
        with patch.object(realtime_bus, "TAGO_API_KEY", None), patch.object(
            realtime_bus.requests, "get"
        ) as request_get:
            self.assertIsNone(realtime_bus.get_arrival_minutes("DJB8000001", "DJB30300001"))
            request_get.assert_not_called()

    def test_request_retries_once_with_five_second_timeout(self):
        response = Mock()
        with patch.object(
            realtime_bus.requests,
            "get",
            side_effect=[requests.Timeout, response],
        ) as request_get, patch.object(realtime_bus.time, "sleep") as sleep:
            self.assertIs(
                response,
                realtime_bus._request_with_retry("https://example.test", {}),
            )

        self.assertEqual(2, request_get.call_count)
        request_get.assert_has_calls([
            unittest.mock.call("https://example.test", params={}, timeout=5),
            unittest.mock.call("https://example.test", params={}, timeout=5),
        ])
        sleep.assert_called_once_with(1)

    def test_request_falls_back_after_two_timeouts(self):
        with patch.object(
            realtime_bus.requests,
            "get",
            side_effect=requests.Timeout,
        ) as request_get, patch.object(realtime_bus.time, "sleep") as sleep:
            self.assertIsNone(realtime_bus._request_with_retry("https://example.test", {}))

        self.assertEqual(2, request_get.call_count)
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
