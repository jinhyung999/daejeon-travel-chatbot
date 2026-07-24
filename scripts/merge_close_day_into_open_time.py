# =====================================================
# merge_close_day_into_open_time.py
# place 테이블 전체에서 close_day 값을 open_time에 합치고 close_day는 비운다.
#
# 병합 규칙:
#   open_time 있음 -> "{open_time} (휴무: {close_day})"
#   open_time 없음 -> close_day 값을 그대로 open_time으로 사용
#
# 사용법:
#   python scripts/merge_close_day_into_open_time.py           # dry-run
#   python scripts/merge_close_day_into_open_time.py --apply   # 실제 반영
# =====================================================

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from collectors.common import get_conn  # noqa: E402


def build_merged(open_time, close_day):
    if open_time:
        return f"{open_time} (휴무: {close_day})"
    return close_day


def main():
    apply = "--apply" in sys.argv

    conn = get_conn()
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT id, name, source_api, open_time, close_day FROM place "
        "WHERE close_day IS NOT NULL AND close_day != ''"
    ).fetchall()

    print(f"close_day가 채워진 행: {len(rows)}건\n")

    updates = []
    for id_, name, source_api, open_time, close_day in rows:
        merged = build_merged(open_time, close_day)
        updates.append((merged, id_))
        print(f"[{source_api}] {name}")
        print(f"    이전 open_time: {open_time!r}")
        print(f"    이전 close_day: {close_day!r}")
        print(f"    -> 병합 후 open_time: {merged!r}\n")

    if not apply:
        print("[dry-run] 실제 반영하려면: python scripts/merge_close_day_into_open_time.py --apply")
        conn.close()
        return

    cur.executemany("UPDATE place SET open_time = ?, close_day = NULL WHERE id = ?", updates)
    conn.commit()
    print(f"반영 완료: {len(updates)}건")
    conn.close()


if __name__ == "__main__":
    main()
