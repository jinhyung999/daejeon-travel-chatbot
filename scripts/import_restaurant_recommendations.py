from dataclasses import dataclass
from difflib import SequenceMatcher
import math
import sqlite3
import unicodedata


SOURCE_PRIORITY = {
    "tourapi": 0,
    "daejeon_food": 1,
    "sbiz": 2,
    "naver_search": 3,
}


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


def normalize_name(value):
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return "".join(char for char in text if char.isalnum())


def normalize_address(value):
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = text.replace("대전광역시", "대전")
    return "".join(char for char in text if char.isalnum())


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
    contained = min(len(left), len(right)) >= 4 and (
        left in right or right in left
    )
    return contained or SequenceMatcher(None, left, right).ratio() >= 0.72


def select_existing_place(candidate, places, preferred_ids):
    matches = []
    for place in places:
        distance = haversine_metres(
            candidate.latitude,
            candidate.longitude,
            place["lat"],
            place["lng"],
        )
        exact = normalize_name(candidate.name) == normalize_name(place["name"])
        fuzzy = name_is_similar(candidate.name, place["name"])
        same_address = normalize_address(
            candidate.best_address
        ) == normalize_address(place["address"])
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
