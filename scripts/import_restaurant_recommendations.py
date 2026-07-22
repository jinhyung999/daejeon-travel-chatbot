import argparse
import csv
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import unicodedata

try:
    from scripts.recommendation_json import dump_json_object, load_json_object
except ModuleNotFoundError:
    from recommendation_json import dump_json_object, load_json_object


SOURCE_PRIORITY = {
    "tourapi": 0,
    "daejeon_food": 1,
    "sbiz": 2,
    "naver_search": 3,
}

# Inclusive safety envelope covering Daejeon's administrative boundary.
DAEJEON_LATITUDE_RANGE = (36.10, 36.55)
DAEJEON_LONGITUDE_RANGE = (127.20, 127.60)


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


@dataclass(frozen=True)
class ImportStats:
    existing_marked: int
    matched_enriched: int
    inserted: int
    source_overlap: int
    recommended_total: int


def normalize_name(value):
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return "".join(char for char in text if char.isalnum())


def normalize_address(value):
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = text.replace("대전광역시", "대전")
    return "".join(char for char in text if char.isalnum())


def _is_blank(value):
    return not str(value or "").strip()


def _coordinates_in_daejeon(latitude, longitude):
    if latitude is None or longitude is None:
        return False
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError):
        return False
    return (
        math.isfinite(latitude)
        and math.isfinite(longitude)
        and DAEJEON_LATITUDE_RANGE[0]
        <= latitude
        <= DAEJEON_LATITUDE_RANGE[1]
        and DAEJEON_LONGITUDE_RANGE[0]
        <= longitude
        <= DAEJEON_LONGITUDE_RANGE[1]
    )


def _validate_candidate(candidate):
    if not _coordinates_in_daejeon(candidate.latitude, candidate.longitude):
        raise ValueError(
            "candidate coordinate must be finite and inside the documented "
            "Daejeon bounding box "
            f"lat={DAEJEON_LATITUDE_RANGE}, "
            f"lng={DAEJEON_LONGITUDE_RANGE}: {candidate.name!r}"
        )


def haversine_metres(lat1, lng1, lat2, lng2):
    if any(value is None for value in (lat1, lng1, lat2, lng2)):
        return None

    radius = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lng2 - lng1)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1)
        * math.cos(phi2)
        * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(value))


def name_is_similar(left, right):
    left, right = normalize_name(left), normalize_name(right)
    if not left or not right:
        return False
    contained = min(len(left), len(right)) >= 4 and (
        left in right or right in left
    )
    return contained or SequenceMatcher(None, left, right).ratio() >= 0.72


def select_existing_place(candidate, places, preferred_ids):
    matches = []
    candidate_name = normalize_name(candidate.name)
    candidate_address = normalize_address(candidate.best_address)
    for place in places:
        distance = haversine_metres(
            candidate.latitude,
            candidate.longitude,
            place["lat"],
            place["lng"],
        )
        place_name = normalize_name(place["name"])
        place_address = normalize_address(place["address"])
        exact = bool(candidate_name and place_name and candidate_name == place_name)
        fuzzy = name_is_similar(candidate.name, place["name"])
        same_address = bool(
            candidate_address
            and place_address
            and candidate_address == place_address
        )
        if (
            (exact and distance is not None and distance <= 100)
            or (fuzzy and distance is not None and distance <= 50)
            or (distance is None and exact and same_address)
        ):
            matches.append(
                (place, distance if distance is not None else float("inf"))
            )

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


def ensure_recommend_schema(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(place)")}
    if "recommend" not in columns:
        conn.execute("ALTER TABLE place ADD COLUMN recommend TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_place_category_recommend "
        "ON place(category, recommend)"
    )


def merge_recommendation_extra(raw_extra, candidate):
    extra = load_json_object(raw_extra)
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
    return dump_json_object(extra)


def stable_place_id(candidate):
    identity = "|".join(
        (
            normalize_name(candidate.name),
            normalize_address(candidate.best_address),
            f"{candidate.latitude:.7f}",
            f"{candidate.longitude:.7f}",
        )
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return "naver_restaurant_" + digest


def _read_existing_ids(path):
    with open(path, encoding="utf-8-sig", newline="") as stream:
        return {
            row["place_id"].strip()
            for row in csv.DictReader(stream)
            if row.get("place_id", "").strip()
        }


def _read_approved_candidates(path):
    with open(path, encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            candidate = Candidate(
                district=row["district"].strip(),
                name=row["name"].strip(),
                category=row["category"].strip(),
                address=row["address"].strip(),
                road_address=row["road_address"].strip(),
                latitude=float(row["latitude"]),
                longitude=float(row["longitude"]),
                naver_link=row["naver_link"].strip(),
                recommendation_score=int(row["recommendation_score"]),
                recommendation_reason=row["recommendation_reason"].strip(),
            )
            _validate_candidate(candidate)
            yield candidate


def _restaurant_places(conn):
    cursor = conn.execute(
        "SELECT place_id, name, address, lat, lng, source_api, extra_json, "
        "homepage "
        "FROM place WHERE category='restaurant'"
    )
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _validate_import_postconditions(conn, preferred_ids):
    resolved_preferred_ids = {
        row[0]
        for row in conn.execute(
            "SELECT place_id FROM place "
            "WHERE category='restaurant' AND recommend='추천'"
        )
        if row[0] in preferred_ids
    }
    unresolved = sorted(preferred_ids - resolved_preferred_ids)
    if unresolved:
        preview = ", ".join(unresolved[:5])
        raise ValueError(
            f"preferred recommendation IDs did not resolve: {preview}"
        )

    recommended_rows = conn.execute(
        "SELECT place_id, lat, lng, extra_json FROM place "
        "WHERE category='restaurant' AND recommend='추천'"
    ).fetchall()
    for place_id, latitude, longitude, extra_json in recommended_rows:
        if not _coordinates_in_daejeon(latitude, longitude):
            raise ValueError(
                f"recommended place has invalid coordinate: {place_id}"
            )
        try:
            load_json_object(extra_json, label=f"extra_json for {place_id}")
        except ValueError as error:
            raise ValueError(
                f"recommended place has invalid JSON: {place_id}"
            ) from error

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise ValueError(f"database integrity postcondition failed: {integrity}")


def apply_recommendations(conn, existing_csv, approved_csv):
    preferred_ids = _read_existing_ids(existing_csv)
    candidates = list(_read_approved_candidates(approved_csv))
    existing_marked = 0
    matched_enriched = 0
    inserted = 0
    source_overlap = 0

    # sqlite3.Connection.__enter__ does not issue BEGIN, so start a savepoint
    # before the schema migration to make DDL part of the same rollback boundary.
    conn.execute("SAVEPOINT restaurant_recommendation_import")
    with conn:
        ensure_recommend_schema(conn)
        for place_id in sorted(preferred_ids):
            cursor = conn.execute(
                "UPDATE place SET recommend='추천' "
                "WHERE place_id=? AND category='restaurant'",
                (place_id,),
            )
            existing_marked += cursor.rowcount

        places = _restaurant_places(conn)
        for candidate in candidates:
            match = select_existing_place(candidate, places, preferred_ids)
            extra_json = merge_recommendation_extra(
                match["extra_json"] if match else None,
                candidate,
            )
            if match:
                replace_coordinates = (
                    match["lat"] is None or match["lng"] is None
                )
                lat = candidate.latitude if replace_coordinates else match["lat"]
                lng = candidate.longitude if replace_coordinates else match["lng"]
                address = (
                    candidate.best_address
                    if _is_blank(match["address"])
                    else match["address"]
                )
                homepage = (
                    candidate.naver_link
                    if (
                        _is_blank(match["homepage"])
                        and not _is_blank(candidate.naver_link)
                    )
                    else match["homepage"]
                )
                conn.execute(
                    "UPDATE place SET recommend='추천', extra_json=?, lat=?, lng=?, "
                    "address=?, homepage=? "
                    "WHERE place_id=?",
                    (
                        extra_json,
                        lat,
                        lng,
                        address,
                        homepage,
                        match["place_id"],
                    ),
                )
                match["extra_json"] = extra_json
                match["lat"] = lat
                match["lng"] = lng
                match["address"] = address
                match["homepage"] = homepage
                matched_enriched += 1
                source_overlap += match["place_id"] in preferred_ids
                continue

            place_id = stable_place_id(candidate)
            conn.execute(
                "INSERT INTO place "
                "(place_id, name, category, address, lat, lng, source_api, "
                "extra_json, homepage, recommend) "
                "VALUES (?, ?, 'restaurant', ?, ?, ?, 'naver_search', ?, ?, '추천')",
                (
                    place_id,
                    candidate.name,
                    candidate.best_address,
                    candidate.latitude,
                    candidate.longitude,
                    extra_json,
                    candidate.naver_link,
                ),
            )
            places.append(
                {
                    "place_id": place_id,
                    "name": candidate.name,
                    "address": candidate.best_address,
                    "lat": candidate.latitude,
                    "lng": candidate.longitude,
                    "source_api": "naver_search",
                    "extra_json": extra_json,
                    "homepage": candidate.naver_link,
                }
            )
            inserted += 1

        recommended_total = conn.execute(
            "SELECT COUNT(*) FROM place "
            "WHERE category='restaurant' AND recommend='추천'"
        ).fetchone()[0]
        _validate_import_postconditions(conn, preferred_ids)

    return ImportStats(
        existing_marked=existing_marked,
        matched_enriched=matched_enriched,
        inserted=inserted,
        source_overlap=source_overlap,
        recommended_total=recommended_total,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Apply approved restaurant recommendations to the place database."
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--existing-csv", type=Path, required=True)
    parser.add_argument("--approved-csv", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    if not args.db.is_file():
        parser.error(f"database does not exist: {args.db}")

    if args.apply:
        source = sqlite3.connect(args.db)
    else:
        source = sqlite3.connect(
            args.db.resolve().as_uri() + "?mode=ro",
            uri=True,
        )
    try:
        if args.apply:
            target = source
        else:
            target = sqlite3.connect(":memory:")
            source.backup(target)

        try:
            stats = apply_recommendations(
                target,
                args.existing_csv,
                args.approved_csv,
            )
        finally:
            if target is not source:
                target.close()
    finally:
        source.close()

    summary = {"mode": "apply" if args.apply else "dry-run", **asdict(stats)}
    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
    )


if __name__ == "__main__":
    main()
