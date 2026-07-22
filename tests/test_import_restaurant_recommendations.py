import csv
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
import hashlib
from io import StringIO
import json
from pathlib import Path
import re
import sqlite3
import unittest
from unittest.mock import patch
import uuid

from scripts import import_restaurant_recommendations as importer
from scripts import dedupe_place
from scripts.init_db import init_db


Candidate = importer.Candidate
ensure_recommend_schema = importer.ensure_recommend_schema
select_existing_place = importer.select_existing_place
apply_recommendations = getattr(importer, "apply_recommendations", None)
stable_place_id = getattr(importer, "stable_place_id", None)


REPO_ROOT = Path(__file__).resolve().parents[1]
APPROVED_FIELDS = [
    "district",
    "name",
    "category",
    "address",
    "road_address",
    "latitude",
    "longitude",
    "naver_link",
    "recommendation_score",
    "recommendation_reason",
]


def make_place_db(database=":memory:", with_recommend=False, factory=None):
    connect_args = {"factory": factory} if factory is not None else {}
    conn = sqlite3.connect(database, **connect_args)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE place (
          place_id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          category TEXT NOT NULL,
          address TEXT,
          lat REAL,
          lng REAL,
          open_time TEXT,
          close_day TEXT,
          fee TEXT,
          has_parking INTEGER,
          tel TEXT,
          source_api TEXT,
          extra_json TEXT,
          overview TEXT,
          homepage TEXT
        )
        """
    )
    if with_recommend:
        ensure_recommend_schema(conn)
    return conn


class FailingIntegrityConnection(sqlite3.Connection):
    class _IntegrityResult:
        @staticmethod
        def fetchone():
            return ("simulated corruption",)

    def execute(self, sql, parameters=()):
        if sql.strip().lower() == "pragma integrity_check":
            return self._IntegrityResult()
        return super().execute(sql, parameters)


class RecommendSchemaTest(unittest.TestCase):
    def test_dedupe_column_lists_preserve_recommend(self):
        self.assertIn("recommend", dedupe_place.ALL_COLUMNS)
        self.assertIn("recommend", dedupe_place.MERGE_COLUMNS)

    def test_schema_migration_is_idempotent(self):
        conn = make_place_db()
        self.addCleanup(conn.close)

        ensure_recommend_schema(conn)
        ensure_recommend_schema(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(place)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(place)")}
        self.assertIn("recommend", columns)
        self.assertIn("idx_place_category_recommend", indexes)

    def test_init_db_migrates_legacy_place_table_before_schema_index(self):
        db_path = REPO_ROOT / f".tmp_recommend_{uuid.uuid4().hex}.db"
        try:
            conn = make_place_db(db_path)
            conn.close()

            with redirect_stdout(StringIO()):
                init_db(db_path=db_path)

            conn = sqlite3.connect(db_path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(place)")}
                indexes = {row[1] for row in conn.execute("PRAGMA index_list(place)")}
                self.assertIn("recommend", columns)
                self.assertIn("idx_place_category_recommend", indexes)
            finally:
                conn.close()
        finally:
            db_path.unlink(missing_ok=True)


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


def candidate_at(name, lat, lng, address="대전 중구 중앙로 1"):
    return Candidate(
        "중구", name, "한식", address, address, lat, lng, "", 80, "검증 완료"
    )


def insert_place(
    conn,
    place_id,
    name,
    lat,
    lng,
    extra_json="{}",
    address="대전 중구 중앙로 1",
    homepage=None,
):
    conn.execute(
        "INSERT INTO place "
        "(place_id,name,category,address,lat,lng,source_api,extra_json,homepage) "
        "VALUES (?,?,'restaurant',?, ?,?,'sbiz',?,?)",
        (place_id, name, address, lat, lng, extra_json, homepage),
    )


def apply_rows(conn, approved, existing_ids=()):
    assert apply_recommendations is not None, "apply_recommendations is missing"
    token = uuid.uuid4().hex
    approved_path = REPO_ROOT / f".tmp_approved_{token}.csv"
    existing_path = REPO_ROOT / f".tmp_existing_{token}.csv"
    try:
        with approved_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=APPROVED_FIELDS)
            writer.writeheader()
            for item in approved:
                writer.writerow(asdict(item))
        with existing_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["place_id"])
            writer.writeheader()
            writer.writerows({"place_id": value} for value in existing_ids)
        return apply_recommendations(conn, existing_path, approved_path)
    finally:
        approved_path.unlink(missing_ok=True)
        existing_path.unlink(missing_ok=True)


def place_row(
    place_id,
    name,
    lat,
    lng,
    source_api,
    address="대전 중구 중앙로 1",
):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE p(place_id, name, lat, lng, source_api, address)"
    )
    conn.execute(
        "INSERT INTO p VALUES (?, ?, ?, ?, ?, ?)",
        (place_id, name, lat, lng, source_api, address),
    )
    row = conn.execute("SELECT * FROM p").fetchone()
    result = dict(row)
    conn.close()
    return result


class PlaceMatchingTest(unittest.TestCase):
    def test_blank_normalized_names_do_not_match_by_proximity(self):
        candidate = candidate_at("---", 36.35, 127.38)
        rows = [place_row("p1", "...", 36.3501, 127.38, "sbiz")]

        self.assertIsNone(select_existing_place(candidate, rows, set()))

    def test_blank_normalized_addresses_do_not_match_without_coordinates(self):
        candidate = candidate_at("중앙식당", 36.35, 127.38, address="---")
        rows = [
            place_row("p1", "중앙식당", None, None, "sbiz", address="...")
        ]

        self.assertIsNone(select_existing_place(candidate, rows, set()))

    def test_exact_name_within_100m_reuses_existing_place(self):
        candidate = candidate_at("영화 반점", 36.40, 127.42)
        rows = [place_row("p1", "영화반점", 36.4005, 127.4202, "sbiz")]

        self.assertEqual(
            select_existing_place(candidate, rows, set())["place_id"], "p1"
        )

    def test_similar_name_within_50m_reuses_existing_place(self):
        candidate = candidate_at("맛집부추해물칼국수", 36.44955, 127.43159)
        rows = [
            place_row(
                "p1", "부추해물칼국수", 36.44950, 127.43160, "tourapi"
            )
        ]

        self.assertEqual(
            select_existing_place(candidate, rows, set())["place_id"], "p1"
        )

    def test_ratio_only_similar_name_within_50m_reuses_existing_place(self):
        candidate = candidate_at("중앙칼국수", 36.35, 127.38)
        rows = [
            place_row("p1", "중앙칼국시", 36.3501, 127.38, "tourapi")
        ]

        self.assertNotIn("중앙칼국수", "중앙칼국시")
        self.assertNotIn("중앙칼국시", "중앙칼국수")
        self.assertEqual(
            select_existing_place(candidate, rows, set())["place_id"], "p1"
        )

    def test_same_name_far_away_is_a_distinct_branch(self):
        candidate = candidate_at("상하이양꼬치", 36.30, 127.40)
        rows = [
            place_row("p1", "상하이양꼬치", 36.39, 127.39, "sbiz")
        ]

        self.assertIsNone(select_existing_place(candidate, rows, set()))

    def test_fuzzy_name_beyond_50m_is_not_reused(self):
        candidate = candidate_at("맛집부추해물칼국수", 36.44955, 127.43159)
        rows = [
            place_row(
                "p1", "부추해물칼국수", 36.45005, 127.43159, "tourapi"
            )
        ]

        self.assertIsNone(select_existing_place(candidate, rows, set()))

    def test_exact_name_and_normalized_address_match_without_coordinates(self):
        candidate = candidate_at(
            "중앙식당", 36.35, 127.38, "대전광역시 중구 중앙로 1"
        )
        rows = [
            place_row(
                "p1", "중앙 식당", None, None, "sbiz", "대전 중구 중앙로 1"
            )
        ]

        self.assertEqual(
            select_existing_place(candidate, rows, set())["place_id"], "p1"
        )

    def test_exact_name_with_different_address_does_not_match_without_coordinates(self):
        candidate = candidate_at(
            "중앙식당", 36.35, 127.38, "대전 중구 중앙로 1"
        )
        rows = [
            place_row(
                "p1", "중앙 식당", None, None, "sbiz", "대전 서구 둔산로 2"
            )
        ]

        self.assertIsNone(select_existing_place(candidate, rows, set()))

    def test_preferred_recommendation_row_wins_multiple_matches(self):
        candidate = candidate_at("중앙식당", 36.35, 127.38)
        rows = [
            place_row("tour", "중앙식당", 36.3501, 127.3801, "tourapi"),
            place_row(
                "preferred", "중앙식당", 36.3502, 127.3801, "sbiz"
            ),
        ]

        self.assertEqual(
            select_existing_place(candidate, rows, {"preferred"})["place_id"],
            "preferred",
        )

    def test_source_priority_wins_before_distance(self):
        candidate = candidate_at("중앙식당", 36.35, 127.38)
        rows = [
            place_row("near", "중앙식당", 36.35001, 127.38, "sbiz"),
            place_row("tour", "중앙식당", 36.3505, 127.38, "tourapi"),
        ]

        self.assertEqual(
            select_existing_place(candidate, rows, set())["place_id"], "tour"
        )

    def test_distance_wins_for_equal_source_priority(self):
        candidate = candidate_at("중앙식당", 36.35, 127.38)
        rows = [
            place_row("far", "중앙식당", 36.3505, 127.38, "sbiz"),
            place_row("near", "중앙식당", 36.3501, 127.38, "sbiz"),
        ]

        self.assertEqual(
            select_existing_place(candidate, rows, set())["place_id"], "near"
        )

    def test_place_id_breaks_an_exact_tie(self):
        candidate = candidate_at("중앙식당", 36.35, 127.38)
        rows = [
            place_row("z-place", "중앙식당", 36.3501, 127.38, "sbiz"),
            place_row("a-place", "중앙식당", 36.3501, 127.38, "sbiz"),
        ]

        self.assertEqual(
            select_existing_place(candidate, rows, set())["place_id"],
            "a-place",
        )


class RecommendationImportTest(unittest.TestCase):
    def test_rejects_nonfinite_and_out_of_bounds_coordinates(self):
        invalid_coordinates = [
            (float("nan"), 127.38),
            (float("inf"), 127.38),
            (-float("inf"), 127.38),
            (36.09, 127.38),
            (36.35, 127.61),
        ]

        for latitude, longitude in invalid_coordinates:
            with self.subTest(latitude=latitude, longitude=longitude):
                conn = make_place_db(with_recommend=True)
                try:
                    with self.assertRaisesRegex(ValueError, "coordinate"):
                        apply_rows(
                            conn,
                            approved=[candidate_at("범위밖식당", latitude, longitude)],
                        )
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM place").fetchone()[0], 0
                    )
                finally:
                    conn.close()

    def test_nonstandard_json_constant_causes_full_rollback(self):
        conn = make_place_db(with_recommend=True)
        self.addCleanup(conn.close)
        insert_place(conn, "marked", "기존식당", 36.31, 127.31)
        insert_place(
            conn,
            "bad-json",
            "중앙식당",
            36.35,
            127.38,
            extra_json='{"legacy": NaN}',
        )
        conn.commit()

        with self.assertRaisesRegex(ValueError, "JSON"):
            apply_rows(
                conn,
                approved=[candidate_at("중앙식당", 36.35, 127.38)],
                existing_ids=["marked"],
            )

        marked = conn.execute(
            "SELECT recommend FROM place WHERE place_id='marked'"
        ).fetchone()[0]
        raw_extra = conn.execute(
            "SELECT extra_json FROM place WHERE place_id='bad-json'"
        ).fetchone()[0]
        self.assertIsNone(marked)
        self.assertEqual(raw_extra, '{"legacy": NaN}')

    def test_unresolved_preferred_id_rolls_back_schema_and_data(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        insert_place(conn, "present", "기존식당", 36.31, 127.31)
        conn.commit()

        with self.assertRaisesRegex(ValueError, "preferred"):
            apply_rows(
                conn,
                approved=[candidate_at("신규식당", 36.40, 127.42)],
                existing_ids=["present", "missing"],
            )

        columns = {row[1] for row in conn.execute("PRAGMA table_info(place)")}
        self.assertNotIn("recommend", columns)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM place").fetchone()[0], 1)

    def test_missing_recommended_coordinates_roll_back(self):
        conn = make_place_db(with_recommend=True)
        self.addCleanup(conn.close)
        insert_place(conn, "missing-coords", "기존식당", None, None)
        conn.commit()

        with self.assertRaisesRegex(ValueError, "coordinate"):
            apply_rows(conn, approved=[], existing_ids=["missing-coords"])

        recommend = conn.execute(
            "SELECT recommend FROM place WHERE place_id='missing-coords'"
        ).fetchone()[0]
        self.assertIsNone(recommend)

    def test_out_of_bounds_recommended_coordinates_roll_back(self):
        conn = make_place_db(with_recommend=True)
        self.addCleanup(conn.close)
        insert_place(conn, "outside-bounds", "기존식당", 0.0, 0.0)
        conn.commit()

        with self.assertRaisesRegex(ValueError, "coordinate"):
            apply_rows(conn, approved=[], existing_ids=["outside-bounds"])

        recommend = conn.execute(
            "SELECT recommend FROM place WHERE place_id='outside-bounds'"
        ).fetchone()[0]
        self.assertIsNone(recommend)

    def test_invalid_recommended_json_rolls_back(self):
        conn = make_place_db(with_recommend=True)
        self.addCleanup(conn.close)
        insert_place(
            conn,
            "invalid-json",
            "기존식당",
            36.31,
            127.31,
            extra_json='{"value": Infinity}',
        )
        conn.commit()

        with self.assertRaisesRegex(ValueError, "JSON"):
            apply_rows(conn, approved=[], existing_ids=["invalid-json"])

        recommend = conn.execute(
            "SELECT recommend FROM place WHERE place_id='invalid-json'"
        ).fetchone()[0]
        self.assertIsNone(recommend)

    def test_failed_integrity_postcondition_rolls_back(self):
        conn = make_place_db(
            with_recommend=True,
            factory=FailingIntegrityConnection,
        )
        self.addCleanup(conn.close)
        insert_place(conn, "existing", "기존식당", 36.31, 127.31)
        conn.commit()

        with self.assertRaisesRegex(ValueError, "integrity"):
            apply_rows(conn, approved=[], existing_ids=["existing"])

        recommend = conn.execute(
            "SELECT recommend FROM place WHERE place_id='existing'"
        ).fetchone()[0]
        self.assertIsNone(recommend)

    def test_blank_address_and_homepage_are_backfilled(self):
        conn = make_place_db(with_recommend=True)
        self.addCleanup(conn.close)
        insert_place(
            conn,
            "p1",
            "중앙식당",
            36.35,
            127.38,
            address="  ",
            homepage="",
        )
        candidate = replace(
            candidate_at(
                "중앙식당",
                36.35,
                127.38,
                address="대전 중구 새주소 9",
            ),
            naver_link="https://naver.me/example",
        )

        apply_rows(conn, approved=[candidate])

        row = conn.execute(
            "SELECT address, homepage FROM place WHERE place_id='p1'"
        ).fetchone()
        self.assertEqual(tuple(row), (candidate.best_address, candidate.naver_link))

    def test_existing_address_and_homepage_are_preserved(self):
        conn = make_place_db(with_recommend=True)
        self.addCleanup(conn.close)
        insert_place(
            conn,
            "p1",
            "중앙식당",
            36.35,
            127.38,
            address="대전 중구 기존주소 1",
            homepage="https://canonical.example/restaurant",
        )
        candidate = replace(
            candidate_at(
                "중앙식당",
                36.35,
                127.38,
                address="대전 중구 새주소 9",
            ),
            naver_link="https://naver.me/example",
        )

        apply_rows(conn, approved=[candidate])

        row = conn.execute(
            "SELECT address, homepage FROM place WHERE place_id='p1'"
        ).fetchone()
        self.assertEqual(
            tuple(row),
            ("대전 중구 기존주소 1", "https://canonical.example/restaurant"),
        )

    def test_blank_naver_link_does_not_replace_null_homepage(self):
        conn = make_place_db(with_recommend=True)
        self.addCleanup(conn.close)
        insert_place(
            conn,
            "p1",
            "중앙식당",
            36.35,
            127.38,
            homepage=None,
        )

        apply_rows(
            conn,
            approved=[candidate_at("중앙식당", 36.35, 127.38)],
        )

        homepage = conn.execute(
            "SELECT homepage FROM place WHERE place_id='p1'"
        ).fetchone()[0]
        self.assertIsNone(homepage)

    def test_existing_coordinates_and_extra_keys_are_preserved(self):
        conn = make_place_db(with_recommend=True)
        self.addCleanup(conn.close)
        insert_place(
            conn,
            "p1",
            "부추해물칼국수",
            36.44,
            127.43,
            extra_json='{"legacy": 1}',
        )

        stats = apply_rows(
            conn,
            approved=[candidate_at("맛집부추해물칼국수", 36.4401, 127.4301)],
        )

        row = conn.execute("SELECT * FROM place WHERE place_id='p1'").fetchone()
        extra = json.loads(row["extra_json"])
        self.assertEqual((row["lat"], row["lng"]), (36.44, 127.43))
        self.assertEqual(row["recommend"], "추천")
        self.assertEqual(extra["legacy"], 1)
        self.assertEqual(
            extra["recommendation"],
            {
                "source": "naver_review",
                "detailed_category": "한식",
                "score": 80,
                "reason": "검증 완료",
                "road_address": "대전 중구 중앙로 1",
                "naver_link": "",
                "naver_latitude": 36.4401,
                "naver_longitude": 127.4301,
            },
        )
        self.assertNotIn("recent_blog_count", extra["recommendation"])
        self.assertEqual(stats.matched_enriched, 1)

    def test_missing_coordinate_pair_is_filled_from_naver(self):
        conn = make_place_db(with_recommend=True)
        self.addCleanup(conn.close)
        insert_place(conn, "p1", "중앙식당", None, 127.38)

        apply_rows(
            conn,
            approved=[
                candidate_at(
                    "중앙식당",
                    36.35,
                    127.38,
                    address="대전 중구 중앙로 1",
                )
            ],
        )

        row = conn.execute(
            "SELECT lat, lng FROM place WHERE place_id='p1'"
        ).fetchone()
        self.assertEqual(tuple(row), (36.35, 127.38))

    def test_unmatched_candidate_is_inserted_once(self):
        conn = make_place_db(with_recommend=True)
        self.addCleanup(conn.close)
        candidate = replace(
            candidate_at("새로운식당", 36.35, 127.38),
            naver_link="https://naver.me/new",
        )

        first = apply_rows(conn, approved=[candidate])
        second = apply_rows(conn, approved=[candidate])

        self.assertIsNotNone(stable_place_id)
        row = conn.execute("SELECT * FROM place").fetchone()
        self.assertEqual(first.inserted, 1)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM place").fetchone()[0], 1)
        self.assertEqual(row["place_id"], stable_place_id(candidate))
        self.assertEqual(row["category"], "restaurant")
        self.assertEqual(row["address"], candidate.best_address)
        self.assertEqual(row["source_api"], "naver_search")
        self.assertEqual(row["homepage"], candidate.naver_link)
        self.assertEqual(row["recommend"], "추천")

    def test_existing_ids_are_marked_and_source_overlap_is_counted(self):
        conn = make_place_db(with_recommend=True)
        self.addCleanup(conn.close)
        insert_place(conn, "existing-only", "기존식당", 36.31, 127.31)
        insert_place(conn, "overlap", "중앙식당", 36.35, 127.38)

        stats = apply_rows(
            conn,
            approved=[candidate_at("중앙식당", 36.35, 127.38)],
            existing_ids=["existing-only", "overlap"],
        )

        marked = conn.execute(
            "SELECT COUNT(*) FROM place WHERE recommend='추천'"
        ).fetchone()[0]
        self.assertEqual(stats.existing_marked, 2)
        self.assertEqual(stats.matched_enriched, 1)
        self.assertEqual(stats.inserted, 0)
        self.assertEqual(stats.source_overlap, 1)
        self.assertEqual(stats.recommended_total, 2)
        self.assertEqual(marked, 2)


class RecommendationImportCliTest(unittest.TestCase):
    def setUp(self):
        token = uuid.uuid4().hex
        self.db_path = REPO_ROOT / f".tmp_cli_{token}.db"
        self.existing_path = REPO_ROOT / f".tmp_cli_existing_{token}.csv"
        self.approved_path = REPO_ROOT / f".tmp_cli_approved_{token}.csv"

        conn = make_place_db(self.db_path, with_recommend=True)
        insert_place(conn, "existing", "홍길동 식당", 36.35, 127.38)
        conn.commit()
        conn.close()

        with self.existing_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["place_id"])
            writer.writeheader()
            writer.writerow({"place_id": "existing"})
        with self.approved_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=APPROVED_FIELDS)
            writer.writeheader()
            writer.writerow(asdict(candidate_at("신규 식당", 36.40, 127.42)))

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)
        self.existing_path.unlink(missing_ok=True)
        self.approved_path.unlink(missing_ok=True)

    def _run_cli(self, apply=False):
        arguments = [
            "--db", str(self.db_path),
            "--existing-csv", str(self.existing_path),
            "--approved-csv", str(self.approved_path),
        ]
        if apply:
            arguments.append("--apply")
        output = StringIO()
        with redirect_stdout(output):
            importer.main(arguments)
        return json.loads(output.getvalue())

    def test_dry_run_reports_counts_without_mutating_database(self):
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()

        summary = self._run_cli()

        after = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self.assertEqual(after, before)
        self.assertEqual(summary["mode"], "dry-run")
        self.assertEqual(summary["existing_marked"], 1)
        self.assertEqual(summary["matched_enriched"], 0)
        self.assertEqual(summary["inserted"], 1)
        self.assertEqual(summary["source_overlap"], 0)
        self.assertEqual(summary["recommended_total"], 2)

    def test_dry_run_opens_source_database_read_only(self):
        real_connect = sqlite3.connect
        calls = []

        def recording_connect(database, *args, **kwargs):
            calls.append((database, kwargs.copy()))
            return real_connect(database, *args, **kwargs)

        with patch.object(importer.sqlite3, "connect", side_effect=recording_connect):
            self._run_cli()

        source, options = calls[0]
        self.assertTrue(str(source).startswith("file:"))
        self.assertIn("mode=ro", str(source))
        self.assertIs(options.get("uri"), True)

    def test_dry_run_does_not_create_missing_database(self):
        missing = REPO_ROOT / f".tmp_missing_{uuid.uuid4().hex}.db"
        arguments = [
            "--db", str(missing),
            "--existing-csv", str(self.existing_path),
            "--approved-csv", str(self.approved_path),
        ]
        caught = None
        created = None
        try:
            with redirect_stderr(StringIO()):
                importer.main(arguments)
        except BaseException as error:
            caught = error
        finally:
            created = missing.exists()
            missing.unlink(missing_ok=True)

        self.assertIsInstance(caught, SystemExit)
        self.assertFalse(created)

    def test_apply_persists_changes_and_reports_counts(self):
        summary = self._run_cli(apply=True)

        conn = sqlite3.connect(self.db_path)
        try:
            recommended = conn.execute(
                "SELECT COUNT(*) FROM place WHERE recommend=?",
                ("\ucd94\ucc9c",),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(summary["mode"], "apply")
        self.assertEqual(summary["inserted"], 1)
        self.assertEqual(summary["recommended_total"], 2)
        self.assertEqual(recommended, 2)
