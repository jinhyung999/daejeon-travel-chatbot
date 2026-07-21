import csv
from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class ApprovedCandidateDataTest(unittest.TestCase):
    def test_approved_candidate_snapshot_has_only_required_fields(self):
        path = REPO_ROOT / "data" / "curation" / "restaurant_candidates_approved.csv"
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))

        self.assertEqual(len(rows), 438)
        self.assertEqual(
            set(rows[0]),
            {
                "district", "name", "category", "address", "road_address",
                "latitude", "longitude", "naver_link",
                "recommendation_score", "recommendation_reason",
            },
        )
        self.assertNotIn("recent_blog_count", rows[0])
        self.assertTrue(all(row["name"].strip() for row in rows))

    def test_approved_candidate_reasons_exclude_blog_count_wording(self):
        path = REPO_ROOT / "data" / "curation" / "restaurant_candidates_approved.csv"
        with path.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))

        count_or_period_pattern = re.compile(
            r"최근\s*\d+\s*(?:일|개월|년)|(?:블로그|게시글|글)\s*\d+\s*건"
        )
        self.assertFalse(
            any(count_or_period_pattern.search(row["recommendation_reason"]) for row in rows)
        )
        self.assertIn("지역성: 칼국수", {row["recommendation_reason"] for row in rows})
