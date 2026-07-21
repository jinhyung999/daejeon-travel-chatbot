import sqlite3


def ensure_recommend_schema(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(place)")}
    if "recommend" not in columns:
        conn.execute("ALTER TABLE place ADD COLUMN recommend TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_place_category_recommend "
        "ON place(category, recommend)"
    )
