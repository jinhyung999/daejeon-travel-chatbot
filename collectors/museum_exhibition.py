import hashlib
import html
import re
from datetime import date

from common import request_with_retry, save_raw, upsert_event


BASE_URL = "https://www.daejeon.go.kr/dma/DmaExhibList.do"
MUSEUM_ADDRESS = "대전광역시 서구 둔산대로 155"
MUSEUM_LAT = 36.367063
MUSEUM_LNG = 127.387874


def _make_event_id(name: str, start_date: str, end_date: str, place_name: str) -> str:
    digest = hashlib.sha1(
        f"{name}|{start_date}|{end_date}|{place_name}".encode("utf-8")
    ).hexdigest()[:16]
    return f"daejeon_museum_{digest}"


def _clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def _parse_schedule_items(page_html: str) -> list[dict]:
    pattern = re.compile(
        r"(?P<category>기획전시|소장품전시|창작센터전시(?:\(분관\))?|어린이전시|특별전시|대관전시|열린미술관)\s+"
        r"(?P<name>.+?)\s+"
        r"(?P<start>\d{4}-\d{2}-\d{2})\s*~\s*(?P<end>\d{4}-\d{2}-\d{2})\s+"
        r"(?P<place>[^<\n]+)",
        re.DOTALL,
    )
    rows = []
    for match in pattern.finditer(page_html):
        name = _clean_text(match.group("name"))
        place_name = _clean_text(match.group("place"))
        start_date = match.group("start")
        end_date = match.group("end")
        rows.append({
            # event_id 해시 입력은 기존(YYYY-MM-DD)을 유지해 재수집 시 ID가 바뀌지 않게 함
            "event_id": _make_event_id(name, start_date, end_date, place_name),
            "name": name,
            "place_name": place_name,
            "address": MUSEUM_ADDRESS,
            "lat": MUSEUM_LAT,
            "lng": MUSEUM_LNG,
            # DB에는 다른 소스와 동일하게 YYYYMMDD 형식으로 저장 (기간 필터 호환)
            "start_date": start_date.replace("-", ""),
            "end_date": end_date.replace("-", ""),
            "fee": None,
            "source_api": "daejeon_museum",
        })
    return rows


def fetch_schedule():
    params = {
        "exType": "04",
        "menuSeq": "6086",
    }
    resp = request_with_retry(BASE_URL, params)
    resp.encoding = resp.apparent_encoding or resp.encoding
    save_raw("daejeon_museum_schedule", 1, {
        "url": resp.url,
        "html": resp.text,
    })
    return _parse_schedule_items(resp.text)


def collect(from_date: date | None = None):
    if from_date is None:
        from_date = date.today()

    rows = [
        row for row in fetch_schedule()
        if row["end_date"] >= from_date.strftime("%Y%m%d")
    ]
    upsert_event(rows)
    return rows


if __name__ == "__main__":
    collect()
