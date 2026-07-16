"""Build and atomically apply Daejeon Metro Line 1 data."""

from __future__ import annotations

import argparse
import csv
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Iterable


LINE_ID = "DJM1"
EXPECTED_STATION_NUMBERS = list(range(101, 123))
TRANSFER_RADIUS_M = 600.0

SUBWAY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS subway_line (
  line_id TEXT PRIMARY KEY,
  name_ko TEXT NOT NULL,
  name_en TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subway_station (
  station_id TEXT PRIMARY KEY,
  line_id TEXT NOT NULL REFERENCES subway_line(line_id) ON DELETE CASCADE,
  station_no INTEGER NOT NULL UNIQUE CHECK (station_no BETWEEN 101 AND 122),
  name_ko TEXT NOT NULL,
  name_en TEXT NOT NULL,
  address TEXT,
  lat REAL NOT NULL CHECK (lat BETWEEN -90 AND 90),
  lng REAL NOT NULL CHECK (lng BETWEEN -180 AND 180),
  coordinate_source TEXT NOT NULL CHECK (coordinate_source = 'derived_bus_stops')
);
CREATE INDEX IF NOT EXISTS idx_subway_station_line_no ON subway_station(line_id, station_no);
CREATE INDEX IF NOT EXISTS idx_subway_station_latlng ON subway_station(lat, lng);
CREATE TABLE IF NOT EXISTS subway_edge (
  line_id TEXT NOT NULL REFERENCES subway_line(line_id) ON DELETE CASCADE,
  sequence INTEGER NOT NULL CHECK (sequence BETWEEN 1 AND 21),
  from_station_id TEXT NOT NULL REFERENCES subway_station(station_id),
  to_station_id TEXT NOT NULL REFERENCES subway_station(station_id),
  travel_seconds INTEGER NOT NULL CHECK (travel_seconds > 0),
  distance_km REAL NOT NULL CHECK (distance_km > 0),
  PRIMARY KEY (line_id, sequence),
  UNIQUE (line_id, from_station_id, to_station_id),
  CHECK (from_station_id <> to_station_id)
);
CREATE INDEX IF NOT EXISTS idx_subway_edge_from ON subway_edge(from_station_id);
CREATE INDEX IF NOT EXISTS idx_subway_edge_to ON subway_edge(to_station_id);
CREATE TABLE IF NOT EXISTS subway_schedule (
  station_id TEXT NOT NULL REFERENCES subway_station(station_id) ON DELETE CASCADE,
  day_type TEXT NOT NULL CHECK (day_type IN ('01', '02', '03')),
  direction TEXT NOT NULL CHECK (direction IN ('up', 'down')),
  train_no TEXT NOT NULL,
  arrival_time TEXT CHECK (
    arrival_time IS NULL OR (length(arrival_time) = 6 AND arrival_time NOT GLOB '*[^0-9]*')
  ),
  departure_time TEXT NOT NULL CHECK (
    length(departure_time) = 6 AND departure_time NOT GLOB '*[^0-9]*'
  ),
  PRIMARY KEY (station_id, day_type, direction, train_no, departure_time)
);
CREATE INDEX IF NOT EXISTS idx_subway_schedule_lookup
  ON subway_schedule(station_id, day_type, direction, departure_time);
CREATE TABLE IF NOT EXISTS transit_transfer (
  station_id TEXT NOT NULL REFERENCES subway_station(station_id) ON DELETE CASCADE,
  stop_id TEXT NOT NULL REFERENCES transport(stop_id) ON DELETE CASCADE,
  distance_m REAL NOT NULL CHECK (distance_m >= 0 AND distance_m <= 600),
  walking_minutes REAL NOT NULL CHECK (walking_minutes >= 0),
  PRIMARY KEY (station_id, stop_id)
);
CREATE INDEX IF NOT EXISTS idx_transit_transfer_stop ON transit_transfer(stop_id);
"""


def _normalized_header(value: str) -> str:
    return "".join(character.lower() for character in value if character.isalnum())


def _field(row: dict[str, str], aliases: Iterable[str], position: int) -> str:
    normalized = {_normalized_header(key): value for key, value in row.items() if key is not None}
    for alias in aliases:
        key = _normalized_header(alias)
        if key in normalized and normalized[key] not in (None, ""):
            return normalized[key]
    values = list(row.values())
    if position < len(values) and values[position] not in (None, ""):
        return values[position]
    raise ValueError(f"CSV field is missing: {next(iter(aliases))}")


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with Path(path).open("r", encoding="cp949", newline="") as handle:
            return list(csv.DictReader(handle))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise ValueError(f"cannot read CP949 CSV {path}: {exc}") from exc


def _edge_travel_seconds(row: dict[str, str]) -> int:
    normalized = {_normalized_header(key): value for key, value in row.items() if key is not None}
    for alias in ("travel_seconds", "소요시간초"):
        value = normalized.get(_normalized_header(alias))
        if value not in (None, ""):
            return int(float(value.strip()))

    compact_value = normalized.get(_normalized_header("소요시간"))
    if compact_value not in (None, ""):
        compact = compact_value.strip()
        if not compact.isascii() or not compact.isdigit() or len(compact) > 4:
            raise ValueError("edge compact MMSS must contain one to four digits")
        minutes, seconds = divmod(int(compact), 100)
        if seconds >= 60 or minutes * 60 + seconds <= 0:
            raise ValueError("edge compact MMSS must be a positive valid time")
        return minutes * 60 + seconds

    return int(float(_field(row, ("travel_seconds",), 3).strip()))


def _normalize_korean_name(value: str) -> str:
    value = value.strip()
    opening = value.find("(")
    if opening < 0:
        return "".join(value.split())
    main = "".join(value[:opening].split())
    return main + value[opening:].strip()


def _base_station_name(name: str) -> str:
    return name.split("(", 1)[0]


def _normalize_stop_name(name: str) -> str:
    return "".join(name.split())


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    earth_radius_m = 6_371_000.0
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(delta_lng / 2) ** 2
    )
    return 2 * earth_radius_m * math.asin(math.sqrt(a))


def _load_bus_stops(db_path: Path) -> list[dict[str, object]]:
    try:
        database_uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(database_uri, uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT stop_id, name, lat, lng FROM transport WHERE type = 'bus'"
        ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError(f"cannot load bus stops from {db_path}: {exc}") from exc
    finally:
        if "connection" in locals():
            connection.close()
    return [dict(row) for row in rows]


def _validate_station_numbers(stations: list[dict[str, object]]) -> None:
    numbers = [int(station["station_no"]) for station in stations]
    if len(stations) != 22 or sorted(numbers) != EXPECTED_STATION_NUMBERS or len(set(numbers)) != 22:
        raise ValueError("stations must be exactly the sequential numbers 101 through 122")


def _validate_snapshot(snapshot: dict) -> None:
    lines = snapshot.get("lines", [])
    stations = snapshot.get("stations", [])
    edges = snapshot.get("edges", [])
    transfers = snapshot.get("transfers", [])
    if len(lines) != 1 or lines[0].get("line_id") != LINE_ID:
        raise ValueError("snapshot must contain line DJM1")
    _validate_station_numbers(stations)
    if len(edges) != 21:
        raise ValueError("snapshot must contain exactly 21 edges")
    expected_edges = {
        (sequence, f"DJM{100 + sequence}", f"DJM{101 + sequence}")
        for sequence in range(1, 22)
    }
    actual_edges = {
        (int(edge["sequence"]), edge["from_station_id"], edge["to_station_id"])
        for edge in edges
    }
    if actual_edges != expected_edges or len(actual_edges) != 21:
        raise ValueError("edges must be the 21 unique adjacent station pairs")
    expected_station_ids = {f"DJM{station_no}" for station_no in EXPECTED_STATION_NUMBERS}
    transfer_station_ids = {row["station_id"] for row in transfers}
    unknown_station_ids = transfer_station_ids - expected_station_ids
    if unknown_station_ids:
        raise ValueError(
            "transfers reference unknown station IDs: " + ", ".join(sorted(unknown_station_ids))
        )
    missing_station_ids = expected_station_ids - transfer_station_ids
    if missing_station_ids:
        raise ValueError(
            "transfer coverage must include all stations; missing: "
            + ", ".join(sorted(missing_station_ids))
        )
    if any(not (0 <= float(row["distance_m"]) <= TRANSFER_RADIUS_M) for row in transfers):
        raise ValueError("transfer distance exceeds 600 metres")
    for row in snapshot.get("schedules", []):
        for field in ("departure_time", "arrival_time"):
            value = row.get(field)
            if field == "arrival_time" and value is None:
                continue
            if (
                not isinstance(value, str)
                or len(value) != 6
                or not value.isascii()
                or not value.isdigit()
                or int(value[:2]) > 23
                or int(value[2:4]) > 59
                or int(value[4:6]) > 59
            ):
                raise ValueError(f"schedule {field} must be a valid HHMMSS time")


def build_snapshot(station_csv: Path, edge_csv: Path, db_path: Path) -> dict:
    """Parse and validate source files without changing the database."""
    station_rows = _read_csv(station_csv)
    edge_rows = _read_csv(edge_csv)
    bus_stops = _load_bus_stops(db_path)

    stations: list[dict[str, object]] = []
    for row in station_rows:
        try:
            station_no = int(_field(row, ("station_no", "역번호", "정거장번호"), 0).strip())
            name_ko = _normalize_korean_name(
                _field(row, ("korean_name", "역명", "한 글", "한글", "한글역명", "정거장명"), 2)
            )
            name_en = _field(
                row, ("english_name", "영문명", "영문역명", "로 마 자", "로마자"), 4
            ).strip()
            address = _field(row, ("address", "주소", "도로명주소", "소재지도로명주소"), 3).strip()
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid station row: {row}") from exc
        base = _base_station_name(name_ko)
        suffix_candidates = [
            stop for stop in bus_stops if base + "역" in _normalize_stop_name(str(stop["name"]))
        ]
        exact_candidates = [
            stop for stop in bus_stops if _normalize_stop_name(str(stop["name"])) == base
        ]
        candidates = suffix_candidates or exact_candidates
        if not candidates:
            raise ValueError(f"no bus-stop coordinate candidates for station {station_no} {base}")
        stations.append(
            {
                "station_id": f"DJM{station_no}",
                "line_id": LINE_ID,
                "station_no": station_no,
                "name_ko": name_ko,
                "name_en": name_en,
                "address": address,
                "lat": median(float(stop["lat"]) for stop in candidates),
                "lng": median(float(stop["lng"]) for stop in candidates),
                "coordinate_source": "derived_bus_stops",
            }
        )
    _validate_station_numbers(stations)
    stations.sort(key=lambda row: int(row["station_no"]))

    edges: list[dict[str, object]] = []
    seen_pairs: set[tuple[int, int]] = set()
    seen_sequences: set[int] = set()
    for row in edge_rows:
        try:
            sequence = int(_field(row, ("sequence", "순번", "연번", "구간순번"), 0).strip())
            from_no = int(_field(row, ("from_station_no", "출발역번호", "시작역번호", "from"), 1).strip())
            to_no = int(_field(row, ("to_station_no", "도착역번호", "종료역번호", "to"), 2).strip())
            travel_seconds = _edge_travel_seconds(row)
            distance_km = float(_field(row, ("distance_km", "거리km", "거리"), 4).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid edge row: {row}") from exc
        if sequence == 20 and (from_no, to_no) == (120, 120):
            to_no = 121
        expected_pair = (100 + sequence, 101 + sequence)
        pair = (from_no, to_no)
        if sequence not in range(1, 22) or pair != expected_pair:
            raise ValueError(f"edge sequence {sequence} must connect adjacent stations")
        if sequence in seen_sequences or pair in seen_pairs:
            raise ValueError(f"duplicate edge at sequence {sequence}")
        if travel_seconds <= 0 or distance_km <= 0:
            raise ValueError("edge time and distance must be positive")
        seen_sequences.add(sequence)
        seen_pairs.add(pair)
        edges.append(
            {
                "line_id": LINE_ID,
                "sequence": sequence,
                "from_station_id": f"DJM{from_no}",
                "to_station_id": f"DJM{to_no}",
                "travel_seconds": travel_seconds,
                "distance_km": distance_km,
            }
        )
    edges.sort(key=lambda row: int(row["sequence"]))

    transfers = []
    for station in stations:
        for stop in bus_stops:
            distance_m = _haversine_m(
                float(station["lat"]),
                float(station["lng"]),
                float(stop["lat"]),
                float(stop["lng"]),
            )
            if distance_m <= TRANSFER_RADIUS_M:
                transfers.append(
                    {
                        "station_id": station["station_id"],
                        "stop_id": stop["stop_id"],
                        "distance_m": distance_m,
                        "walking_minutes": distance_m * 1.25 / 4_500 * 60,
                    }
                )

    snapshot = {
        "lines": [{"line_id": LINE_ID, "name_ko": "대전도시철도 1호선", "name_en": "Daejeon Metro Line 1"}],
        "stations": stations,
        "edges": edges,
        "schedules": [],
        "transfers": transfers,
    }
    _validate_snapshot(snapshot)
    return snapshot


def apply_snapshot(snapshot: dict, db_path: Path) -> Path:
    """Replace all subway rows in one immediate transaction and return the backup path."""
    _validate_snapshot(snapshot)
    db_path = Path(db_path)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = db_path.with_name(f"{db_path.name}.backup-{timestamp}")

    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        source_uri = f"{db_path.resolve().as_uri()}?mode=ro"
        source_connection = sqlite3.connect(source_uri, uri=True)
        backup_connection = sqlite3.connect(backup_path)
        try:
            source_connection.backup(backup_connection)
        finally:
            backup_connection.close()
            source_connection.close()
        # Recreate all related tables so schema changes migrate existing databases.
        # executescript() implicitly commits, so execute statements individually to
        # keep the migration and replacement inside this immediate transaction.
        for table in ("transit_transfer", "subway_schedule", "subway_edge", "subway_station", "subway_line"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        for statement in SUBWAY_SCHEMA_SQL.split(";"):
            if statement.strip():
                connection.execute(statement)
        connection.executemany(
            "INSERT INTO subway_line (line_id, name_ko, name_en) VALUES (:line_id, :name_ko, :name_en)",
            snapshot["lines"],
        )
        connection.executemany(
            "INSERT INTO subway_station "
            "(station_id, line_id, station_no, name_ko, name_en, address, lat, lng, coordinate_source) "
            "VALUES (:station_id, :line_id, :station_no, :name_ko, :name_en, :address, :lat, :lng, :coordinate_source)",
            snapshot["stations"],
        )
        connection.executemany(
            "INSERT INTO subway_edge "
            "(line_id, sequence, from_station_id, to_station_id, travel_seconds, distance_km) "
            "VALUES (:line_id, :sequence, :from_station_id, :to_station_id, :travel_seconds, :distance_km)",
            snapshot["edges"],
        )
        connection.executemany(
            "INSERT INTO subway_schedule "
            "(station_id, day_type, direction, train_no, arrival_time, departure_time) "
            "VALUES (:station_id, :day_type, :direction, :train_no, :arrival_time, :departure_time)",
            snapshot.get("schedules", []),
        )
        connection.executemany(
            "INSERT INTO transit_transfer (station_id, stop_id, distance_m, walking_minutes) "
            "VALUES (:station_id, :stop_id, :distance_m, :walking_minutes)",
            snapshot["transfers"],
        )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("foreign key validation failed")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("database integrity validation failed")
        if connection.execute("SELECT count(*) FROM subway_station").fetchone()[0] != 22:
            raise ValueError("applied station count is not 22")
        if connection.execute("SELECT count(*) FROM subway_edge").fetchone()[0] != 21:
            raise ValueError("applied edge count is not 21")
        uncovered = connection.execute(
            "SELECT s.station_id FROM subway_station AS s "
            "LEFT JOIN transit_transfer AS t ON t.station_id = s.station_id "
            "GROUP BY s.station_id HAVING count(t.station_id) = 0 ORDER BY s.station_id"
        ).fetchall()
        if uncovered:
            raise ValueError(
                "subway stations without transfer coverage: "
                + ", ".join(row[0] for row in uncovered)
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station-csv", type=Path, required=True)
    parser.add_argument("--edge-csv", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="write the validated snapshot to the database")
    args = parser.parse_args()
    snapshot = build_snapshot(args.station_csv, args.edge_csv, args.db)
    print(f"validated {len(snapshot['stations'])} stations, {len(snapshot['edges'])} edges, "
          f"{len(snapshot['transfers'])} transfers")
    if args.apply:
        print(f"applied; backup: {apply_snapshot(snapshot, args.db)}")
    else:
        print("dry-run only; pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
