"""Export polled station status to CSV.

    python -m poller.export --db station_status.sqlite3 --out status.csv [--since 2026-10-21]
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

from .core import STATION_COLUMNS


def export_csv(db_path: Path, out_path: Path, since: str | None = None) -> int:
    """Write one row per station per successful poll; return the row count."""

    # Read-only, so it is safe to run while the poller is writing.
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    query = f"SELECT polled_at, {', '.join(STATION_COLUMNS)} FROM station_status"
    params: tuple[str, ...] = ()
    if since:
        query += " WHERE polled_at >= ?"
        params = (since,)
    query += " ORDER BY polled_at, station_number"
    count = 0
    with out_path.open("w", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(("polled_at", *STATION_COLUMNS))
        for row in connection.execute(query, params):
            writer.writerow(row)
            count += 1
    connection.close()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Export station status to CSV")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--since", help="UTC date or timestamp, e.g. 2026-10-21")
    args = parser.parse_args()
    print(f"Wrote {export_csv(args.db, args.out, args.since)} rows to {args.out}")


if __name__ == "__main__":
    main()
