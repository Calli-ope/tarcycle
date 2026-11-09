"""Hourly weather for Tartu from the Open-Meteo historical archive.

    python -m prediction.weather --start 2025-01-01 --end 2025-12-31

Writes ``cache/prediction/weather_2025.csv`` with one row per UTC hour. Open-Meteo
is free for non-commercial use, needs no API key, and its data is CC BY 4.0.

The model is trained and evaluated on *observed* weather. When it runs live, the
same variables come from Open-Meteo's forecast API instead, and those forecasts
are less accurate than observations a few hours ahead. Test scores are
therefore slightly optimistic about weather (noted in docs/prediction.md).
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd

from .trips import OUTPUT_FILE as TRIPS_FILE


ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
TARTU = (58.378, 26.729)  # Raekoja plats
VARIABLES = [
    "temperature_2m",
    "precipitation",
    "snowfall",
    "snow_depth",
    "wind_speed_10m",
    "cloud_cover",
    "is_day",
]
WEATHER_FILE = TRIPS_FILE.parent / "weather_2025.csv"
USER_AGENT = "tarcycle-its-course-project (University of Tartu)"


def fetch_hourly(start: str, end: str, location: tuple[float, float] = TARTU) -> pd.DataFrame:
    """Hourly weather between two dates (inclusive), keyed by ``hour_utc``."""

    query = urllib.parse.urlencode(
        {
            "latitude": location[0],
            "longitude": location[1],
            "start_date": start,
            "end_date": end,
            "hourly": ",".join(VARIABLES),
            "timezone": "UTC",
        }
    )
    request = urllib.request.Request(f"{ARCHIVE_URL}?{query}", headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    return parse_hourly(payload)


def parse_hourly(payload: dict) -> pd.DataFrame:
    """Open-Meteo's ``hourly`` block (column lists) as a frame."""

    hourly = payload["hourly"]
    weather = pd.DataFrame({name: hourly[name] for name in VARIABLES})
    weather.insert(0, "hour_utc", pd.to_datetime(hourly["time"]).tz_localize("UTC"))
    return weather


def read_weather(path: Path = WEATHER_FILE) -> pd.DataFrame:
    weather = pd.read_csv(path)
    weather["hour_utc"] = pd.to_datetime(weather["hour_utc"], utc=True)
    return weather


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    # One day either side so that every UTC hour of the local year is covered.
    parser.add_argument("--start", default="2024-12-31")
    parser.add_argument("--end", default="2026-01-01")
    parser.add_argument("--out", type=Path, default=WEATHER_FILE)
    args = parser.parse_args()

    weather = fetch_hourly(args.start, args.end)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    weather.to_csv(args.out, index=False)
    missing = weather[VARIABLES].isna().sum()
    print(f"{len(weather):,} hours {weather['hour_utc'].min()} .. {weather['hour_utc'].max()}")
    if missing.any():
        print("Missing values:\n" + missing[missing > 0].to_string())
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
