import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.export_giftshop_detail_review import FIELDNAMES
from scripts.recommendation_json import dump_json_object, load_json_object


@dataclass(frozen=True)
class ImportStats:
    approved: int
    updated: int
    pending: int
    rejected: int


def _text(row: dict, key: str) -> str:
    return str(row.get(key) or "").strip()


def read_review_rows(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        missing = [
            field for field in FIELDNAMES if field not in (reader.fieldnames or [])
        ]
        if missing:
            raise ValueError(f"review CSV missing columns: {', '.join(missing)}")
        rows = list(reader)
    ids = [_text(row, "place_id") for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("review CSV contains duplicate place_id")
    return rows


def _validate_approved(row: dict) -> None:
    verified_at = _text(row, "verified_at")
    try:
        datetime.strptime(verified_at, "%Y-%m-%d")
    except ValueError as error:
        raise ValueError(
            f"invalid verified_at for {_text(row, 'place_id')}"
        ) from error
    if _text(row, "tel") and not _text(row, "tel_source_url"):
        raise ValueError(
            f"telephone source is required for {_text(row, 'place_id')}"
        )
    if (
        _text(row, "open_time") or _text(row, "close_day")
    ) and not _text(row, "hours_source_url"):
        raise ValueError(f"hours source is required for {_text(row, 'place_id')}")


def _merge_extra(raw: str | None, row: dict) -> str:
    place_id = _text(row, "place_id")
    extra = load_json_object(raw, label=f"extra_json for {place_id}")
    existing_detail = extra.get("detail_enrichment")
    if existing_detail is not None and not isinstance(existing_detail, dict):
        raise ValueError(f"detail_enrichment must be an object for {place_id}")
    detail = dict(existing_detail or {})
    detail.update({
        "verified_at": _text(row, "verified_at"),
        "verification": "manual",
    })
    if _text(row, "tel_source_url"):
        detail["tel_source_url"] = _text(row, "tel_source_url")
    if _text(row, "hours_source_url"):
        detail["hours_source_url"] = _text(row, "hours_source_url")
    if _text(row, "review_note"):
        detail["review_note"] = _text(row, "review_note")
    extra["detail_enrichment"] = detail
    return dump_json_object(extra)


def apply_review_rows(
    conn: sqlite3.Connection,
    rows: list[dict],
) -> ImportStats:
    statuses = {"approved", "pending", "rejected"}
    approved = sum(_text(row, "review_status") == "approved" for row in rows)
    pending = sum(_text(row, "review_status") == "pending" for row in rows)
    rejected = sum(_text(row, "review_status") == "rejected" for row in rows)
    unknown = sorted({_text(row, "review_status") for row in rows} - statuses)
    if unknown:
        raise ValueError(f"unknown review_status: {', '.join(unknown)}")

    conn.execute("SAVEPOINT giftshop_detail_import")
    try:
        updated = 0
        for row in rows:
            if _text(row, "review_status") != "approved":
                continue
            _validate_approved(row)
            place_id = _text(row, "place_id")
            current = conn.execute(
                "SELECT tel, open_time, close_day, extra_json FROM place "
                "WHERE place_id=? AND category='giftshop'",
                (place_id,),
            ).fetchone()
            if current is None:
                raise ValueError(
                    f"approved place is missing or not giftshop: {place_id}"
                )
            tel, open_time, close_day, raw_extra = current
            conn.execute(
                "UPDATE place SET tel=?, open_time=?, close_day=?, extra_json=? "
                "WHERE place_id=?",
                (
                    _text(row, "tel") or tel,
                    _text(row, "open_time") or open_time,
                    _text(row, "close_day") or close_day,
                    _merge_extra(raw_extra, row),
                    place_id,
                ),
            )
            updated += 1
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"database integrity failed: {integrity}")
        conn.execute("RELEASE SAVEPOINT giftshop_detail_import")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT giftshop_detail_import")
        conn.execute("RELEASE SAVEPOINT giftshop_detail_import")
        raise
    return ImportStats(
        approved=approved,
        updated=updated,
        pending=pending,
        rejected=rejected,
    )


def _backup_database(source: sqlite3.Connection, db_path: Path) -> Path:
    backup_dir = Path(db_path).parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"travel_pre_giftshop_detail_{timestamp}.db"
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
    finally:
        target.close()
    return backup_path


def import_review_file(
    db_path: Path,
    csv_path: Path,
    *,
    apply: bool,
) -> tuple[ImportStats, Path | None]:
    rows = read_review_rows(csv_path)
    source = sqlite3.connect(db_path)
    try:
        if apply:
            backup = _backup_database(source, db_path)
            stats = apply_review_rows(source, rows)
            source.commit()
            return stats, backup
        target = sqlite3.connect(":memory:")
        try:
            source.backup(target)
            stats = apply_review_rows(target, rows)
            return stats, None
        finally:
            target.close()
    finally:
        source.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Import reviewed giftshop details")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    stats, backup = import_review_file(args.db, args.csv, apply=args.apply)
    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        **asdict(stats),
        "backup": str(backup) if backup else None,
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
