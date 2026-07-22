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
