import argparse
import csv
import json
import sqlite3
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "db" / "travel.db"
DEFAULT_OUTPUT_PATH = REPO_ROOT / "data" / "curation" / "restaurant_recommendations.csv"

FIELDNAMES = [
    "place_id",
    "name",
    "category",
    "address",
    "district",
    "recommend",
    "source_api",
    "recommendation_basis",
    "overview",
    "representative_food",
    "source_summary",
]


def _parse_extra_json(raw_value):
    try:
        value = json.loads(raw_value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _district_from_address(address):
    address = address or ""
    for district in ("유성구", "동구", "서구", "대덕구", "중구"):
        if district in address:
            return district
    return ""


def collect_recommendations(conn):
    conn.row_factory = sqlite3.Row
    places = conn.execute(
        """
        SELECT place_id, name, category, address, source_api, overview, extra_json
        FROM place
        WHERE category = 'restaurant' AND recommend = '추천'
        ORDER BY place_id
        """
    ).fetchall()

    recommendations = []
    for place in places:
        overview = " ".join((place["overview"] or "").split())
        extra = _parse_extra_json(place["extra_json"])
        recommendation = extra.get("recommendation")
        recommendation = recommendation if isinstance(recommendation, dict) else {}
        representative_food = str(
            recommendation.get("detailed_category") or extra.get("rprsFod") or ""
        ).strip()
        source_summary = str(
            recommendation.get("source") or extra.get("restrntSumm") or ""
        ).strip()

        recommendations.append(
            {
                "place_id": place["place_id"],
                "name": place["name"],
                "category": place["category"],
                "address": place["address"] or "",
                "district": _district_from_address(place["address"]),
                "recommend": "추천",
                "source_api": place["source_api"] or "",
                "recommendation_basis": str(
                    recommendation.get("reason") or ""
                ).strip(),
                "overview": overview,
                "representative_food": representative_food,
                "source_summary": source_summary,
            }
        )

    return recommendations


def export_recommendations(db_path: Path, output_path: Path) -> int:
    db_path = Path(db_path)
    output_path = Path(output_path)

    conn = sqlite3.connect(db_path)
    try:
        recommendations = collect_recommendations(conn)
    finally:
        conn.close()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(recommendations)

    return len(recommendations)


def main():
    parser = argparse.ArgumentParser(description="음식점 1차 추천 목록을 CSV로 내보냅니다.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    args = parser.parse_args()

    exported = export_recommendations(args.db, args.output)
    print(f"exported={exported} output={args.output}")


if __name__ == "__main__":
    main()
