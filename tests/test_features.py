import numpy as np
import pandas as pd
import pytest

from prediction.baselines import HistoricalMean, baseline_predictions
from prediction.features import FEATURE_COLUMNS, build_features, lag_columns
from prediction.weather import VARIABLES


def hourly(values, station=1, start="2025-03-03 00:00"):
    hours = pd.date_range(start, periods=len(values), freq="h", tz="UTC")
    return pd.DataFrame({"station": station, "hour_utc": hours, "pickups": values, "returns": values})


def weather_for(demand):
    hours = pd.DataFrame({"hour_utc": demand["hour_utc"].unique()})
    return hours.assign(**{name: 0.0 for name in VARIABLES})


def test_lags_only_use_hours_up_to_t_minus_horizon():
    series = hourly(np.arange(400, dtype=float))  # count = hour index
    lags = lag_columns(series, horizon=3)
    row = 200
    assert lags.loc[row, "pickups_last"] == 197
    assert lags.loc[row, "pickups_recent3"] == 195 + 196 + 197
    assert lags.loc[row, "pickups_day"] == 176  # horizon 3 -> yesterday is known
    assert lags.loc[row, "pickups_week"] == 32
    assert np.isnan(lags.loc[100, "pickups_week"])  # no week of history yet


def test_day_lag_steps_back_two_days_for_horizons_over_a_day():
    lags = lag_columns(hourly(np.arange(400, dtype=float)), horizon=30)
    assert lags.loc[200, "pickups_day"] == 200 - 48


def test_lags_stay_within_a_station():
    two = pd.concat([hourly([1.0] * 5, station=1), hourly([9.0] * 5, station=2)], ignore_index=True)
    lags = lag_columns(two, horizon=1)
    assert np.isnan(lags.loc[5, "pickups_last"])  # first hour of station 2 must not see station 1
    assert lags.loc[6, "pickups_last"] == 9


def test_outage_hours_are_unusable_and_missing_in_lags():
    demand = hourly([2] * 800)
    outage = pd.DatetimeIndex([demand.loc[400, "hour_utc"]])
    features = build_features(demand, weather_for(demand), outage, horizon=1)
    assert not features.loc[400, "usable"]
    assert np.isnan(features.loc[401, "pickups_last"])
    assert np.isnan(features.loc[401, "net_pickups_recent3"])
    assert np.isnan(features.loc[400 + 168, "pickups_week"])
    assert features.loc[400 + 168, "pickups_4w"] == 2  # weeks 2 and 3 still count
    assert set(FEATURE_COLUMNS) <= set(features.columns)


def test_horizon_is_checked():
    with pytest.raises(ValueError):
        lag_columns(hourly([1.0] * 10), horizon=0)


def test_historical_mean_falls_back_to_station_hour_then_zero():
    train = pd.DataFrame({"station": [1, 1, 1], "weekday": [0, 0, 1], "hour": [8, 8, 8], "pickups": [2, 4, 9]})
    test = pd.DataFrame({"station": [1, 1, 2], "weekday": [0, 3, 0], "hour": [8, 8, 8]})
    predicted = HistoricalMean().fit(train, "pickups").predict(test)
    assert predicted.tolist() == [3.0, 5.0, 0.0]


def test_lag_baselines_fall_back_to_the_mean():
    columns = {"station": [1], "weekday": [0], "hour": [8], "pickups": [4], "returns": [2]}
    train = pd.DataFrame(columns)
    test = pd.DataFrame({**columns, "pickups_week": [np.nan], "returns_week": [5.0], "pickups_4w": [1.5], "returns_4w": [np.nan]})
    predicted = baseline_predictions(train, test)
    assert predicted[("last_week", "pickups")].tolist() == [4.0]
    assert predicted[("last_week", "returns")].tolist() == [5.0]
    assert predicted[("mean_4w", "pickups")].tolist() == [1.5]
    assert predicted[("mean_4w", "returns")].tolist() == [2.0]


def test_recency_weights_halve_every_half_life():
    from prediction.model import recency_weights

    hours = pd.Series(pd.to_datetime(["2025-01-01", "2025-01-29", "2025-02-26"], utc=True))
    assert np.allclose(recency_weights(hours, 28), [0.25, 0.5, 1.0])


def test_poisson_class_shares_sum_to_one_and_match_zero_probability():
    from prediction.experiments import observed_class_shares, poisson_class_shares

    shares = poisson_class_shares(np.array([0.0, 0.6]))
    assert shares.sum() == pytest.approx(1.0)
    assert shares["0"] == pytest.approx((1 + np.exp(-0.6)) / 2)
    observed = observed_class_shares(pd.Series([0, 0, 1, 5]))
    assert observed.tolist() == [0.5, 0.25, 0.0, 0.25]


def test_split_dates_can_be_moved():
    from prediction.evaluate import split

    hours = pd.date_range("2025-04-25", "2025-07-05", freq="D", tz="UTC")
    rows = pd.DataFrame({"hour_utc": hours, "usable": True})
    start = lambda m: pd.Timestamp(2025, m, 1, tz="Europe/Tallinn")
    train, valid, test = split(rows, start(5), start(6), start(7))
    assert train["hour_utc"].max() < start(5) <= valid["hour_utc"].min()
    assert valid["hour_utc"].max() < start(6) <= test["hour_utc"].min()
    assert test["hour_utc"].max() < start(7)
