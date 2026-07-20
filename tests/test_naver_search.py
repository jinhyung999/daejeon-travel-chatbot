import unittest

from collectors.naver_search import NaverSearchClient, NaverSearchError


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class NaverSearchClientTest(unittest.TestCase):
    def test_local_search_sends_secret_headers_and_fixed_limits(self):
        session = FakeSession(
            [FakeResponse(200, {"items": [{"title": "식당"}]})]
        )
        client = NaverSearchClient(
            "client-id", "client-secret", session=session
        )

        items = client.search_local("대덕구 칼국수", sort="comment")

        self.assertEqual(items, [{"title": "식당"}])
        url, kwargs = session.calls[0]
        self.assertTrue(url.endswith("/v1/search/local.json"))
        self.assertEqual(
            kwargs["params"],
            {
                "query": "대덕구 칼국수",
                "display": 5,
                "start": 1,
                "sort": "comment",
            },
        )
        self.assertEqual(
            kwargs["headers"]["X-Naver-Client-Id"], "client-id"
        )
        self.assertEqual(
            kwargs["headers"]["X-Naver-Client-Secret"], "client-secret"
        )

    def test_blog_search_returns_channel_metadata(self):
        payload = {"total": 12, "items": [{"postdate": "20260701"}]}
        session = FakeSession([FakeResponse(200, payload)])
        client = NaverSearchClient("id", "secret", session=session)

        self.assertEqual(
            client.search_blog("식당 유성구", sort="date"), payload
        )
        self.assertEqual(session.calls[0][1]["params"]["display"], 100)

    def test_retries_429_then_succeeds(self):
        sleeps = []
        session = FakeSession(
            [
                FakeResponse(429, {}),
                FakeResponse(200, {"items": []}),
            ]
        )
        client = NaverSearchClient(
            "id", "secret", session=session, sleeper=sleeps.append
        )

        self.assertEqual(
            client.search_local("중구 국밥", sort="random"), []
        )
        self.assertEqual(sleeps, [1])
        self.assertEqual(len(session.calls), 2)

    def test_does_not_retry_non_429_client_error(self):
        session = FakeSession([FakeResponse(401, {})])
        client = NaverSearchClient(
            "id", "secret", session=session, sleeper=lambda _: None
        )

        with self.assertRaisesRegex(NaverSearchError, "HTTP 401"):
            client.search_blog("식당", sort="sim")
        self.assertEqual(len(session.calls), 1)

    def test_stops_after_three_retries_for_server_errors(self):
        session = FakeSession([FakeResponse(500, {}) for _ in range(4)])
        client = NaverSearchClient(
            "id", "secret", session=session, sleeper=lambda _: None
        )

        with self.assertRaisesRegex(NaverSearchError, "HTTP 500"):
            client.search_local("서구 한식", sort="comment")
        self.assertEqual(len(session.calls), 4)


if __name__ == "__main__":
    unittest.main()
