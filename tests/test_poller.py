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

# Trimmed real response from https://serverapp.ratas.tartu.ee/api/map/stations/
PAYLOAD = {
    "total": 2,
    "results": [
        {
            "station_id": "6beda072-5062-4dc9-989d-0b3b81360186", "name": "AHHAA", "number": "46",
            "num_vehicles_available": 0, "num_docks_available": 6,
            "location_lat": 58.377009641904955, "location_lon": 26.733028152457116,
            "status": "open", "stock_level": "empty", "is_dockless": False, "is_physical": True,
            "area": {"@class": "GeoCircle"},
        },
        {
            "station_id": "aaaa", "name": "Siili", "number": "90",
            "num_vehicles_available": 2, "num_docks_available": 0,
            "location_lat": 58.35, "location_lon": 26.70,
            "status": "open", "stock_level": "normal", "is_dockless": True, "is_physical": False,
            "station_cycles": [{"serial_number": "04401", "cycle_type": "pedelec", "battery_level": 68}],
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
        "station_name": "AHHAA", "bikes_available": 0, "docks_available": 6, "status": "open",
        "stock_level": "empty", "is_physical": True, "is_dockless": False,
        "lat": 58.377009641904955, "lon": 26.733028152457116,
    }


def test_successful_poll_stores_stations_and_raw_response(settings):
    connection = connect(settings.db_path)
    assert poll_once(connection, settings, fetcher=ok)
    poll = connection.execute("SELECT http_status, station_count, error, raw_gzip FROM polls").fetchone()
    assert poll[:3] == (200, 2, None)
    assert json.loads(gzip.decompress(poll[3])) == PAYLOAD
    stations = connection.execute(
        "SELECT station_name, bikes_available, docks_available FROM station_status ORDER BY station_name"
    ).fetchall()
    assert stations == [("AHHAA", 0, 6), ("Siili", 2, 0)]


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
    assert rows[0]["station_name"] == "AHHAA" and rows[0]["bikes_available"] == "0"
    assert export_csv(settings.db_path, out, since="2999-01-01") == 0
