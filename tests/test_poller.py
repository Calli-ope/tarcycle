import csv
import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

from poller.core import (
    FetchResult,
    Settings,
    connect,
    last_success_age_seconds,
    parse_stations,
    poll_once,
    seconds_until_next_poll,
)
from poller.export import export_csv

# Trimmed real response from POST https://api.ratas.tartu.ee/cxf/am/station/map/search
PAYLOAD = {
    "total": 2,
    "results": [
        {
            "id": "6beda072-5062-4dc9-989d-0b3b81360186", "name": "AHHAA", "serialNumber": "46",
            "primaryLockedCycleCount": 0, "secondaryLockedCycleCount": 0, "totalLockedCycleCount": 0,
            "freeSpacesCount": 30, "freeDocksCount": 6,
            "areaCentroid": {"@class": "GeoPoint", "latitude": 58.377009641904955, "longitude": 26.733028152457116},
            "stationStatus": "OPEN", "stationStockingStatus": "EMPTY", "lockingStationType": "PHYSICAL",
            "dockingStationType": "REGULAR", "cyclesInStation": [], "lastUpdateCycleCountAt": 1793118488705,
        },
        {
            "id": "aaaa", "name": "Siili", "serialNumber": "90",
            "primaryLockedCycleCount": 0, "secondaryLockedCycleCount": 2, "totalLockedCycleCount": 2,
            "freeSpacesCount": 8, "freeDocksCount": 0,
            "areaCentroid": {"@class": "GeoPoint", "latitude": 58.35, "longitude": 26.70},
            "stationStatus": "OPEN", "stationStockingStatus": "NORMAL", "lockingStationType": "VIRTUAL",
            "dockingStationType": "FREE_FLOATING", "lastUpdateCycleCountAt": 1793118000000,
            "cyclesInStation": [{"serialNumber": "04401"}],
        },
    ],
}


def ok(_url, _timeout):
    return FetchResult(200, json.dumps(PAYLOAD).encode(), None)


def server_error(_url, _timeout):
    return FetchResult(500, b'{"message":"Wrong request!"}', "HTTP 500")


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=tmp_path / "status.sqlite3")


def test_parse_stations():
    rows = parse_stations(PAYLOAD)
    assert len(rows) == 2
    assert rows[0] == {
        "station_id": "6beda072-5062-4dc9-989d-0b3b81360186", "station_number": "46",
        "station_name": "AHHAA", "bikes_total": 0, "bikes_docked": 0, "bikes_temp_lock": 0,
        "free_docks": 6, "free_spaces": 30, "stocking_status": "EMPTY", "station_status": "OPEN",
        "locking_type": "PHYSICAL", "last_update_at": "2026-10-27T16:28:08Z",
        "lat": 58.377009641904955, "lon": 26.733028152457116,
    }


def test_incomplete_page_is_an_error(settings):
    partial = {**PAYLOAD, "total": 500}
    with pytest.raises(ValueError, match="2 of 500"):
        parse_stations(partial)
    connection = connect(settings.db_path)
    fetcher = lambda u, t: FetchResult(200, json.dumps(partial).encode(), None)
    assert not poll_once(connection, settings, fetcher=fetcher, sleep=lambda s: None)


def test_successful_poll_stores_stations_and_raw_response(settings):
    connection = connect(settings.db_path)
    assert poll_once(connection, settings, fetcher=ok)
    poll = connection.execute("SELECT http_status, station_count, error, raw_gzip FROM polls").fetchone()
    assert poll[:3] == (200, 2, None)
    assert json.loads(gzip.decompress(poll[3])) == PAYLOAD
    stations = connection.execute(
        "SELECT station_name, bikes_total, bikes_temp_lock, free_docks FROM station_status ORDER BY station_name"
    ).fetchall()
    assert stations == [("AHHAA", 0, 0, 6), ("Siili", 2, 2, 0)]


def test_failed_poll_retries_once_then_records_error(settings):
    connection = connect(settings.db_path)
    calls, sleeps = [], []
    fetcher = lambda url, timeout: calls.append(url) or server_error(url, timeout)
    assert not poll_once(connection, settings, fetcher=fetcher, sleep=sleeps.append)
    assert len(calls) == 2 and sleeps == [settings.retry_delay_seconds]
    assert connection.execute("SELECT http_status, error FROM polls").fetchall() == [(500, "HTTP 500")]
    assert connection.execute("SELECT COUNT(*) FROM station_status").fetchone() == (0,)


def test_retry_success_records_only_the_good_poll(settings):
    connection = connect(settings.db_path)
    responses = iter([server_error, ok])
    assert poll_once(connection, settings, fetcher=lambda u, t: next(responses)(u, t), sleep=lambda s: None)
    assert connection.execute("SELECT COUNT(*), MAX(error) FROM polls").fetchone() == (1, None)


def test_bad_json_is_recorded_not_raised(settings):
    connection = connect(settings.db_path)
    fetcher = lambda u, t: FetchResult(200, b"<html>maintenance</html>", None)
    assert not poll_once(connection, settings, fetcher=fetcher, sleep=lambda s: None)
    error = connection.execute("SELECT error FROM polls").fetchone()[0]
    assert error.startswith("Bad response")


@pytest.mark.parametrize(("now", "expected"), [(1000.0, 200.0), (1200.0, 300.0), (1499.5, 0.5)])
def test_seconds_until_next_poll_aligns_to_clock(now, expected):
    assert seconds_until_next_poll(now, 300) == pytest.approx(expected)


def test_health_age_counts_only_successful_polls(settings):
    connection = connect(settings.db_path)
    assert last_success_age_seconds(connection, datetime.now(timezone.utc)) is None
    poll_once(connection, settings, fetcher=ok)
    later = datetime.now(timezone.utc) + timedelta(minutes=20)
    poll_once(connection, settings, fetcher=server_error, sleep=lambda s: None)
    assert last_success_age_seconds(connection, later) > 15 * 60


def test_export_csv(settings, tmp_path):
    connection = connect(settings.db_path)
    poll_once(connection, settings, fetcher=ok)
    connection.close()
    out = tmp_path / "status.csv"
    assert export_csv(settings.db_path, out) == 2
    rows = list(csv.DictReader(out.open()))
    assert rows[0]["station_name"] == "AHHAA" and rows[0]["bikes_total"] == "0"
    assert export_csv(settings.db_path, out, since="2999-01-01") == 0
