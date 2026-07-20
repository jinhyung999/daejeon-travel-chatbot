import csv
import html
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote_plus

from collectors.naver_search import NaverSearchClient


DISTRICTS = ("대덕구", "유성구", "동구", "서구", "중구")
NON_MEAL_TERMS = ("카페", "디저트", "베이커리", "숙박", "마트", "편의점")
LOCATION_SEEDS = {
    "대덕구": ("대덕구", "신탄진", "송촌동", "비래동", "오정동", "중리동"),
    "유성구": (
        "유성구",
        "봉명동",
        "궁동",
        "어은동",
        "관평동",
        "전민동",
        "노은동",
        "지족동",
        "원내동",
    ),
    "동구": ("동구", "대전역", "소제동", "가양동", "용운동", "판암동", "산내"),
    "서구": (
        "서구",
        "둔산동",
        "갈마동",
        "월평동",
        "도마동",
        "관저동",
        "만년동",
        "탄방동",
    ),
    "중구": (
        "중구",
        "대흥동",
        "은행동",
        "선화동",
        "오류동",
        "유천동",
        "산성동",
        "보문산",
    ),
}
FOOD_SEEDS = (
    "맛집",
    "한식",
    "향토음식",
    "노포",
    "칼국수",
    "국밥",
    "냉면",
    "두부두루치기",
    "삼계탕",
    "고기",
    "해산물",
    "중식",
    "일식",
    "분식",
)
LOCAL_VALUE_TERMS = (
    "노포",
    "향토",
    "본점",
    "전통시장",
    "대전",
    "칼국수",
    "두부두루치기",
)
LOW_VALUE_TERMS = (
    "치킨",
    "피자",
    "햄버거",
    "패스트푸드",
    "도시락",
    "롯데리아",
    "맥도날드",
    "버거킹",
    "kfc",
    "서브웨이",
    "맘스터치",
    "bbq",
    "bhc",
    "교촌치킨",
    "도미노피자",
    "피자헛",
    "미스터피자",
)


@dataclass
class ExistingRestaurant:
    name: str
    address: str
    district: str
    latitude: float | None = None
    longitude: float | None = None


@dataclass
class Candidate:
    district: str
    name: str
    category: str
    address: str
    road_address: str
    latitude: float | None
    longitude: float | None
    naver_link: str
    matched_queries: set[str] = field(default_factory=set)
    local_hit_count: int = 0
    comment_sort_hit_count: int = 0
    blog_result_count: int = 0
    recent_blog_count: int = 0
    distinct_blogger_count: int = 0
    latest_post_date: str = ""
    recommendation_score: int = 0
    recommendation_reason: str = ""
    possible_duplicate: str = ""
    reject_reason: str = ""


def single_line(value) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def clean_title(value) -> str:
    return single_line(re.sub(r"<[^>]+>", "", str(value or "")))


def normalize_name(value) -> str:
    value = re.sub(
        r"\b(?:주식회사|유한회사|㈜)\b", "", clean_title(value)
    )
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value).lower()


def normalize_address(value) -> str:
    value = single_line(value).replace("대전광역시", "")
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value).lower()


def scaled_coordinate(value) -> float | None:
    try:
        return int(value) / 10_000_000
    except (TypeError, ValueError):
        return None


def candidate_from_item(
    item: dict, district: str, query: str, sort: str
) -> tuple[Candidate | None, str]:
    name = clean_title(item.get("title"))
    address = single_line(item.get("address"))
    road_address = single_line(item.get("roadAddress"))
    category = single_line(item.get("category"))
    description = single_line(item.get("description"))
    joined_address = road_address or address
    if not name or not joined_address:
        return None, "missing_name_or_address"
    if "대전" not in joined_address or district not in joined_address:
        return None, "target_district_mismatch"
    if any(term in category for term in NON_MEAL_TERMS):
        return None, "non_meal_category"
    classification = f"{category} {description}"
    if "배달전문" in classification or "포장전문" in classification:
        return None, "delivery_only"
    candidate = Candidate(
        district=district,
        name=name,
        category=category,
        address=address,
        road_address=road_address,
        latitude=scaled_coordinate(item.get("mapy")),
        longitude=scaled_coordinate(item.get("mapx")),
        naver_link=single_line(item.get("link")),
    )
    candidate.matched_queries.add(query)
    candidate.local_hit_count = 1
    candidate.comment_sort_hit_count = int(sort == "comment")
    return candidate, ""


def load_existing_restaurants(
    csv_path: Path, db_path: Path
) -> list[ExistingRestaurant]:
    with Path(csv_path).open(encoding="utf-8", newline="") as stream:
        source_rows = list(csv.DictReader(stream))

    coordinates = {}
    if Path(db_path).exists():
        conn = sqlite3.connect(db_path)
        try:
            ids = [
                row["place_id"] for row in source_rows if row.get("place_id")
            ]
            for offset in range(0, len(ids), 900):
                chunk = ids[offset : offset + 900]
                marks = ",".join("?" for _ in chunk)
                coordinates.update(
                    {
                        place_id: (lat, lng)
                        for place_id, lat, lng in conn.execute(
                            "SELECT place_id, lat, lng FROM place "
                            f"WHERE place_id IN ({marks})",
                            chunk,
                        )
                    }
                )
        finally:
            conn.close()

    return [
        ExistingRestaurant(
            name=row.get("name", ""),
            address=row.get("address", ""),
            district=row.get("district", ""),
            latitude=coordinates.get(row.get("place_id"), (None, None))[0],
            longitude=coordinates.get(row.get("place_id"), (None, None))[1],
        )
        for row in source_rows
    ]


def distance_metres(
    a_lat: float, a_lng: float, b_lat: float, b_lng: float
) -> float:
    radius = 6_371_000
    a1, a2 = math.radians(a_lat), math.radians(b_lat)
    d_lat = math.radians(b_lat - a_lat)
    d_lng = math.radians(b_lng - a_lng)
    value = (
        math.sin(d_lat / 2) ** 2
        + math.cos(a1) * math.cos(a2) * math.sin(d_lng / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(value))


def duplicate_status(
    candidate: Candidate, existing_rows: list[ExistingRestaurant]
) -> str:
    candidate_name = normalize_name(candidate.name)
    candidate_address = normalize_address(
        candidate.road_address or candidate.address
    )
    possible = False
    for existing in existing_rows:
        existing_name = normalize_name(existing.name)
        name_ratio = SequenceMatcher(
            None, candidate_name, existing_name
        ).ratio()
        existing_address = normalize_address(existing.address)
        if (
            candidate_name == existing_name
            and candidate_address == existing_address
        ):
            return "confirmed"
        has_coordinates = None not in (
            candidate.latitude,
            candidate.longitude,
            existing.latitude,
            existing.longitude,
        )
        if has_coordinates and name_ratio >= 0.92:
            if (
                distance_metres(
                    candidate.latitude,
                    candidate.longitude,
                    existing.latitude,
                    existing.longitude,
                )
                <= 50
            ):
                return "confirmed"
        if (
            name_ratio >= 0.92
            and not has_coordinates
            and candidate.district == existing.district
        ):
            possible = True
    return "possible" if possible else "clear"


def iter_local_queries(district: str):
    seen = set()
    for location in LOCATION_SEEDS[district]:
        for food in FOOD_SEEDS:
            query = f"{location} {food}"
            if query not in seen:
                seen.add(query)
                yield query


def merge_candidate(rows: list[Candidate], incoming: Candidate) -> bool:
    for current in rows:
        same_address = normalize_address(
            current.road_address or current.address
        ) == normalize_address(incoming.road_address or incoming.address)
        similar_name = (
            SequenceMatcher(
                None,
                normalize_name(current.name),
                normalize_name(incoming.name),
            ).ratio()
            >= 0.92
        )
        close = (
            None
            not in (
                current.latitude,
                current.longitude,
                incoming.latitude,
                incoming.longitude,
            )
            and distance_metres(
                current.latitude,
                current.longitude,
                incoming.latitude,
                incoming.longitude,
            )
            <= 50
        )
        if (same_address and similar_name) or (close and similar_name):
            current.matched_queries.update(incoming.matched_queries)
            current.local_hit_count += incoming.local_hit_count
            current.comment_sort_hit_count += (
                incoming.comment_sort_hit_count
            )
            return True
    rows.append(incoming)
    return False


def collect_local_candidates(
    client: NaverSearchClient,
    district: str,
    existing_rows: list[ExistingRestaurant],
    *,
    target_pool: int = 120,
) -> list[Candidate]:
    candidates = []
    for query in iter_local_queries(district):
        for sort in ("comment", "random"):
            for item in client.search_local(query, sort):
                candidate, reject_reason = candidate_from_item(
                    item, district, query, sort
                )
                if reject_reason:
                    continue
                status = duplicate_status(candidate, existing_rows)
                if status == "confirmed":
                    continue
                candidate.possible_duplicate = (
                    "Y" if status == "possible" else ""
                )
                merge_candidate(candidates, candidate)
        if len(candidates) >= target_pool:
            break
    return candidates


def blog_query(candidate: Candidate) -> str:
    match = re.search(
        r"([가-힣]+동)", candidate.road_address or candidate.address
    )
    location = match.group(1) if match else candidate.district
    return f"{candidate.name} {location}"


def build_blog_search_url(candidate: Candidate) -> str:
    return (
        "https://search.naver.com/search.naver?where=blog&query="
        + quote_plus(blog_query(candidate))
    )


def parse_post_date(value: str) -> date | None:
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except (TypeError, ValueError):
        return None


def enrich_blog_metrics(
    client: NaverSearchClient, candidate: Candidate, *, today: date
) -> None:
    query = blog_query(candidate)
    similarity = client.search_blog(query, "sim")
    recent = client.search_blog(query, "date")
    candidate.blog_result_count = int(similarity.get("total") or 0)
    bloggers = {
        single_line(item.get("bloggerlink") or item.get("bloggername"))
        for item in similarity.get("items", [])
        if single_line(item.get("bloggerlink") or item.get("bloggername"))
    }
    candidate.distinct_blogger_count = len(bloggers)
    cutoff = date(today.year - 1, today.month, today.day)
    parsed_dates = [
        parsed
        for parsed in (
            parse_post_date(item.get("postdate", ""))
            for item in recent.get("items", [])
        )
        if parsed is not None
    ]
    candidate.recent_blog_count = sum(
        parsed >= cutoff for parsed in parsed_dates
    )
    candidate.latest_post_date = (
        max(parsed_dates).strftime("%Y%m%d") if parsed_dates else ""
    )


def score_candidate(candidate: Candidate) -> int:
    combined = f"{candidate.name} {candidate.category}".lower()
    local_signals = [
        term for term in LOCAL_VALUE_TERMS if term.lower() in combined
    ]
    low_value_signals = [
        term for term in LOW_VALUE_TERMS if term.lower() in combined
    ]
    score = 0
    score += min(20, candidate.local_hit_count * 4)
    score += min(15, candidate.comment_sort_hit_count * 3)
    score += 15 if ">" in candidate.category else 10
    score += min(
        10, int(math.log10(candidate.blog_result_count + 1) * 3)
    )
    score += min(15, candidate.recent_blog_count * 2)
    score += min(10, candidate.distinct_blogger_count)
    score += min(15, len(local_signals) * 5)
    score -= min(30, len(low_value_signals) * 15)
    candidate.recommendation_score = max(0, min(100, score))

    reasons = []
    if local_signals:
        reasons.append("지역성: " + ", ".join(local_signals))
    if candidate.recent_blog_count:
        reasons.append(
            f"최근 12개월 블로그 {candidate.recent_blog_count}건"
        )
    if low_value_signals:
        reasons.append(
            "배달형 음식 감점: " + ", ".join(low_value_signals)
        )
    candidate.recommendation_reason = (
        "; ".join(reasons) or "지역검색 후보"
    )
    return candidate.recommendation_score
