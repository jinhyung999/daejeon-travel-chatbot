import sqlite3
import sys
from pathlib import Path

try:
    from scripts.import_restaurant_recommendations import ensure_recommend_schema
except ModuleNotFoundError:
    from import_restaurant_recommendations import ensure_recommend_schema

# collectors/는 scripts/의 형제 디렉터리라, 직접 실행(python scripts/init_db.py) 시
# sys.path에 scripts/만 잡혀서 collectors 패키지가 안 보인다. repo root를 명시적으로 추가한다.
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from collectors.blog_concept_enrich import ensure_giftshop_enrichment_schema

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
