"""Fetch, parse and store Tartu Smart Bike station status.

Pure standard library, so the Docker image needs no pip install.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("poller")

# The endpoint the official map (ratas.tartu.ee) uses. It answers only when the
# request carries the map's Origin header; otherwise it returns HTTP 500.
API_URL = "https://serverapp.ratas.tartu.ee/api/map/stations/"
ORIGIN = "https://ratas.tartu.ee"
USER_AGENT = "Tarcycle-poller/0.1 (University of Tartu ITS course project)"


@dataclass(frozen=True)
class Settings:
    db_path: Path
    api_url: str = API_URL
    interval_seconds: int = 300
    timeout_seconds: int = 20
    retry_delay_seconds: int = 30

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=Path(os.environ.get("DB_PATH", "cache/poller/station_status.sqlite3")),
            api_url=os.environ.get("API_URL", API_URL),
            interval_seconds=int(os.environ.get("POLL_INTERVAL_SECONDS", "300")),
        )


@dataclass(frozen=True)
class FetchResult:
    http_status: int | None
    body: bytes
    error: str | None


Fetcher = Callable[[str, int], FetchResult]

SCHEMA = """
CREATE TABLE IF NOT EXISTS polls (
    poll_id       INTEGER PRIMARY KEY,
    polled_at     TEXT NOT NULL,      -- UTC, ISO 8601
    http_status   INTEGER,
    station_count INTEGER,
    error         TEXT,               -- NULL when the poll succeeded
    raw_gzip      BLOB                -- full response body, gzip-compressed
);
CREATE TABLE IF NOT EXISTS station_status (
    poll_id         INTEGER NOT NULL REFERENCES polls(poll_id),
    polled_at       TEXT NOT NULL,
    station_id      TEXT NOT NULL,
    station_number  TEXT,
    station_name    TEXT,
    bikes_available INTEGER,
    docks_available INTEGER,          -- free docks, not total docks
    status          TEXT,
    stock_level     TEXT,             -- empty / low / normal / full / overfull
    is_physical     INTEGER,
    is_dockless     INTEGER,
    lat             REAL,
    lon             REAL,
    PRIMARY KEY (poll_id, station_id)
);
CREATE INDEX IF NOT EXISTS station_status_station_time
    ON station_status (station_id, polled_at);
"""

STATION_COLUMNS = (
    "station_id", "station_number", "station_name", "bikes_available", "docks_available",
    "status", "stock_level", "is_physical", "is_dockless", "lat", "lon",
)


def fetch(url: str, timeout_seconds: int) -> FetchResult:
    """GET the station list. Never raises: failures come back as ``error``."""

    request = urllib.request.Request(
        url,
        headers={"Origin": ORIGIN, "User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return FetchResult(response.status, response.read(), None)
    except urllib.error.HTTPError as error:
        return FetchResult(error.code, error.read(), f"HTTP {error.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return FetchResult(None, b"", f"{type(error).__name__}: {error}")


def parse_stations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per station from the API's ``{"total": n, "results": [...]}``."""

    rows = []
    for station in payload["results"]:
        rows.append(
            {
                "station_id": station["station_id"],
                "station_number": station.get("number"),
                "station_name": station.get("name"),
                "bikes_available": station.get("num_vehicles_available"),
                "docks_available": station.get("num_docks_available"),
                "status": station.get("status"),
                "stock_level": station.get("stock_level"),
                "is_physical": station.get("is_physical"),
                "is_dockless": station.get("is_dockless"),
                "lat": station.get("location_lat"),
                "lon": station.get("location_lon"),
            }
        )
    return rows


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    # WAL lets exports read the database while the poller is writing.
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    return connection


def save_poll(
    connection: sqlite3.Connection,
    polled_at: str,
    result: FetchResult,
    rows: list[dict[str, Any]],
    error: str | None,
) -> int:
    """Store one poll (and its station rows) in a single transaction."""

    with connection:
        cursor = connection.execute(
            "INSERT INTO polls (polled_at, http_status, station_count, error, raw_gzip) "
            "VALUES (?, ?, ?, ?, ?)",
            (polled_at, result.http_status, len(rows) if rows else None, error, gzip.compress(result.body)),
        )
        poll_id = cursor.lastrowid
        connection.executemany(
            f"INSERT INTO station_status (poll_id, polled_at, {', '.join(STATION_COLUMNS)}) "
            f"VALUES (?, ?, {', '.join('?' * len(STATION_COLUMNS))})",
            [(poll_id, polled_at, *(row[column] for column in STATION_COLUMNS)) for row in rows],
        )
    return poll_id


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def poll_once(
    connection: sqlite3.Connection,
    settings: Settings,
    fetcher: Fetcher = fetch,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Fetch (with one retry), parse and store. Returns whether it succeeded."""

    for attempt in (1, 2):
        polled_at = utc_now()
        result = fetcher(settings.api_url, settings.timeout_seconds)
        rows: list[dict[str, Any]] = []
        error = result.error
        if error is None:
            try:
                rows = parse_stations(json.loads(result.body))
            except (ValueError, KeyError, TypeError) as parse_error:
                error = f"Bad response: {type(parse_error).__name__}: {parse_error}"
        if error is None or attempt == 2:
            save_poll(connection, polled_at, result, rows, error)
            if error is None:
                log.info("Saved %d stations at %s", len(rows), polled_at)
            else:
                log.error("Poll failed at %s: %s", polled_at, error)
            return error is None
        log.warning("Poll failed (%s); retrying in %ss", error, settings.retry_delay_seconds)
        sleep(settings.retry_delay_seconds)
    return False


def seconds_until_next_poll(now: float, interval_seconds: int) -> float:
    """Wait until the next clock boundary (e.g. :00, :05, :10 for 5 minutes)."""

    return interval_seconds - (now % interval_seconds)


def last_success_age_seconds(connection: sqlite3.Connection, now: datetime) -> float | None:
    """Seconds since the last successful poll, or ``None`` if there is none."""

    row = connection.execute(
        "SELECT MAX(polled_at) FROM polls WHERE error IS NULL"
    ).fetchone()
    if row[0] is None:
        return None
    last = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (now - last).total_seconds()
