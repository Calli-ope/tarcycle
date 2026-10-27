"""Station list and station x hour demand table from the cleaned trips.

    python -m prediction.demand

Reads ``cache/prediction/trips_2025.csv.gz`` (from ``prediction.trips``) and writes

- ``cache/prediction/stations_2025.csv``: one row per station, with the
  period it was in use,
- ``cache/prediction/demand_hourly_2025.csv.gz``: one row per station and
  hour while the station was in use, with ``pickups``, ``returns`` and
  ``net_flow = returns - pickups``. Hours without trips are 0, not missing.

Station numbers in the trips are the public API's ``serialNumber``, so the
poller's data joins on ``station``. Hours are keyed in UTC (``hour_utc``):
in local time 30 March has 23 hours and 26 October 25, which would break a
regular hourly grid. ``hour_local`` is added for features.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from .trips import LOCAL_TZ, OUTPUT_FILE as TRIPS_FILE


OUTPUT_DIR = TRIPS_FILE.parent
STATIONS_FILE = OUTPUT_DIR / "stations_2025.csv"
DEMAND_FILE = OUTPUT_DIR / "demand_hourly_2025.csv.gz"
# Stations used on fewer days than this are temporary event stations
# (e.g. "ÖÖ25", one evening in April), not part of the network.
MIN_ACTIVE_DAYS = 7


def read_trips(path: Path) -> pd.DataFrame:
    trips = pd.read_csv(path)
    for column in ("start_utc", "end_utc"):
        trips[column] = pd.to_datetime(trips[column], utc=True)
    return trips


def events(trips: pd.DataFrame) -> pd.DataFrame:
    """Every pickup and return as (station, name, time, kind)."""

    pickups = pd.DataFrame(
        {"station": trips["StartStationSerialNumber"], "name": trips["StartStationName"], "time": trips["start_utc"], "kind": "pickups"}
    )
    returns = pd.DataFrame(
        {"station": trips["EndStationSerialNumber"], "name": trips["EndStationName"], "time": trips["end_utc"], "kind": "returns"}
    )
    return pd.concat([pickups, returns], ignore_index=True)


def station_table(trips: pd.DataFrame, min_active_days: int = MIN_ACTIVE_DAYS) -> pd.DataFrame:
    """One row per station: name, first and last use, trip counts, and whether to model it.

    A station is "in use" from its first to its last pickup or return; before
    and after that, an hour without trips says nothing about demand.
    """

    all_events = events(trips)
    stations = all_events.groupby("station").agg(
        name=("name", "first"),
        first_use=("time", "min"),
        last_use=("time", "max"),
        active_days=("time", lambda times: times.dt.date.nunique()),
    )
    counts = all_events.groupby(["station", "kind"]).size().unstack(fill_value=0)
    stations = stations.join(counts).reset_index()
    stations["modelled"] = stations["active_days"] >= min_active_days
    return stations


def hourly_demand(trips: pd.DataFrame, stations: pd.DataFrame) -> pd.DataFrame:
    """Pickups, returns and net flow per station and UTC hour, with zero-filled hours."""

    all_events = events(trips)
    all_events["hour_utc"] = all_events["time"].dt.floor("h")
    counts = all_events.groupby(["station", "hour_utc", "kind"]).size().unstack(fill_value=0)

    grids = []
    for station in stations[stations["modelled"]].itertuples():
        hours = pd.date_range(station.first_use.floor("h"), station.last_use.floor("h"), freq="h")
        grids.append(pd.DataFrame({"station": station.station, "hour_utc": hours}))
    grid = pd.concat(grids, ignore_index=True)

    demand = grid.merge(counts.reset_index(), on=["station", "hour_utc"], how="left")
    for column in ("pickups", "returns"):
        demand[column] = demand.get(column, 0).fillna(0).astype("int64")
    demand["net_flow"] = demand["returns"] - demand["pickups"]
    demand["hour_local"] = demand["hour_utc"].dt.tz_convert(LOCAL_TZ)
    return demand[["station", "hour_utc", "hour_local", "pickups", "returns", "net_flow"]]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--trips", type=Path, default=TRIPS_FILE)
    args = parser.parse_args()

    trips = read_trips(args.trips)
    stations = station_table(trips)
    demand = hourly_demand(trips, stations)
    stations.to_csv(STATIONS_FILE, index=False)
    demand.to_csv(DEMAND_FILE, index=False)

    left_out = stations[~stations["modelled"]]
    print(f"{len(stations)} stations, {int(stations['modelled'].sum())} modelled")
    for row in left_out.itertuples():
        print(f"  left out: {row.name} ({row.station}), used on {row.active_days} day(s)")
    print(f"{len(demand):,} station-hours, {demand['pickups'].sum():,} pickups, {demand['returns'].sum():,} returns")
    print(f"Hours with no pickup: {(demand['pickups'] == 0).mean():.1%}")
    print(f"Wrote {STATIONS_FILE} and {DEMAND_FILE}")


if __name__ == "__main__":
    main()
