import json
import sqlite3
import unittest

from scripts.export_restaurant_recommendations import collect_recommendations


def make_export_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE place (
            place_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            address TEXT,
            source_api TEXT,
            overview TEXT,
            extra_json TEXT,
            recommend TEXT
        )
        """
    )
    return conn


class ExportRestaurantRecommendationsTest(unittest.TestCase):
    def test_exports_only_rows_with_recommend_flag(self):
        conn = make_export_db()
        self.addCleanup(conn.close)
        conn.executemany(
            "INSERT INTO place VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("yes", "추천 식당", "restaurant", "대전 중구", "tourapi", "소개", "{}", "추천"),
                ("no", "일반 식당", "restaurant", "대전 서구", "tourapi", "소개", "{}", None),
                ("cafe", "추천 카페", "cafe", "대전 동구", "tourapi", "소개", "{}", "추천"),
            ],
        )

        rows = collect_recommendations(conn)

        self.assertEqual([row["place_id"] for row in rows], ["yes"])

    def test_exports_optional_fields_from_recommendation_extra_json(self):
        conn = make_export_db()
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO place VALUES (?, ?, 'restaurant', ?, ?, ?, ?, '추천')",
            (
                "naver-1",
                "네이버 식당",
                "대전 서구 둔산로 2",
                "naver_search",
                None,
                json.dumps(
                    {
                        "recommendation": {
                            "source": "naver_review",
                            "detailed_category": "한식>칼국수",
                            "reason": "지역성: 칼국수",
                        }
                    },
                    ensure_ascii=False,
                ),
            ),
        )

        row = collect_recommendations(conn)[0]

        self.assertEqual(row["recommend"], "추천")
        self.assertEqual(row["recommendation_basis"], "지역성: 칼국수")
        self.assertEqual(row["representative_food"], "한식>칼국수")
        self.assertEqual(row["source_summary"], "naver_review")

    def test_preserves_legacy_optional_export_fields(self):
        conn = make_export_db()
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO place VALUES (?, ?, 'restaurant', ?, ?, ?, ?, '추천')",
            (
                "food-1",
                "대전 식당",
                "대전 서구 둔산로 2",
                "daejeon_food",
                None,
                json.dumps(
                    {"rprsFod": "칼국수 / 8,000원", "restrntSumm": "칼국수 전문점"},
                    ensure_ascii=False,
                ),
            ),
        )

        row = collect_recommendations(conn)[0]

        self.assertEqual(row["district"], "서구")
        self.assertEqual(row["representative_food"], "칼국수 / 8,000원")
        self.assertEqual(row["source_summary"], "칼국수 전문점")

    def test_normalizes_multiline_overview_to_one_physical_csv_line(self):
        conn = make_export_db()
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO place VALUES (?, ?, 'restaurant', ?, ?, ?, ?, ?)",
            (
                "multiline",
                "한줄 식당",
                "대전광역시 중구",
                "tourapi",
                "첫째 줄\r\n둘째 줄\n셋째 줄",
                "{}",
                "\ucd94\ucc9c",
            ),
        )

        row = collect_recommendations(conn)[0]

        self.assertEqual(row["overview"], "첫째 줄 둘째 줄 셋째 줄")


if __name__ == "__main__":
    unittest.main()
