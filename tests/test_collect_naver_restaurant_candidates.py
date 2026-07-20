import csv
import sqlite3
import unittest
import uuid
from pathlib import Path

from scripts.collect_naver_restaurant_candidates import (
    Candidate,
    ExistingRestaurant,
    candidate_from_item,
    collect_local_candidates,
    duplicate_status,
    iter_local_queries,
    load_existing_restaurants,
    merge_candidate,
    normalize_address,
    normalize_name,
)


class FakeSearchClient:
    def __init__(self, local_by_call=None, blog_by_call=None):
        self.local_by_call = list(local_by_call or [])
        self.blog_by_call = list(blog_by_call or [])
        self.local_calls = []
        self.blog_calls = []

    def search_local(self, query, sort):
        self.local_calls.append((query, sort))
        return self.local_by_call.pop(0) if self.local_by_call else []

    def search_blog(self, query, sort):
        self.blog_calls.append((query, sort))
        return self.blog_by_call.pop(0)


class CandidateNormalizationTest(unittest.TestCase):
    def test_normalizes_html_name_address_and_scaled_coordinates(self):
        item = {
            "title": "<b>대전</b>식당 (유성점)",
            "category": "한식>칼국수",
            "address": "대전광역시 유성구 봉명동 1-1",
            "roadAddress": "대전광역시 유성구 대학로 1",
            "mapx": "1271234567",
            "mapy": "363123456",
            "link": "https://example.test/place",
        }

        candidate, reason = candidate_from_item(
            item, "유성구", "유성구 칼국수", "comment"
        )

        self.assertEqual(reason, "")
        self.assertEqual(candidate.name, "대전식당 (유성점)")
        self.assertAlmostEqual(candidate.longitude, 127.1234567)
        self.assertAlmostEqual(candidate.latitude, 36.3123456)
        self.assertEqual(candidate.comment_sort_hit_count, 1)
        self.assertEqual(normalize_name(candidate.name), "대전식당유성점")
        self.assertEqual(
            normalize_address(candidate.road_address), "유성구대학로1"
        )

    def test_rejects_wrong_district_and_non_travel_categories(self):
        wrong = {
            "title": "식당",
            "category": "한식",
            "address": "대전광역시 서구 둔산동",
        }
        cafe = {
            "title": "카페",
            "category": "카페,디저트",
            "address": "대전광역시 유성구 봉명동",
        }
        delivery = {
            "title": "배달식당",
            "category": "음식점>치킨",
            "address": "대전광역시 유성구 봉명동",
            "description": "배달전문 치킨점",
        }

        self.assertEqual(
            candidate_from_item(wrong, "유성구", "q", "random")[1],
            "target_district_mismatch",
        )
        self.assertEqual(
            candidate_from_item(cafe, "유성구", "q", "random")[1],
            "non_meal_category",
        )
        self.assertEqual(
            candidate_from_item(delivery, "유성구", "q", "random")[1],
            "delivery_only",
        )


class ExistingDuplicateTest(unittest.TestCase):
    def test_loads_csv_rows_and_db_coordinates_by_place_id(self):
        prefix = f".tmp_restaurants_{uuid.uuid4().hex}"
        csv_path = Path.cwd() / f"{prefix}.csv"
        db_path = Path.cwd() / f"{prefix}.db"
        try:
            with csv_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(
                    stream,
                    fieldnames=["place_id", "name", "address", "district"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "place_id": "p1",
                        "name": "기존식당",
                        "address": "대전광역시 동구 중앙로 1",
                        "district": "동구",
                    }
                )
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE place (place_id TEXT, lat REAL, lng REAL)"
            )
            conn.execute("INSERT INTO place VALUES ('p1', 36.33, 127.43)")
            conn.commit()
            conn.close()

            rows = load_existing_restaurants(csv_path, db_path)
        finally:
            csv_path.unlink(missing_ok=True)
            db_path.unlink(missing_ok=True)

        self.assertEqual(rows[0].latitude, 36.33)
        self.assertEqual(rows[0].longitude, 127.43)

    def test_confirms_same_place_but_keeps_different_branch(self):
        existing = [
            ExistingRestaurant(
                "지역체인 유성점",
                "대전 유성구 대학로 1",
                "유성구",
                36.36,
                127.35,
            )
        ]
        same = Candidate(
            "유성구",
            "지역체인 유성점",
            "한식",
            "",
            "대전 유성구 대학로 1",
            36.3601,
            127.3501,
            "",
        )
        other_branch = Candidate(
            "유성구",
            "지역체인 노은점",
            "한식",
            "",
            "대전 유성구 노은로 9",
            36.38,
            127.32,
            "",
        )

        self.assertEqual(duplicate_status(same, existing), "confirmed")
        self.assertEqual(duplicate_status(other_branch, existing), "clear")

    def test_marks_unresolved_same_name_as_possible(self):
        existing = [
            ExistingRestaurant(
                "한밭식당", "대전 동구 중앙로 1", "동구"
            )
        ]
        candidate = Candidate(
            "동구",
            "한밭식당",
            "한식",
            "",
            "대전 동구 새길 2",
            None,
            None,
            "",
        )

        self.assertEqual(duplicate_status(candidate, existing), "possible")


class LocalCollectionTest(unittest.TestCase):
    def test_query_order_is_stable_and_contains_location_food_pairs(self):
        queries = list(iter_local_queries("대덕구"))

        self.assertEqual(queries[0], "대덕구 맛집")
        self.assertIn("신탄진 칼국수", queries)
        self.assertEqual(len(queries), len(set(queries)))

    def test_merge_accumulates_queries_and_comment_hits(self):
        first = Candidate(
            "대덕구",
            "식당",
            "한식",
            "대전 대덕구 중리동 1",
            "",
            36.3,
            127.4,
            "",
        )
        first.matched_queries = {"대덕구 맛집"}
        first.local_hit_count = 1
        second = Candidate(
            "대덕구",
            "식당",
            "한식",
            "대전 대덕구 중리동 1",
            "",
            36.3,
            127.4,
            "",
        )
        second.matched_queries = {"중리동 한식"}
        second.local_hit_count = 1
        second.comment_sort_hit_count = 1

        merged = merge_candidate([first], second)

        self.assertTrue(merged)
        self.assertEqual(first.local_hit_count, 2)
        self.assertEqual(first.comment_sort_hit_count, 1)
        self.assertEqual(
            first.matched_queries, {"대덕구 맛집", "중리동 한식"}
        )

    def test_collection_calls_both_sorts_and_drops_confirmed_existing(self):
        item = {
            "title": "기존식당",
            "category": "한식",
            "address": "대전광역시 동구 중앙로 1",
            "mapx": "1274300000",
            "mapy": "363300000",
            "roadAddress": "",
        }
        client = FakeSearchClient(local_by_call=[[item], [item]])
        existing = [
            ExistingRestaurant(
                "기존식당",
                "대전광역시 동구 중앙로 1",
                "동구",
                36.33,
                127.43,
            )
        ]

        rows = collect_local_candidates(
            client, "동구", existing, target_pool=1
        )

        self.assertEqual(rows, [])
        self.assertEqual(
            client.local_calls[:2],
            [("동구 맛집", "comment"), ("동구 맛집", "random")],
        )


if __name__ == "__main__":
    unittest.main()
