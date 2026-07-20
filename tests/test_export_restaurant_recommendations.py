import json
import sqlite3
import unittest

from scripts.export_restaurant_recommendations import collect_recommendations


class ExportRestaurantRecommendationsTest(unittest.TestCase):
    def test_exports_only_qualified_restaurants(self):
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
                extra_json TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO place VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("tour-1", "관광식당", "restaurant", "대전 동구 중앙로 1", "tourapi", "대표 메뉴가 포함된 충분한 소개", None),
                (
                    "food-1",
                    "대전식당",
                    "restaurant",
                    "대전 서구 둔산로 2",
                    "daejeon_food",
                    None,
                    json.dumps({"rprsFod": "칼국수 / 8,000원", "restrntSumm": "칼국수 전문점"}, ensure_ascii=False),
                ),
                ("cafe-1", "관광카페", "cafe", "대전 중구 중앙로 3", "tourapi", "소개가 있는 카페", None),
                ("plain-1", "일반식당", "restaurant", "대전 유성구 대학로 4", "sbiz", None, "{}"),
            ],
        )

        rows = collect_recommendations(conn)
        conn.close()

        self.assertEqual([row["place_id"] for row in rows], ["food-1", "tour-1"])
        self.assertEqual({row.get("recommend") for row in rows}, {"추천"})
        self.assertTrue(all("recommand" not in row for row in rows))
        self.assertEqual(rows[0]["district"], "서구")
        self.assertEqual(rows[0]["representative_food"], "칼국수 / 8,000원")
        self.assertEqual(rows[0]["source_summary"], "칼국수 전문점")
        self.assertEqual(rows[1]["recommendation_basis"], "valid_overview")


if __name__ == "__main__":
    unittest.main()
