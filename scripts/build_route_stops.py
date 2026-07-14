# =====================================================
# build_route_stops.py
#
# 저장된 TAGO 원천 캐시로 버스 정류소/노선/경유 순서를 하나의 식별자
# 체계로 재구성한다. transport.stop_id는 TAGO nodeId를 그대로 사용한다.
#
# 기본 실행은 검증만 수행하며 DB를 바꾸지 않는다.
#   python scripts/build_route_stops.py
#   python scripts/build_route_stops.py --apply
#
# --apply 시 기존 DB를 db/backups/에 백업한 후, 검증을 통과한 전체
# 스냅샷을 단일 트랜잭션으로 교체한다. 일부 노선만 적재되는 상태는
# 허용하지 않는다.
# =====================================================

import argparse
import glob
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).parent.parent
DB_PATH = ROOT_DIR / "db" / "travel.db"
RAW_DIR = ROOT_DIR / "data" / "raw"
BACKUP_DIR = ROOT_DIR / "db" / "backups"


def _response_items(payload: dict, source: Path) -> tuple[list[dict], dict]:
    response = payload.get("response", {})
    header = response.get("header", {})
    result_code = str(header.get("resultCode", ""))
    if result_code not in ("0", "00"):
        raise ValueError(
            f"TAGO 응답 오류: {source.name}: "
            f"{result_code} {header.get('resultMsg', '')}"
        )

    body = response.get("body", {})
    if not isinstance(body, dict):
        raise ValueError(f"TAGO 응답 body 형식 오류: {source.name}")

    items = body.get("items", {})
    item = items.get("item", []) if items and not isinstance(items, str) else []
    if isinstance(item, dict):
        item = [item]
    return item, body


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _load_route_meta() -> tuple[dict[str, dict], list[Path]]:
    paths = [Path(p) for p in sorted(glob.glob(str(RAW_DIR / "tago_route_list_page*.json")))]
    if not paths:
        raise ValueError("TAGO 노선 목록 캐시가 없습니다.")

    routes = {}
    declared_totals = set()
    for path in paths:
        items, body = _response_items(_load_json(path), path)
        declared_totals.add(int(body.get("totalCount", 0)))
        for item in items:
            route_id = str(item.get("routeid") or "")
            route_no = item.get("routeno")
            if not route_id or route_no is None:
                raise ValueError(f"노선 필수 필드 누락: {path.name}: {item}")
            if route_id in routes:
                raise ValueError(f"중복 routeId: {route_id}")
            routes[route_id] = {
                "route_id": route_id,
                "route_no": str(route_no),
                "route_type": item.get("routetp"),
            }

    if len(declared_totals) != 1 or declared_totals != {len(routes)}:
        raise ValueError(
            f"노선 목록 페이지 누락 가능성: totalCount={declared_totals}, "
            f"실제 고유 노선={len(routes)}"
        )
    return routes, paths


def build_snapshot() -> dict:
    routes, route_list_paths = _load_route_meta()
    stop_paths = [Path(p) for p in sorted(glob.glob(str(RAW_DIR / "tago_route_stops_*.json")))]
    if not stop_paths:
        raise ValueError("TAGO 노선별 정류소 캐시가 없습니다.")

    stops = {}
    route_stop_rows = []
    route_stop_keys = set()
    routes_with_stops = set()
    stop_route_labels = defaultdict(set)

    for path in stop_paths:
        items, body = _response_items(_load_json(path), path)
        total_count = int(body.get("totalCount", len(items)))
        if total_count != len(items):
            raise ValueError(
                f"정류소 페이지 누락 가능성: {path.name}: "
                f"totalCount={total_count}, items={len(items)}"
            )
        if not items:
            raise ValueError(f"빈 노선별 정류소 응답: {path.name}")

        file_route_ids = {str(item.get("routeid") or "") for item in items}
        if len(file_route_ids) != 1:
            raise ValueError(f"한 파일에 여러 routeId 존재: {path.name}: {file_route_ids}")
        route_id = next(iter(file_route_ids))
        if route_id not in routes:
            raise ValueError(f"노선 목록에 없는 routeId: {route_id}")
        routes_with_stops.add(route_id)

        route = routes[route_id]
        route_label = f"{route['route_no']}({route['route_type']})"
        for item in items:
            required = ("nodeid", "nodenm", "gpslati", "gpslong", "nodeord", "updowncd")
            missing = [key for key in required if item.get(key) is None or item.get(key) == ""]
            if missing:
                raise ValueError(f"정류소 필수 필드 누락: {path.name}: {missing}: {item}")

            node_id = str(item["nodeid"])
            stop = {
                "stop_id": node_id,
                "name": str(item["nodenm"]),
                "type": "bus",
                "lat": float(item["gpslati"]),
                "lng": float(item["gpslong"]),
                "source_api": "tago",
            }
            previous = stops.get(node_id)
            if previous and (
                previous["name"] != stop["name"]
                or previous["lat"] != stop["lat"]
                or previous["lng"] != stop["lng"]
            ):
                raise ValueError(
                    f"같은 TAGO nodeId의 속성이 서로 다름: {node_id}: "
                    f"{previous} != {stop}"
                )
            stops[node_id] = stop
            stop_route_labels[node_id].add(route_label)

            key = (route_id, int(item["updowncd"]), int(item["nodeord"]))
            if key in route_stop_keys:
                raise ValueError(f"중복 노선·방향·순번: {key}")
            route_stop_keys.add(key)
            route_stop_rows.append((*key, node_id))

    missing_route_files = set(routes) - routes_with_stops
    extra_route_files = routes_with_stops - set(routes)
    if missing_route_files or extra_route_files:
        raise ValueError(
            f"노선별 정류소 캐시 불일치: missing={sorted(missing_route_files)}, "
            f"extra={sorted(extra_route_files)}"
        )

    source_paths = route_list_paths + stop_paths
    source_collected_at = datetime.fromtimestamp(
        max(path.stat().st_mtime for path in source_paths)
    ).date().isoformat()

    transport_rows = []
    for node_id in sorted(stops):
        stop = stops[node_id]
        transport_rows.append(
            (
                stop["stop_id"],
                stop["name"],
                stop["type"],
                stop["lat"],
                stop["lng"],
                ",".join(sorted(stop_route_labels[node_id])),
                stop["source_api"],
            )
        )

    route_rows = [
        (
            route["route_id"],
            route["route_no"],
            route["route_type"],
            source_collected_at,
        )
        for route in sorted(routes.values(), key=lambda row: row["route_id"])
    ]

    return {
        "transport_rows": transport_rows,
        "route_rows": route_rows,
        "route_stop_rows": sorted(route_stop_rows),
        "source_collected_at": source_collected_at,
        "route_list_files": len(route_list_paths),
        "route_stop_files": len(stop_paths),
    }


def print_summary(snapshot: dict) -> None:
    print("TAGO 단일 스냅샷 검증 완료")
    print(f"  원천 수집일: {snapshot['source_collected_at']}")
    print(f"  노선 목록 캐시: {snapshot['route_list_files']}개")
    print(f"  노선별 정류소 캐시: {snapshot['route_stop_files']}개")
    print(f"  transport(TAGO nodeId): {len(snapshot['transport_rows'])}개")
    print(f"  bus_route: {len(snapshot['route_rows'])}개")
    print(f"  bus_route_stop: {len(snapshot['route_stop_rows'])}개")


def _backup_database() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"travel_pre_tago_{timestamp}.db"
    source = sqlite3.connect(DB_PATH)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup_path


def apply_snapshot(snapshot: dict) -> Path:
    backup_path = _backup_database()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN IMMEDIATE")

        for statement in (
            "DROP TABLE IF EXISTS bus_route_stop_tago_new",
            "DROP TABLE IF EXISTS bus_route_tago_new",
            "DROP TABLE IF EXISTS transport_tago_new",
            """CREATE TABLE transport_tago_new (
              stop_id    TEXT PRIMARY KEY,
              name       TEXT NOT NULL,
              type       TEXT NOT NULL DEFAULT 'bus',
              lat        REAL NOT NULL,
              lng        REAL NOT NULL,
              routes     TEXT,
              source_api TEXT NOT NULL DEFAULT 'tago' CHECK (source_api = 'tago')
            )""",
            """CREATE TABLE bus_route_tago_new (
              route_id     TEXT PRIMARY KEY,
              route_no     TEXT NOT NULL,
              route_type   TEXT,
              collected_at TEXT NOT NULL
            )""",
            """CREATE TABLE bus_route_stop_tago_new (
              route_id   TEXT NOT NULL REFERENCES bus_route_tago_new(route_id) ON DELETE CASCADE,
              updowncd   INTEGER NOT NULL,
              node_order INTEGER NOT NULL,
              stop_id    TEXT NOT NULL REFERENCES transport_tago_new(stop_id),
              PRIMARY KEY (route_id, updowncd, node_order)
            )""",
        ):
            conn.execute(statement)

        conn.executemany(
            "INSERT INTO transport_tago_new "
            "(stop_id, name, type, lat, lng, routes, source_api) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            snapshot["transport_rows"],
        )
        conn.executemany(
            "INSERT INTO bus_route_tago_new "
            "(route_id, route_no, route_type, collected_at) VALUES (?, ?, ?, ?)",
            snapshot["route_rows"],
        )
        conn.executemany(
            "INSERT INTO bus_route_stop_tago_new "
            "(route_id, updowncd, node_order, stop_id) VALUES (?, ?, ?, ?)",
            snapshot["route_stop_rows"],
        )

        expected = (
            len(snapshot["transport_rows"]),
            len(snapshot["route_rows"]),
            len(snapshot["route_stop_rows"]),
        )
        actual = (
            conn.execute("SELECT COUNT(*) FROM transport_tago_new").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM bus_route_tago_new").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM bus_route_stop_tago_new").fetchone()[0],
        )
        if actual != expected:
            raise RuntimeError(f"스냅샷 적재 건수 불일치: expected={expected}, actual={actual}")

        for statement in (
            "DROP TABLE bus_route_stop",
            "DROP TABLE bus_route",
            "DROP TABLE transport",
            "ALTER TABLE transport_tago_new RENAME TO transport",
            "ALTER TABLE bus_route_tago_new RENAME TO bus_route",
            "ALTER TABLE bus_route_stop_tago_new RENAME TO bus_route_stop",
            "CREATE INDEX idx_transport_latlng ON transport(lat, lng)",
            "CREATE INDEX idx_route_stop_stop ON bus_route_stop(stop_id)",
        ):
            conn.execute(statement)
        conn.commit()

        conn.execute("PRAGMA foreign_keys=ON")
        fk_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_errors:
            raise RuntimeError(f"외래키 검증 실패: {fk_errors[:10]}")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite integrity_check 실패")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return backup_path


def main() -> None:
    parser = argparse.ArgumentParser(description="TAGO 버스 데이터 단일 스냅샷 빌드")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="검증된 TAGO 스냅샷으로 transport/bus_route/bus_route_stop을 교체",
    )
    args = parser.parse_args()

    snapshot = build_snapshot()
    print_summary(snapshot)
    if not args.apply:
        print("[dry-run] DB는 변경하지 않았습니다. 실제 반영: python scripts/build_route_stops.py --apply")
        return

    backup_path = apply_snapshot(snapshot)
    print(f"TAGO 단일화 반영 완료. 백업: {backup_path}")


if __name__ == "__main__":
    main()
