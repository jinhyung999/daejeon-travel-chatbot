from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

from collectors import common
from scripts import dedupe_place


PLACE_SCHEMA = """
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
  homepage TEXT,
  recommend TEXT
)
"""


def create_database(path):
    conn = sqlite3.connect(path)
    conn.execute(PLACE_SCHEMA)
    return conn


def collector_row(place_id, extra_json):
    return {
        "place_id": place_id,
        "name": "중앙식당",
        "category": "restaurant",
        "address": "대전 중구 중앙로 1",
        "lat": 36.35,
        "lng": 127.38,
        "open_time": "10:00",
        "close_day": None,
        "fee": None,
        "has_parking": None,
        "tel": "042-000-0000",
        "source_api": "daejeon_food",
        "extra_json": extra_json,
    }


def place_values(
    place_id,
    source_api,
    extra_json,
    recommend=None,
):
    values = {
        "place_id": place_id,
        "name": "중앙식당",
        "category": "restaurant",
        "address": "대전 중구 중앙로 1",
        "lat": 36.35,
        "lng": 127.38,
        "source_api": source_api,
        "extra_json": extra_json,
        "recommend": recommend,
    }
    return tuple(values.get(column) for column in dedupe_place.ALL_COLUMNS)


class RecommendationLifecycleTest(unittest.TestCase):
    def setUp(self):
        repo_root = Path(__file__).resolve().parents[1]
        self.db_path = repo_root / f".tmp_lifecycle_{uuid.uuid4().hex}.db"
        self.addCleanup(self._remove_database_files)

    def _remove_database_files(self):
        self.db_path.unlink(missing_ok=True)
        Path(str(self.db_path) + "-wal").unlink(missing_ok=True)
        Path(str(self.db_path) + "-shm").unlink(missing_ok=True)

    def test_collector_common_runs_as_a_direct_script(self):
        repo_root = Path(__file__).resolve().parents[1]

        result = subprocess.run(
            [sys.executable, str(repo_root / "collectors" / "common.py")],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_collector_upsert_preserves_existing_recommendation_object(self):
        conn = create_database(self.db_path)
        recommendation = {
            "source": "naver_review",
            "score": 91,
            "reason": "지역성: 칼국수",
        }
        conn.execute(
            "INSERT INTO place "
            "(place_id,name,category,address,lat,lng,source_api,extra_json,recommend) "
            "VALUES ('p1','중앙식당','restaurant','대전 중구 중앙로 1',"
            "36.35,127.38,'sbiz',?,'추천')",
            (json.dumps({"old": True, "recommendation": recommendation}),),
        )
        conn.commit()
        conn.close()
        incoming = json.dumps(
            {
                "collector": "fresh",
                "recommendation": {"score": 1, "collector_note": "new"},
            },
            ensure_ascii=False,
        )

        with patch.object(common, "DB_PATH", self.db_path):
            with redirect_stdout(StringIO()):
                common.upsert_place([collector_row("p1", incoming)])

        conn = sqlite3.connect(self.db_path)
        try:
            extra_json, recommend = conn.execute(
                "SELECT extra_json, recommend FROM place WHERE place_id='p1'"
            ).fetchone()
        finally:
            conn.close()
        extra = json.loads(extra_json)
        self.assertEqual(extra["collector"], "fresh")
        self.assertNotIn("old", extra)
        self.assertEqual(
            extra["recommendation"],
            {
                **recommendation,
                "collector_note": "new",
            },
        )
        self.assertEqual(recommend, "추천")

    def test_dedupe_migrates_legacy_backup_and_preserves_loser_recommendation(self):
        conn = create_database(self.db_path)
        legacy_columns = [
            column for column in dedupe_place.ALL_COLUMNS if column != "recommend"
        ]
        conn.execute(
            "CREATE TABLE place_removed ("
            + ", ".join(f"{column} TEXT" for column in legacy_columns)
            + ", merged_into TEXT, removed_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO place_removed (place_id, name, category) "
            "VALUES ('legacy-backup', '기존백업', 'restaurant')"
        )
        placeholders = ", ".join("?" for _ in dedupe_place.ALL_COLUMNS)
        conn.execute(
            f"INSERT INTO place ({', '.join(dedupe_place.ALL_COLUMNS)}) "
            f"VALUES ({placeholders})",
            place_values("winner", "tourapi", '{"tour":"kept"}'),
        )
        loser_recommendation = {
            "source": "naver_review",
            "score": 88,
            "reason": "검증 완료",
        }
        conn.execute(
            f"INSERT INTO place ({', '.join(dedupe_place.ALL_COLUMNS)}) "
            f"VALUES ({placeholders})",
            place_values(
                "loser",
                "sbiz",
                json.dumps({"recommendation": loser_recommendation}),
                "추천",
            ),
        )
        conn.commit()
        conn.close()

        error = None
        try:
            with patch.object(dedupe_place, "DB_PATH", self.db_path):
                with redirect_stdout(StringIO()):
                    dedupe_place.run(apply=True)
        except Exception as caught:
            error = f"{type(caught).__name__}: {caught}"

        self.assertIsNone(error)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            dedupe_place.ensure_place_removed_schema(conn)
            dedupe_place.ensure_place_removed_schema(conn)
            removed_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(place_removed)")
            }
            winner = conn.execute(
                "SELECT extra_json, recommend FROM place WHERE place_id='winner'"
            ).fetchone()
            loser_backup = conn.execute(
                "SELECT recommend, merged_into FROM place_removed "
                "WHERE place_id='loser'"
            ).fetchone()
            backup_count = conn.execute(
                "SELECT COUNT(*) FROM place_removed"
            ).fetchone()[0]
        finally:
            conn.close()

        self.assertIn("recommend", removed_columns)
        self.assertEqual(json.loads(winner["extra_json"])["tour"], "kept")
        self.assertEqual(
            json.loads(winner["extra_json"])["recommendation"],
            loser_recommendation,
        )
        self.assertEqual(winner["recommend"], "추천")
        self.assertEqual(tuple(loser_backup), ("추천", "winner"))
        self.assertEqual(backup_count, 2)


if __name__ == "__main__":
    unittest.main()
