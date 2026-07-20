import csv
import locale
import sqlite3
import subprocess
import sys
import unittest
import uuid
from datetime import date
from pathlib import Path

from scripts.collect_naver_restaurant_candidates import (
    Candidate,
    ExistingRestaurant,
    FIELDNAMES,
    build_blog_search_url,
    candidate_from_item,
    collect_local_candidates,
    duplicate_status,
    enrich_blog_metrics,
    iter_local_queries,
    load_existing_restaurants,
    merge_candidate,
    normalize_address,
    normalize_name,
    run_collection,
    score_candidate,
    select_candidates,
    validate_output_rows,
    write_candidates,
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


def blog_item(blogger_id, postdate):
    return {
        "title": "식당 방문기",
        "link": f"https://blog.naver.com/{blogger_id}/1",
        "description": "식당 후기 요약",
        "bloggername": f"블로그 {blogger_id}",
        "bloggerlink": f"https://blog.naver.com/{blogger_id}",
        "postdate": postdate,
    }


def blog_response(total, items):
    return {
        "lastBuildDate": "Mon, 20 Jul 2026 12:00:00 +0900",
        "total": total,
        "start": 1,
        "display": len(items),
        "items": items,
    }


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


class BlogEnrichmentTest(unittest.TestCase):
    def test_aggregates_total_recent_posts_distinct_bloggers_and_latest_date(self):
        similarity = blog_response(
            321,
            [
                blog_item("a", "20260501"),
                blog_item("a", "20250401"),
                blog_item("b", "20260301"),
            ],
        )
        by_date = blog_response(
            321,
            [
                blog_item("c", "20260701"),
                blog_item("d", "20250801"),
                blog_item("e", "20240701"),
            ],
        )
        client = FakeSearchClient(blog_by_call=[similarity, by_date])
        candidate = Candidate(
            "중구",
            "대전칼국수",
            "한식>칼국수",
            "대전 중구 대흥동 1",
            "",
            None,
            None,
            "",
        )

        enrich_blog_metrics(client, candidate, today=date(2026, 7, 20))

        self.assertEqual(candidate.blog_result_count, 321)
        self.assertEqual(candidate.recent_blog_count, 2)
        self.assertEqual(candidate.distinct_blogger_count, 2)
        self.assertEqual(candidate.latest_post_date, "20260701")
        self.assertIn("where=blog", build_blog_search_url(candidate))
        self.assertEqual(
            client.blog_calls,
            [("대전칼국수 대흥동", "sim"), ("대전칼국수 대흥동", "date")],
        )

    def test_scores_local_value_and_penalizes_generic_delivery_food(self):
        local = Candidate(
            "중구",
            "원도심 노포 칼국수 본점",
            "한식>칼국수",
            "대전 중구 대흥동",
            "",
            None,
            None,
            "",
        )
        local.local_hit_count = 4
        local.comment_sort_hit_count = 3
        local.blog_result_count = 300
        local.recent_blog_count = 8
        local.distinct_blogger_count = 9
        generic = Candidate(
            "중구",
            "전국치킨",
            "음식점>치킨",
            "대전 중구 대흥동",
            "",
            None,
            None,
            "",
        )

        score_candidate(local)
        score_candidate(generic)

        self.assertGreater(
            local.recommendation_score, generic.recommendation_score
        )
        self.assertIn("지역성", local.recommendation_reason)
        self.assertIn("배달형 음식 감점", generic.recommendation_reason)


class CsvExportTest(unittest.TestCase):
    def test_selects_positive_scores_in_stable_order_and_limit(self):
        a = Candidate(
            "서구", "가식당", "한식", "대전 서구", "", None, None, ""
        )
        b = Candidate(
            "서구", "나식당", "한식", "대전 서구", "", None, None, ""
        )
        c = Candidate(
            "서구", "다식당", "한식", "대전 서구", "", None, None, ""
        )
        a.recommendation_score = 20
        b.recommendation_score = 30
        c.recommendation_score = 0

        selected = select_candidates([a, b, c], 2)

        self.assertEqual([row.name for row in selected], ["나식당", "가식당"])

    def test_atomic_writer_creates_parseable_single_line_csv(self):
        candidate = Candidate(
            "서구",
            "식당\n본점",
            "한식",
            "대전 서구\n둔산동",
            "",
            36.3,
            127.3,
            "",
        )
        candidate.recommendation_score = 10
        prefix = f".tmp_candidate_export_{uuid.uuid4().hex}"
        output = Path.cwd() / f"{prefix}.csv"
        try:
            write_candidates([candidate], output)
            with output.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
        finally:
            output.unlink(missing_ok=True)
            output.with_suffix(".csv.tmp").unlink(missing_ok=True)

        self.assertEqual(list(rows[0]), FIELDNAMES)
        self.assertEqual(rows[0]["review_status"], "pending")
        self.assertEqual(rows[0]["name"], "식당 본점")
        self.assertNotIn("\n", rows[0]["address"])

    def test_validation_rejects_wrong_district_and_confirmed_duplicate(self):
        candidate = Candidate(
            "유성구",
            "기존",
            "한식",
            "대전 서구 둔산동",
            "",
            None,
            None,
            "",
        )
        existing = [
            ExistingRestaurant("기존", "대전 서구 둔산동", "서구")
        ]

        errors = validate_output_rows([candidate], existing)

        self.assertIn("district/address mismatch: 기존", errors)
        self.assertIn("confirmed duplicate: 기존", errors)


class CollectionOrchestratorTest(unittest.TestCase):
    def test_dry_run_validates_inputs_without_search_calls(self):
        client = FakeSearchClient()

        summary = run_collection(
            client=client,
            districts=["대덕구"],
            existing_rows=[],
            output_path=None,
            max_per_district=5,
            skip_blog=False,
            dry_run=True,
            today=date(2026, 7, 20),
        )

        self.assertEqual(summary, {"대덕구": 0})
        self.assertEqual(client.local_calls, [])

    def test_orchestrator_scores_selects_and_writes_rows(self):
        item = {
            "title": "새칼국수",
            "category": "한식>칼국수",
            "address": "대전광역시 대덕구 중리동 1",
            "mapx": "1274000000",
            "mapy": "363000000",
            "roadAddress": "",
        }
        blog = blog_response(10, [blog_item("a", "20260701")])
        client = FakeSearchClient(
            local_by_call=[[item], [item]],
            blog_by_call=[blog, blog],
        )
        output = Path.cwd() / f".tmp_orchestrator_{uuid.uuid4().hex}.csv"
        try:
            summary = run_collection(
                client=client,
                districts=["대덕구"],
                existing_rows=[],
                output_path=output,
                max_per_district=1,
                skip_blog=False,
                dry_run=False,
                today=date(2026, 7, 20),
            )
            with output.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
        finally:
            output.unlink(missing_ok=True)
            output.with_suffix(".csv.tmp").unlink(missing_ok=True)

        self.assertEqual(summary, {"대덕구": 1})
        self.assertEqual(rows[0]["name"], "새칼국수")


class CollectorCliTest(unittest.TestCase):
    def test_script_runs_directly_in_dry_run_mode(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "collect_naver_restaurant_candidates.py"
        )

        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--dry-run",
                "--district",
                "대덕구",
                "--max-per-district",
                "5",
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("대덕구: candidates=0 shortage=0", result.stdout)


if __name__ == "__main__":
    unittest.main()
