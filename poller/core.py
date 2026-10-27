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

# Official public station API (given to us by Tartu Smart Bike). POST-only; the
# body {"start": 0, "limit": N} pages through stations (110 in Oct 2026).
# Field meanings: "RR2021 Tartu - Bike share public API explanation" (Estonian).
API_URL = "https://api.ratas.tartu.ee/cxf/am/station/map/search"
REQUEST_BODY = {"start": 0, "limit": 500}
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
    bikes_total     INTEGER,          -- totalLockedCycleCount
    bikes_docked    INTEGER,          -- primaryLockedCycleCount: locked in physical docks
    bikes_temp_lock INTEGER,          -- secondaryLockedCycleCount: temporary lock in station area
    free_docks      INTEGER,          -- freeDocksCount: free physical docks
    free_spaces     INTEGER,          -- freeSpacesCount: free virtual spaces in station area
    stocking_status TEXT,             -- EMPTY / LOW / NORMAL / FULL / OVERFULL
    station_status  TEXT,             -- OPEN / CLOSED / PLANNED
    locking_type    TEXT,             -- PHYSICAL / VIRTUAL
    last_update_at  TEXT,             -- lastUpdateCycleCountAt (UTC): last lock/unlock/manual update
    lat             REAL,
    lon             REAL,
    PRIMARY KEY (poll_id, station_id)
);
CREATE INDEX IF NOT EXISTS station_status_station_time
    ON station_status (station_id, polled_at);
"""

STATION_COLUMNS = (
    "station_id", "station_number", "station_name", "bikes_total", "bikes_docked",
    "bikes_temp_lock", "free_docks", "free_spaces", "stocking_status", "station_status",
    "locking_type", "last_update_at", "lat", "lon",
)


def fetch(url: str, timeout_seconds: int) -> FetchResult:
    """POST the station search. Never raises: failures come back as ``error``."""

    request = urllib.request.Request(
        url,
        data=json.dumps(REQUEST_BODY).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return FetchResult(response.status, response.read(), None)
    except urllib.error.HTTPError as error:
        return FetchResult(error.code, error.read(), f"HTTP {error.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return FetchResult(None, b"", f"{type(error).__name__}: {error}")


def _utc_from_millis(value: Any) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_stations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per station from the API's ``{"total": n, "results": [...]}``.

    Raises ``ValueError`` if the page is incomplete (more stations than returned).
    """

    stations = payload["results"]
    if payload.get("total", len(stations)) > len(stations):
        raise ValueError(f"Got {len(stations)} of {payload['total']} stations; raise REQUEST_BODY limit")
    rows = []
    for station in stations:
        centre = station.get("areaCentroid") or {}
        rows.append(
            {
                "station_id": station["id"],
                "station_number": station.get("serialNumber"),
                "station_name": station.get("name"),
                "bikes_total": station.get("totalLockedCycleCount"),
                "bikes_docked": station.get("primaryLockedCycleCount"),
                "bikes_temp_lock": station.get("secondaryLockedCycleCount"),
                "free_docks": station.get("freeDocksCount"),
                "free_spaces": station.get("freeSpacesCount"),
                "stocking_status": station.get("stationStockingStatus"),
                "station_status": station.get("stationStatus"),
                "locking_type": station.get("lockingStationType"),
                "last_update_at": _utc_from_millis(station.get("lastUpdateCycleCountAt")),
                "lat": centre.get("latitude"),
                "lon": centre.get("longitude"),
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
