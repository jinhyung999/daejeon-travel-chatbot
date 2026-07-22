import unittest
from unittest.mock import Mock

from collectors.kakao_giftshop_detail import (
    KakaoLocalClient,
    classify_candidate,
    district_from_address,
    normalize_name,
)


PLACE = {
    "name": "소품샵 잠시다락",
    "address": "대전광역시 서구 둔산로 32-11",
    "lat": 36.3510,
    "lng": 127.3770,
}


def kakao_doc(**overrides):
    value = {
        "place_name": "소품샵 잠시다락",
        "road_address_name": "대전 서구 둔산로 32-11",
        "address_name": "대전 서구 둔산동 1",
        "x": "127.3770",
        "y": "36.3510",
        "phone": "042-123-4567",
        "place_url": "https://place.map.kakao.com/1",
    }
    value.update(overrides)
    return value


class CandidateClassificationTest(unittest.TestCase):
    def test_normalizes_branch_suffix_and_spacing(self):
        self.assertEqual("소품샵잠시다락", normalize_name("소품샵 잠시다락점"))
        self.assertEqual("서구", district_from_address("대전광역시 서구 둔산로 1"))

    def test_exact_name_within_200m_is_matched(self):
        result = classify_candidate(PLACE, [kakao_doc()])
        self.assertEqual("matched", result["match_status"])
        self.assertEqual("042-123-4567", result["kakao_tel"])

    def test_multiple_matching_candidates_are_ambiguous(self):
        result = classify_candidate(
            PLACE,
            [kakao_doc(), kakao_doc(place_url="https://place.map.kakao.com/2")],
        )
        self.assertEqual("ambiguous", result["match_status"])

    def test_wrong_district_or_over_200m_is_ambiguous(self):
        wrong_district = kakao_doc(
            road_address_name="대전 유성구 대학로 1",
            address_name="대전 유성구 궁동 1",
        )
        result = classify_candidate(PLACE, [wrong_district])
        self.assertEqual("ambiguous", result["match_status"])

    def test_unrelated_or_empty_results_are_not_found(self):
        result = classify_candidate(PLACE, [kakao_doc(place_name="다른 가게")])
        self.assertEqual("not_found", result["match_status"])
        self.assertEqual("not_found", classify_candidate(PLACE, [])["match_status"])


class KakaoLocalClientTest(unittest.TestCase):
    def test_sends_key_query_coordinates_and_distance_sort(self):
        response = Mock(status_code=200)
        response.json.return_value = {"documents": [kakao_doc()]}
        session = Mock()
        session.get.return_value = response
        client = KakaoLocalClient("secret", session=session, sleeper=lambda _: None)

        documents = client.search_keyword("대전 잠시다락", lat=36.351, lng=127.377)

        self.assertEqual(1, len(documents))
        _, kwargs = session.get.call_args
        self.assertEqual("KakaoAK secret", kwargs["headers"]["Authorization"])
        self.assertEqual("distance", kwargs["params"]["sort"])
        self.assertEqual(15, kwargs["params"]["size"])

    def test_retries_429_then_succeeds(self):
        limited = Mock(status_code=429)
        success = Mock(status_code=200)
        success.json.return_value = {"documents": []}
        session = Mock()
        session.get.side_effect = [limited, success]
        client = KakaoLocalClient("secret", session=session, sleeper=lambda _: None)

        self.assertEqual(
            [], client.search_keyword("대전 잠시다락", lat=36.351, lng=127.377)
        )
        self.assertEqual(2, session.get.call_count)


if __name__ == "__main__":
    unittest.main()
