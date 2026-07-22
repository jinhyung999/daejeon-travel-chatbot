# 소품샵 수집 및 추천 장소 보강 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 대전 소품샵을 네이버 지역검색으로 100여 건 신규 수집해 `place`에 반영하고, `recommend='추천'`인 모든 장소(현재 restaurant 696건 + 신규 소품샵)를 네이버 블로그 검색 + LLM으로 보강한다.

**Architecture:** `collectors/naver_giftshop.py`(신규 수집, `place` 직접 upsert) → `collectors/blog_concept_enrich.py`(`recommend='추천'` 행을 블로그 스니펫 + OpenAI로 보강, `detail_enrich.py`와 동일한 조회→UPDATE 패턴) 두 개의 독립 실행 가능한 수집기. 스키마 변경은 `scripts/init_db.py`에 기존 `ensure_recommend_schema`와 같은 방식으로 마이그레이션 함수를 추가해 지원한다.

**Tech Stack:** Python, sqlite3, `requests`(기존 `collectors/naver_search.py` 재사용), `openai`(신규 의존성), `python-dotenv`

## Global Constraints

- `place_id`는 상호명+도로명주소+좌표(소수점 7자리) 기반 `sha256` 결정적 해시로 생성한다 (`docs/superpowers/specs/2026-07-22-giftshop-collection-and-recommend-enrichment-design.md` "컴포넌트 1"). 재실행해도 동일 ID여야 한다.
- 목표 수량(소품샵 100건)을 못 채워도 임의로 후보를 복제하거나 필터를 낮춰 채우지 않는다. 부족분은 실행 종료 시 콘솔 요약에 명시한다.
- 원본 블로그 스니펫(제목/본문/요약)은 CSV·DB 어디에도 저장하지 않는다. 저장하는 것은 LLM이 가공한 결과 필드와 대표 링크 3개뿐이다.
- `open_time`/`close_day`/`has_parking`은 기존 값이 있으면 덮어쓰지 않는다 (`COALESCE`로 NULL일 때만 채움).
- `collectors/common.py`의 `upsert_place`는 `recommend` 컬럼을 갱신하지 않는다 (스키마에 없음). 소품샵을 `recommend='추천'`으로 만들려면 upsert 이후 별도 `UPDATE place SET recommend='추천' WHERE place_id IN (...)`를 실행해야 한다.
- 새 `collectors/*.py` 파일은 `collectors/tour_attraction.py`처럼 직접 실행(`cd collectors && python x.py`)도, `collectors.x` 패키지 임포트(테스트에서 사용)도 모두 되어야 한다. 따라서 아래 dual-import 패턴을 그대로 사용한다.

```python
try:
    from common import get_conn, upsert_place
    from naver_search import NaverSearchClient
except ModuleNotFoundError:
    from collectors.common import get_conn, upsert_place
    from collectors.naver_search import NaverSearchClient
```

- 테스트는 `python -m unittest tests.test_xxx -v`로 실행한다 (이 저장소에 `pytest`는 설치돼 있지 않다).
- 네트워크·LLM 호출은 전부 테스트에서 페이크/모의 객체로 대체한다. 실제 API 스모크 테스트는 계획 마지막에 수동 단계로 남긴다.

---

### Task 1: 스키마 마이그레이션 (`place`에 6개 컬럼 추가)

**Files:**
- Modify: `db/schema.sql`
- Modify: `collectors/blog_concept_enrich.py` (신규 생성, `ensure_giftshop_enrichment_schema` 정의)
- Modify: `scripts/init_db.py`
- Test: `tests/test_blog_concept_enrich.py` (신규 생성)

**Interfaces:**
- Produces: `ensure_giftshop_enrichment_schema(conn: sqlite3.Connection) -> None` — `concept_tag TEXT`, `photo_spot INTEGER`, `has_workshop INTEGER`, `blog_url_1 TEXT`, `blog_url_2 TEXT`, `blog_url_3 TEXT` 컬럼을 없으면 추가. 몇 번을 실행해도 안전(idempotent).

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_blog_concept_enrich.py` 파일을 새로 만든다.

```python
import sqlite3
import unittest

from collectors import blog_concept_enrich as enrich_mod


def make_place_db():
    conn = sqlite3.connect(":memory:")
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
          homepage TEXT,
          recommend TEXT
        )
        """
    )
    return conn


class EnsureGiftshopEnrichmentSchemaTest(unittest.TestCase):
    def test_adds_missing_columns(self):
        conn = make_place_db()
        self.addCleanup(conn.close)

        enrich_mod.ensure_giftshop_enrichment_schema(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(place)")}
        for expected in (
            "concept_tag", "photo_spot", "has_workshop",
            "blog_url_1", "blog_url_2", "blog_url_3",
        ):
            self.assertIn(expected, columns)

    def test_is_idempotent(self):
        conn = make_place_db()
        self.addCleanup(conn.close)

        enrich_mod.ensure_giftshop_enrichment_schema(conn)
        enrich_mod.ensure_giftshop_enrichment_schema(conn)  # 두 번째도 에러 없어야 함

        columns = [row[1] for row in conn.execute("PRAGMA table_info(place)")]
        self.assertEqual(columns.count("concept_tag"), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m unittest tests.test_blog_concept_enrich -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'collectors.blog_concept_enrich'` (아직 파일이 없음)

- [ ] **Step 3: `collectors/blog_concept_enrich.py`에 최소 구현 작성**

```python
import os

from dotenv import load_dotenv

try:
    from common import get_conn
    from naver_search import NaverSearchClient
except ModuleNotFoundError:
    from collectors.common import get_conn
    from collectors.naver_search import NaverSearchClient

load_dotenv()


def ensure_giftshop_enrichment_schema(conn) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(place)")}
    new_columns = {
        "concept_tag": "TEXT",
        "photo_spot": "INTEGER",
        "has_workshop": "INTEGER",
        "blog_url_1": "TEXT",
        "blog_url_2": "TEXT",
        "blog_url_3": "TEXT",
    }
    for name, sql_type in new_columns.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE place ADD COLUMN {name} {sql_type}")


if __name__ == "__main__":
    conn = get_conn()
    ensure_giftshop_enrichment_schema(conn)
    conn.commit()
    conn.close()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m unittest tests.test_blog_concept_enrich -v`
Expected: `OK` (2 tests)

- [ ] **Step 5: `db/schema.sql`에 신규 DB용 컬럼 반영**

`db/schema.sql`에서 `place` 테이블 정의를 찾아 `recommend TEXT` 다음 줄에 추가한다.

```sql
  recommend     TEXT,
  concept_tag   TEXT,
  photo_spot    INTEGER,
  has_workshop  INTEGER,
  blog_url_1    TEXT,
  blog_url_2    TEXT,
  blog_url_3    TEXT
```

(기존에 `recommend TEXT`로 컬럼 목록이 끝나던 자리이므로, 그 줄 끝의 콤마 유무를 확인하고 마지막 컬럼에는 콤마를 붙이지 않는다.)

- [ ] **Step 6: `scripts/init_db.py`에 마이그레이션 연결**

`scripts/init_db.py`를 읽어 기존 `ensure_recommend_schema` 임포트/호출 바로 아래에 새 함수를 추가한다.

```python
import sqlite3
from pathlib import Path

try:
    from scripts.import_restaurant_recommendations import ensure_recommend_schema
except ModuleNotFoundError:
    from import_restaurant_recommendations import ensure_recommend_schema

try:
    from collectors.blog_concept_enrich import ensure_giftshop_enrichment_schema
except ModuleNotFoundError:
    from blog_concept_enrich import ensure_giftshop_enrichment_schema

DB_PATH = Path(__file__).parent.parent / "db" / "travel.db"
SCHEMA_PATH = Path(__file__).parent.parent / "db" / "schema.sql"

def init_db(db_path=DB_PATH, schema_path=SCHEMA_PATH):
    conn = sqlite3.connect(db_path)

    place_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'place'"
    ).fetchone()
    if place_exists:
        ensure_recommend_schema(conn)
        ensure_giftshop_enrichment_schema(conn)

    with open(schema_path, "r", encoding="utf-8") as f:
        conn.executescript(f.read())

    conn.commit()
    conn.close()

    print(f"DB initialized at {db_path}")

if __name__ == "__main__":
    init_db()
```

- [ ] **Step 7: 기존 DB에 실제로 마이그레이션 적용**

Run: `cd "C:\Users\SAMITECH\Desktop\프로젝트\daejeon-travel-chatbot" && python scripts/init_db.py`
Expected: `DB initialized at ...travel.db` 출력, 에러 없음.

검증: `python -c "import sqlite3; conn=sqlite3.connect('db/travel.db'); print([r[1] for r in conn.execute('PRAGMA table_info(place)')])"` 출력에 `concept_tag`, `photo_spot`, `has_workshop`, `blog_url_1`, `blog_url_2`, `blog_url_3`이 모두 있어야 한다.

- [ ] **Step 8: 커밋**

```bash
git add db/schema.sql scripts/init_db.py collectors/blog_concept_enrich.py tests/test_blog_concept_enrich.py
git commit -m "feat: add giftshop enrichment columns to place schema"
```

---

### Task 2: 소품샵 수집기 (`collectors/naver_giftshop.py`)

**중요 — 먼저 고쳐야 하는 기존 버그:** `collectors/common.py`의 `upsert_place`는 인자로 커넥션을 받지 않고 내부에서 항상 `get_conn()`(실제 `db/travel.db` 파일)을 새로 연다. 이 상태로는 테스트에서 인메모리 DB를 넘겨도 무시되고 실제 DB 파일에 쓰기 시도를 하게 된다. `upsert_place`가 외부 커넥션을 선택적으로 받도록 먼저 고친다 (기존 호출부는 인자를 안 넘기므로 동작 그대로 유지됨).

**Files:**
- Modify: `collectors/common.py`
- Create: `collectors/naver_giftshop.py`
- Test: `tests/test_naver_giftshop.py`

**Interfaces:**
- Consumes: `collectors.common.get_conn() -> sqlite3.Connection`, `collectors.common.upsert_place(rows: list[dict], conn=None) -> dict`, `collectors.naver_search.NaverSearchClient(client_id, client_secret).search_local(query: str, sort: str) -> list[dict]` (5건 고정 반환, 필드: `title`, `category`, `address`, `roadAddress`, `mapx`, `mapy`, `telephone`, `link`)
- Produces: `stable_place_id(name, road_address, lat, lng) -> str`, `is_duplicate(name, lat, lng, existing: list[tuple]) -> bool`, `collect(target_count=100, conn=None, client=None) -> list[dict]`

- [ ] **Step 1: `upsert_place`에 커넥션 주입 테스트 작성**

`tests/test_common.py` 파일을 새로 만든다.

```python
import sqlite3
import unittest

from collectors import common


def make_place_db():
    conn = sqlite3.connect(":memory:")
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
          extra_json TEXT
        )
        """
    )
    return conn


ROW = {
    "place_id": "p1", "name": "테스트", "category": "giftshop", "address": "주소",
    "lat": 36.0, "lng": 127.0, "open_time": None, "close_day": None, "fee": None,
    "has_parking": None, "tel": None, "source_api": "naver_search", "extra_json": "{}",
}


class UpsertPlaceInjectedConnTest(unittest.TestCase):
    def test_writes_to_injected_conn_and_leaves_it_open(self):
        conn = make_place_db()
        self.addCleanup(conn.close)

        common.upsert_place([ROW], conn=conn)

        row = conn.execute("SELECT name FROM place WHERE place_id='p1'").fetchone()
        self.assertEqual(row[0], "테스트")
        # 커넥션이 닫히지 않았어야 추가 쿼리가 가능하다
        conn.execute("SELECT 1")

    def test_empty_rows_returns_zero_without_touching_conn(self):
        conn = make_place_db()
        self.addCleanup(conn.close)

        result = common.upsert_place([], conn=conn)

        self.assertEqual(result, {"total": 0, "inserted": 0, "updated": 0})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m unittest tests.test_common -v`
Expected: FAIL — `TypeError: upsert_place() got an unexpected keyword argument 'conn'`

- [ ] **Step 3: `collectors/common.py`의 `upsert_place` 수정**

`collectors/common.py`에서 `upsert_place` 함수 정의부(`def upsert_place(rows: list[dict]):`부터 `conn.close()` 직전까지)를 아래로 교체한다.

```python
def upsert_place(rows: list[dict], conn=None):

    if not rows:
        print("place upsert: 0건 / 신규 0건 / 갱신 0건")
        return {"total": 0, "inserted": 0, "updated": 0}

    # DB 연결 (외부에서 주입되지 않았으면 새로 연다)
    owns_conn = conn is None
    if conn is None:
        conn = get_conn()

    # Cursor 생성
    cur = conn.cursor()

    unique_place_ids = sorted({
        row["place_id"]
        for row in rows
        if row.get("place_id")
    })
    existing_place_ids = set()

    # SQLite has a variable limit, so check existing IDs in chunks.
    for i in range(0, len(unique_place_ids), 900):
        chunk = unique_place_ids[i:i + 900]
        placeholders = ",".join("?" for _ in chunk)
        existing_place_ids.update(
            place_id
            for (place_id,) in cur.execute(
                f"SELECT place_id FROM place WHERE place_id IN ({placeholders})",
                chunk,
            ).fetchall()
        )

    inserted_count = len(set(unique_place_ids) - existing_place_ids)
    updated_count = len(existing_place_ids)

    # 여러 행을 한 번에 실행
    cur.executemany("""
        INSERT INTO place (place_id, name, category, address, lat, lng,
                            open_time, close_day, fee, has_parking, tel,
                            source_api, extra_json)
        VALUES (:place_id, :name, :category, :address, :lat, :lng,
                :open_time, :close_day, :fee, :has_parking, :tel,
                :source_api, :extra_json)

        -- place_id가 이미 존재하면 UPDATE 수행
        ON CONFLICT(place_id) DO UPDATE SET
            name=excluded.name, category=excluded.category,
            address=excluded.address, lat=excluded.lat, lng=excluded.lng,
            open_time=excluded.open_time, close_day=excluded.close_day,
            fee=excluded.fee, has_parking=excluded.has_parking,
            tel=excluded.tel, source_api=excluded.source_api,
            extra_json=excluded.extra_json
    """, rows)

    # DB 반영
    conn.commit()

    # 외부에서 주입된 커넥션은 이 함수가 닫지 않는다
    if owns_conn:
        conn.close()
```

(이후 `print(...)`와 `return {...}` 블록은 그대로 둔다.)

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m unittest tests.test_common -v`
Expected: `OK` (2 tests)

- [ ] **Step 5: 기존 호출부가 안 깨졌는지 회귀 확인**

Run: `python -m unittest tests.test_collect_naver_restaurant_candidates tests.test_export_restaurant_recommendations tests.test_import_restaurant_recommendations -v`
Expected: `OK` (기존 테스트 전부 그대로 통과 — `conn` 인자를 안 쓰는 기존 호출부는 동작이 바뀌지 않아야 한다)

- [ ] **Step 6: 커밋**

```bash
git add collectors/common.py tests/test_common.py
git commit -m "fix: let upsert_place accept an injected connection for testability"
```

- [ ] **Step 7: 소품샵 수집기 실패하는 테스트 작성**

`tests/test_naver_giftshop.py`를 새로 만든다.

```python
import sqlite3
import unittest
from io import StringIO
from contextlib import redirect_stdout

from collectors import naver_giftshop


def make_place_db():
    conn = sqlite3.connect(":memory:")
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
          homepage TEXT,
          recommend TEXT
        )
        """
    )
    return conn


class FakeNaverClient:
    """query별로 미리 정해둔 지역검색 결과를 돌려주는 가짜 클라이언트."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls = []

    def search_local(self, query, sort):
        self.calls.append((query, sort))
        return self.responses.get(query, [])


GIFTSHOP_ITEM = {
    "title": "<b>소품샵</b> 소소로와",
    "category": "가구,인테리어>인테리어소품",
    "address": "대전광역시 중구 대흥동 178-2 2층",
    "roadAddress": "대전광역시 중구 대종로 451 2층",
    "mapx": "1273550890",
    "mapy": "363371579",
    "telephone": "",
    "link": "https://blog.naver.com/example",
}

NON_GIFTSHOP_ITEM = {
    "title": "소품샵 흉내내는 철물점",
    "category": "생활,편의>철물점",
    "address": "대전광역시 중구 아무데 1",
    "roadAddress": "대전광역시 중구 아무로 1",
    "mapx": "1273550890",
    "mapy": "363371579",
    "telephone": "",
    "link": "https://blog.naver.com/other",
}


class StablePlaceIdTest(unittest.TestCase):
    def test_deterministic_for_same_input(self):
        id1 = naver_giftshop.stable_place_id("소소로와", "대전광역시 중구 대종로 451 2층", 36.3371579, 127.3550890)
        id2 = naver_giftshop.stable_place_id("소소로와", "대전광역시 중구 대종로 451 2층", 36.3371579, 127.3550890)
        self.assertEqual(id1, id2)
        self.assertTrue(id1.startswith("naver_giftshop_"))

    def test_different_for_different_address(self):
        id1 = naver_giftshop.stable_place_id("소소로와", "주소A", 36.0, 127.0)
        id2 = naver_giftshop.stable_place_id("소소로와", "주소B", 36.0, 127.0)
        self.assertNotEqual(id1, id2)


class IsDuplicateTest(unittest.TestCase):
    def test_same_name_within_30m_is_duplicate(self):
        existing = [("소소로와", 36.3371579, 127.3550890)]
        self.assertTrue(
            naver_giftshop.is_duplicate("소소로와", 36.3371600, 127.3550900, existing)
        )

    def test_same_name_far_away_is_not_duplicate(self):
        existing = [("소소로와", 36.3371579, 127.3550890)]
        self.assertFalse(
            naver_giftshop.is_duplicate("소소로와", 36.40, 127.40, existing)
        )

    def test_different_name_is_not_duplicate(self):
        existing = [("소소로와", 36.3371579, 127.3550890)]
        self.assertFalse(
            naver_giftshop.is_duplicate("잠시다락", 36.3371579, 127.3550890, existing)
        )


class CollectTest(unittest.TestCase):
    def test_filters_out_non_giftshop_category(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        client = FakeNaverClient({"대흥동 소품샵": [NON_GIFTSHOP_ITEM]})

        rows = naver_giftshop.collect(target_count=100, conn=conn, client=client)

        self.assertEqual(rows, [])

    def test_collects_giftshop_item_with_recommend_flag(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        client = FakeNaverClient({"대흥동 소품샵": [GIFTSHOP_ITEM]})

        rows = naver_giftshop.collect(target_count=1, conn=conn, client=client)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["category"], "giftshop")
        stored = conn.execute(
            "SELECT recommend, category FROM place WHERE place_id=?", (rows[0]["place_id"],)
        ).fetchone()
        self.assertEqual(stored, ("추천", "giftshop"))

    def test_stops_once_target_reached(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        item2 = dict(GIFTSHOP_ITEM, title="소품샵 잠시다락", mapx="1274550890", mapy="363471579")
        client = FakeNaverClient({
            "대덕구 소품샵": [GIFTSHOP_ITEM],
            "신탄진 소품샵": [item2],
        })

        rows = naver_giftshop.collect(target_count=1, conn=conn, client=client)

        self.assertEqual(len(rows), 1)

    def test_prints_shortfall_when_target_not_reached(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        client = FakeNaverClient({})  # 모든 검색어에 결과 없음

        out = StringIO()
        with redirect_stdout(out):
            naver_giftshop.collect(target_count=100, conn=conn, client=client)

        self.assertIn("목표 100건 중 0건", out.getvalue())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 8: 테스트 실패 확인**

Run: `python -m unittest tests.test_naver_giftshop -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'collectors.naver_giftshop'`

- [ ] **Step 9: `collectors/naver_giftshop.py` 구현**

```python
import hashlib
import json
import math
import os
import re

from dotenv import load_dotenv

try:
    from common import get_conn, upsert_place
    from naver_search import NaverSearchClient
except ModuleNotFoundError:
    from collectors.common import get_conn, upsert_place
    from collectors.naver_search import NaverSearchClient

load_dotenv()

TARGET_COUNT = 100
CATEGORY = "giftshop"
DEDUPE_RADIUS_M = 30

LOCATION_SEEDS = {
    "대덕구": ["대덕구", "신탄진", "송촌동", "비래동", "오정동", "중리동"],
    "유성구": ["유성구", "봉명동", "궁동", "어은동", "관평동", "전민동", "노은동", "지족동", "원내동"],
    "동구": ["동구", "대전역", "소제동", "가양동", "용운동", "판암동", "산내"],
    "서구": ["서구", "둔산동", "갈마동", "월평동", "도마동", "관저동", "만년동", "탄방동"],
    "중구": ["중구", "대흥동", "은행동", "선화동", "오류동", "유천동", "산성동", "보문산"],
}

_TAG_RE = re.compile(r"<[^>]+>")
_BRANCH_SUFFIX_RE = re.compile(r"\(.*?\)|점$|점포|지점")
_WHITESPACE_RE = re.compile(r"\s+")


def _clean_html(text: str | None) -> str:
    return _TAG_RE.sub("", text or "").strip()


def _normalize_name(name: str) -> str:
    name = _BRANCH_SUFFIX_RE.sub("", name or "")
    name = _WHITESPACE_RE.sub("", name)
    return name.strip().lower()


def _haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def is_duplicate(name, lat, lng, existing) -> bool:
    """existing: (normalized_name, lat, lng) 튜플 리스트"""
    norm = _normalize_name(name)
    for cand_norm, cand_lat, cand_lng in existing:
        if cand_norm != norm:
            continue
        if _haversine_m(lat, lng, cand_lat, cand_lng) <= DEDUPE_RADIUS_M:
            return True
    return False


def stable_place_id(name, road_address, lat, lng) -> str:
    identity = "|".join((
        _normalize_name(name),
        (road_address or "").strip(),
        f"{lat:.7f}",
        f"{lng:.7f}",
    ))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return "naver_giftshop_" + digest


def _load_existing_index(conn):
    rows = conn.execute(
        "SELECT name, lat, lng FROM place WHERE lat IS NOT NULL AND lng IS NOT NULL"
    ).fetchall()
    return [(_normalize_name(name), lat, lng) for name, lat, lng in rows]


def _parse_item(item):
    category = item.get("category") or ""
    if "인테리어소품" not in category:
        return None

    mapx = item.get("mapx")
    mapy = item.get("mapy")
    if not mapx or not mapy:
        return None

    lat = float(mapy) / 10_000_000
    lng = float(mapx) / 10_000_000
    name = _clean_html(item.get("title"))
    road_address = _clean_html(item.get("roadAddress"))
    address = _clean_html(item.get("address"))

    return {
        "name": name,
        "address": road_address or address,
        "road_address": road_address,
        "lat": lat,
        "lng": lng,
        "tel": item.get("telephone") or None,
        "naver_category": category,
        "naver_link": item.get("link") or None,
    }


def collect(target_count=TARGET_COUNT, conn=None, client=None):
    owns_conn = conn is None
    if conn is None:
        conn = get_conn()
    if client is None:
        client = NaverSearchClient(os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET"))

    existing = _load_existing_index(conn)
    place_rows = []
    seen_ids = set()
    queries_tried = 0

    for neighborhoods in LOCATION_SEEDS.values():
        for neighborhood in neighborhoods:
            if len(place_rows) >= target_count:
                break
            queries_tried += 1
            items = client.search_local(f"{neighborhood} 소품샵", sort="random")
            for item in items:
                parsed = _parse_item(item)
                if not parsed:
                    continue
                if is_duplicate(parsed["name"], parsed["lat"], parsed["lng"], existing):
                    continue

                place_id = stable_place_id(
                    parsed["name"], parsed["road_address"], parsed["lat"], parsed["lng"]
                )
                if place_id in seen_ids:
                    continue
                seen_ids.add(place_id)

                place_rows.append({
                    "place_id": place_id,
                    "name": parsed["name"],
                    "category": CATEGORY,
                    "address": parsed["address"],
                    "lat": parsed["lat"],
                    "lng": parsed["lng"],
                    "open_time": None,
                    "close_day": None,
                    "fee": None,
                    "has_parking": None,
                    "tel": parsed["tel"],
                    "source_api": "naver_search",
                    "extra_json": json.dumps(
                        {
                            "naver_category": parsed["naver_category"],
                            "naver_link": parsed["naver_link"],
                        },
                        ensure_ascii=False,
                    ),
                })
                existing.append((_normalize_name(parsed["name"]), parsed["lat"], parsed["lng"]))
        if len(place_rows) >= target_count:
            break

    upsert_place(place_rows, conn=conn)

    if place_rows:
        placeholders = ",".join("?" for _ in place_rows)
        conn.execute(
            f"UPDATE place SET recommend='추천' WHERE place_id IN ({placeholders})",
            [row["place_id"] for row in place_rows],
        )
        conn.commit()

    if len(place_rows) < target_count:
        print(
            f"소품샵 수집 완료: 목표 {target_count}건 중 {len(place_rows)}건 반영 "
            f"(검색 조합 {queries_tried}개 소진)"
        )
    else:
        print(f"소품샵 수집 완료: {len(place_rows)}건 반영 (목표 {target_count}건 달성)")

    if owns_conn:
        conn.close()

    return place_rows


if __name__ == "__main__":
    collect()
```

- [ ] **Step 10: 테스트 통과 확인**

Run: `python -m unittest tests.test_naver_giftshop -v`
Expected: `OK` (8 tests)

- [ ] **Step 11: 커밋**

```bash
git add collectors/naver_giftshop.py tests/test_naver_giftshop.py
git commit -m "feat: add naver_giftshop collector for giftshop place discovery"
```

---

### Task 3: LLM 컨셉 추출 래퍼 (`collectors/concept_llm.py`)

**Files:**
- Create: `collectors/concept_llm.py`
- Test: `tests/test_concept_llm.py`
- Modify: `requirements.txt`

**Interfaces:**
- Produces: `extract_concept_fields(place_name: str, snippets: list[str], *, client=None, model="gpt-4o-mini") -> dict` — 반환 키는 `concept_tag`, `open_time`, `close_day`, `parking`(`"가능"|"불가"|None`), `photo_spot`(`True|False|None`), `has_workshop`(`True|False|None`). 근거 없는 값은 `None`.
- `ConceptExtractionError(RuntimeError)` — LLM 응답이 JSON이 아니거나 스키마에 안 맞을 때.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_concept_llm.py`를 새로 만든다.

```python
import json
import unittest

from collectors import concept_llm


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeCompletionResponse:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


class FakeCompletions:
    def __init__(self, content):
        self._content = content
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return FakeCompletionResponse(self._content)


class FakeChat:
    def __init__(self, content):
        self.completions = FakeCompletions(content)


class FakeOpenAIClient:
    def __init__(self, content):
        self.chat = FakeChat(content)


VALID_RESPONSE = json.dumps({
    "concept_tag": "빈티지",
    "open_time": "화~일요일 12:00-19:00",
    "close_day": "매주 월요일",
    "parking": "불가",
    "photo_spot": True,
    "has_workshop": False,
}, ensure_ascii=False)


class ExtractConceptFieldsTest(unittest.TestCase):
    def test_returns_all_none_when_no_snippets(self):
        result = concept_llm.extract_concept_fields("다구로잉", [], client=FakeOpenAIClient(VALID_RESPONSE))

        self.assertEqual(
            result,
            {
                "concept_tag": None, "open_time": None, "close_day": None,
                "parking": None, "photo_spot": None, "has_workshop": None,
            },
        )

    def test_parses_valid_llm_response(self):
        client = FakeOpenAIClient(VALID_RESPONSE)

        result = concept_llm.extract_concept_fields("다구로잉", ["스니펫1", "스니펫2"], client=client)

        self.assertEqual(result["concept_tag"], "빈티지")
        self.assertEqual(result["parking"], "불가")
        self.assertIs(result["photo_spot"], True)
        self.assertIs(result["has_workshop"], False)

    def test_sends_snippets_and_place_name_in_prompt(self):
        client = FakeOpenAIClient(VALID_RESPONSE)

        concept_llm.extract_concept_fields("다구로잉", ["영업시간 화~일 12-19시"], client=client)

        user_message = client.chat.completions.last_kwargs["messages"][1]["content"]
        self.assertIn("다구로잉", user_message)
        self.assertIn("영업시간 화~일 12-19시", user_message)

    def test_raises_on_non_json_response(self):
        client = FakeOpenAIClient("이건 JSON이 아님")

        with self.assertRaises(concept_llm.ConceptExtractionError):
            concept_llm.extract_concept_fields("다구로잉", ["스니펫"], client=client)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m unittest tests.test_concept_llm -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'collectors.concept_llm'`

- [ ] **Step 3: `collectors/concept_llm.py` 구현**

```python
import json
import os

from dotenv import load_dotenv

load_dotenv()

FIELDS = ("concept_tag", "open_time", "close_day", "parking", "photo_spot", "has_workshop")

SYSTEM_PROMPT = (
    "너는 대전 지역 장소에 대한 블로그 발췌문 여러 개를 읽고 정보를 추출하는 도우미다. "
    "아래 JSON 스키마로만 응답하고 다른 텍스트는 출력하지 마라. "
    "근거가 없는 값은 반드시 null로 남겨라. 추측으로 채우지 마라.\n"
    '{"concept_tag": string|null, "open_time": string|null, "close_day": string|null, '
    '"parking": "가능"|"불가"|null, "photo_spot": true|false|null, "has_workshop": true|false|null}'
)


class ConceptExtractionError(RuntimeError):
    pass


def _empty_result() -> dict:
    return {field: None for field in FIELDS}


def extract_concept_fields(place_name: str, snippets: list[str], *, client=None, model="gpt-4o-mini") -> dict:
    if not snippets:
        return _empty_result()

    if client is None:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    user_content = f"장소명: {place_name}\n\n블로그 발췌문:\n" + "\n---\n".join(snippets)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )

    raw = response.choices[0].message.content
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConceptExtractionError(f"LLM 응답이 JSON이 아님: {raw!r}") from exc

    if not isinstance(parsed, dict):
        raise ConceptExtractionError(f"LLM 응답이 객체가 아님: {parsed!r}")

    return {field: parsed.get(field) for field in FIELDS}
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m unittest tests.test_concept_llm -v`
Expected: `OK` (4 tests)

- [ ] **Step 5: `requirements.txt`에 `openai` 추가**

`requirements.txt` 끝에 한 줄 추가:

```
openai
```

- [ ] **Step 6: 실제 패키지 설치 확인**

Run: `pip install -r requirements.txt`
Expected: `openai` 패키지가 정상 설치됨 (이미 설치돼 있으면 `Requirement already satisfied`)

- [ ] **Step 7: 커밋**

```bash
git add collectors/concept_llm.py tests/test_concept_llm.py requirements.txt
git commit -m "feat: add OpenAI-backed concept field extraction from blog snippets"
```

---

### Task 4: 추천 장소 보강 파이프라인 완성 (`collectors/blog_concept_enrich.py`)

Task 1에서 `ensure_giftshop_enrichment_schema`만 넣어뒀던 `collectors/blog_concept_enrich.py`에 실제 보강 로직(`enrich`)을 추가한다.

**Files:**
- Modify: `collectors/blog_concept_enrich.py`
- Modify: `tests/test_blog_concept_enrich.py`

**Interfaces:**
- Consumes: `collectors.naver_search.NaverSearchClient.search_blog(query, sort) -> dict` (payload with `items`, `total`), `collectors.concept_llm.extract_concept_fields(place_name, snippets, client=None) -> dict`
- Produces: `enrich(batch_commit=20, conn=None, naver_client=None, extract_fn=extract_concept_fields) -> None`

- [ ] **Step 1: 실패하는 테스트 추가**

`tests/test_blog_concept_enrich.py` 파일 상단 import에 아래를 추가하고, 파일 끝(`if __name__ ==`) 이전에 새 테스트 클래스를 추가한다.

```python
from unittest.mock import patch


class FakeNaverBlogClient:
    def __init__(self, payloads: dict):
        self.payloads = payloads  # {(query, sort): payload}
        self.calls = []

    def search_blog(self, query, sort):
        self.calls.append((query, sort))
        return self.payloads.get((query, sort), {"items": [], "total": 0})


def fake_extract_fn(place_name, snippets, **kwargs):
    if not snippets:
        return {k: None for k in (
            "concept_tag", "open_time", "close_day", "parking", "photo_spot", "has_workshop"
        )}
    return {
        "concept_tag": "빈티지",
        "open_time": "12:00-19:00",
        "close_day": "매주 월요일",
        "parking": "불가",
        "photo_spot": True,
        "has_workshop": None,
    }


class EnrichTest(unittest.TestCase):
    def _seed_place(self, conn, place_id, **overrides):
        row = {
            "place_id": place_id, "name": "다구로잉", "category": "giftshop",
            "address": "대전 중구", "lat": 36.0, "lng": 127.0,
            "open_time": None, "close_day": None, "fee": None, "has_parking": None,
            "tel": None, "source_api": "naver_search", "extra_json": "{}",
            "overview": None, "homepage": None, "recommend": "추천",
        }
        row.update(overrides)
        conn.execute(
            """
            INSERT INTO place (place_id, name, category, address, lat, lng, open_time,
                close_day, fee, has_parking, tel, source_api, extra_json, overview,
                homepage, recommend)
            VALUES (:place_id, :name, :category, :address, :lat, :lng, :open_time,
                :close_day, :fee, :has_parking, :tel, :source_api, :extra_json, :overview,
                :homepage, :recommend)
            """,
            row,
        )
        conn.commit()

    def test_only_targets_recommend_rows(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        enrich_mod.ensure_giftshop_enrichment_schema(conn)
        self._seed_place(conn, "p1", recommend="추천")
        self._seed_place(conn, "p2", recommend=None)
        naver_client = FakeNaverBlogClient({
            ("다구로잉 대전 중구", "sim"): {"items": [{"link": "u1", "description": "빈티지 소품샵"}], "total": 1},
            ("다구로잉 대전 중구", "date"): {"items": [{"link": "u1", "description": "빈티지 소품샵"}], "total": 1},
        })

        enrich_mod.enrich(conn=conn, naver_client=naver_client, extract_fn=fake_extract_fn)

        self.assertEqual(len(naver_client.calls), 2)  # p2는 대상이 아니므로 호출 없음

    def test_fills_concept_fields_and_blog_url(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        enrich_mod.ensure_giftshop_enrichment_schema(conn)
        self._seed_place(conn, "p1")
        naver_client = FakeNaverBlogClient({
            ("다구로잉 대전 중구", "sim"): {"items": [{"link": "u1", "description": "빈티지 소품샵"}], "total": 1},
            ("다구로잉 대전 중구", "date"): {"items": [{"link": "u1", "description": "빈티지 소품샵"}], "total": 1},
        })

        enrich_mod.enrich(conn=conn, naver_client=naver_client, extract_fn=fake_extract_fn)

        row = conn.execute(
            "SELECT concept_tag, open_time, close_day, has_parking, photo_spot, has_workshop, blog_url_1 "
            "FROM place WHERE place_id='p1'"
        ).fetchone()
        self.assertEqual(row, ("빈티지", "12:00-19:00", "매주 월요일", 0, 1, None, "u1"))

    def test_does_not_overwrite_existing_open_time(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        enrich_mod.ensure_giftshop_enrichment_schema(conn)
        self._seed_place(conn, "p1", open_time="이미 확인된 시간")
        naver_client = FakeNaverBlogClient({
            ("다구로잉 대전 중구", "sim"): {"items": [{"link": "u1", "description": "빈티지 소품샵"}], "total": 1},
            ("다구로잉 대전 중구", "date"): {"items": [{"link": "u1", "description": "빈티지 소품샵"}], "total": 1},
        })

        enrich_mod.enrich(conn=conn, naver_client=naver_client, extract_fn=fake_extract_fn)

        row = conn.execute("SELECT open_time FROM place WHERE place_id='p1'").fetchone()
        self.assertEqual(row[0], "이미 확인된 시간")

    def test_skips_when_no_snippets_found(self):
        conn = make_place_db()
        self.addCleanup(conn.close)
        enrich_mod.ensure_giftshop_enrichment_schema(conn)
        self._seed_place(conn, "p1")
        naver_client = FakeNaverBlogClient({})  # 검색 결과 없음

        enrich_mod.enrich(conn=conn, naver_client=naver_client, extract_fn=fake_extract_fn)

        row = conn.execute("SELECT concept_tag FROM place WHERE place_id='p1'").fetchone()
        self.assertIsNone(row[0])
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python -m unittest tests.test_blog_concept_enrich -v`
Expected: FAIL — `AttributeError: module 'collectors.blog_concept_enrich' has no attribute 'enrich'`

- [ ] **Step 3: `collectors/blog_concept_enrich.py`에 `enrich` 구현 추가**

파일 상단 import 블록을 아래로 교체한다.

```python
import os
import time

from dotenv import load_dotenv

try:
    from common import get_conn
    from naver_search import NaverSearchClient
    from concept_llm import extract_concept_fields
except ModuleNotFoundError:
    from collectors.common import get_conn
    from collectors.naver_search import NaverSearchClient
    from collectors.concept_llm import extract_concept_fields

load_dotenv()
```

`ensure_giftshop_enrichment_schema` 함수 뒤, `if __name__ ==` 블록 앞에 아래 함수들을 추가한다.

```python
def _collect_snippets(client, query):
    """스니펫 설명 목록과 최근순 대표 링크(최대 3개)를 반환한다."""
    seen_links = set()
    snippets = []
    recent_links = []

    for sort in ("sim", "date"):
        payload = client.search_blog(query, sort=sort)
        for item in payload.get("items", []):
            link = item.get("link")
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            description = item.get("description") or ""
            if description:
                snippets.append(description)
            if sort == "date":
                recent_links.append(link)

    return snippets, recent_links[:3]


def _parking_to_int(value):
    if value == "가능":
        return 1
    if value == "불가":
        return 0
    return None


def _bool_to_int(value):
    if value is True:
        return 1
    if value is False:
        return 0
    return None


def enrich(batch_commit=20, conn=None, naver_client=None, extract_fn=extract_concept_fields):
    owns_conn = conn is None
    if conn is None:
        conn = get_conn()
    ensure_giftshop_enrichment_schema(conn)
    conn.commit()

    if naver_client is None:
        naver_client = NaverSearchClient(os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET"))

    rows = conn.execute(
        "SELECT place_id, name, address FROM place WHERE recommend='추천'"
    ).fetchall()

    updated, skipped, failed = 0, 0, 0
    for place_id, name, address in rows:
        query = f"{name} {address or ''}".strip()
        snippets, blog_urls = _collect_snippets(naver_client, query)

        if not snippets:
            skipped += 1
            continue

        fields = extract_fn(name, snippets)

        blog_url_1 = blog_urls[0] if len(blog_urls) > 0 else None
        blog_url_2 = blog_urls[1] if len(blog_urls) > 1 else None
        blog_url_3 = blog_urls[2] if len(blog_urls) > 2 else None

        conn.execute(
            """
            UPDATE place SET
                open_time = COALESCE(open_time, ?),
                close_day = COALESCE(close_day, ?),
                has_parking = COALESCE(has_parking, ?),
                concept_tag = ?,
                photo_spot = ?,
                has_workshop = ?,
                blog_url_1 = ?,
                blog_url_2 = ?,
                blog_url_3 = ?
            WHERE place_id = ?
            """,
            (
                fields["open_time"],
                fields["close_day"],
                _parking_to_int(fields["parking"]),
                fields["concept_tag"],
                _bool_to_int(fields["photo_spot"]),
                _bool_to_int(fields["has_workshop"]),
                blog_url_1,
                blog_url_2,
                blog_url_3,
                place_id,
            ),
        )
        updated += 1

        if updated % batch_commit == 0:
            conn.commit()

        if owns_conn:
            time.sleep(0.3)

    conn.commit()
    print(f"블로그 보강 완료: {updated}건 갱신, {skipped}건 스킵(스니펫 없음), {failed}건 실패")

    if owns_conn:
        conn.close()
```

`if __name__ == "__main__":` 블록을 아래로 교체한다.

```python
if __name__ == "__main__":
    enrich()
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python -m unittest tests.test_blog_concept_enrich -v`
Expected: `OK` (7 tests: Task 1의 2개 + 이번 5개)

- [ ] **Step 5: 커밋**

```bash
git add collectors/blog_concept_enrich.py tests/test_blog_concept_enrich.py
git commit -m "feat: enrich recommend='추천' places with blog-derived concept fields"
```

---

### Task 5: 실제 API 스모크 테스트 (수동, 커밋 없음)

이 태스크는 코드 변경이 없다. 실제 네이버/OpenAI API 키로 소규모 실행이 되는지 수동 확인한다.

- [ ] **Step 1: 소품샵 수집기를 목표 5건으로 축소해 실제 실행**

Run:
```bash
cd "C:\Users\SAMITECH\Desktop\프로젝트\daejeon-travel-chatbot\collectors"
python -c "from naver_giftshop import collect; collect(target_count=5)"
```
Expected: "소품샵 수집 완료: ..." 출력, 에러 없음. `db/travel.db`의 `place`에 `category='giftshop'` 행이 5건 이하로 생겼는지 확인.

- [ ] **Step 2: 보강기를 실제로 1~2건만 돌려서 OpenAI 응답 확인**

Run:
```bash
python -c "
from blog_concept_enrich import enrich
enrich(batch_commit=1)
"
```
Expected: "블로그 보강 완료: N건 갱신 ..." 출력. `SELECT concept_tag, blog_url_1 FROM place WHERE category='giftshop' LIMIT 3`로 실제 값이 채워졌는지 확인.

주의: `recommend='추천'`인 696개 restaurant 전체가 대상이 되므로, 처음 실행할 때는 코드를 임시로 수정해 `LIMIT 3`을 쿼리에 붙이거나 소품샵 5건짜리 별도 DB로 먼저 검증한다. 전체 696건에 대한 정식 실행은 이 계획 범위 밖이며, 비용·시간을 사용자와 먼저 확인한 뒤 진행한다.

- [ ] **Step 3: 목표 수량을 정식 값(100)으로 되돌려 실행 여부를 사용자에게 확인 후 진행**

이 단계는 사용자 승인 후에만 실행한다. 자동으로 실행하지 않는다.
