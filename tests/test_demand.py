import pandas as pd

from prediction.demand import hourly_demand, station_table


def trips_frame(rows):
    """Trips as (start station, end station, start UTC, end UTC)."""

    return pd.DataFrame(
        {
            "StartStationSerialNumber": [row[0] for row in rows],
            "StartStationName": [f"S{row[0]}" for row in rows],
            "EndStationSerialNumber": [row[1] for row in rows],
            "EndStationName": [f"S{row[1]}" for row in rows],
            "start_utc": pd.to_datetime([row[2] for row in rows], utc=True),
            "end_utc": pd.to_datetime([row[3] for row in rows], utc=True),
        }
    )


TRIPS = trips_frame(
    [
        (1, 2, "2025-03-01 08:05", "2025-03-01 08:20"),
        (1, 2, "2025-03-01 08:40", "2025-03-01 09:10"),
        (2, 1, "2025-03-01 11:00", "2025-03-01 11:15"),
    ]
)


def test_hours_without_trips_are_zero_within_the_use_period():
    stations = station_table(TRIPS, min_active_days=1)
    demand = hourly_demand(TRIPS, stations).set_index(["station", "hour_utc"])
    station_1 = demand.loc[1]
    # Station 1 in use 08:05-11:15: hours 08, 09, 10, 11; 09 and 10 are quiet.
    assert list(station_1.index.hour) == [8, 9, 10, 11]
    assert station_1["pickups"].tolist() == [2, 0, 0, 0]
    assert station_1["returns"].tolist() == [0, 0, 0, 1]
    assert station_1["net_flow"].tolist() == [-2, 0, 0, 1]
    # A return in the next hour counts in that hour.
    assert demand.loc[(2, pd.Timestamp("2025-03-01 09:00", tz="UTC")), "returns"] == 1


def test_short_lived_stations_are_not_modelled():
    stations = station_table(TRIPS, min_active_days=2)
    assert not stations["modelled"].any()


def test_local_hour_follows_daylight_saving():
    summer = trips_frame([(1, 1, "2025-07-01 05:10", "2025-07-01 05:30")])
    demand = hourly_demand(summer, station_table(summer, min_active_days=1))
    assert demand["hour_local"].iloc[0].hour == 8  # 05 UTC = 08 Tallinn summer time
