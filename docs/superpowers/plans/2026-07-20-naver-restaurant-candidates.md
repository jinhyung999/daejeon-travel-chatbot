# Naver Restaurant Candidates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a repeatable Naver Local Search plus Blog Search pipeline that produces 80–100 reviewable, non-duplicate restaurant candidates for each of Daejeon's five districts.

**Architecture:** A focused Naver search client owns authentication, retries, and response validation. A command-line collection script owns deterministic query generation, candidate normalization, existing-list deduplication, blog-metadata enrichment, scoring, and atomic CSV export; all network boundaries are injected so unit tests use fake responses.

**Tech Stack:** Python 3, standard library (`argparse`, `csv`, `dataclasses`, `datetime`, `difflib`, `html`, `math`, `sqlite3`, `urllib.parse`), `requests`, `python-dotenv`, `unittest`

## Global Constraints

- Read credentials only from `NAVER_CLIENT_ID` and `NAVER_CLIENT_SECRET`; never print them or place them in exceptions, logs, or CSV output.
- Do not persist raw Naver API responses, blog titles, blog snippets, blog bodies, images, or author personal data.
- Treat automatic scores only as review ordering; every exported row starts with `review_status=pending`.
- Preserve separate branches of the same brand when address and coordinates differ.
- Exclude a candidate as an existing duplicate only when normalized name and address agree, or name similarity is at least `0.92` and coordinates are at most `50` metres apart.
- Do not pad a district with duplicated or low-quality rows when fewer than 80 eligible candidates are available.
- Write `data/curation/restaurant_candidates.csv` as RFC 4180 UTF-8 without BOM and normalize text fields to one physical line.
- Keep Naver API usage to candidate discovery and internal curation; confirm current Naver terms before external deployment.
- Add no new runtime dependency.

---

## File Structure

- Create `collectors/naver_search.py`: Naver Search API client, retry rules, response shape validation, and typed API failure.
- Create `scripts/collect_naver_restaurant_candidates.py`: query seeds, candidate model, normalization, filtering, deduplication, blog enrichment, scoring, CSV export, validation summary, and CLI.
- Create `tests/test_naver_search.py`: client authentication, endpoint parameters, retry, and failure tests with a fake HTTP session.
- Create `tests/test_collect_naver_restaurant_candidates.py`: pure pipeline and CSV tests with in-memory fixtures and a temporary SQLite database.
- Generate `data/curation/restaurant_candidates.csv`: final human-review queue produced by the live run; never hand-author this file.

### Task 1: Naver Search API Client

**Files:**
- Create: `collectors/naver_search.py`
- Test: `tests/test_naver_search.py`

**Interfaces:**
- Consumes: `requests.Session`-compatible object, client ID, client secret, optional sleeper.
- Produces: `NaverSearchClient.search_local(query: str, sort: str) -> list[dict]`, `NaverSearchClient.search_blog(query: str, sort: str) -> dict`, and `NaverSearchError`.

- [ ] **Step 1: Write failing success-path and authentication tests**

```python
import unittest

from collectors.naver_search import NaverSearchClient


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class NaverSearchClientTest(unittest.TestCase):
    def test_local_search_sends_secret_headers_and_fixed_limits(self):
        session = FakeSession([FakeResponse(200, {"items": [{"title": "식당"}]})])
        client = NaverSearchClient("client-id", "client-secret", session=session)

        items = client.search_local("대덕구 칼국수", sort="comment")

        self.assertEqual(items, [{"title": "식당"}])
        url, kwargs = session.calls[0]
        self.assertTrue(url.endswith("/v1/search/local.json"))
        self.assertEqual(kwargs["params"], {
            "query": "대덕구 칼국수", "display": 5, "start": 1, "sort": "comment"
        })
        self.assertEqual(kwargs["headers"]["X-Naver-Client-Id"], "client-id")
        self.assertEqual(kwargs["headers"]["X-Naver-Client-Secret"], "client-secret")

    def test_blog_search_returns_channel_metadata(self):
        payload = {"total": 12, "items": [{"postdate": "20260701"}]}
        session = FakeSession([FakeResponse(200, payload)])
        client = NaverSearchClient("id", "secret", session=session)

        self.assertEqual(client.search_blog("식당 유성구", sort="date"), payload)
        self.assertEqual(session.calls[0][1]["params"]["display"], 100)
```

- [ ] **Step 2: Run the focused tests and verify the missing module failure**

Run: `python -m unittest tests.test_naver_search -v`

Expected: `ModuleNotFoundError: No module named 'collectors.naver_search'`.

- [ ] **Step 3: Implement the minimal successful request path**

```python
import time
from typing import Callable

import requests


BASE_URL = "https://openapi.naver.com/v1/search"


class NaverSearchError(RuntimeError):
    pass


class NaverSearchClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        session=None,
        sleeper: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
        timeout: int = 10,
    ):
        if not client_id or not client_secret:
            raise ValueError("NAVER_CLIENT_ID and NAVER_CLIENT_SECRET are required")
        self._headers = {
            "X-Naver-Client-Id": client_id,
            "X-Naver-Client-Secret": client_secret,
        }
        self._session = session or requests.Session()
        self._sleeper = sleeper
        self._max_retries = max_retries
        self._timeout = timeout

    def _get(self, resource: str, params: dict) -> dict:
        response = self._session.get(
            f"{BASE_URL}/{resource}.json",
            params=params,
            headers=self._headers,
            timeout=self._timeout,
        )
        if response.status_code != 200:
            raise NaverSearchError(f"Naver Search API failed with HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
            raise NaverSearchError("Naver Search API returned an invalid response shape")
        return payload

    def search_local(self, query: str, sort: str) -> list[dict]:
        return self._get("local", {
            "query": query, "display": 5, "start": 1, "sort": sort
        }).get("items", [])

    def search_blog(self, query: str, sort: str) -> dict:
        return self._get("blog", {
            "query": query, "display": 100, "start": 1, "sort": sort
        })
```

- [ ] **Step 4: Run the success-path tests**

Run: `python -m unittest tests.test_naver_search -v`

Expected: 2 tests pass.

- [ ] **Step 5: Add retry and terminal-error tests**

Add these methods inside the existing `NaverSearchClientTest` class:

```python
    def test_retries_429_then_succeeds(self):
        sleeps = []
        session = FakeSession([
            FakeResponse(429, {}),
            FakeResponse(200, {"items": []}),
        ])
        client = NaverSearchClient("id", "secret", session=session, sleeper=sleeps.append)

        self.assertEqual(client.search_local("중구 국밥", "random"), [])
        self.assertEqual(sleeps, [1])
        self.assertEqual(len(session.calls), 2)

    def test_does_not_retry_non_429_client_error(self):
        session = FakeSession([FakeResponse(401, {})])
        client = NaverSearchClient("id", "secret", session=session, sleeper=lambda _: None)

        with self.assertRaisesRegex(NaverSearchError, "HTTP 401"):
            client.search_blog("식당", "sim")
        self.assertEqual(len(session.calls), 1)

    def test_stops_after_three_retries_for_server_errors(self):
        session = FakeSession([FakeResponse(500, {}) for _ in range(4)])
        client = NaverSearchClient("id", "secret", session=session, sleeper=lambda _: None)

        with self.assertRaisesRegex(NaverSearchError, "HTTP 500"):
            client.search_local("서구 한식", "comment")
        self.assertEqual(len(session.calls), 4)
```

Also extend the import at the top of `tests/test_naver_search.py` to `from collectors.naver_search import NaverSearchClient, NaverSearchError`.

- [ ] **Step 6: Implement bounded retry without leaking credentials**

Replace `_get()` with a loop that catches `requests.RequestException`, retries only network failures, HTTP 429, and HTTP 5xx, sleeps `1`, `2`, then `4` seconds, and raises `NaverSearchError` containing only the HTTP status or exception class name:

```python
    def _get(self, resource: str, params: dict) -> dict:
        last_reason = "unknown error"
        for attempt in range(self._max_retries + 1):
            try:
                response = self._session.get(
                    f"{BASE_URL}/{resource}.json",
                    params=params,
                    headers=self._headers,
                    timeout=self._timeout,
                )
                status = response.status_code
                if status == 200:
                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(payload.get("items", []), list):
                        raise NaverSearchError("Naver Search API returned an invalid response shape")
                    return payload
                last_reason = f"HTTP {status}"
                retryable = status == 429 or status >= 500
                if not retryable:
                    raise NaverSearchError(
                        f"Naver Search API failed for query={params['query']!r} with {last_reason}"
                    )
            except requests.RequestException as exc:
                last_reason = type(exc).__name__

            if attempt == self._max_retries:
                break
            self._sleeper(2 ** attempt)

        raise NaverSearchError(
            f"Naver Search API failed for query={params['query']!r} after retries: {last_reason}"
        )
```

- [ ] **Step 7: Run client tests and commit**

Run: `python -m unittest tests.test_naver_search -v`

Expected: 5 tests pass.

```powershell
git add collectors/naver_search.py tests/test_naver_search.py
git commit -m "feat: add resilient Naver search client"
```

### Task 2: Candidate Model, Normalization, Filtering, and Existing Deduplication

**Files:**
- Create: `scripts/collect_naver_restaurant_candidates.py`
- Test: `tests/test_collect_naver_restaurant_candidates.py`

**Interfaces:**
- Consumes: Naver local item dictionaries, `data/curation/restaurant_recommendations.csv`, `db/travel.db`.
- Produces: `Candidate`, `ExistingRestaurant`, `candidate_from_item()`, `load_existing_restaurants()`, `duplicate_status()`.

- [ ] **Step 1: Write failing normalization, coordinate, and filter tests**

```python
import unittest

from scripts.collect_naver_restaurant_candidates import (
    candidate_from_item,
    normalize_address,
    normalize_name,
)


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

        candidate, reason = candidate_from_item(item, "유성구", "유성구 칼국수", "comment")

        self.assertEqual(reason, "")
        self.assertEqual(candidate.name, "대전식당 (유성점)")
        self.assertAlmostEqual(candidate.longitude, 127.1234567)
        self.assertAlmostEqual(candidate.latitude, 36.3123456)
        self.assertEqual(candidate.comment_sort_hit_count, 1)
        self.assertEqual(normalize_name(candidate.name), "대전식당유성점")
        self.assertEqual(normalize_address(candidate.road_address), "유성구대학로1")

    def test_rejects_wrong_district_and_cafe_only_categories(self):
        wrong = {"title": "식당", "category": "한식", "address": "대전광역시 서구 둔산동"}
        cafe = {"title": "카페", "category": "카페,디저트", "address": "대전광역시 유성구 봉명동"}
        delivery = {
            "title": "배달식당", "category": "음식점>치킨", "address": "대전광역시 유성구 봉명동",
            "description": "배달전문 치킨점",
        }

        self.assertEqual(candidate_from_item(wrong, "유성구", "q", "random")[1], "target_district_mismatch")
        self.assertEqual(candidate_from_item(cafe, "유성구", "q", "random")[1], "non_meal_category")
        self.assertEqual(candidate_from_item(delivery, "유성구", "q", "random")[1], "delivery_only")
```

- [ ] **Step 2: Run the focused tests and verify missing symbols**

Run: `python -m unittest tests.test_collect_naver_restaurant_candidates.CandidateNormalizationTest -v`

Expected: import failure for `Candidate` pipeline symbols.

- [ ] **Step 3: Implement candidate types and normalization**

```python
import html
import re
from dataclasses import dataclass, field


DISTRICTS = ("대덕구", "유성구", "동구", "서구", "중구")
NON_MEAL_TERMS = ("카페", "디저트", "베이커리", "숙박", "마트", "편의점")


@dataclass
class ExistingRestaurant:
    name: str
    address: str
    district: str
    latitude: float | None = None
    longitude: float | None = None


@dataclass
class Candidate:
    district: str
    name: str
    category: str
    address: str
    road_address: str
    latitude: float | None
    longitude: float | None
    naver_link: str
    matched_queries: set[str] = field(default_factory=set)
    local_hit_count: int = 0
    comment_sort_hit_count: int = 0
    blog_result_count: int = 0
    recent_blog_count: int = 0
    distinct_blogger_count: int = 0
    latest_post_date: str = ""
    recommendation_score: int = 0
    recommendation_reason: str = ""
    possible_duplicate: str = ""
    reject_reason: str = ""


def single_line(value) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def clean_title(value) -> str:
    return single_line(re.sub(r"<[^>]+>", "", str(value or "")))


def normalize_name(value) -> str:
    value = re.sub(r"\b(?:주식회사|유한회사|㈜)\b", "", clean_title(value))
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value).lower()


def normalize_address(value) -> str:
    value = single_line(value).replace("대전광역시", "")
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value).lower()


def scaled_coordinate(value) -> float | None:
    try:
        return int(value) / 10_000_000
    except (TypeError, ValueError):
        return None


def candidate_from_item(item: dict, district: str, query: str, sort: str):
    name = clean_title(item.get("title"))
    address = single_line(item.get("address"))
    road_address = single_line(item.get("roadAddress"))
    category = single_line(item.get("category"))
    description = single_line(item.get("description"))
    joined_address = road_address or address
    if not name or not joined_address:
        return None, "missing_name_or_address"
    if "대전" not in joined_address or district not in joined_address:
        return None, "target_district_mismatch"
    if any(term in category for term in NON_MEAL_TERMS):
        return None, "non_meal_category"
    if "배달전문" in f"{category} {description}" or "포장전문" in f"{category} {description}":
        return None, "delivery_only"
    candidate = Candidate(
        district=district,
        name=name,
        category=category,
        address=address,
        road_address=road_address,
        latitude=scaled_coordinate(item.get("mapy")),
        longitude=scaled_coordinate(item.get("mapx")),
        naver_link=single_line(item.get("link")),
    )
    candidate.matched_queries.add(query)
    candidate.local_hit_count = 1
    candidate.comment_sort_hit_count = int(sort == "comment")
    return candidate, ""
```

- [ ] **Step 4: Run normalization tests**

Run: `python -m unittest tests.test_collect_naver_restaurant_candidates.CandidateNormalizationTest -v`

Expected: 2 tests pass.

- [ ] **Step 5: Write failing existing-list duplicate tests**

```python
import csv
import sqlite3
import tempfile
from pathlib import Path

from scripts.collect_naver_restaurant_candidates import (
    Candidate,
    ExistingRestaurant,
    duplicate_status,
    load_existing_restaurants,
)


class ExistingDuplicateTest(unittest.TestCase):
    def test_loads_csv_rows_and_db_coordinates_by_place_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            csv_path = root / "existing.csv"
            db_path = root / "travel.db"
            with csv_path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["place_id", "name", "address", "district"])
                writer.writeheader()
                writer.writerow({"place_id": "p1", "name": "기존식당", "address": "대전광역시 동구 중앙로 1", "district": "동구"})
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE place (place_id TEXT, lat REAL, lng REAL)")
            conn.execute("INSERT INTO place VALUES ('p1', 36.33, 127.43)")
            conn.commit()
            conn.close()

            rows = load_existing_restaurants(csv_path, db_path)

        self.assertEqual(rows[0].latitude, 36.33)
        self.assertEqual(rows[0].longitude, 127.43)

    def test_confirms_same_place_but_keeps_different_branch(self):
        existing = [ExistingRestaurant("지역체인 유성점", "대전 유성구 대학로 1", "유성구", 36.36, 127.35)]
        same = Candidate("유성구", "지역체인 유성점", "한식", "", "대전 유성구 대학로 1", 36.3601, 127.3501, "")
        other_branch = Candidate("유성구", "지역체인 노은점", "한식", "", "대전 유성구 노은로 9", 36.38, 127.32, "")

        self.assertEqual(duplicate_status(same, existing), "confirmed")
        self.assertEqual(duplicate_status(other_branch, existing), "clear")

    def test_marks_unresolved_same_name_as_possible(self):
        existing = [ExistingRestaurant("한밭식당", "대전 동구 중앙로 1", "동구")]
        candidate = Candidate("동구", "한밭식당", "한식", "", "대전 동구 새길 2", None, None, "")

        self.assertEqual(duplicate_status(candidate, existing), "possible")
```

- [ ] **Step 6: Implement CSV/DB loading and the two-tier duplicate rule**

```python
import csv
import math
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path


def load_existing_restaurants(csv_path: Path, db_path: Path) -> list[ExistingRestaurant]:
    with Path(csv_path).open(encoding="utf-8", newline="") as stream:
        source_rows = list(csv.DictReader(stream))
    coordinates = {}
    if Path(db_path).exists():
        conn = sqlite3.connect(db_path)
        try:
            ids = [row["place_id"] for row in source_rows if row.get("place_id")]
            for offset in range(0, len(ids), 900):
                chunk = ids[offset:offset + 900]
                marks = ",".join("?" for _ in chunk)
                coordinates.update({
                    place_id: (lat, lng)
                    for place_id, lat, lng in conn.execute(
                        f"SELECT place_id, lat, lng FROM place WHERE place_id IN ({marks})", chunk
                    )
                })
        finally:
            conn.close()
    return [
        ExistingRestaurant(
            name=row.get("name", ""),
            address=row.get("address", ""),
            district=row.get("district", ""),
            latitude=coordinates.get(row.get("place_id"), (None, None))[0],
            longitude=coordinates.get(row.get("place_id"), (None, None))[1],
        )
        for row in source_rows
    ]


def distance_metres(a_lat, a_lng, b_lat, b_lng) -> float:
    radius = 6_371_000
    a1, a2 = math.radians(a_lat), math.radians(b_lat)
    d_lat = math.radians(b_lat - a_lat)
    d_lng = math.radians(b_lng - a_lng)
    value = math.sin(d_lat / 2) ** 2 + math.cos(a1) * math.cos(a2) * math.sin(d_lng / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(value))


def duplicate_status(candidate: Candidate, existing_rows: list[ExistingRestaurant]) -> str:
    candidate_name = normalize_name(candidate.name)
    candidate_address = normalize_address(candidate.road_address or candidate.address)
    possible = False
    for existing in existing_rows:
        name_ratio = SequenceMatcher(None, candidate_name, normalize_name(existing.name)).ratio()
        existing_address = normalize_address(existing.address)
        if candidate_name == normalize_name(existing.name) and candidate_address == existing_address:
            return "confirmed"
        has_coordinates = None not in (
            candidate.latitude, candidate.longitude, existing.latitude, existing.longitude
        )
        if has_coordinates and name_ratio >= 0.92:
            if distance_metres(
                candidate.latitude, candidate.longitude, existing.latitude, existing.longitude
            ) <= 50:
                return "confirmed"
        if name_ratio >= 0.92 and not has_coordinates and candidate.district == existing.district:
            possible = True
    return "possible" if possible else "clear"
```

- [ ] **Step 7: Run pipeline tests and commit**

Run: `python -m unittest tests.test_collect_naver_restaurant_candidates -v`

Expected: 5 tests pass.

```powershell
git add scripts/collect_naver_restaurant_candidates.py tests/test_collect_naver_restaurant_candidates.py
git commit -m "feat: normalize and dedupe restaurant candidates"
```

### Task 3: Deterministic Query Generation and Local Candidate Aggregation

**Files:**
- Modify: `scripts/collect_naver_restaurant_candidates.py`
- Modify: `tests/test_collect_naver_restaurant_candidates.py`

**Interfaces:**
- Consumes: `NaverSearchClient.search_local()`, `Candidate`, existing rows, target pool size.
- Produces: `iter_local_queries(district: str)`, `merge_candidate()`, `collect_local_candidates()`.

- [ ] **Step 1: Add failing query and merge tests**

```python
from scripts.collect_naver_restaurant_candidates import (
    Candidate,
    collect_local_candidates,
    iter_local_queries,
    merge_candidate,
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


class LocalCollectionTest(unittest.TestCase):
    def test_query_order_is_stable_and_contains_location_food_pairs(self):
        queries = list(iter_local_queries("대덕구"))
        self.assertEqual(queries[0], "대덕구 맛집")
        self.assertIn("신탄진 칼국수", queries)
        self.assertEqual(len(queries), len(set(queries)))

    def test_merge_accumulates_queries_and_comment_hits(self):
        first = Candidate("대덕구", "식당", "한식", "대전 대덕구 중리동 1", "", 36.3, 127.4, "")
        first.matched_queries = {"대덕구 맛집"}
        first.local_hit_count = 1
        second = Candidate("대덕구", "식당", "한식", "대전 대덕구 중리동 1", "", 36.3, 127.4, "")
        second.matched_queries = {"중리동 한식"}
        second.local_hit_count = 1
        second.comment_sort_hit_count = 1

        merged = merge_candidate([first], second)

        self.assertTrue(merged)
        self.assertEqual(first.local_hit_count, 2)
        self.assertEqual(first.comment_sort_hit_count, 1)
        self.assertEqual(first.matched_queries, {"대덕구 맛집", "중리동 한식"})

    def test_collection_calls_both_sorts_and_drops_confirmed_existing(self):
        item = {
            "title": "기존식당", "category": "한식", "address": "대전광역시 동구 중앙로 1",
            "mapx": "1274300000", "mapy": "363300000", "roadAddress": "",
        }
        client = FakeSearchClient(local_by_call=[[item], [item]])
        existing = [ExistingRestaurant("기존식당", "대전광역시 동구 중앙로 1", "동구", 36.33, 127.43)]

        rows = collect_local_candidates(client, "동구", existing, target_pool=1)

        self.assertEqual(rows, [])
        self.assertEqual(client.local_calls[:2], [("동구 맛집", "comment"), ("동구 맛집", "random")])
```

- [ ] **Step 2: Run the new test class and verify missing functions**

Run: `python -m unittest tests.test_collect_naver_restaurant_candidates.LocalCollectionTest -v`

Expected: import failure for `collect_local_candidates` or related symbols.

- [ ] **Step 3: Implement seeds, stable queries, merge, and collection**

```python
from collectors.naver_search import NaverSearchClient


LOCATION_SEEDS = {
    "대덕구": ("대덕구", "신탄진", "송촌동", "비래동", "오정동", "중리동"),
    "유성구": ("유성구", "봉명동", "궁동", "어은동", "관평동", "전민동", "노은동", "지족동", "원내동"),
    "동구": ("동구", "대전역", "소제동", "가양동", "용운동", "판암동", "산내"),
    "서구": ("서구", "둔산동", "갈마동", "월평동", "도마동", "관저동", "만년동", "탄방동"),
    "중구": ("중구", "대흥동", "은행동", "선화동", "오류동", "유천동", "산성동", "보문산"),
}
FOOD_SEEDS = (
    "맛집", "한식", "향토음식", "노포", "칼국수", "국밥", "냉면", "두부두루치기",
    "삼계탕", "고기", "해산물", "중식", "일식", "분식",
)


def iter_local_queries(district: str):
    seen = set()
    for location in LOCATION_SEEDS[district]:
        for food in FOOD_SEEDS:
            query = f"{location} {food}"
            if query not in seen:
                seen.add(query)
                yield query


def merge_candidate(rows: list[Candidate], incoming: Candidate) -> bool:
    for current in rows:
        same_address = normalize_address(current.road_address or current.address) == normalize_address(
            incoming.road_address or incoming.address
        )
        similar_name = SequenceMatcher(
            None, normalize_name(current.name), normalize_name(incoming.name)
        ).ratio() >= 0.92
        close = (
            None not in (current.latitude, current.longitude, incoming.latitude, incoming.longitude)
            and distance_metres(
                current.latitude, current.longitude, incoming.latitude, incoming.longitude
            ) <= 50
        )
        if (same_address and similar_name) or (close and similar_name):
            current.matched_queries.update(incoming.matched_queries)
            current.local_hit_count += incoming.local_hit_count
            current.comment_sort_hit_count += incoming.comment_sort_hit_count
            return True
    rows.append(incoming)
    return False


def collect_local_candidates(
    client: NaverSearchClient,
    district: str,
    existing_rows: list[ExistingRestaurant],
    *,
    target_pool: int = 120,
) -> list[Candidate]:
    candidates = []
    for query in iter_local_queries(district):
        for sort in ("comment", "random"):
            for item in client.search_local(query, sort):
                candidate, reject_reason = candidate_from_item(item, district, query, sort)
                if reject_reason:
                    continue
                status = duplicate_status(candidate, existing_rows)
                if status == "confirmed":
                    continue
                candidate.possible_duplicate = "Y" if status == "possible" else ""
                merge_candidate(candidates, candidate)
        if len(candidates) >= target_pool:
            break
    return candidates
```

- [ ] **Step 4: Run collection tests and all pipeline tests**

Run: `python -m unittest tests.test_collect_naver_restaurant_candidates -v`

Expected: 8 tests pass.

- [ ] **Step 5: Commit deterministic local collection**

```powershell
git add scripts/collect_naver_restaurant_candidates.py tests/test_collect_naver_restaurant_candidates.py
git commit -m "feat: collect district restaurant candidate pools"
```

### Task 4: Blog Metadata Enrichment and Recommendation Scoring

**Files:**
- Modify: `scripts/collect_naver_restaurant_candidates.py`
- Modify: `tests/test_collect_naver_restaurant_candidates.py`

**Interfaces:**
- Consumes: `NaverSearchClient.search_blog()`, a `Candidate`, and an execution date.
- Produces: `enrich_blog_metrics()`, `score_candidate()`, and `build_blog_search_url()`.

- [ ] **Step 1: Add failing blog metric and scoring tests**

```python
from datetime import date

from scripts.collect_naver_restaurant_candidates import (
    build_blog_search_url,
    enrich_blog_metrics,
    score_candidate,
)


class BlogEnrichmentTest(unittest.TestCase):
    def test_aggregates_total_recent_posts_distinct_bloggers_and_latest_date(self):
        sim = {
            "total": 321,
            "items": [
                {"bloggerlink": "https://blog.naver.com/a", "postdate": "20260501"},
                {"bloggerlink": "https://blog.naver.com/a", "postdate": "20250401"},
                {"bloggerlink": "https://blog.naver.com/b", "postdate": "20260301"},
            ],
        }
        by_date = {
            "total": 321,
            "items": [
                {"bloggerlink": "https://blog.naver.com/c", "postdate": "20260701"},
                {"bloggerlink": "https://blog.naver.com/d", "postdate": "20250801"},
                {"bloggerlink": "https://blog.naver.com/e", "postdate": "20240701"},
            ],
        }
        client = FakeSearchClient(blog_by_call=[sim, by_date])
        candidate = Candidate("중구", "대전칼국수", "한식>칼국수", "대전 중구 대흥동 1", "", None, None, "")

        enrich_blog_metrics(client, candidate, today=date(2026, 7, 20))

        self.assertEqual(candidate.blog_result_count, 321)
        self.assertEqual(candidate.recent_blog_count, 2)
        self.assertEqual(candidate.distinct_blogger_count, 2)
        self.assertEqual(candidate.latest_post_date, "20260701")
        self.assertIn("where=blog", build_blog_search_url(candidate))

    def test_scores_local_value_and_penalizes_generic_delivery_food(self):
        local = Candidate("중구", "원도심 노포 칼국수 본점", "한식>칼국수", "대전 중구 대흥동", "", None, None, "")
        local.local_hit_count = 4
        local.comment_sort_hit_count = 3
        local.blog_result_count = 300
        local.recent_blog_count = 8
        local.distinct_blogger_count = 9
        generic = Candidate("중구", "전국치킨", "음식점>치킨", "대전 중구 대흥동", "", None, None, "")

        score_candidate(local)
        score_candidate(generic)

        self.assertGreater(local.recommendation_score, generic.recommendation_score)
        self.assertIn("지역성", local.recommendation_reason)
        self.assertIn("배달형 음식 감점", generic.recommendation_reason)
```

- [ ] **Step 2: Run enrichment tests and verify missing functions**

Run: `python -m unittest tests.test_collect_naver_restaurant_candidates.BlogEnrichmentTest -v`

Expected: import failure for blog enrichment symbols.

- [ ] **Step 3: Implement blog aggregation and deterministic scoring**

```python
import math
from datetime import date
from urllib.parse import quote_plus


LOCAL_VALUE_TERMS = (
    "노포", "향토", "본점", "전통시장", "대전", "칼국수", "두부두루치기",
)
LOW_VALUE_TERMS = (
    "치킨", "피자", "햄버거", "패스트푸드", "도시락", "롯데리아", "맥도날드",
    "버거킹", "kfc", "서브웨이", "맘스터치", "bbq", "bhc", "교촌치킨",
    "도미노피자", "피자헛", "미스터피자",
)


def blog_query(candidate: Candidate) -> str:
    match = re.search(r"([가-힣]+동)", candidate.road_address or candidate.address)
    location = match.group(1) if match else candidate.district
    return f"{candidate.name} {location}"


def build_blog_search_url(candidate: Candidate) -> str:
    return "https://search.naver.com/search.naver?where=blog&query=" + quote_plus(blog_query(candidate))


def parse_post_date(value: str) -> date | None:
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except (TypeError, ValueError):
        return None


def enrich_blog_metrics(client: NaverSearchClient, candidate: Candidate, *, today: date) -> None:
    query = blog_query(candidate)
    similarity = client.search_blog(query, "sim")
    recent = client.search_blog(query, "date")
    candidate.blog_result_count = int(similarity.get("total") or 0)
    bloggers = {
        single_line(item.get("bloggerlink") or item.get("bloggername"))
        for item in similarity.get("items", [])
        if single_line(item.get("bloggerlink") or item.get("bloggername"))
    }
    candidate.distinct_blogger_count = len(bloggers)
    cutoff = date(today.year - 1, today.month, today.day)
    parsed_dates = [
        parsed for parsed in (parse_post_date(item.get("postdate", "")) for item in recent.get("items", []))
        if parsed is not None
    ]
    candidate.recent_blog_count = sum(parsed >= cutoff for parsed in parsed_dates)
    candidate.latest_post_date = max(parsed_dates).strftime("%Y%m%d") if parsed_dates else ""


def score_candidate(candidate: Candidate) -> int:
    combined = f"{candidate.name} {candidate.category}"
    local_signals = [term for term in LOCAL_VALUE_TERMS if term in combined]
    low_value_signals = [term for term in LOW_VALUE_TERMS if term in combined]
    score = 0
    score += min(20, candidate.local_hit_count * 4)
    score += min(15, candidate.comment_sort_hit_count * 3)
    score += 15 if ">" in candidate.category else 10
    score += min(10, int(math.log10(candidate.blog_result_count + 1) * 3))
    score += min(15, candidate.recent_blog_count * 2)
    score += min(10, candidate.distinct_blogger_count)
    score += min(15, len(local_signals) * 5)
    score -= min(30, len(low_value_signals) * 15)
    candidate.recommendation_score = max(0, min(100, score))
    reasons = []
    if local_signals:
        reasons.append("지역성: " + ", ".join(local_signals))
    if candidate.recent_blog_count:
        reasons.append(f"최근 12개월 블로그 {candidate.recent_blog_count}건")
    if low_value_signals:
        reasons.append("배달형 음식 감점: " + ", ".join(low_value_signals))
    candidate.recommendation_reason = "; ".join(reasons) or "지역검색 후보"
    return candidate.recommendation_score
```

- [ ] **Step 4: Run blog and complete pipeline tests**

Run: `python -m unittest tests.test_collect_naver_restaurant_candidates -v`

Expected: 10 tests pass.

- [ ] **Step 5: Commit metadata enrichment**

```powershell
git add scripts/collect_naver_restaurant_candidates.py tests/test_collect_naver_restaurant_candidates.py
git commit -m "feat: score candidates with Naver blog metadata"
```

### Task 5: Atomic CSV Export, Quality Summary, and CLI

**Files:**
- Modify: `scripts/collect_naver_restaurant_candidates.py`
- Modify: `tests/test_collect_naver_restaurant_candidates.py`

**Interfaces:**
- Consumes: enriched candidates, CLI arguments, `.env` credentials.
- Produces: `select_candidates()`, `write_candidates()`, `validate_output_rows()`, `run_collection()`, and `main()`.

- [ ] **Step 1: Add failing selection and CSV tests**

```python
from scripts.collect_naver_restaurant_candidates import (
    FIELDNAMES,
    select_candidates,
    validate_output_rows,
    write_candidates,
)


class CsvExportTest(unittest.TestCase):
    def test_selects_positive_scores_in_stable_order_and_limit(self):
        a = Candidate("서구", "가식당", "한식", "대전 서구", "", None, None, "")
        b = Candidate("서구", "나식당", "한식", "대전 서구", "", None, None, "")
        c = Candidate("서구", "다식당", "한식", "대전 서구", "", None, None, "")
        a.recommendation_score, b.recommendation_score, c.recommendation_score = 20, 30, 0

        self.assertEqual([row.name for row in select_candidates([a, b, c], 2)], ["나식당", "가식당"])

    def test_atomic_writer_creates_parseable_single_line_csv(self):
        candidate = Candidate("서구", "식당\n본점", "한식", "대전 서구\n둔산동", "", 36.3, 127.3, "")
        candidate.recommendation_score = 10
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "candidates.csv"
            write_candidates([candidate], output)
            with output.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))

        self.assertEqual(list(rows[0]), FIELDNAMES)
        self.assertEqual(rows[0]["review_status"], "pending")
        self.assertEqual(rows[0]["name"], "식당 본점")
        self.assertNotIn("\n", rows[0]["address"])

    def test_validation_rejects_wrong_district_and_confirmed_duplicate(self):
        candidate = Candidate("유성구", "기존", "한식", "대전 서구 둔산동", "", None, None, "")
        existing = [ExistingRestaurant("기존", "대전 서구 둔산동", "서구")]

        errors = validate_output_rows([candidate], existing)

        self.assertIn("district/address mismatch: 기존", errors)
```

- [ ] **Step 2: Run export tests and verify missing functions**

Run: `python -m unittest tests.test_collect_naver_restaurant_candidates.CsvExportTest -v`

Expected: import failure for export symbols.

- [ ] **Step 3: Implement stable selection, row conversion, atomic write, and validation**

```python
FIELDNAMES = [
    "review_status", "district", "name", "category", "address", "road_address",
    "latitude", "longitude", "naver_link", "blog_search_url", "matched_queries",
    "local_hit_count", "comment_sort_hit_count", "blog_result_count", "recent_blog_count",
    "distinct_blogger_count", "latest_post_date", "recommendation_score",
    "recommendation_reason", "possible_duplicate", "reject_reason",
]


def select_candidates(candidates: list[Candidate], limit: int) -> list[Candidate]:
    return sorted(
        (candidate for candidate in candidates if candidate.recommendation_score > 0),
        key=lambda candidate: (-candidate.recommendation_score, candidate.name),
    )[:limit]


def candidate_to_row(candidate: Candidate) -> dict:
    return {
        "review_status": "pending",
        "district": candidate.district,
        "name": single_line(candidate.name),
        "category": single_line(candidate.category),
        "address": single_line(candidate.address),
        "road_address": single_line(candidate.road_address),
        "latitude": "" if candidate.latitude is None else f"{candidate.latitude:.7f}",
        "longitude": "" if candidate.longitude is None else f"{candidate.longitude:.7f}",
        "naver_link": candidate.naver_link,
        "blog_search_url": build_blog_search_url(candidate),
        "matched_queries": " | ".join(sorted(candidate.matched_queries)),
        "local_hit_count": candidate.local_hit_count,
        "comment_sort_hit_count": candidate.comment_sort_hit_count,
        "blog_result_count": candidate.blog_result_count,
        "recent_blog_count": candidate.recent_blog_count,
        "distinct_blogger_count": candidate.distinct_blogger_count,
        "latest_post_date": candidate.latest_post_date,
        "recommendation_score": candidate.recommendation_score,
        "recommendation_reason": single_line(candidate.recommendation_reason),
        "possible_duplicate": candidate.possible_duplicate,
        "reject_reason": single_line(candidate.reject_reason),
    }


def write_candidates(candidates: list[Candidate], output_path: Path) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(candidate_to_row(candidate) for candidate in candidates)
    temp_path.replace(output_path)


def validate_output_rows(candidates, existing_rows) -> list[str]:
    errors = []
    for candidate in candidates:
        if candidate.district not in (candidate.road_address or candidate.address):
            errors.append(f"district/address mismatch: {candidate.name}")
        if duplicate_status(candidate, existing_rows) == "confirmed":
            errors.append(f"confirmed duplicate: {candidate.name}")
    return errors
```

- [ ] **Step 4: Add failing dry-run and orchestrator tests**

```python
from scripts.collect_naver_restaurant_candidates import run_collection


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
            "title": "새칼국수", "category": "한식>칼국수", "address": "대전광역시 대덕구 중리동 1",
            "mapx": "1274000000", "mapy": "363000000", "roadAddress": "",
        }
        blog = {"total": 10, "items": [{"bloggerlink": "blog-a", "postdate": "20260701"}]}
        client = FakeSearchClient(local_by_call=[[item], [item]], blog_by_call=[blog, blog])
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "out.csv"
            summary = run_collection(
                client=client, districts=["대덕구"], existing_rows=[], output_path=output,
                max_per_district=1, skip_blog=False, dry_run=False, today=date(2026, 7, 20),
            )
            rows = list(csv.DictReader(output.open(encoding="utf-8", newline="")))

        self.assertEqual(summary, {"대덕구": 1})
        self.assertEqual(rows[0]["name"], "새칼국수")
```

- [ ] **Step 5: Implement orchestration and CLI**

```python
import argparse
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXISTING_CSV = REPO_ROOT / "data" / "curation" / "restaurant_recommendations.csv"
DEFAULT_DB = REPO_ROOT / "db" / "travel.db"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "curation" / "restaurant_candidates.csv"


def run_collection(
    *, client, districts, existing_rows, output_path, max_per_district,
    skip_blog, dry_run, today,
) -> dict[str, int]:
    if dry_run:
        return {district: 0 for district in districts}
    selected_all = []
    summary = {}
    for district in districts:
        target_pool = max(max_per_district, math.ceil(max_per_district * 1.2))
        candidates = collect_local_candidates(
            client, district, existing_rows, target_pool=target_pool
        )
        for candidate in candidates:
            if not skip_blog:
                enrich_blog_metrics(client, candidate, today=today)
            score_candidate(candidate)
        selected = select_candidates(candidates, max_per_district)
        selected_all.extend(selected)
        summary[district] = len(selected)
    errors = validate_output_rows(selected_all, existing_rows)
    if errors:
        raise RuntimeError("Output validation failed: " + "; ".join(errors))
    if output_path is not None:
        write_candidates(selected_all, Path(output_path))
    return summary


def parse_args():
    parser = argparse.ArgumentParser(description="네이버 API로 대전 음식점 검수 후보를 수집합니다.")
    parser.add_argument("--district", choices=DISTRICTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-per-district", type=int, default=100)
    parser.add_argument("--skip-blog", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    load_dotenv(REPO_ROOT / ".env")
    existing = load_existing_restaurants(DEFAULT_EXISTING_CSV, DEFAULT_DB)
    districts = [args.district] if args.district else list(DISTRICTS)
    if args.dry_run:
        client = None
    else:
        client = NaverSearchClient(
            os.getenv("NAVER_CLIENT_ID", ""), os.getenv("NAVER_CLIENT_SECRET", "")
        )
    summary = run_collection(
        client=client,
        districts=districts,
        existing_rows=existing,
        output_path=args.output,
        max_per_district=args.max_per_district,
        skip_blog=args.skip_blog,
        dry_run=args.dry_run,
        today=date.today(),
    )
    for district in districts:
        count = summary[district]
        shortage = max(0, 80 - count) if args.max_per_district >= 80 else 0
        print(f"{district}: candidates={count} shortage={shortage}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run focused and complete test suites**

Run: `python -m unittest tests.test_collect_naver_restaurant_candidates -v`

Expected: 15 tests pass.

Run: `python -m unittest discover -s tests -v`

Expected: all repository tests pass.

- [ ] **Step 7: Exercise dry-run and commit the complete collector**

Run: `python scripts/collect_naver_restaurant_candidates.py --dry-run --district 대덕구 --max-per-district 5`

Expected: `대덕구: candidates=0 shortage=0`; no API call and no output file change.

```powershell
git add scripts/collect_naver_restaurant_candidates.py tests/test_collect_naver_restaurant_candidates.py
git commit -m "feat: export reviewable restaurant candidate CSV"
```

### Task 6: Live Smoke Test, Full Collection, and Artifact Verification

**Files:**
- Generate: `data/curation/restaurant_candidates.csv`
- Modify only if live findings expose a tested defect: `scripts/collect_naver_restaurant_candidates.py`
- Modify only with the matching regression test: `tests/test_collect_naver_restaurant_candidates.py`

**Interfaces:**
- Consumes: configured `.env`, live Naver Search API, existing recommendation CSV and DB.
- Produces: final candidate CSV and district count summary.

- [ ] **Step 1: Run all tests immediately before network use**

Run: `python -m unittest discover -s tests -v`

Expected: all tests pass with no network access.

- [ ] **Step 2: Run a one-district, five-row live smoke collection**

Run:

```powershell
python scripts/collect_naver_restaurant_candidates.py --district 대덕구 --max-per-district 5 --output data/curation/restaurant_candidates_smoke.csv
```

Expected: exit code 0, `대덕구: candidates=1..5 shortage=0`, and a parseable smoke CSV. If the sandbox blocks network access, rerun this exact command with network approval rather than changing the implementation.

- [ ] **Step 3: Inspect the smoke artifact without printing secrets**

Run:

```powershell
python -c "import csv; p='data/curation/restaurant_candidates_smoke.csv'; rows=list(csv.DictReader(open(p,encoding='utf-8',newline=''))); print('rows=',len(rows),'fields=',list(rows[0]) if rows else [])"
```

Expected: `rows=1..5`, fields exactly equal `FIELDNAMES`, and no credential values.

- [ ] **Step 4: Remove only the explicitly temporary smoke artifact**

Run in PowerShell after resolving and confirming the target is inside `data/curation`:

```powershell
$smoke = (Resolve-Path 'data/curation/restaurant_candidates_smoke.csv').Path
$curation = (Resolve-Path 'data/curation').Path
if (-not $smoke.StartsWith($curation + [IO.Path]::DirectorySeparatorChar)) { throw 'Unsafe smoke path' }
Remove-Item -LiteralPath $smoke
```

Expected: only `restaurant_candidates_smoke.csv` is removed.

- [ ] **Step 5: Run the full five-district collection**

Run:

```powershell
python scripts/collect_naver_restaurant_candidates.py --max-per-district 100
```

Expected: exit code 0 and a printed count for all five districts. A count below 80 is reported as a shortage and is not treated as permission to duplicate rows.

- [ ] **Step 6: Verify shape, districts, duplicates, line safety, and parseability**

Run:

```powershell
python -c "import csv,collections; p='data/curation/restaurant_candidates.csv'; rows=list(csv.DictReader(open(p,encoding='utf-8',newline=''))); counts=collections.Counter(r['district'] for r in rows); keys=[(r['district'],''.join(c.lower() for c in r['name'] if c.isalnum()),''.join(c.lower() for c in (r['road_address'] or r['address']) if c.isalnum())) for r in rows]; print('rows=',len(rows)); print('districts=',dict(counts)); print('duplicate_keys=',len(keys)-len(set(keys))); print('bad_status=',sum(r['review_status']!='pending' for r in rows)); print('embedded_newlines=',sum(any('\n' in v or '\r' in v for v in r.values()) for r in rows))"
```

Expected: five district keys, no district above 100, `duplicate_keys=0`, `bad_status=0`, and `embedded_newlines=0`.

- [ ] **Step 7: Search generated artifacts for accidental secret names or raw blog content fields**

Run:

```powershell
rg -n "NAVER_CLIENT_SECRET|X-Naver-Client-Secret|description|bloggername|bloggerlink" data/curation/restaurant_candidates.csv
```

Expected: no matches.

- [ ] **Step 8: Re-run the full test suite after live validation**

Run: `python -m unittest discover -s tests -v`

Expected: all tests pass.

- [ ] **Step 9: Commit the generated review queue separately**

```powershell
git add data/curation/restaurant_candidates.csv
git commit -m "data: add Naver restaurant review candidates"
```

The separate data commit makes later candidate refreshes reviewable without mixing generated rows into collector code changes.
