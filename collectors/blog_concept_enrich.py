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


def _collect_snippets(client, query):
    """스니펫 설명 목록과 최근순 대표 링크(최대 3개)를 반환한다.

    스니펫 중복 제거(sim/date 양쪽에 같은 글이 나오는 경우 본문 한 번만 사용)와
    대표 링크 기록은 서로 다른 관심사라 별도 seen-set으로 추적한다. 그렇지 않으면
    sim 패스에서 먼저 본 링크가 date 패스에서 다시 나와도 최근 링크로 기록되지
    않는다.
    """
    seen_snippet_links = set()
    snippets = []

    seen_recent_links = set()
    recent_links = []

    for sort in ("sim", "date"):
        payload = client.search_blog(query, sort=sort)
        for item in payload.get("items", []):
            link = item.get("link")
            if not link:
                continue

            if link not in seen_snippet_links:
                seen_snippet_links.add(link)
                description = item.get("description") or ""
                if description:
                    snippets.append(description)

            if sort == "date" and link not in seen_recent_links:
                seen_recent_links.add(link)
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


if __name__ == "__main__":
    enrich()
