import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "travel.db"
SCHEMA_PATH = Path(__file__).parent.parent / "db" / "schema.sql"

def init_db():
    conn = sqlite3.connect(DB_PATH)

    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())

    conn.commit()
    conn.close()

    print(f"DB initialized at {DB_PATH}")

if __name__ == "__main__":
    init_db()