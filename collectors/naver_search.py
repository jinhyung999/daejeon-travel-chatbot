import time
from typing import Callable

import requests


BASE_URL = "https://openapi.naver.com/v1/search"


class NaverSearchError(RuntimeError):
    pass


class NaverSearchClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        session=None,
        sleeper: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
        timeout: int = 10,
    ):
        if not client_id or not client_secret:
            raise ValueError(
                "NAVER_CLIENT_ID and NAVER_CLIENT_SECRET are required"
            )
        self._headers = {
            "X-Naver-Client-Id": client_id,
            "X-Naver-Client-Secret": client_secret,
        }
        self._session = session or requests.Session()
        self._sleeper = sleeper
        self._max_retries = max_retries
        self._timeout = timeout

    def _get(self, resource: str, params: dict) -> dict:
        last_reason = "unknown error"
        for attempt in range(self._max_retries + 1):
            try:
                response = self._session.get(
                    f"{BASE_URL}/{resource}.json",
                    params=params,
                    headers=self._headers,
                    timeout=self._timeout,
                )
                status = response.status_code
                if status == 200:
                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("items", []), list
                    ):
                        raise NaverSearchError(
                            "Naver Search API returned an invalid response shape"
                        )
                    return payload
                last_reason = f"HTTP {status}"
                retryable = status == 429 or status >= 500
                if not retryable:
                    raise NaverSearchError(
                        "Naver Search API failed for "
                        f"query={params['query']!r} with {last_reason}"
                    )
            except requests.RequestException as exc:
                last_reason = type(exc).__name__

            if attempt == self._max_retries:
                break
            self._sleeper(2**attempt)

        raise NaverSearchError(
            "Naver Search API failed for "
            f"query={params['query']!r} after retries: {last_reason}"
        )

    def search_local(self, query: str, sort: str) -> list[dict]:
        return self._get(
            "local",
            {
                "query": query,
                "display": 5,
                "start": 1,
                "sort": sort,
            },
        ).get("items", [])

    def search_blog(self, query: str, sort: str) -> dict:
        return self._get(
            "blog",
            {
                "query": query,
                "display": 100,
                "start": 1,
                "sort": sort,
            },
        )
