import json
import unittest

from collectors import concept_llm


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeCompletionResponse:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


class FakeCompletions:
    def __init__(self, content):
        self._content = content
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return FakeCompletionResponse(self._content)


class FakeChat:
    def __init__(self, content):
        self.completions = FakeCompletions(content)


class FakeOpenAIClient:
    def __init__(self, content):
        self.chat = FakeChat(content)


VALID_RESPONSE = json.dumps({
    "concept_tag": "빈티지",
    "open_time": "화~일요일 12:00-19:00",
    "close_day": "매주 월요일",
    "parking": "불가",
    "photo_spot": True,
    "has_workshop": False,
}, ensure_ascii=False)


class ExtractConceptFieldsTest(unittest.TestCase):
    def test_returns_all_none_when_no_snippets(self):
        result = concept_llm.extract_concept_fields("다구로잉", [], client=FakeOpenAIClient(VALID_RESPONSE))

        self.assertEqual(
            result,
            {
                "concept_tag": None, "open_time": None, "close_day": None,
                "parking": None, "photo_spot": None, "has_workshop": None,
            },
        )

    def test_parses_valid_llm_response(self):
        client = FakeOpenAIClient(VALID_RESPONSE)

        result = concept_llm.extract_concept_fields("다구로잉", ["스니펫1", "스니펫2"], client=client)

        self.assertEqual(result["concept_tag"], "빈티지")
        self.assertEqual(result["parking"], "불가")
        self.assertIs(result["photo_spot"], True)
        self.assertIs(result["has_workshop"], False)

    def test_sends_snippets_and_place_name_in_prompt(self):
        client = FakeOpenAIClient(VALID_RESPONSE)

        concept_llm.extract_concept_fields("다구로잉", ["영업시간 화~일 12-19시"], client=client)

        user_message = client.chat.completions.last_kwargs["messages"][1]["content"]
        self.assertIn("다구로잉", user_message)
        self.assertIn("영업시간 화~일 12-19시", user_message)

    def test_raises_on_non_json_response(self):
        client = FakeOpenAIClient("이건 JSON이 아님")

        with self.assertRaises(concept_llm.ConceptExtractionError):
            concept_llm.extract_concept_fields("다구로잉", ["스니펫"], client=client)


if __name__ == "__main__":
    unittest.main()
