import sys
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from place_lookup import resolve_place


class ResolvePlaceInputTest(unittest.TestCase):
    def test_empty_or_whitespace_name_is_rejected(self):
        self.assertIsNone(resolve_place(""))
        self.assertIsNone(resolve_place("   \t\r\n"))


if __name__ == "__main__":
    unittest.main()
