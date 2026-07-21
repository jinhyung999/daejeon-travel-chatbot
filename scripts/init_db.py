import sqlite3
from pathlib import Path

try:
    from scripts.import_restaurant_recommendations import ensure_recommend_schema
except ModuleNotFoundError:
    from import_restaurant_recommendations import ensure_recommend_schema

DB_PATH = Path(__file__).parent.parent / "db" / "travel.db"
SCHEMA_PATH = Path(__file__).parent.parent / "db" / "schema.sql"

def init_db(db_path=DB_PATH, schema_path=SCHEMA_PATH):
    conn = sqlite3.connect(db_path)

    place_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'place'"
    ).fetchone()
    if place_exists:
        ensure_recommend_schema(conn)

    with open(schema_path, "r", encoding="utf-8") as f:
        conn.executescript(f.read())

    conn.commit()
    conn.close()

    print(f"DB initialized at {db_path}")

if __name__ == "__main__":
    init_db()
