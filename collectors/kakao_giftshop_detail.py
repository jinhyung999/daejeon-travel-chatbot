from dataclasses import dataclass
import math
import re
import time
import unicodedata

import requests


KAKAO_KEYWORD_URL = "https://dapi.kakao.com/v2/local/search/keyword.json"
MATCH_RADIUS_M = 200
DISTRICTS = ("유성구", "대덕구", "동구", "서구", "중구")
_BRANCH_SUFFIX = re.compile(r"(?:점|지점)$")


def normalize_name(value):
    text = unicodedata.normalize("NFKC", str(value or "")).lower().strip()
    text = re.sub(r"\([^)]*\)", "", text)
    text = _BRANCH_SUFFIX.sub("", text)
    return "".join(char for char in text if char.isalnum())


def district_from_address(value):
    text = str(value or "")
    return next((district for district in DISTRICTS if district in text), "")


def haversine_m(lat1, lng1, lat2, lng2):
    radius = 6_371_000
    phi1, phi2 = math.radians(float(lat1)), math.radians(float(lat2))
    delta_phi = math.radians(float(lat2) - float(lat1))
    delta_lambda = math.radians(float(lng2) - float(lng1))
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(value))


def _candidate_row(place, document):
    address = document.get("road_address_name") or document.get("address_name") or ""
    distance = haversine_m(place["lat"], place["lng"], document["y"], document["x"])
    return {
        "kakao_name": document.get("place_name") or "",
        "kakao_address": address,
        "kakao_distance_m": round(distance, 1),
        "kakao_tel": document.get("phone") or "",
        "kakao_place_url": document.get("place_url") or "",
        "match_error": "",
    }


def _blank_result(status):
    return {
        "kakao_name": "",
        "kakao_address": "",
        "kakao_distance_m": "",
        "kakao_tel": "",
        "kakao_place_url": "",
        "match_status": status,
        "match_error": "",
    }


def classify_candidate(place, documents):
    rows = []
    place_name = normalize_name(place["name"])
    place_district = district_from_address(place["address"])
    for document in documents:
        try:
            row = _candidate_row(place, document)
        except (KeyError, TypeError, ValueError):
            continue
        if normalize_name(row["kakao_name"]) == place_name:
            rows.append(row)

    if not rows:
        return _blank_result("not_found")

    rows.sort(key=lambda row: row["kakao_distance_m"])
    eligible = [
        row
        for row in rows
        if row["kakao_distance_m"] <= MATCH_RADIUS_M
        and district_from_address(row["kakao_address"]) == place_district
    ]
    selected = eligible[0] if eligible else rows[0]
    selected["match_status"] = (
        "matched" if len(rows) == 1 and len(eligible) == 1 else "ambiguous"
    )
    return selected


class KakaoLocalError(RuntimeError):
    pass


class KakaoLocalClient:
    def __init__(
        self,
        rest_api_key,
        *,
        session=None,
        sleeper=time.sleep,
        max_retries=3,
        timeout=10,
    ):
        if not rest_api_key:
            raise ValueError("KAKAO_REST_API_KEY is required")
        self._headers = {"Authorization": f"KakaoAK {rest_api_key}"}
        self._session = session or requests.Session()
        self._sleeper = sleeper
        self._max_retries = max_retries
        self._timeout = timeout

    def search_keyword(self, query, *, lat, lng):
        params = {
            "query": query,
            "x": str(lng),
            "y": str(lat),
            "sort": "distance",
            "page": 1,
            "size": 15,
        }
        for attempt in range(self._max_retries + 1):
            try:
                response = self._session.get(
                    KAKAO_KEYWORD_URL,
                    params=params,
                    headers=self._headers,
                    timeout=self._timeout,
                )
                if response.status_code == 200:
                    payload = response.json()
                    documents = payload.get("documents")
                    if not isinstance(documents, list):
                        raise KakaoLocalError("Kakao response has no documents list")
                    return documents
                retryable = response.status_code == 429 or response.status_code >= 500
                if not retryable:
                    raise KakaoLocalError(
                        f"Kakao keyword search failed: HTTP {response.status_code}"
                    )
            except requests.RequestException as error:
                if attempt == self._max_retries:
                    raise KakaoLocalError(
                        "Kakao keyword search failed after retries"
                    ) from error
            if attempt == self._max_retries:
                break
            self._sleeper(2**attempt)
        raise KakaoLocalError("Kakao keyword search failed after retries")
