# =====================================================
# reclassify_attraction.py
# place.category='attraction' 중 sbiz(상가정보) 기반 생활 오락시설을
# 'entertainment' 카테고리로 분리하는 스크립트
#
# 배경: sbiz_market.py가 상가정보 중분류 "유원지·오락"/"도서관·사적지"를
#       전부 attraction으로 매핑해서, 노래방/PC방/독서실 등 일상 생활시설
#       2,073건이 TourAPI 실제 관광지(98건)와 섞여 있었음.
#       실제 소분류를 까보면 "유원지·오락"/"도서관·사적지"라는 중분류명과
#       달리 노래방·PC방·복권방 등 관광과 무관한 업종이 대부분이었음.
#
# 분리 기준 (소분류 기준, 삭제 아님 — category 값만 변경):
#   entertainment로 이동: 노래방, PC방, 독서실/스터디 카페, 전자 게임장,
#     복권 발행/판매업, 비디오방, 바둑/장기/체스 경기 운영업,
#     기타 오락장, 기타 오락관련 서비스업
#   attraction 유지: 수상/해양 레저업, 낚시터 운영업 (실제 레저/관광 성격)
#
# ※ "기타 오락장"/"기타 오락관련 서비스업"은 실내 키즈카페·VR방 등일
#    가능성도 있어 100% 확신은 아님 — 소분류가 모호한 catch-all이라
#    보수적으로 entertainment 쪽에 분류함(관광지로 잘못 노출되는 리스크가
#    더 크다고 판단). 필요시 아래 ENTERTAINMENT_SUBCATS만 조정하면 됨.
#
# 사용법:
#   python scripts/reclassify_attraction.py           # dry-run (건수만 출력)
#   python scripts/reclassify_attraction.py --apply    # 실제 반영
# =====================================================

import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "travel.db"

ENTERTAINMENT_SUBCATS = {
    "노래방",
    "PC방",
    "독서실/스터디 카페",
    "전자 게임장",
    "복권 발행/판매업",
    "비디오방",
    "바둑/장기/체스 경기 운영업",
    "기타 오락장",
    "기타 오락관련 서비스업",
}


def run(apply=False):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    rows = cur.execute("""
        SELECT place_id, name, extra_json FROM place
        WHERE category='attraction' AND source_api='sbiz'
    """).fetchall()

    to_move, kept, unmapped = [], [], []
    for place_id, name, extra_json in rows:
        sub = json.loads(extra_json or "{}").get("소분류")
        if sub in ENTERTAINMENT_SUBCATS:
            to_move.append((place_id, name, sub))
        elif sub:
            kept.append((place_id, name, sub))
        else:
            unmapped.append((place_id, name, sub))

    from collections import Counter
    moved_breakdown = Counter(sub for _, _, sub in to_move)
    print("entertainment로 이동 대상:")
    for sub, cnt in moved_breakdown.most_common():
        print(f"  {cnt:5d}  {sub}")
    print(f"\nattraction 유지: {len(kept)}건 ({Counter(s for _,_,s in kept)})")
    if unmapped:
        print(f"소분류 미상(변경 없음): {len(unmapped)}건")

    print(f"\n총 {len(to_move)}건을 attraction → entertainment로 변경 예정")

    if apply:
        cur.executemany(
            "UPDATE place SET category='entertainment' WHERE place_id=?",
            [(pid,) for pid, _, _ in to_move],
        )
        conn.commit()
        remain = cur.execute(
            "SELECT COUNT(*) FROM place WHERE category='attraction'"
        ).fetchone()[0]
        ent = cur.execute(
            "SELECT COUNT(*) FROM place WHERE category='entertainment'"
        ).fetchone()[0]
        print(f"\n반영 완료. attraction 잔여 {remain}건 / entertainment {ent}건")
    else:
        print("\n[dry-run] 실제 반영하려면: python scripts/reclassify_attraction.py --apply")

    conn.close()


if __name__ == "__main__":
    run(apply="--apply" in sys.argv)
