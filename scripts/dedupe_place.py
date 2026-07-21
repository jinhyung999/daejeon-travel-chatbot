# =====================================================
# dedupe_place.py
# place 테이블 중복 장소 병합 스크립트
#
# 중복 판정: 이름 정규화(공백 제거) 후 동일 이름이면서
#            (좌표 거리 50m 이내) 또는 (주소 완전 동일)
#            ※ 이름이 같아도 멀리 떨어져 있으면 체인점/동명 업소로 보고 유지
#
# 병합 규칙:
#   1. 소스 우선순위(tourapi > daejeon_food > daejeon_shopping
#      > mois_lodging > sbiz)가 높은 행을 대표로 남김
#      (동순위면 채워진 필드가 많은 행 → place_id 사전순)
#   2. 대표 행의 결측 필드는 제거되는 행의 값으로 채움 (정보 손실 방지)
#   3. 제거되는 행은 place_removed 백업 테이블에 원본 그대로 보관
#      (merged_into 컬럼으로 대표 place_id 기록 → 복구 가능)
#
# 사용법:
#   python scripts/dedupe_place.py           # dry-run (보고서만 출력)
#   python scripts/dedupe_place.py --apply   # 실제 병합 실행
# =====================================================

import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "travel.db"

DIST_THRESHOLD_M = 50  # 이 거리 이내의 동일 이름 장소만 중복으로 판정

SOURCE_PRIORITY = {
    "tourapi": 0,          # overview/homepage 등 보강 데이터 보유
    "daejeon_food": 1,     # 전화번호/영업시간/휴무일 보유
    "daejeon_shopping": 2,
    "mois_lodging": 3,
    "sbiz": 4,             # 이름/주소/좌표만 있는 대량 데이터
}

# 병합 시 대표 행의 결측을 채울 컬럼들
MERGE_COLUMNS = [
    "address", "open_time", "close_day", "fee", "has_parking",
    "tel", "overview", "homepage", "extra_json", "recommend",
]

ALL_COLUMNS = [
    "place_id", "name", "category", "address", "lat", "lng", "open_time",
    "close_day", "fee", "has_parking", "tel", "source_api", "extra_json",
    "overview", "homepage", "recommend",
]


def haversine(lat1, lng1, lat2, lng2):
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _norm_name(name):
    return (name or "").replace(" ", "")


def _row_score(row):
    # 동순위 소스일 때 어느 행을 남길지: 채워진 필드 수가 많은 쪽
    return sum(1 for c in MERGE_COLUMNS if row[c] not in (None, ""))


def find_clusters(rows):
    """동일 정규화 이름 그룹 내에서 거리/주소 기준으로 중복 클러스터 구성 (union-find)"""
    groups = defaultdict(list)
    for row in rows:
        groups[_norm_name(row["name"])].append(row)

    parent = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for group in groups.values():
        if len(group) < 2:
            continue
        for row in group:
            parent.setdefault(row["place_id"], row["place_id"])
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                same_addr = a["address"] and a["address"] == b["address"]
                near = (
                    a["lat"] is not None and b["lat"] is not None
                    and haversine(a["lat"], a["lng"], b["lat"], b["lng"]) <= DIST_THRESHOLD_M
                )
                if same_addr or near:
                    union(a["place_id"], b["place_id"])

    by_id = {r["place_id"]: r for g in groups.values() for r in g}
    clusters = defaultdict(list)
    for pid in parent:
        clusters[find(pid)].append(by_id[pid])
    return [c for c in clusters.values() if len(c) > 1]


def pick_winner(cluster):
    return sorted(
        cluster,
        key=lambda r: (
            SOURCE_PRIORITY.get(r["source_api"], 9),
            -_row_score(r),
            r["place_id"],
        ),
    )


def run(apply=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    rows = cur.execute(f"SELECT {', '.join(ALL_COLUMNS)} FROM place").fetchall()
    clusters = find_clusters(rows)

    print(f"중복 클러스터: {len(clusters)}개 "
          f"(총 {sum(len(c) for c in clusters)}행 → {len(clusters)}행으로 병합)\n")

    if apply:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS place_removed (
                {', '.join(c + ' TEXT' if c not in ('lat', 'lng', 'has_parking') else c + ' REAL' for c in ALL_COLUMNS)},
                merged_into TEXT,
                removed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

    removed = 0
    for cluster in clusters:
        ordered = pick_winner(cluster)
        winner, losers = ordered[0], ordered[1:]

        # 대표 행의 결측 필드를 우선순위 순서대로 제거 행에서 채움
        fills = {}
        for col in MERGE_COLUMNS:
            if winner[col] in (None, ""):
                for loser in losers:
                    if loser[col] not in (None, ""):
                        fills[col] = loser[col]
                        break

        print(f"[유지] {winner['place_id']} ({winner['source_api']}) {winner['name']}"
              + (f"  +흡수필드: {list(fills)}" if fills else ""))
        for loser in losers:
            print(f"  [제거] {loser['place_id']} ({loser['source_api']}) {loser['name']} / {loser['address']}")

        if apply:
            if fills:
                sets = ", ".join(f"{c}=?" for c in fills)
                cur.execute(f"UPDATE place SET {sets} WHERE place_id=?",
                            (*fills.values(), winner["place_id"]))
            for loser in losers:
                cur.execute(f"""
                    INSERT INTO place_removed ({', '.join(ALL_COLUMNS)}, merged_into)
                    VALUES ({', '.join('?' * len(ALL_COLUMNS))}, ?)
                """, (*[loser[c] for c in ALL_COLUMNS], winner["place_id"]))
                cur.execute("DELETE FROM place WHERE place_id=?", (loser["place_id"],))
                removed += 1

    if apply:
        conn.commit()
        total = cur.execute("SELECT COUNT(*) FROM place").fetchone()[0]
        backup = cur.execute("SELECT COUNT(*) FROM place_removed").fetchone()[0]
        print(f"\n병합 완료: {removed}행 제거(place_removed에 백업), place 총 {total}행 (백업 누적 {backup}행)")
    else:
        print("\n[dry-run] 실제 반영하려면: python scripts/dedupe_place.py --apply")
    conn.close()


if __name__ == "__main__":
    run(apply="--apply" in sys.argv)
