"""Load and clean the 2025 Smart Bike trip records.

    python -m prediction.trips --routes ../Routes/routes_2025.csv

Writes the kept trips to ``cache/prediction/trips_2025.csv.gz`` (trip-level
data: private, gitignored) and prints how many trips each rule removed.

Personal columns (``PersonalIdCode``, ``YearOfBirth``, ``UserId``) are never
read: demand prediction needs where and when, not who.

Time zone: the export's times are UTC+2 all year, not Estonian clock time.
``RouteCode`` is the unlock moment in UTC epoch milliseconds and is always
exactly 2 h behind ``UnlockedAt``, also in summer (when Estonia is UTC+3), and
the weekday morning peak moves one hour earlier in the weeks clocks change.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ROUTES_FILE = ROOT_DIR.parent / "Routes" / "routes_2025.csv"
OUTPUT_FILE = ROOT_DIR / "cache" / "prediction" / "trips_2025.csv.gz"

COLUMNS = [
    "RouteCode",
    "CycleNumber",
    "UnlockedAt",
    "UnlockedAtTime",
    "LockedAt",
    "LockedAtTime",
    "StartStationSerialNumber",
    "StartStationName",
    "EndStationSerialNumber",
    "EndStationName",
    "Length",
    "DurationMinutes",
    "CycleType",
]
EXPORT_UTC_OFFSET = pd.Timedelta(hours=2)
LOCAL_TZ = "Europe/Tallinn"

WORKSHOP = "töökoda"
UNDETERMINED = ("undetermined", "määramata")
MAX_TRIP_HOURS = 5.0
MIN_TRIP_MINUTES = 2.0


def load_trips(path: Path) -> pd.DataFrame:
    """The trip columns we use, and nothing personal."""

    return pd.read_csv(path, usecols=COLUMNS, low_memory=False)


def export_time_to_utc(date: pd.Series, time: pd.Series) -> pd.Series:
    """Combine the export's date and time columns (UTC+2) into UTC timestamps.

    Times look like ``08:15:31``, sometimes with a ``+00`` suffix that carries
    no information; unparsable values become ``NaT``.
    """

    naive = pd.to_datetime(date + " " + time.str.slice(0, 8), format="%Y-%m-%d %H:%M:%S", errors="coerce")
    return (naive - EXPORT_UTC_OFFSET).dt.tz_localize("UTC")


def add_times(trips: pd.DataFrame) -> pd.DataFrame:
    """``start_utc`` / ``end_utc`` for joining with weather and the poller,
    ``start_local`` / ``end_local`` (Estonian clock time) for hour and weekday."""

    start = export_time_to_utc(trips["UnlockedAt"], trips["UnlockedAtTime"])
    end = export_time_to_utc(trips["LockedAt"], trips["LockedAtTime"])
    return trips.drop(columns=["UnlockedAt", "UnlockedAtTime", "LockedAt", "LockedAtTime"]).assign(
        start_utc=start,
        end_utc=end,
        start_local=start.dt.tz_convert(LOCAL_TZ),
        end_local=end.dt.tz_convert(LOCAL_TZ),
    )


def _name_has(names: pd.Series, words: tuple[str, ...]) -> pd.Series:
    lower = names.fillna("").astype(str).str.lower()
    return lower.apply(lambda name: any(word in name for word in words))


def drop_reason(trips: pd.DataFrame) -> pd.Series:
    """Why each trip is removed (first rule that applies), or ``""`` if kept.

    Rules from Tartu Smart Bike (Kärt Lang): trips ending at the workshop
    (Töökoda) are faulty bikes; trips from or to an undetermined station
    (outside any station area) are usually faulty bikes. Ours: zero length or
    under 2 minutes is an aborted rental, not a ride.
    """

    rules = [
        ("missing_time_or_bike", trips["start_utc"].isna() | trips["end_utc"].isna() | trips["CycleNumber"].isna()),
        ("workshop", _name_has(trips["EndStationName"], (WORKSHOP,)) | _name_has(trips["StartStationName"], (WORKSHOP,))),
        ("undetermined_station", _name_has(trips["StartStationName"], UNDETERMINED) | _name_has(trips["EndStationName"], UNDETERMINED)),
        ("aborted", (trips["Length"] <= 0) | (trips["DurationMinutes"] < MIN_TRIP_MINUTES)),
    ]
    reason = pd.Series("", index=trips.index)
    for name, applies in rules:
        reason = reason.mask((reason == "") & applies, name)
    return reason


def clean_trips(trips: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Kept trips (with a ``too_long`` flag for review) and the count per drop reason."""

    trips = add_times(trips)
    reason = drop_reason(trips)
    kept = trips[reason == ""].assign(too_long=lambda frame: frame["DurationMinutes"] > MAX_TRIP_HOURS * 60)
    kept = kept.astype({"CycleNumber": "int64"}).sort_values("start_utc", kind="stable")
    counts = reason[reason != ""].value_counts()
    return kept.reset_index(drop=True), counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--routes", type=Path, default=DEFAULT_ROUTES_FILE)
    parser.add_argument("--out", type=Path, default=OUTPUT_FILE)
    args = parser.parse_args()

    raw = load_trips(args.routes)
    kept, dropped = clean_trips(raw)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    kept.to_csv(args.out, index=False)

    print(f"Read {len(raw):,} trips, kept {len(kept):,} ({len(kept) / len(raw):.1%})")
    for name, count in dropped.items():
        print(f"  dropped {name}: {count:,}")
    print(f"  flagged too_long (> {MAX_TRIP_HOURS:g} h, kept): {int(kept['too_long'].sum()):,}")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
