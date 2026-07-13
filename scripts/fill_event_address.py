# =====================================================
# fill_event_address.py
# event 테이블의 address/lat/lng 결측 보강 스크립트
#
# 1순위: place 테이블에서 place_name과 동일(또는 개칭 전 이름)한
#        장소를 찾아 주소+좌표를 복사
# 2순위: 웹 검색으로 확인한 공연장 주소 매핑(VENUE_ADDRESS)으로
#        주소만 채움 (좌표는 지오코딩 미도입으로 보류)
#
# ※ VENUE_ADDRESS는 2026-07-13 웹 검색(공연장 공식 홈페이지,
#    KOPIS, 지자체 사이트 등) 기준으로 수집한 값입니다.
#    재수집으로 새 공연장이 생기면 이 매핑에 추가하세요.
# =====================================================

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "travel.db"

# event.place_name → place.name 별칭 매핑 (개칭·표기 차이 대응)
PLACE_ALIAS = {
    "대덕구문예회관 (구. 대덕...": "대덕문예회관",  # 2023년 대덕문예회관에서 개칭
}

# 웹 검색으로 확인한 공연장 주소 (event.place_name 원문 그대로 키 사용)
VENUE_ADDRESS = {
    "대전예술의전당 아트홀": "대전광역시 서구 둔산대로 135 (만년동)",
    "대전예술의전당 앙상블홀": "대전광역시 서구 둔산대로 135 (만년동)",
    "우송예술회관": "대전광역시 동구 동대전로 171 (자양동)",
    "별별마당 우금치 관용극장": "대전광역시 중구 중앙로112번길 15 (대흥동)",
    "한밭대학교 아트홀 (하모니...": "대전광역시 유성구 동서대로 125 (덕명동)",
    "이수아트홀 [대전]": "대전광역시 서구 문정로 78",
    "복합문화공간 플랜에이 플랜...": "대전광역시 유성구 엑스포로97번길 40 (도룡동)",
    "조이마루아트홀": "대전광역시 유성구 엑스포로97번길 40 (도룡동)",  # 플랜에이 내 공연장
    "대전컨벤션센터 (DCC) ...": "대전광역시 유성구 엑스포로 107 (도룡동)",
    "대전음악창작소 공연장": "대전광역시 중구 대흥로175번길 25",
    "대전시립연정국악원 작은마당": "대전광역시 서구 둔산대로 181 (만년동)",
    "대전시립연정국악원 큰마당": "대전광역시 서구 둔산대로 181 (만년동)",
    "작은극장 다함": "대전광역시 동구 대전로448번길 70 (가오동)",
    "인터플레이 [대전]": "대전광역시 서구 대덕대로162번길 11 (갈마동)",
    "이음아트홀 [대전]": "대전광역시 유성구 도안대로 591 (봉명동)",
    "아신극장 1관 (3층)": "대전광역시 중구 대종로 458 (대흥동)",
    "아신극장 2관 (2층)": "대전광역시 중구 대종로 458 (대흥동)",
    "소극장 고도": "대전광역시 중구 중앙로112번길 13 (대흥동)",
    "상상아트홀 [대전]": "대전광역시 중구 대종로505번길 28 (선화동)",
    "목원대학교 대운동장": "대전광역시 서구 도안북로 88 (도안동)",
    "런던스테이지 [대전 중구]...": "대전광역시 서구 대덕대로175번길 16 지하 B101호",
    "김인홀(KIMIN HALL...": "대전광역시 유성구 북유성대로 93 유성선병원 지하3층",
    "드림아트홀 [대전]": "대전광역시 중구 선화서로 2 (대흥동) 지하 1층",
}


def run():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    targets = cur.execute("""
        SELECT DISTINCT place_name FROM event
        WHERE address IS NULL OR address = ''
    """).fetchall()

    from_place, from_web, unresolved = 0, 0, []
    for (venue,) in targets:
        # 1순위: place 테이블에서 동일 이름(별칭 포함) 검색 → 주소+좌표
        lookup_name = PLACE_ALIAS.get(venue, venue)
        row = cur.execute(
            "SELECT address, lat, lng FROM place WHERE name = ? AND address IS NOT NULL",
            (lookup_name,),
        ).fetchone()
        if row:
            cur.execute(
                """UPDATE event SET address=?, lat=COALESCE(lat, ?), lng=COALESCE(lng, ?)
                   WHERE place_name=? AND (address IS NULL OR address='')""",
                (row[0], row[1], row[2], venue),
            )
            from_place += cur.rowcount
            continue

        # 2순위: 웹 검색으로 확인한 주소 매핑
        if venue in VENUE_ADDRESS:
            cur.execute(
                """UPDATE event SET address=?
                   WHERE place_name=? AND (address IS NULL OR address='')""",
                (VENUE_ADDRESS[venue], venue),
            )
            from_web += cur.rowcount
            continue

        unresolved.append(venue)

    conn.commit()

    print(f"place 테이블 매칭으로 채움: {from_place}건")
    print(f"웹 검색 주소로 채움: {from_web}건")
    if unresolved:
        print(f"미해결 공연장 {len(unresolved)}곳:")
        for v in unresolved:
            cnt = cur.execute("SELECT COUNT(*) FROM event WHERE place_name=?", (v,)).fetchone()[0]
            print(f"  - {v} ({cnt}건)")

    total = cur.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    filled = cur.execute("SELECT COUNT(*) FROM event WHERE address IS NOT NULL AND address<>''").fetchone()[0]
    print(f"address 채움률: {filled}/{total}")
    conn.close()


if __name__ == "__main__":
    run()
