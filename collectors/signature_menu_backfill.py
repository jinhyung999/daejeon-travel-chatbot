import json
from dataclasses import dataclass

try:
    from common import get_conn
except ModuleNotFoundError:
    from collectors.common import get_conn


def ensure_signature_menu_schema(conn) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(place)")}
    if "signature_menu" not in columns:
        conn.execute("ALTER TABLE place ADD COLUMN signature_menu TEXT")


def _clean(value) -> str | None:
    if not value:
        return None
    text = str(value).replace('"', "").strip()
    return text or None


@dataclass(frozen=True)
class BackfillStats:
    updated: int
    skipped: int


def backfill(conn=None) -> BackfillStats:
    owns_conn = conn is None
    if conn is None:
        conn = get_conn()
    ensure_signature_menu_schema(conn)
    conn.commit()

    rows = conn.execute(
        "SELECT place_id, extra_json, signature_menu, overview FROM place "
        "WHERE source_api='daejeon_food' AND recommend='추천'"
    ).fetchall()

    updated, skipped = 0, 0
    for place_id, extra_json, signature_menu, overview in rows:
        extra = json.loads(extra_json or "{}")
        new_signature_menu = signature_menu or _clean(extra.get("rprsFod"))
        new_overview = overview or _clean(extra.get("restrntSumm"))

        if new_signature_menu == signature_menu and new_overview == overview:
            skipped += 1
            continue

        conn.execute(
            "UPDATE place SET signature_menu=?, overview=? WHERE place_id=?",
            (new_signature_menu, new_overview, place_id),
        )
        updated += 1

    conn.commit()
    if owns_conn:
        conn.close()

    return BackfillStats(updated=updated, skipped=skipped)


if __name__ == "__main__":
    stats = backfill()
    print(f"signature_menu/overview 백필 완료: {stats.updated}건 갱신, {stats.skipped}건 스킵(변경 없음)")
