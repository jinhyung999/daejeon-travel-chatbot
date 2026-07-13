# =====================================================
# event_maintenance.py
# event 테이블 정기 유지보수 스크립트 (주 1회 재수집 후 실행 권장)
#
# 1. 날짜 형식 통일: 'YYYY-MM-DD' → 'YYYYMMDD'
#    (소스별로 형식이 섞이면 문자열 비교 기반 기간 필터가 깨짐)
# 2. 종료된 행사 삭제: end_date < 오늘
# 3. 형식 검증: 8자리 숫자가 아닌 날짜가 남아있으면 경고
# =====================================================

import re
import sqlite3
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "travel.db"


def run(today: str | None = None):
    if today is None:
        today = date.today().strftime("%Y%m%d")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 1. 날짜 형식 통일 (하이픈 제거)
    cur.execute("""
        UPDATE event
        SET start_date = REPLACE(start_date, '-', ''),
            end_date   = REPLACE(end_date, '-', '')
        WHERE start_date LIKE '%-%' OR end_date LIKE '%-%'
    """)
    normalized = cur.rowcount
    print(f"날짜 형식 통일: {normalized}건")

    # 2. 종료된 행사 삭제
    ended = cur.execute(
        "SELECT event_id, name, end_date FROM event WHERE end_date < ?", (today,)
    ).fetchall()
    for eid, name, end in ended:
        print(f"  삭제: {name} (종료 {end})")
    cur.execute("DELETE FROM event WHERE end_date < ?", (today,))
    print(f"종료 행사 삭제: {cur.rowcount}건")

    conn.commit()

    # 3. 형식 검증
    invalid = [
        row for row in cur.execute("SELECT event_id, name, start_date, end_date FROM event")
        if not (re.fullmatch(r"\d{8}", row[2] or "") and re.fullmatch(r"\d{8}", row[3] or ""))
    ]
    if invalid:
        print(f"[경고] 형식이 잘못된 날짜 {len(invalid)}건:")
        for row in invalid:
            print(" ", row)
    else:
        print("형식 검증 통과: 모든 날짜가 YYYYMMDD 형식")

    remaining = cur.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    print(f"현재 유효 행사: {remaining}건")
    conn.close()


if __name__ == "__main__":
    run()
