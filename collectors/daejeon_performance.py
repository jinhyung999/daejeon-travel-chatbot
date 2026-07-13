# =====================================================
# daejeon_performance.py
# 대전 공연 일정 파일(csv/daejeon_performances.json)을 읽어
# event 테이블에 저장하는 모듈
#
# 주요 기능
# 1. 공연 일정 JSON 파일 로드 (API가 아닌 로컬 파일 소스)
# 2. 원본을 raw_daejeon_performance 테이블에 그대로 보존
#    (raw_daejeon_food / raw_daejeon_shopping 과 같은 패턴)
# 3. 기간 문자열("2026.07.28 ~ 2026.07.28") → YYYYMMDD 정규화
#    ("~ 오픈런" 공연은 종료일 미정이므로 sentinel '99991231' 사용)
# 4. 공연장 이름으로 place 테이블에서 좌표 조회 (확실한 경우만)
# 5. SQLite event 테이블에 저장(Upsert)
# =====================================================

import hashlib
import json
import re
from pathlib import Path

from common import get_conn, upsert_event

# 공연 일정 원본 파일 경로
SOURCE_PATH = Path(__file__).parent.parent / "csv" / "daejeon_performances.json"

# "2026.07.28 ~ 2026.07.28" 또는 "2025.10.24 ~ 오픈런" 형식
PERIOD_RE = re.compile(
    r"^(?P<start>\d{4}\.\d{2}\.\d{2})\s*~\s*(?:(?P<end>\d{4}\.\d{2}\.\d{2})|오픈런)$"
)

# 오픈런(종료일 미정) 공연의 end_date sentinel — 날짜 비교 쿼리가 그대로 동작하도록 함
OPEN_RUN_END = "99991231"


def _make_event_id(title: str, period: str, venue: str) -> str:
    # 원본에 고유 ID가 없어 제목+기간+장소 해시로 생성 (daejeon_food와 동일 방식)
    digest = hashlib.sha1(f"{title}|{period}|{venue}".encode("utf-8")).hexdigest()[:16]
    return f"daejeon_perf_{digest}"


def _parse_period(period: str) -> tuple[str, str] | None:
    match = PERIOD_RE.match(period.strip())
    if not match:
        return None
    start = match.group("start").replace(".", "")
    end = match.group("end").replace(".", "") if match.group("end") else OPEN_RUN_END
    return start, end


def _venue_candidates(venue: str) -> list[str]:
    # 원본 공연장 이름은 말줄임(...)·부가표기([대전], (3층) 등)가 섞여 있어
    # place 테이블 검색용 후보 문자열을 만들어 순서대로 시도
    key = re.sub(r"\[.*?\]|\(.*?\)|\.\.\.", " ", venue)
    key = re.sub(r"\s+", " ", key).strip()
    first = key.split(" ")[0] if key else ""
    candidates = [key, first]
    # "대전평송청소년문화센터" → "평송청소년문화센터" 같은 접두어 차이 대응
    if first.startswith("대전") and len(first) > 6:
        candidates.append(first[2:])
    return [c for c in candidates if len(c) >= 4]


def _lookup_venue(cur, venue: str) -> tuple[float, float, str] | None:
    # 관광지/문화시설 카테고리에서 이름 전방일치로 검색하고,
    # 후보가 정확히 1곳일 때만 좌표를 사용 (오매칭 방지)
    for cand in _venue_candidates(venue):
        rows = cur.execute(
            """
            SELECT DISTINCT lat, lng, address FROM place
            WHERE name LIKE ? AND category IN ('culture', 'attraction')
              AND lat IS NOT NULL
            LIMIT 2
            """,
            (cand + "%",),
        ).fetchall()
        if len(rows) == 1:
            return rows[0]
    return None


def _save_raw_table(items: list[dict]):
    # 원본 필드를 그대로 보존하는 raw 테이블 (팀 컨벤션: 원본 필드명 + collected_at)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS raw_daejeon_performance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            genre TEXT, title TEXT, period TEXT, venue TEXT,
            planning TEXT, production TEXT, host TEXT, organizer TEXT,
            collected_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.executemany(
        """
        INSERT INTO raw_daejeon_performance
            (genre, title, period, venue, planning, production, host, organizer)
        VALUES (:genre, :title, :period, :venue, :planning, :production, :host, :organizer)
        """,
        items,
    )
    conn.commit()
    conn.close()
    print(f"raw_daejeon_performance 저장: {len(items)}건")


def collect():
    items = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))

    _save_raw_table(items)

    conn = get_conn()
    cur = conn.cursor()

    rows, skipped = [], 0
    for it in items:
        parsed = _parse_period(it["period"])
        if not parsed:
            print(f"[skip] 기간 파싱 실패: {it['title']} / {it['period']}")
            skipped += 1
            continue
        start_date, end_date = parsed

        venue = it["venue"].strip()
        matched = _lookup_venue(cur, venue)
        lat, lng, address = matched if matched else (None, None, None)

        rows.append({
            "event_id": _make_event_id(it["title"], it["period"], venue),
            "name": it["title"],
            "place_name": venue,
            "address": address,
            "lat": lat,
            "lng": lng,
            "start_date": start_date,
            "end_date": end_date,
            "fee": None,
            "source_api": "daejeon_performance",
        })

    conn.close()
    upsert_event(rows)
    matched_cnt = sum(1 for r in rows if r["lat"] is not None)
    print(f"공연 수집 완료: {len(rows)}건 저장, {skipped}건 스킵, 좌표 매칭 {matched_cnt}건")
    return rows


if __name__ == "__main__":
    collect()
