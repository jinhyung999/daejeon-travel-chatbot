import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "travel.db"

def run():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    print("=== 테이블별 건수 ===")
    for table in ["place", "event", "parking", "transport"]:
        cnt = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table}: {cnt}건")

    print("\n=== place 카테고리별 건수 ===")
    rows = cur.execute("SELECT category, COUNT(*) FROM place GROUP BY category").fetchall()

    if not rows:
        print("아직 place 데이터가 없습니다.")
    else:
        for cat, cnt in rows:
            print(f"{cat}: {cnt}건")

    print("\n=== 좌표 결측률 ===")
    total = cur.execute("SELECT COUNT(*) FROM place").fetchone()[0]

    if total == 0:
        print("place 데이터가 없어 좌표 결측률을 계산하지 않습니다.")
    else:
        missing = cur.execute("""
            SELECT COUNT(*)
            FROM place
            WHERE lat IS NULL OR lng IS NULL
        """).fetchone()[0]
        print(f"place 좌표 결측: {missing}/{total} ({missing / total * 100:.1f}%)")

    conn.close()

if __name__ == "__main__":
    run()