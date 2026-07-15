import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

import realtime_bus


class RealtimeBusFailureTest(unittest.TestCase):
    def test_missing_api_key_skips_http_request(self):
        with patch.object(realtime_bus, "TAGO_API_KEY", None), patch.object(
            realtime_bus.requests, "get"
        ) as request_get:
            self.assertIsNone(realtime_bus.get_arrival_minutes("DJB8000001", "DJB30300001"))
            request_get.assert_not_called()

    def test_request_uses_single_short_timeout_before_static_fallback(self):
        with patch.object(
            realtime_bus.requests,
            "get",
            side_effect=requests.Timeout,
        ) as request_get, patch.object(realtime_bus.time, "sleep") as sleep:
            self.assertIsNone(realtime_bus._request_with_retry("https://example.test", {}))
            request_get.assert_called_once_with("https://example.test", params={}, timeout=2)
            sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
