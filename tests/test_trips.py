import pandas as pd

from prediction.trips import add_times, clean_trips, export_time_to_utc


def trip(**overrides):
    row = {
        "RouteCode": 1,
        "CycleNumber": 2637.0,
        "UnlockedAt": "2025-01-15",
        "UnlockedAtTime": "08:00:00",
        "LockedAt": "2025-01-15",
        "LockedAtTime": "08:10:00",
        "StartStationSerialNumber": 56,
        "StartStationName": "Magistri",
        "EndStationSerialNumber": 12,
        "EndStationName": "Roosi",
        "Length": 1.1,
        "DurationMinutes": 10.0,
        "CycleType": "Pedelec",
    }
    return {**row, **overrides}


def test_export_time_is_utc_plus_two_all_year():
    utc = export_time_to_utc(pd.Series(["2025-01-01", "2025-07-01"]), pd.Series(["00:07:05", "12:29:00+00"]))
    assert list(utc.dt.strftime("%Y-%m-%d %H:%M:%S")) == ["2024-12-31 22:07:05", "2025-07-01 10:29:00"]


def test_local_time_follows_estonian_daylight_saving():
    # 08:00 in the export is 06:00 UTC: 08:00 on the clock in winter, 09:00 in summer.
    frame = add_times(pd.DataFrame([trip(), trip(UnlockedAt="2025-07-15", LockedAt="2025-07-15")]))
    assert list(frame["start_local"].dt.hour) == [8, 9]


def test_route_code_matches_start_time():
    # The RouteCode of the first 2025 trip is its unlock moment in UTC milliseconds.
    frame = add_times(pd.DataFrame([trip(RouteCode=1735682825801, UnlockedAt="2025-01-01", UnlockedAtTime="00:07:05")]))
    assert frame["start_utc"].iloc[0] == pd.to_datetime(1735682825801, unit="ms", utc=True).floor("s")


def test_clean_trips_applies_each_rule():
    raw = pd.DataFrame(
        [
            trip(),
            trip(EndStationName="Töökoda"),
            trip(StartStationName="Undetermined"),
            trip(EndStationName="-Määramata-"),
            trip(Length=0.0),
            trip(DurationMinutes=1.5),
            trip(CycleNumber=None),
            trip(UnlockedAtTime="not a time"),
            trip(DurationMinutes=400.0, LockedAtTime="14:40:00"),
        ]
    )
    kept, dropped = clean_trips(raw)
    assert len(kept) == 2
    assert kept["too_long"].tolist() == [False, True]
    assert dropped.to_dict() == {
        "undetermined_station": 2,
        "aborted": 2,
        "missing_time_or_bike": 2,
        "workshop": 1,
    }
