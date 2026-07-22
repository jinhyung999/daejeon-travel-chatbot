# Giftshop Detail Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enrich the 33 existing `giftshop` rows with verified telephone, opening-hours, and closing-day data without OpenAI or map-page scraping.

**Architecture:** A Kakao Local API adapter produces review candidates but never writes to SQLite. An exporter combines DB rows and Kakao candidates in a UTF-8 CSV, a human verifies owner-controlled or map-detail sources, and a transactional importer applies only approved rows while preserving `extra_json` and creating a live SQLite backup.

**Tech Stack:** Python 3, standard-library `csv`, `dataclasses`, `sqlite3`, `unittest`, `requests`, `python-dotenv`, Kakao Local REST API.

## Global Constraints

- Keep `docs/superpowers/specs/2026-07-22-giftshop-collection-and-recommend-enrichment-design.md` unchanged.
- Target only rows where `place.category='giftshop'`; the expected live count is exactly 33.
- Core fields are `tel`, `open_time`, and `close_day`; do not add schema columns.
- Do not use OpenAI, blog-derived guesses, private map APIs, or HTML scraping of Kakao/Naver map pages.
- Never infer a missing value. Preserve it as NULL/blank when no trustworthy source exists.
- Kakao results are candidates only. No row is written without `review_status='approved'`.
- A nonblank telephone requires `tel_source_url`; nonblank hours or closing day requires `hours_source_url`.
- Preserve all existing `extra_json` keys and merge provenance under `detail_enrichment`.
- Import is atomic, does not erase DB values with blank CSV cells, makes a live SQLite backup before apply, and requires `PRAGMA integrity_check='ok'`.

---

## File Structure

- Create `collectors/kakao_giftshop_detail.py`: Kakao client, name/address normalization, distance calculation, candidate classification.
- Create `scripts/export_giftshop_detail_review.py`: select giftshop rows, collect candidates, and write the review CSV.
- Create `scripts/import_giftshop_detail_review.py`: validate reviewed CSV, merge provenance, dry-run, backup, and atomic apply.
- Create `tests/test_kakao_giftshop_detail.py`: client parameter and candidate-classification tests.
- Create `tests/test_export_giftshop_detail_review.py`: giftshop-only export and error-continuation tests.
- Create `tests/test_import_giftshop_detail_review.py`: validation, merge, rollback, idempotence, backup, and CLI-mode tests.
- Generate `data/curation/giftshop_detail_review.csv`: one-time working review artifact; do not commit it until every row is finalized and its source URLs are safe to retain.

### Task 1: Kakao Candidate Matching

**Files:**
- Create: `collectors/kakao_giftshop_detail.py`
- Create: `tests/test_kakao_giftshop_detail.py`

**Interfaces:**
- Produces: `KakaoLocalClient(rest_api_key, session=None, sleeper=time.sleep, max_retries=3, timeout=10)`.
- Produces: `KakaoLocalClient.search_keyword(query: str, *, lat: float, lng: float) -> list[dict]`.
- Produces: `classify_candidate(place: dict, documents: list[dict]) -> dict` with keys `kakao_name`, `kakao_address`, `kakao_distance_m`, `kakao_tel`, `kakao_place_url`, `match_status`, `match_error`.
- Produces: `normalize_name(value: str) -> str`, `district_from_address(value: str) -> str`, and `haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float` for direct unit testing.

- [ ] **Step 1: Write failing normalization and classification tests**

```python
# tests/test_kakao_giftshop_detail.py
import unittest

from collectors.kakao_giftshop_detail import (
    classify_candidate,
    district_from_address,
    normalize_name,
)


PLACE = {
    "name": "소품샵 잠시다락",
    "address": "대전광역시 서구 둔산로 32-11",
    "lat": 36.3510,
    "lng": 127.3770,
}


def kakao_doc(**overrides):
    value = {
        "place_name": "소품샵 잠시다락",
        "road_address_name": "대전 서구 둔산로 32-11",
        "address_name": "대전 서구 둔산동 1",
        "x": "127.3770",
        "y": "36.3510",
        "phone": "042-123-4567",
        "place_url": "https://place.map.kakao.com/1",
    }
    value.update(overrides)
    return value


class CandidateClassificationTest(unittest.TestCase):
    def test_normalizes_branch_suffix_and_spacing(self):
        self.assertEqual("소품샵잠시다락", normalize_name("소품샵 잠시다락점"))
        self.assertEqual("서구", district_from_address("대전광역시 서구 둔산로 1"))

    def test_exact_name_within_200m_is_matched(self):
        result = classify_candidate(PLACE, [kakao_doc()])
        self.assertEqual("matched", result["match_status"])
        self.assertEqual("042-123-4567", result["kakao_tel"])

    def test_multiple_matching_candidates_are_ambiguous(self):
        result = classify_candidate(
            PLACE,
            [kakao_doc(), kakao_doc(place_url="https://place.map.kakao.com/2")],
        )
        self.assertEqual("ambiguous", result["match_status"])

    def test_wrong_district_or_over_200m_is_ambiguous(self):
        wrong_district = kakao_doc(
            road_address_name="대전 유성구 대학로 1",
            address_name="대전 유성구 궁동 1",
        )
        result = classify_candidate(PLACE, [wrong_district])
        self.assertEqual("ambiguous", result["match_status"])

    def test_unrelated_or_empty_results_are_not_found(self):
        result = classify_candidate(PLACE, [kakao_doc(place_name="다른 가게")])
        self.assertEqual("not_found", result["match_status"])
        self.assertEqual("not_found", classify_candidate(PLACE, [])["match_status"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests and verify the module is missing**

Run: `python -m unittest tests.test_kakao_giftshop_detail -v`

Expected: `ERROR` with `ModuleNotFoundError: No module named 'collectors.kakao_giftshop_detail'`.

- [ ] **Step 3: Implement candidate normalization and classification**

```python
# collectors/kakao_giftshop_detail.py
from dataclasses import dataclass
import math
import re
import time
import unicodedata

import requests


KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
MATCH_RADIUS_M = 200
DISTRICTS = ("유성구", "대덕구", "동구", "서구", "중구")
_BRANCH_SUFFIX = re.compile(r"(?:점|지점)$")


def normalize_name(value):
    text = unicodedata.normalize("NFKC", str(value or "")).lower().strip()
    text = re.sub(r"\([^)]*\)", "", text)
    text = _BRANCH_SUFFIX.sub("", text)
    return "".join(char for char in text if char.isalnum())


def district_from_address(value):
    text = str(value or "")
    return next((district for district in DISTRICTS if district in text), "")


def haversine_m(lat1, lng1, lat2, lng2):
    radius = 6_371_000
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    delta_phi = math.radians(float(lat2) - float(lat1))
    delta_lambda = math.radians(float(lng2) - float(lng1))
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(value))


def _candidate_row(place, document):
    address = document.get("road_address_name") or document.get("address_name") or ""
    distance = haversine_m(place["lat"], place["lng"], document["y"], document["x"])
    return {
        "kakao_name": document.get("place_name") or "",
        "kakao_address": address,
        "kakao_distance_m": round(distance, 1),
        "kakao_tel": document.get("phone") or "",
        "kakao_place_url": document.get("place_url") or "",
        "match_error": "",
    }


def _blank_result(status):
    return {
        "kakao_name": "",
        "kakao_address": "",
        "kakao_distance_m": "",
        "kakao_tel": "",
        "kakao_place_url": "",
        "match_status": status,
        "match_error": "",
    }


def classify_candidate(place, documents):
    rows = []
    place_name = normalize_name(place["name"])
    place_district = district_from_address(place["address"])
    for document in documents:
        try:
            row = _candidate_row(place, document)
        except (KeyError, TypeError, ValueError):
            continue
        if normalize_name(row["kakao_name"]) == place_name:
            rows.append(row)

    if not rows:
        return _blank_result("not_found")

    rows.sort(key=lambda row: row["kakao_distance_m"])
    eligible = [
        row
        for row in rows
        if row["kakao_distance_m"] <= MATCH_RADIUS_M
        and district_from_address(row["kakao_address"]) == place_district
    ]
    selected = eligible[0] if eligible else rows[0]
    selected["match_status"] = (
        "matched" if len(rows) == 1 and len(eligible) == 1 else "ambiguous"
    )
    return selected
```

- [ ] **Step 4: Run classification tests and verify they pass**

Run: `python -m unittest tests.test_kakao_giftshop_detail -v`

Expected: 5 tests run, all `OK`.

- [ ] **Step 5: Add failing Kakao request and retry tests**

```python
# Insert above the final `if __name__ == "__main__":` block in tests/test_kakao_giftshop_detail.py
from unittest.mock import Mock

from collectors.kakao_giftshop_detail import KakaoLocalClient


class KakaoLocalClientTest(unittest.TestCase):
    def test_sends_key_query_coordinates_and_distance_sort(self):
        response = Mock(status_code=200)
        response.json.return_value = {"documents": [kakao_doc()]}
        session = Mock()
        session.get.return_value = response
        client = KakaoLocalClient("secret", session=session, sleeper=lambda _: None)

        documents = client.search_keyword("대전 잠시다락", lat=36.351, lng=127.377)

        self.assertEqual(1, len(documents))
        _, kwargs = session.get.call_args
        self.assertEqual("KakaoAK secret", kwargs["headers"]["Authorization"])
        self.assertEqual("distance", kwargs["params"]["sort"])
        self.assertEqual(15, kwargs["params"]["size"])

    def test_retries_429_then_succeeds(self):
        limited = Mock(status_code=429)
        success = Mock(status_code=200)
        success.json.return_value = {"documents": []}
        session = Mock()
        session.get.side_effect = [limited, success]
        client = KakaoLocalClient("secret", session=session, sleeper=lambda _: None)

        self.assertEqual([], client.search_keyword("대전 잠시다락", lat=36.351, lng=127.377))
        self.assertEqual(2, session.get.call_count)
```

- [ ] **Step 6: Implement the Kakao client**

```python
# Append to collectors/kakao_giftshop_detail.py
class KakaoLocalError(RuntimeError):
    pass


class KakaoLocalClient:
    def __init__(
        self,
        rest_api_key,
        *,
        session=None,
        sleeper=time.sleep,
        max_retries=3,
        timeout=10,
    ):
        if not rest_api_key:
            raise ValueError("KAKAO_REST_API_KEY is required")
        self._headers = {"Authorization": f"KakaoAK {rest_api_key}"}
        self._session = session or requests.Session()
        self._sleeper = sleeper
        self._max_retries = max_retries
        self._timeout = timeout

    def search_keyword(self, query, *, lat, lng):
        params = {
            "query": query,
            "x": str(lng),
            "y": str(lat),
            "sort": "distance",
            "page": 1,
            "size": 15,
        }
        for attempt in range(self._max_retries + 1):
            try:
                response = self._session.get(
                    KAKAO_KEYWORD_URL,
                    params=params,
                    headers=self._headers,
                    timeout=self._timeout,
                )
                if response.status_code == 200:
                    payload = response.json()
                    documents = payload.get("documents")
                    if not isinstance(documents, list):
                        raise KakaoLocalError("Kakao response has no documents list")
                    return documents
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable:
                    raise KakaoLocalError(f"Kakao keyword search failed: HTTP {response.status_code}")
            except requests.RequestException as error:
                if attempt == self._max_retries:
                    raise KakaoLocalError("Kakao keyword search failed after retries") from error
            if attempt == self._max_retries:
                break
            self._sleeper(2**attempt)
        raise KakaoLocalError("Kakao keyword search failed after retries")
```

- [ ] **Step 7: Run all module tests and commit**

Run: `python -m unittest tests.test_kakao_giftshop_detail -v`

Expected: 7 tests run, all `OK`.

```bash
git add collectors/kakao_giftshop_detail.py tests/test_kakao_giftshop_detail.py
git commit -m "feat: add Kakao giftshop candidate matching"
```

### Task 2: Review CSV Export

**Files:**
- Create: `scripts/export_giftshop_detail_review.py`
- Create: `tests/test_export_giftshop_detail_review.py`

**Interfaces:**
- Consumes: `KakaoLocalClient.search_keyword()` and `classify_candidate()` from Task 1.
- Produces: `collect_review_rows(conn: sqlite3.Connection, client) -> list[dict]`.
- Produces: `export_review_csv(db_path: Path, output_path: Path, client) -> int`.
- Produces CLI: `python scripts/export_giftshop_detail_review.py --db PATH --output PATH`.

- [ ] **Step 1: Write failing exporter tests**

```python
# tests/test_export_giftshop_detail_review.py
import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.export_giftshop_detail_review import (
    collect_review_rows,
    export_review_csv,
)


def make_db(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE place (place_id TEXT PRIMARY KEY, name TEXT, category TEXT, "
        "address TEXT, lat REAL, lng REAL, tel TEXT, open_time TEXT, close_day TEXT, extra_json TEXT)"
    )
    conn.executemany(
        "INSERT INTO place VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, '{}')",
        [
            ("g1", "잠시다락", "giftshop", "대전 서구 둔산로 1", 36.35, 127.37),
            ("r1", "식당", "restaurant", "대전 서구 둔산로 2", 36.35, 127.37),
        ],
    )
    conn.commit()
    return conn


class FakeClient:
    def __init__(self, error=False):
        self.error = error

    def search_keyword(self, query, *, lat, lng):
        if self.error:
            raise RuntimeError("network down")
        return [{
            "place_name": "잠시다락",
            "road_address_name": "대전 서구 둔산로 1",
            "address_name": "",
            "x": str(lng),
            "y": str(lat),
            "phone": "042-111-2222",
            "place_url": "https://place.map.kakao.com/1",
        }]


class GiftshopReviewExportTest(unittest.TestCase):
    def test_exports_only_giftshops_with_pending_status(self):
        conn = make_db()
        self.addCleanup(conn.close)
        rows = collect_review_rows(conn, FakeClient())
        self.assertEqual(["g1"], [row["place_id"] for row in rows])
        self.assertEqual("pending", rows[0]["review_status"])
        self.assertEqual("042-111-2222", rows[0]["tel"])
        self.assertEqual(rows[0]["kakao_place_url"], rows[0]["tel_source_url"])

    def test_api_error_becomes_error_row_and_collection_continues(self):
        conn = make_db()
        self.addCleanup(conn.close)
        row = collect_review_rows(conn, FakeClient(error=True))[0]
        self.assertEqual("error", row["match_status"])
        self.assertIn("network down", row["match_error"])

    def test_writes_utf8_csv_with_exact_header(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "test.db"
            conn = make_db(db_path)
            conn.close()
            output = Path(root) / "review.csv"
            count = export_review_csv(db_path, output, FakeClient())
            with output.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(1, count)
            self.assertEqual("잠시다락", rows[0]["name"])
            self.assertEqual("", rows[0]["verified_at"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run exporter tests and verify failure**

Run: `python -m unittest tests.test_export_giftshop_detail_review -v`

Expected: `ERROR` with `ModuleNotFoundError: No module named 'scripts.export_giftshop_detail_review'`.

- [ ] **Step 3: Implement the exporter**

```python
# scripts/export_giftshop_detail_review.py
import argparse
import csv
import os
from pathlib import Path
import sqlite3
import sys

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from collectors.kakao_giftshop_detail import KakaoLocalClient, classify_candidate


DEFAULT_DB_PATH = REPO_ROOT / "db" / "travel.db"
DEFAULT_OUTPUT_PATH = REPO_ROOT / "data" / "curation" / "giftshop_detail_review.csv"
FIELDNAMES = [
    "place_id", "name", "address", "lat", "lng",
    "kakao_name", "kakao_address", "kakao_distance_m", "kakao_tel",
    "kakao_place_url", "match_status", "match_error",
    "tel", "open_time", "close_day", "tel_source_url", "hours_source_url",
    "verified_at", "review_status", "review_note",
]


def _giftshops(conn):
    conn.row_factory = sqlite3.Row
    return [dict(row) for row in conn.execute(
        "SELECT place_id, name, address, lat, lng, tel, open_time, close_day "
        "FROM place WHERE category='giftshop' ORDER BY place_id"
    )]


def collect_review_rows(conn, client):
    output = []
    for place in _giftshops(conn):
        try:
            documents = client.search_keyword(
                f"대전 {place['name']}", lat=place["lat"], lng=place["lng"]
            )
            candidate = classify_candidate(place, documents)
        except Exception as error:
            candidate = {
                "kakao_name": "", "kakao_address": "", "kakao_distance_m": "",
                "kakao_tel": "", "kakao_place_url": "", "match_status": "error",
                "match_error": str(error),
            }
        suggested_tel = place["tel"] or candidate["kakao_tel"]
        output.append({
            "place_id": place["place_id"], "name": place["name"],
            "address": place["address"] or "", "lat": place["lat"], "lng": place["lng"],
            **candidate,
            "tel": suggested_tel or "",
            "open_time": place["open_time"] or "",
            "close_day": place["close_day"] or "",
            "tel_source_url": candidate["kakao_place_url"] if suggested_tel == candidate["kakao_tel"] else "",
            "hours_source_url": "", "verified_at": "", "review_status": "pending",
            "review_note": "",
        })
    return output


def export_review_csv(db_path, output_path, client):
    with sqlite3.connect(db_path) as conn:
        rows = collect_review_rows(conn, client)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(description="Export giftshop detail review CSV")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args(argv)
    client = KakaoLocalClient(os.getenv("KAKAO_REST_API_KEY"))
    count = export_review_csv(args.db, args.output, client)
    print(f"exported={count} output={args.output}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests and commit**

Run: `python -m unittest tests.test_export_giftshop_detail_review -v`

Expected: 3 tests run, all `OK`.

```bash
git add scripts/export_giftshop_detail_review.py tests/test_export_giftshop_detail_review.py
git commit -m "feat: export giftshop detail review CSV"
```

### Task 3: Validated Transactional Import

**Files:**
- Create: `scripts/import_giftshop_detail_review.py`
- Create: `tests/test_import_giftshop_detail_review.py`

**Interfaces:**
- Consumes: the exact `FIELDNAMES` generated by Task 2.
- Produces: `read_review_rows(path: Path) -> list[dict]`.
- Produces: `apply_review_rows(conn: sqlite3.Connection, rows: list[dict]) -> ImportStats`.
- Produces: `import_review_file(db_path: Path, csv_path: Path, *, apply: bool) -> tuple[ImportStats, Path | None]`.
- Produces CLI dry-run by default and live apply with `--apply`.

- [ ] **Step 1: Write failing validation and transaction tests**

```python
# tests/test_import_giftshop_detail_review.py
import csv
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.export_giftshop_detail_review import FIELDNAMES
from scripts.import_giftshop_detail_review import apply_review_rows, import_review_file


def make_db(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE place (place_id TEXT PRIMARY KEY, name TEXT, category TEXT, "
        "address TEXT, lat REAL, lng REAL, tel TEXT, open_time TEXT, close_day TEXT, extra_json TEXT)"
    )
    conn.execute(
        "INSERT INTO place VALUES ('g1','잠시다락','giftshop','대전 서구',36.35,127.37,NULL,NULL,NULL,?)",
        (json.dumps({
            "naver_link": "https://old.example",
            "detail_enrichment": {"legacy": "kept"},
        }),),
    )
    conn.commit()
    return conn


def approved_row(**overrides):
    row = {field: "" for field in FIELDNAMES}
    row.update({
        "place_id": "g1", "name": "잠시다락", "tel": "042-111-2222",
        "open_time": "매일 12:00~20:00", "close_day": "매주 월요일",
        "tel_source_url": "https://place.map.kakao.com/1",
        "hours_source_url": "https://official.example/hours",
        "verified_at": "2026-07-22", "review_status": "approved",
    })
    row.update(overrides)
    return row


class GiftshopReviewImportTest(unittest.TestCase):
    def test_updates_approved_and_preserves_extra_json(self):
        conn = make_db()
        self.addCleanup(conn.close)
        stats = apply_review_rows(conn, [approved_row()])
        tel, hours, closed, raw = conn.execute(
            "SELECT tel, open_time, close_day, extra_json FROM place WHERE place_id='g1'"
        ).fetchone()
        self.assertEqual(("042-111-2222", "매일 12:00~20:00", "매주 월요일"), (tel, hours, closed))
        extra = json.loads(raw)
        self.assertEqual("https://old.example", extra["naver_link"])
        self.assertEqual("kept", extra["detail_enrichment"]["legacy"])
        self.assertEqual("manual", extra["detail_enrichment"]["verification"])
        self.assertEqual(1, stats.updated)

    def test_pending_and_rejected_do_not_update(self):
        conn = make_db()
        self.addCleanup(conn.close)
        stats = apply_review_rows(conn, [approved_row(review_status="pending")])
        self.assertEqual(0, stats.updated)
        self.assertIsNone(conn.execute("SELECT tel FROM place WHERE place_id='g1'").fetchone()[0])

    def test_rejects_missing_sources_bad_date_and_non_giftshop(self):
        cases = [
            approved_row(tel_source_url=""),
            approved_row(hours_source_url=""),
            approved_row(verified_at="22-07-2026"),
            approved_row(place_id="missing"),
        ]
        for row in cases:
            with self.subTest(row=row):
                conn = make_db()
                try:
                    with self.assertRaises(ValueError):
                        apply_review_rows(conn, [row])
                    self.assertIsNone(conn.execute("SELECT tel FROM place WHERE place_id='g1'").fetchone()[0])
                finally:
                    conn.close()

    def test_invalid_second_row_rolls_back_first(self):
        conn = make_db()
        self.addCleanup(conn.close)
        with self.assertRaises(ValueError):
            apply_review_rows(conn, [approved_row(), approved_row(place_id="missing")])
        self.assertIsNone(conn.execute("SELECT tel FROM place WHERE place_id='g1'").fetchone()[0])

    def test_blank_cells_do_not_erase_and_repeat_is_idempotent(self):
        conn = make_db()
        self.addCleanup(conn.close)
        apply_review_rows(conn, [approved_row()])
        apply_review_rows(conn, [approved_row(open_time="", close_day="", hours_source_url="")])
        self.assertEqual(
            ("매일 12:00~20:00", "매주 월요일"),
            conn.execute("SELECT open_time, close_day FROM place WHERE place_id='g1'").fetchone(),
        )

    def test_live_apply_creates_backup_and_dry_run_does_not_mutate(self):
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "travel.db"
            make_db(db_path).close()
            csv_path = Path(root) / "review.csv"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=FIELDNAMES)
                writer.writeheader()
                writer.writerow(approved_row())
            _, dry_backup = import_review_file(db_path, csv_path, apply=False)
            self.assertIsNone(dry_backup)
            with sqlite3.connect(db_path) as conn:
                self.assertIsNone(conn.execute("SELECT tel FROM place WHERE place_id='g1'").fetchone()[0])
            _, backup = import_review_file(db_path, csv_path, apply=True)
            self.assertTrue(backup.is_file())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run importer tests and verify failure**

Run: `python -m unittest tests.test_import_giftshop_detail_review -v`

Expected: `ERROR` with `ModuleNotFoundError: No module named 'scripts.import_giftshop_detail_review'`.

- [ ] **Step 3: Implement strict parsing, merge, transaction, dry-run, and backup**

```python
# scripts/import_giftshop_detail_review.py
import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import date, datetime
import json
from pathlib import Path
import sqlite3
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.export_giftshop_detail_review import FIELDNAMES
from scripts.recommendation_json import dump_json_object, load_json_object


@dataclass(frozen=True)
class ImportStats:
    approved: int
    updated: int
    pending: int
    rejected: int


def _text(row, key):
    return str(row.get(key) or "").strip()


def read_review_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = [field for field in FIELDNAMES if field not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"review CSV missing columns: {', '.join(missing)}")
        rows = list(reader)
    ids = [_text(row, "place_id") for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("review CSV contains duplicate place_id")
    return rows


def _validate_approved(row):
    verified_at = _text(row, "verified_at")
    try:
        datetime.strptime(verified_at, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError(f"invalid verified_at for {_text(row, 'place_id')}") from error
    if _text(row, "tel") and not _text(row, "tel_source_url"):
        raise ValueError(f"telephone source is required for {_text(row, 'place_id')}")
    if (_text(row, "open_time") or _text(row, "close_day")) and not _text(row, "hours_source_url"):
        raise ValueError(f"hours source is required for {_text(row, 'place_id')}")


def _merge_extra(raw, row):
    extra = load_json_object(raw, label=f"extra_json for {_text(row, 'place_id')}")
    existing_detail = extra.get("detail_enrichment")
    if existing_detail is not None and not isinstance(existing_detail, dict):
        raise ValueError(
            f"detail_enrichment must be an object for {_text(row, 'place_id')}"
        )
    detail = dict(existing_detail or {})
    detail.update({
        "verified_at": _text(row, "verified_at"),
        "verification": "manual",
    })
    if _text(row, "tel_source_url"):
        detail["tel_source_url"] = _text(row, "tel_source_url")
    if _text(row, "hours_source_url"):
        detail["hours_source_url"] = _text(row, "hours_source_url")
    if _text(row, "review_note"):
        detail["review_note"] = _text(row, "review_note")
    extra["detail_enrichment"] = detail
    return dump_json_object(extra)


def apply_review_rows(conn, rows):
    statuses = {"approved", "pending", "rejected"}
    approved = sum(_text(row, "review_status") == "approved" for row in rows)
    pending = sum(_text(row, "review_status") == "pending" for row in rows)
    rejected = sum(_text(row, "review_status") == "rejected" for row in rows)
    unknown = sorted({_text(row, "review_status") for row in rows} - statuses)
    if unknown:
        raise ValueError(f"unknown review_status: {', '.join(unknown)}")

    conn.execute("SAVEPOINT giftshop_detail_import")
    try:
        updated = 0
        for row in rows:
            if _text(row, "review_status") != "approved":
                continue
            _validate_approved(row)
            current = conn.execute(
                "SELECT tel, open_time, close_day, extra_json FROM place "
                "WHERE place_id=? AND category='giftshop'",
                (_text(row, "place_id"),),
            ).fetchone()
            if current is None:
                raise ValueError(f"approved place is missing or not giftshop: {_text(row, 'place_id')}")
            tel, open_time, close_day, raw_extra = current
            conn.execute(
                "UPDATE place SET tel=?, open_time=?, close_day=?, extra_json=? WHERE place_id=?",
                (
                    _text(row, "tel") or tel,
                    _text(row, "open_time") or open_time,
                    _text(row, "close_day") or close_day,
                    _merge_extra(raw_extra, row),
                    _text(row, "place_id"),
                ),
            )
            updated += 1
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"database integrity failed: {integrity}")
        conn.execute("RELEASE SAVEPOINT giftshop_detail_import")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT giftshop_detail_import")
        conn.execute("RELEASE SAVEPOINT giftshop_detail_import")
        raise
    return ImportStats(approved=approved, updated=updated, pending=pending, rejected=rejected)


def _backup_database(source, db_path):
    backup_dir = Path(db_path).parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"travel_pre_giftshop_detail_{timestamp}.db"
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
    finally:
        target.close()
    return backup_path


def import_review_file(db_path, csv_path, *, apply):
    rows = read_review_rows(csv_path)
    source = sqlite3.connect(db_path)
    try:
        if apply:
            backup = _backup_database(source, db_path)
            stats = apply_review_rows(source, rows)
            source.commit()
            return stats, backup
        target = sqlite3.connect(":memory:")
        try:
            source.backup(target)
            stats = apply_review_rows(target, rows)
            return stats, None
        finally:
            target.close()
    finally:
        source.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Import reviewed giftshop details")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    stats, backup = import_review_file(args.db, args.csv, apply=args.apply)
    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        **asdict(stats),
        "backup": str(backup) if backup else None,
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run importer tests and the focused suite**

Run: `python -m unittest tests.test_import_giftshop_detail_review -v`

Expected: 6 tests run, all `OK`.

Run: `python -m unittest tests.test_kakao_giftshop_detail tests.test_export_giftshop_detail_review tests.test_import_giftshop_detail_review -v`

Expected: 16 tests run, all `OK`.

- [ ] **Step 5: Commit importer**

```bash
git add scripts/import_giftshop_detail_review.py tests/test_import_giftshop_detail_review.py
git commit -m "feat: import verified giftshop details atomically"
```

### Task 4: Generate and Audit the Live Review CSV

**Files:**
- Create locally: `data/curation/giftshop_detail_review.csv`
- Modify only through review: `data/curation/giftshop_detail_review.csv`

**Interfaces:**
- Consumes: live `db/travel.db`, `.env` `KAKAO_REST_API_KEY`, and Task 2 exporter.
- Produces: exactly 33 review rows, each with `match_status` and a Kakao candidate where available.

- [ ] **Step 1: Run the complete test suite before live API access**

Run: `python -m unittest discover -s tests -v`

Expected: all tests pass; do not proceed on any failure.

- [ ] **Step 2: Export the live candidate CSV**

Run:

```bash
python scripts/export_giftshop_detail_review.py --db db/travel.db --output data/curation/giftshop_detail_review.csv
```

Expected: `exported=33` and no uncaught API error.

- [ ] **Step 3: Audit structural counts without changing the CSV**

Run:

```bash
python -c "import csv,collections; p='data/curation/giftshop_detail_review.csv'; r=list(csv.DictReader(open(p,encoding='utf-8-sig'))); print({'rows':len(r),'unique_ids':len({x['place_id'] for x in r}),'match_status':dict(collections.Counter(x['match_status'] for x in r)),'review_status':dict(collections.Counter(x['review_status'] for x in r))})"
```

Expected: `rows` and `unique_ids` are both 33, and all `review_status` values are `pending`.

- [ ] **Step 4: Resolve all `error` and `ambiguous` candidates manually**

For each such row, compare the DB name/address/coordinates with `kakao_name`, `kakao_address`, and `kakao_distance_m`. Do not promote an uncertain phone number. Record the reason in `review_note` and retain the original `match_status` as an audit trail.

- [ ] **Step 5: Commit implementation before curation**

Run: `git status --short`

Expected: only the review CSV is untracked or ignored; source and tests are committed. Do not commit `.env`, API responses, cookies, or the database backup.

### Task 5: Verify Sources, Dry-Run, and Apply

**Files:**
- Modify: `data/curation/giftshop_detail_review.csv`
- Modify by importer: `db/travel.db`
- Create by importer: `db/backups/travel_pre_giftshop_detail_YYYYMMDD-HHMMSS.db`

**Interfaces:**
- Consumes: the 33-row review CSV from Task 4.
- Produces: a final CSV with every row `approved` or `rejected`, a backed-up and enriched DB, and a machine-readable import summary.

- [ ] **Step 1: Verify every row from trustworthy sources**

Use this order for each shop: owner website, owner-operated Instagram profile/current notice, Kakao map detail, then Naver map detail. Fill `tel`, `open_time`, and `close_day` only when explicitly shown. Copy the exact evidence URL into `tel_source_url` and/or `hours_source_url`, set `verified_at` to the actual review date, explain conflicts in `review_note`, and set `review_status` to `approved`. Use `rejected` only when the row is not the intended shop or cannot be verified after checking the available sources.

- [ ] **Step 2: Confirm review completion and source coverage**

Run:

```bash
python -c "import csv,collections; r=list(csv.DictReader(open('data/curation/giftshop_detail_review.csv',encoding='utf-8-sig'))); s=collections.Counter(x['review_status'] for x in r); assert len(r)==33 and not s.get('pending') and not s.get(''); assert all((not x['tel'] or x['tel_source_url']) and (not (x['open_time'] or x['close_day']) or x['hours_source_url']) for x in r if x['review_status']=='approved'); print(dict(s))"
```

Expected: the printed counts sum to 33 and contain only `approved` and `rejected`.

- [ ] **Step 3: Run importer in dry-run mode**

Run:

```bash
python scripts/import_giftshop_detail_review.py --db db/travel.db --csv data/curation/giftshop_detail_review.csv
```

Expected: JSON output with `"mode": "dry-run"`, `"updated"` equal to `"approved"`, and `"backup": null`; `git status --short db/travel.db` remains unchanged from before the command.

- [ ] **Step 4: Apply once and retain the reported backup path**

Run:

```bash
python scripts/import_giftshop_detail_review.py --db db/travel.db --csv data/curation/giftshop_detail_review.csv --apply
```

Expected: JSON output with `"mode": "apply"`, `"updated"` equal to `"approved"`, and a non-null backup under `db/backups/`.

- [ ] **Step 5: Verify DB counts, completeness, metadata, and integrity**

Run:

```bash
python -c "import sqlite3,json; c=sqlite3.connect('db/travel.db'); r=c.execute(\"SELECT COUNT(*),SUM(tel IS NOT NULL),SUM(open_time IS NOT NULL),SUM(close_day IS NOT NULL) FROM place WHERE category='giftshop'\").fetchone(); assert r[0]==33; assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; bad=[p for p,x in c.execute(\"SELECT place_id,extra_json FROM place WHERE category='giftshop' AND (tel IS NOT NULL OR open_time IS NOT NULL OR close_day IS NOT NULL)\") if 'detail_enrichment' not in json.loads(x or '{}')]; assert not bad; print({'giftshops':r[0],'tel':r[1],'open_time':r[2],'close_day':r[3],'integrity':'ok'})"
```

Expected: `giftshops` is 33, `integrity` is `ok`, and filled-field counts are reported without assertion failure.

- [ ] **Step 6: Re-run focused and full tests**

Run: `python -m unittest tests.test_kakao_giftshop_detail tests.test_export_giftshop_detail_review tests.test_import_giftshop_detail_review -v`

Expected: 16 tests run, all `OK`.

Run: `python -m unittest discover -s tests -v`

Expected: all tests pass.

- [ ] **Step 7: Commit reviewed data separately**

Review `git status --short` and ensure no `.env`, backup DB, browser data, or unrelated files are staged.

```bash
git add db/travel.db
git add -f data/curation/giftshop_detail_review.csv
git commit -m "data: enrich verified giftshop contact details"
```

The committed CSV is the audit record for the one-time enrichment. If source URLs contain private tokens or session parameters, sanitize those URLs before staging.
