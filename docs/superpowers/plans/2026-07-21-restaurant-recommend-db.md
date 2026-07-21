# Restaurant Recommendation DB Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a single `recommend` flag to `place`, merge the 438 approved Naver restaurants with the 263 existing recommendations, enrich matching rows, insert only unmatched restaurants, and leave 696 unique recommended restaurants in SQLite.

**Architecture:** Materialize the reviewed workbook status as a compact approved CSV, then run a reusable Python importer against SQLite. The importer owns schema migration, deterministic matching, enrichment, insertion, transaction handling, and verification; existing collectors remain compatible because their explicit upserts do not touch `recommend`.

**Tech Stack:** Python 3 standard library (`sqlite3`, `csv`, `json`, `difflib`, `hashlib`, `unittest`), SQLite, `@oai/artifact-tool` for reading the completed `.xlsx` review workbook.

## Global Constraints

- Add only one new `place` column: nullable `recommend TEXT`; use `추천` for recommended rows and `NULL` otherwise.
- Keep one canonical coordinate pair in `place.lat` and `place.lng`; preserve an existing complete pair and use Naver coordinates only when either existing coordinate is missing.
- Store only `source`, detailed category, recommendation score/reason, road address, Naver link, and Naver coordinates under `extra_json.recommendation`.
- Do not store blog counts, blogger counts, recent dates, or search-result counts in the DB or `extra_json`.
- Treat exact normalized names within 100m as the same place; treat contained or similarity-0.72 names within 50m as the same place.
- Keep same-name remote branches separate and never delete existing `place` rows.
- Preserve user-owned changes in `docs/superpowers/specs/2026-07-20-naver-restaurant-candidates-design.md`, `collectors/tour_course.py`, and review workbook files.
- Use tests before production code for every behavior change.

---

## File Structure

- Create `data/curation/restaurant_candidates_approved.csv`: approved 438-row import source without blog metrics.
- Create `scripts/import_restaurant_recommendations.py`: schema migration, matching, enrichment, idempotent import, backup, CLI.
- Create `tests/test_import_restaurant_recommendations.py`: unit and integration tests against temporary SQLite databases.
- Modify `db/schema.sql`: add `recommend` and its lookup index for newly initialized databases.
- Modify `scripts/export_restaurant_recommendations.py`: export rows selected by the DB recommendation flag.
- Modify `tests/test_export_restaurant_recommendations.py`: verify flag-based export.
- Modify `scripts/dedupe_place.py`: preserve `recommend` when future duplicate consolidation runs.
- Modify `db/travel.db`: apply the verified migration and recommendation data transaction.

### Task 1: Materialize the Approved Candidate Source

**Files:**
- Create: `data/curation/restaurant_candidates_approved.csv`
- Test: `tests/test_import_restaurant_recommendations.py`

**Interfaces:**
- Consumes: status column from `outputs/restaurant-review/restaurant_candidates_review_완료_승인반영.xlsx`; row data from `data/curation/restaurant_candidates.csv`.
- Produces: a UTF-8 CSV with fields `district,name,category,address,road_address,latitude,longitude,naver_link,recommendation_score,recommendation_reason`.

- [ ] **Step 1: Write the failing data-contract test**

```python
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
```

- [ ] **Step 2: Run the test and verify RED**

Run: `python -m unittest tests.test_import_restaurant_recommendations.ApprovedCandidateDataTest -v`

Expected: FAIL because `restaurant_candidates_approved.csv` does not exist.

- [ ] **Step 3: Extract approved row numbers with artifact-tool and write the CSV**

Use `@oai/artifact-tool` to read `후보 검토!A2:A461`. For every `approved` row, copy the corresponding 1-based data row from `restaurant_candidates.csv`, assert that workbook and CSV `district`, `name`, and `address` agree, and write only the ten fields declared above. Do not copy link display values from formula cells in the workbook; copy raw URLs from the source CSV.

```js
const statuses = workbook.worksheets.getItem("후보 검토")
  .getRange("A2:A461").values.map((row) => String(row[0]).trim());
const approved = sourceRows.filter((row, index) => statuses[index] === "approved");
if (approved.length !== 438) throw new Error(`approved=${approved.length}`);
```

- [ ] **Step 4: Run the data-contract test and verify GREEN**

Run: `python -m unittest tests.test_import_restaurant_recommendations.ApprovedCandidateDataTest -v`

Expected: PASS with 438 rows and the exact field set.

- [ ] **Step 5: Commit the reviewed input snapshot**

```bash
git add data/curation/restaurant_candidates_approved.csv tests/test_import_restaurant_recommendations.py
git commit -m "data: add approved restaurant import source"
```

### Task 2: Add an Idempotent Recommendation Schema Migration

**Files:**
- Modify: `db/schema.sql:1-24`
- Create: `scripts/import_restaurant_recommendations.py`
- Modify: `tests/test_import_restaurant_recommendations.py`

**Interfaces:**
- Produces: `ensure_recommend_schema(conn: sqlite3.Connection) -> None`.
- Later tasks rely on `place.recommend` and `idx_place_category_recommend`.

- [ ] **Step 1: Write the failing migration test**

```python
class RecommendSchemaTest(unittest.TestCase):
    def test_schema_migration_is_idempotent(self):
        conn = make_place_db()
        ensure_recommend_schema(conn)
        ensure_recommend_schema(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(place)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(place)")}
        self.assertIn("recommend", columns)
        self.assertIn("idx_place_category_recommend", indexes)
```

Define `make_place_db()` in the test module as an in-memory SQLite connection with `row_factory=sqlite3.Row` and the pre-migration `place` columns copied from `db/schema.sql`. This makes the first migration call add the column and the second prove idempotency.

- [ ] **Step 2: Run the migration test and verify RED**

Run: `python -m unittest tests.test_import_restaurant_recommendations.RecommendSchemaTest -v`

Expected: FAIL with an import error or missing `ensure_recommend_schema`.

- [ ] **Step 3: Implement the minimal migration**

```python
def ensure_recommend_schema(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(place)")}
    if "recommend" not in columns:
        conn.execute("ALTER TABLE place ADD COLUMN recommend TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_place_category_recommend "
        "ON place(category, recommend)"
    )
```

Update the `place` definition and indexes in `db/schema.sql`:

```sql
  homepage      TEXT,
  recommend     TEXT
);

CREATE INDEX IF NOT EXISTS idx_place_category_recommend
ON place(category, recommend);
```

- [ ] **Step 4: Run the migration test and full schema initialization**

Run: `python -m unittest tests.test_import_restaurant_recommendations.RecommendSchemaTest -v`

Run: `python scripts/init_db.py`

Expected: test PASS; initialization completes without a duplicate-column error on the existing DB because `schema.sql` uses `CREATE TABLE IF NOT EXISTS`.

- [ ] **Step 5: Commit the migration**

```bash
git add db/schema.sql scripts/import_restaurant_recommendations.py tests/test_import_restaurant_recommendations.py
git commit -m "feat: add restaurant recommendation schema"
```

### Task 3: Match Approved Candidates to Existing Places

**Files:**
- Modify: `scripts/import_restaurant_recommendations.py`
- Modify: `tests/test_import_restaurant_recommendations.py`

**Interfaces:**
- Produces: `Candidate`, `normalize_name`, `normalize_address`, `haversine_metres`, `name_is_similar`, and `select_existing_place(candidate, places, preferred_ids)`.
- `select_existing_place` returns one `sqlite3.Row` or `None`.

Define the production candidate type before the matcher:

```python
@dataclass(frozen=True)
class Candidate:
    district: str
    name: str
    category: str
    address: str
    road_address: str
    latitude: float
    longitude: float
    naver_link: str
    recommendation_score: int
    recommendation_reason: str

    @property
    def best_address(self):
        return self.road_address or self.address
```

- [ ] **Step 1: Write failing tests for exact, fuzzy, and remote matching**

```python
class PlaceMatchingTest(unittest.TestCase):
    def test_exact_name_within_100m_reuses_existing_place(self):
        candidate = candidate_at("영화반점", 36.40, 127.42)
        rows = [place_row("p1", "영화반점", 36.4005, 127.4202, "sbiz")]
        self.assertEqual(select_existing_place(candidate, rows, set())["place_id"], "p1")

    def test_similar_name_within_50m_reuses_existing_place(self):
        candidate = candidate_at("맛집부추해물칼국수", 36.44955, 127.43159)
        rows = [place_row("p1", "부추해물칼국수", 36.44950, 127.43160, "tourapi")]
        self.assertEqual(select_existing_place(candidate, rows, set())["place_id"], "p1")

    def test_same_name_far_away_is_a_distinct_branch(self):
        candidate = candidate_at("상하이양꼬치", 36.30, 127.40)
        rows = [place_row("p1", "상하이양꼬치", 36.39, 127.39, "sbiz")]
        self.assertIsNone(select_existing_place(candidate, rows, set()))

    def test_preferred_recommendation_row_wins_multiple_matches(self):
        candidate = candidate_at("중앙식당", 36.35, 127.38)
        rows = [
            place_row("tour", "중앙식당", 36.3501, 127.3801, "tourapi"),
            place_row("preferred", "중앙식당", 36.3502, 127.3801, "sbiz"),
        ]
        self.assertEqual(
            select_existing_place(candidate, rows, {"preferred"})["place_id"],
            "preferred",
        )
```

Define these test helpers directly above the test class:

```python
def candidate_at(name, lat, lng, address="대전 중구 중앙로 1"):
    return Candidate("중구", name, "한식", address, address, lat, lng, "", 80, "검토 승인")

def place_row(place_id, name, lat, lng, source_api, address="대전 중구 중앙로 1"):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE p(place_id, name, lat, lng, source_api, address)")
    conn.execute("INSERT INTO p VALUES (?, ?, ?, ?, ?, ?)", (place_id, name, lat, lng, source_api, address))
    return conn.execute("SELECT * FROM p").fetchone()
```

- [ ] **Step 2: Run matching tests and verify RED**

Run: `python -m unittest tests.test_import_restaurant_recommendations.PlaceMatchingTest -v`

Expected: FAIL because the matching interfaces are absent.

- [ ] **Step 3: Implement normalization and deterministic selection**

```python
SOURCE_PRIORITY = {"tourapi": 0, "daejeon_food": 1, "sbiz": 2, "naver_search": 3}

def normalize_name(value):
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return "".join(char for char in text if char.isalnum())

def name_is_similar(left, right):
    left, right = normalize_name(left), normalize_name(right)
    contained = min(len(left), len(right)) >= 4 and (left in right or right in left)
    return contained or SequenceMatcher(None, left, right).ratio() >= 0.72

def select_existing_place(candidate, places, preferred_ids):
    matches = []
    for place in places:
        distance = haversine_metres(candidate.latitude, candidate.longitude, place["lat"], place["lng"])
        exact = normalize_name(candidate.name) == normalize_name(place["name"])
        fuzzy = name_is_similar(candidate.name, place["name"])
        same_address = normalize_address(candidate.best_address) == normalize_address(place["address"])
        if (exact and distance is not None and distance <= 100) or (fuzzy and distance is not None and distance <= 50) or (distance is None and exact and same_address):
            matches.append((place, distance if distance is not None else float("inf")))
    if not matches:
        return None
    return min(
        matches,
        key=lambda item: (
            item[0]["place_id"] not in preferred_ids,
            SOURCE_PRIORITY.get(item[0]["source_api"], 9),
            item[1],
            item[0]["place_id"],
        ),
    )[0]
```

- [ ] **Step 4: Run matching tests and verify GREEN**

Run: `python -m unittest tests.test_import_restaurant_recommendations.PlaceMatchingTest -v`

Expected: all matching tests PASS.

- [ ] **Step 5: Commit the matcher**

```bash
git add scripts/import_restaurant_recommendations.py tests/test_import_restaurant_recommendations.py
git commit -m "feat: match approved restaurants to places"
```

### Task 4: Enrich Existing Rows and Insert Unmatched Rows Idempotently

**Files:**
- Modify: `scripts/import_restaurant_recommendations.py`
- Modify: `tests/test_import_restaurant_recommendations.py`

**Interfaces:**
- Produces: `merge_recommendation_extra(raw_extra, candidate) -> str`, `stable_place_id(candidate) -> str`, and `apply_recommendations(conn, existing_csv, approved_csv) -> ImportStats`.
- `ImportStats` fields: `existing_marked`, `matched_enriched`, `inserted`, `source_overlap`, `recommended_total`.

- [ ] **Step 1: Write failing enrichment and idempotency tests**

```python
class RecommendationImportTest(unittest.TestCase):
    def test_existing_coordinates_and_extra_keys_are_preserved(self):
        conn = make_place_db(with_recommend=True)
        insert_place(conn, "p1", "부추해물칼국수", 36.44, 127.43, extra_json='{"legacy": 1}')
        stats = apply_rows(conn, approved=[candidate_at("맛집부추해물칼국수", 36.4401, 127.4301)])
        row = conn.execute("SELECT * FROM place WHERE place_id='p1'").fetchone()
        extra = json.loads(row["extra_json"])
        self.assertEqual((row["lat"], row["lng"]), (36.44, 127.43))
        self.assertEqual(row["recommend"], "추천")
        self.assertEqual(extra["legacy"], 1)
        self.assertEqual(extra["recommendation"]["source"], "naver_review")
        self.assertNotIn("recent_blog_count", extra["recommendation"])
        self.assertEqual(stats.matched_enriched, 1)

    def test_missing_coordinate_pair_is_filled_from_naver(self):
        conn = make_place_db(with_recommend=True)
        insert_place(conn, "p1", "중앙식당", None, 127.38)
        apply_rows(conn, approved=[candidate_at("중앙식당", 36.35, 127.38, address="대전 중구 중앙로 1")])
        row = conn.execute("SELECT lat, lng FROM place WHERE place_id='p1'").fetchone()
        self.assertEqual(tuple(row), (36.35, 127.38))

    def test_unmatched_candidate_is_inserted_once(self):
        conn = make_place_db(with_recommend=True)
        candidate = candidate_at("새로운식당", 36.35, 127.38)
        first = apply_rows(conn, approved=[candidate])
        second = apply_rows(conn, approved=[candidate])
        self.assertEqual(first.inserted, 1)
        self.assertEqual(second.inserted, 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM place").fetchone()[0], 1)
```

Define exact fixture helpers in the test module so the public file-based importer is exercised:

```python
def insert_place(conn, place_id, name, lat, lng, extra_json="{}"):
    conn.execute(
        "INSERT INTO place (place_id,name,category,address,lat,lng,source_api,extra_json) "
        "VALUES (?,?,'restaurant','대전 중구 중앙로 1',?,?,'sbiz',?)",
        (place_id, name, lat, lng, extra_json),
    )

def apply_rows(conn, approved, existing_ids=()):
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        approved_path = directory / "approved.csv"
        existing_path = directory / "existing.csv"
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
```

- [ ] **Step 2: Run import tests and verify RED**

Run: `python -m unittest tests.test_import_restaurant_recommendations.RecommendationImportTest -v`

Expected: FAIL because merge, stable ID, and apply functions are absent.

- [ ] **Step 3: Implement minimal enrichment and insertion**

```python
def merge_recommendation_extra(raw_extra, candidate):
    extra = json.loads(raw_extra or "{}")
    if not isinstance(extra, dict):
        raise ValueError("extra_json must contain an object")
    extra["recommendation"] = {
        "source": "naver_review",
        "detailed_category": candidate.category,
        "score": candidate.recommendation_score,
        "reason": candidate.recommendation_reason,
        "road_address": candidate.road_address,
        "naver_link": candidate.naver_link,
        "naver_latitude": candidate.latitude,
        "naver_longitude": candidate.longitude,
    }
    return json.dumps(extra, ensure_ascii=False, separators=(",", ":"))

def stable_place_id(candidate):
    identity = "|".join((normalize_name(candidate.name), normalize_address(candidate.best_address), f"{candidate.latitude:.7f}", f"{candidate.longitude:.7f}"))
    return "naver_restaurant_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
```

Within `apply_recommendations`, first mark all existing CSV IDs, then match every approved candidate, update only allowed fields, and insert unmatched rows with explicit columns. Execute all statements inside one transaction and return `ImportStats`.

- [ ] **Step 4: Run import tests and verify GREEN**

Run: `python -m unittest tests.test_import_restaurant_recommendations.RecommendationImportTest -v`

Expected: all import tests PASS, including second-run idempotency.

- [ ] **Step 5: Commit the importer**

```bash
git add scripts/import_restaurant_recommendations.py tests/test_import_restaurant_recommendations.py
git commit -m "feat: import approved restaurant recommendations"
```

### Task 5: Preserve and Export the Recommendation Flag

**Files:**
- Modify: `scripts/export_restaurant_recommendations.py:43-86`
- Modify: `tests/test_export_restaurant_recommendations.py`
- Modify: `scripts/dedupe_place.py:42-52`
- Modify: `tests/test_import_restaurant_recommendations.py`

**Interfaces:**
- `collect_recommendations(conn)` selects `category='restaurant' AND recommend='추천'`.
- `dedupe_place.MERGE_COLUMNS` and `ALL_COLUMNS` include `recommend` so a future merge cannot lose it.

- [ ] **Step 1: Write failing export and dedupe-preservation tests**

```python
def test_exports_only_rows_with_recommend_flag(self):
    conn = make_export_db()
    conn.executemany(
        "INSERT INTO place VALUES (?, ?, 'restaurant', ?, ?, ?, ?, ?)",
        [
            ("yes", "추천식당", "대전 중구", "tourapi", "소개", "{}", "추천"),
            ("no", "일반식당", "대전 서구", "tourapi", "소개", "{}", None),
        ],
    )
    self.assertEqual([row["place_id"] for row in collect_recommendations(conn)], ["yes"])

def test_dedupe_column_lists_preserve_recommend(self):
    self.assertIn("recommend", dedupe_place.ALL_COLUMNS)
    self.assertIn("recommend", dedupe_place.MERGE_COLUMNS)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_export_restaurant_recommendations tests.test_import_restaurant_recommendations -v`

Expected: FAIL because export ignores `recommend` and dedupe omits it.

- [ ] **Step 3: Implement flag-based export and dedupe preservation**

Change the export query to:

```sql
SELECT place_id, name, category, address, source_api, overview, extra_json
FROM place
WHERE category = 'restaurant' AND recommend = '추천'
ORDER BY place_id
```

Remove the old `overview`/`daejeon_food` qualification filter; derive optional recommendation fields from `extra_json.recommendation` when present. Add `recommend` to both `MERGE_COLUMNS` and `ALL_COLUMNS` in `dedupe_place.py`.

- [ ] **Step 4: Run focused and full tests**

Run: `python -m unittest tests.test_export_restaurant_recommendations tests.test_import_restaurant_recommendations -v`

Run: `python -m unittest discover -s tests -v`

Expected: all tests PASS without warnings or tracebacks.

- [ ] **Step 5: Commit downstream compatibility**

```bash
git add scripts/export_restaurant_recommendations.py tests/test_export_restaurant_recommendations.py scripts/dedupe_place.py tests/test_import_restaurant_recommendations.py
git commit -m "fix: preserve restaurant recommendation flags"
```

### Task 6: Apply the Import to `travel.db` and Verify the Result

**Files:**
- Modify: `db/travel.db`
- Regenerate: `data/curation/restaurant_recommendations.csv`
- Create locally, do not commit: `db/travel.pre-recommend-20260721.db`

**Interfaces:**
- Consumes: `scripts/import_restaurant_recommendations.py` CLI with `--db`, `--existing-csv`, `--approved-csv`, and `--apply`.
- Produces: migrated SQLite DB and refreshed 696-row recommendation CSV.

- [ ] **Step 1: Run a dry-run audit**

Run:

```bash
python scripts/import_restaurant_recommendations.py \
  --db db/travel.db \
  --existing-csv data/curation/restaurant_recommendations.csv \
  --approved-csv data/curation/restaurant_candidates_approved.csv
```

Expected summary: 263 existing recommendation IDs, 438 approved candidates, 5 overlaps between recommendation sources, approximately 331 matched DB rows, approximately 107 inserts, and 696 final unique recommendations. The command must not change the DB without `--apply`.

- [ ] **Step 2: Create and verify a recoverable DB backup**

Use `Copy-Item -LiteralPath db/travel.db -Destination db/travel.pre-recommend-20260721.db`, then compare file size and SHA-256 of the source and backup before applying changes.

- [ ] **Step 3: Apply the migration and import**

Run the Task 6 Step 1 command with `--apply`.

Expected: one committed transaction, no rejected workbook rows imported, and a machine-readable summary of marked, enriched, inserted, and total recommended rows.

- [ ] **Step 4: Regenerate the recommendation CSV**

Run: `python scripts/export_restaurant_recommendations.py`

Expected: `exported=696` and 697 physical CSV lines including the header.

- [ ] **Step 5: Verify database invariants**

Run a read-only SQLite verification that asserts:

```python
assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
assert conn.execute("SELECT COUNT(*) FROM place WHERE recommend='추천'").fetchone()[0] == 696
assert conn.execute("SELECT COUNT(*) FROM place WHERE recommend='추천' AND category<>'restaurant'").fetchone()[0] == 0
assert conn.execute("SELECT COUNT(*) FROM place WHERE recommend='추천' AND (lat IS NULL OR lng IS NULL)").fetchone()[0] == 0
assert conn.execute("SELECT COUNT(*) FROM place WHERE recommend='추천' AND json_valid(COALESCE(extra_json, '{}'))=0").fetchone()[0] == 0
```

Run the importer a second time with `--apply` and assert `inserted=0`, total place count unchanged, and recommended total still 696.

- [ ] **Step 6: Run the complete regression suite**

Run: `python -m unittest discover -s tests -v`

Expected: all tests PASS.

- [ ] **Step 7: Commit the verified database and regenerated CSV**

```bash
git add db/travel.db data/curation/restaurant_recommendations.csv
git commit -m "data: apply approved restaurant recommendations"
```

Do not add the local backup, Excel temporary lock file, completed review workbooks, the user-modified design document, or `collectors/tour_course.py`.
