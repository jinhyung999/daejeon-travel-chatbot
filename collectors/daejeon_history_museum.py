# =====================================================
# daejeon_history_museum.py
# 대전시립박물관 전시 일정 크롤링 (museum_exhibition.py와 동일 패턴)
#
# 대상: https://www.daejeon.go.kr/his/musDisplayList.do (목록 + 상세, displaySeq로 구분)
#
# *** 중요: 이 파일은 실제 페이지 HTML을 확인하지 못한 상태로 작성되었습니다 ***
# 작성 환경(에이전트 샌드박스)에서 daejeon.go.kr 도메인 전체가 네트워크
# 연결이 되지 않아(방화벽/지역 차단으로 추정), 목록 페이지의 실제 마크업을
# 눈으로 확인하지 못했습니다. 아래 파싱 로직은 같은 대전시 사이트군인
# 대전시립미술관(dma) 사이트의 알려진 패턴과, 검색엔진에 노출된 URL 패턴
# (musDisplayList.do?displaySeq=N&menuSeq=646)을 근거로 추정 작성했습니다.
#
# 그래서 안전장치를 넣었습니다:
#   - 목록에서 항목을 하나도 못 찾으면 DB를 건드리지 않고 즉시 중단 +
#     raw HTML을 파일로 저장해 원인 파악이 가능하게 함
#   - 최초 실행은 반드시 로컬(팀원 PC)에서 하고, 콘솔 출력을 확인해서
#     "찾은 항목 수"가 0이거나 이상하면 정규식을 실제 HTML에 맞게 수정 필요
#
# 사용법:
#   cd collectors && python daejeon_history_museum.py        # 실행+저장
#   cd collectors && python daejeon_history_museum.py --dry  # DB 저장 없이 파싱 결과만 확인
# =====================================================

import hashlib
import html
import re
import sys

from common import get_conn, request_with_retry, save_raw, upsert_event

LIST_URL = "https://www.daejeon.go.kr/his/musDisplayList.do"
MENU_SEQ = "646"

# place 테이블에 이미 있는 좌표와 일치 확인됨 (2026-07-13 기준)
MUSEUM_NAME = "대전시립박물관"
MUSEUM_ADDRESS = "대전광역시 유성구 도안대로 398"
MUSEUM_LAT = 36.3369051804
MUSEUM_LNG = 127.3353561691

# 목록 페이지에서 상세 링크 후보 추출: href 안에 displaySeq=숫자가 있고
# 태그 사이 텍스트(제목으로 추정)를 함께 캡처
_ITEM_RE = re.compile(
    r'href="[^"]*musDisplayList\.do\?[^"]*displaySeq=(?P<seq>\d+)[^"]*"[^>]*>\s*(?P<title>[^<]{2,80}?)\s*<',
)

# 상세 페이지에서 기간 추출: "2026.07.01 ~ 2026.09.30" 또는 "2026-07-01 ~ 2026-09-30" 모두 대응
_PERIOD_RE = re.compile(
    r"(?P<start>\d{4}[.\-]\d{2}[.\-]\d{2})\s*~\s*(?P<end>\d{4}[.\-]\d{2}[.\-]\d{2})"
)

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _make_event_id(title: str, start_date: str, end_date: str) -> str:
    digest = hashlib.sha1(f"{title}|{start_date}|{end_date}".encode("utf-8")).hexdigest()[:16]
    return f"daejeon_history_museum_{digest}"


def fetch_list() -> list[dict]:
    resp = request_with_retry(LIST_URL, {"menuSeq": MENU_SEQ})
    resp.encoding = resp.apparent_encoding or resp.encoding
    save_raw("daejeon_history_museum_list", 1, {"url": resp.url, "html": resp.text})

    seen_seq = set()
    items = []
    for m in _ITEM_RE.finditer(resp.text):
        seq = m.group("seq")
        if seq in seen_seq:
            continue
        seen_seq.add(seq)
        title = _clean(m.group("title"))
        if not title or title in ("이전", "다음", "목록"):
            continue
        items.append({"display_seq": seq, "title": title})

    if not items:
        print("[중단] 목록에서 전시 항목을 찾지 못했습니다.")
        print("       data/raw/daejeon_history_museum_list_page1.json 에 저장된 원본 HTML을 열어")
        print("       실제 목록 마크업을 확인한 뒤 _ITEM_RE 정규식을 수정해주세요.")
    return items


def fetch_detail_period(display_seq: str) -> tuple[str, str] | None:
    resp = request_with_retry(LIST_URL, {"menuSeq": MENU_SEQ, "displaySeq": display_seq})
    resp.encoding = resp.apparent_encoding or resp.encoding
    save_raw("daejeon_history_museum_detail", display_seq, {"url": resp.url, "html": resp.text})

    m = _PERIOD_RE.search(resp.text)
    if not m:
        return None
    start = m.group("start").replace(".", "").replace("-", "")
    end = m.group("end").replace(".", "").replace("-", "")
    return start, end


def _is_duplicate(cur, title: str) -> bool:
    # 사용자 요청: 소스가 달라도 이미 DB에 있는 행사와 겹치면 제외
    # (제목 정규화 후 완전 일치 기준 — 과소 판정보다 과다 삽입 방지를 우선)
    norm = title.replace(" ", "")
    rows = cur.execute("SELECT name FROM event").fetchall()
    return any(r[0].replace(" ", "") == norm for r in rows)


def collect(dry_run=False):
    items = fetch_list()
    if not items:
        return []

    conn = get_conn()
    cur = conn.cursor()

    rows, skipped_dup, skipped_no_period = [], 0, 0
    for item in items:
        if _is_duplicate(cur, item["title"]):
            print(f"[중복 제외] {item['title']}")
            skipped_dup += 1
            continue

        period = fetch_detail_period(item["display_seq"])
        if not period:
            print(f"[스킵] 기간 파싱 실패: {item['title']} (displaySeq={item['display_seq']})")
            skipped_no_period += 1
            continue
        start_date, end_date = period

        rows.append({
            "event_id": _make_event_id(item["title"], start_date, end_date),
            "name": item["title"],
            "place_name": MUSEUM_NAME,
            "address": MUSEUM_ADDRESS,
            "lat": MUSEUM_LAT,
            "lng": MUSEUM_LNG,
            "start_date": start_date,
            "end_date": end_date,
            "fee": None,
            "source_api": "daejeon_history_museum",
        })

    conn.close()

    print(f"\n파싱 결과: 신규 {len(rows)}건 / 중복 제외 {skipped_dup}건 / 기간 파싱 실패 {skipped_no_period}건")
    for r in rows:
        print(f"  - {r['name']} ({r['start_date']}~{r['end_date']})")

    if dry_run:
        print("\n[dry-run] DB에 저장하지 않았습니다.")
        return rows

    if rows:
        upsert_event(rows)
    return rows


if __name__ == "__main__":
    collect(dry_run="--dry" in sys.argv)
