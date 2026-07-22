import json
import os

from dotenv import load_dotenv

load_dotenv()

FIELDS = ("concept_tag", "open_time", "close_day", "parking", "photo_spot", "has_workshop")

SYSTEM_PROMPT = (
    "너는 대전 지역 장소에 대한 블로그 발췌문 여러 개를 읽고 정보를 추출하는 도우미다. "
    "아래 JSON 스키마로만 응답하고 다른 텍스트는 출력하지 마라. "
    "근거가 없는 값은 반드시 null로 남겨라. 추측으로 채우지 마라.\n"
    '{"concept_tag": string|null, "open_time": string|null, "close_day": string|null, '
    '"parking": "가능"|"불가"|null, "photo_spot": true|false|null, "has_workshop": true|false|null}'
)


class ConceptExtractionError(RuntimeError):
    pass


def _empty_result() -> dict:
    return {field: None for field in FIELDS}


def extract_concept_fields(place_name: str, snippets: list[str], *, client=None, model="gpt-4o-mini") -> dict:
    if not snippets:
        return _empty_result()

    if client is None:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

    user_content = f"장소명: {place_name}\n\n블로그 발췌문:\n" + "\n---\n".join(snippets)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )

    raw = response.choices[0].message.content
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConceptExtractionError(f"LLM 응답이 JSON이 아님: {raw!r}") from exc

    if not isinstance(parsed, dict):
        raise ConceptExtractionError(f"LLM 응답이 객체가 아님: {parsed!r}")

    return {field: parsed.get(field) for field in FIELDS}
