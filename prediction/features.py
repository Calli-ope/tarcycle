"""Model table: one row per station and hour, with targets and features for a forecast horizon.

Forecast setup: the forecast for hour ``T`` with horizon ``h`` is made when hour
``T - h`` has just finished. Features may only use trips up to and including
``T - h``. Calendar and weather features describe hour ``T`` itself (weather is
the observed value: see ``prediction.weather``).

Lag features per station, computed on the station's own pickups and returns:

- ``*_last``: count in hour ``T - h`` (the latest known hour),
- ``*_recent3`` / ``*_recent24``: sum over the 3 / 24 hours ending at ``T - h``,
- ``*_day``: same hour on the most recent day that is fully known,
- ``*_week``: same hour one week earlier,
- ``*_4w``: mean of the same hour over the last 4 weeks (skipping missing weeks).

``net_recent3`` is the same 3-hour sum for the whole network. It carries
information that one station's few trips cannot, such as a sudden shower.

Outage hours (``prediction.outages``) get ``usable = False``. Their counts are
treated as missing in every lag that would read them.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .calendar import calendar_features
from .trips import LOCAL_TZ
from .weather import VARIABLES as WEATHER_VARIABLES


TARGETS = ("pickups", "returns")
HOURS_PER_WEEK = 168


def lag_columns(series: pd.DataFrame, horizon: int, by: str = "station") -> pd.DataFrame:
    """Lag features of the ``pickups`` / ``returns`` columns for a horizon.

    ``series`` must hold, for each ``by`` group, one row per consecutive hour,
    sorted by time. Missing counts (outages) stay missing in sums and lags.
    """

    if not 1 <= horizon <= HOURS_PER_WEEK:
        raise ValueError(f"horizon must be 1..{HOURS_PER_WEEK} hours, got {horizon}")
    day_lag = 24 * math.ceil(horizon / 24)
    groups = series.groupby(by, sort=False)
    out = {}
    for target in TARGETS:
        column = groups[target]
        shifted = column.shift(horizon)
        out[f"{target}_last"] = shifted
        for window in (3, 24):
            out[f"{target}_recent{window}"] = shifted.groupby(series[by], sort=False).transform(
                lambda values, window=window: values.rolling(window, min_periods=window).sum()
            )
        out[f"{target}_day"] = column.shift(day_lag)
        weeks = pd.concat([column.shift(HOURS_PER_WEEK * k) for k in range(1, 5)], axis=1)
        out[f"{target}_week"] = weeks.iloc[:, 0]
        out[f"{target}_4w"] = weeks.mean(axis=1, skipna=True)
    return pd.DataFrame(out, index=series.index)


def network_recent(demand: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Pickups and returns of all stations together, summed over the 3 hours ending at ``T - h``."""

    network = demand.groupby("hour_utc")[list(TARGETS)].sum(min_count=1)
    full_hours = pd.date_range(network.index.min(), network.index.max(), freq="h", name="hour_utc")
    network = network.reindex(full_hours)
    recent = network.shift(horizon).rolling(3, min_periods=3).sum()
    return recent.rename(columns={target: f"net_{target}_recent3" for target in TARGETS}).reset_index()


def build_features(
    demand: pd.DataFrame,
    weather: pd.DataFrame,
    outages: pd.DatetimeIndex,
    horizon: int,
) -> pd.DataFrame:
    """The model table for one horizon. Targets: ``pickups``, ``returns``. Keep rows with ``usable``."""

    table = demand.sort_values(["station", "hour_utc"], kind="stable").reset_index(drop=True)
    usable = ~table["hour_utc"].isin(outages)
    observed = table[["station", "hour_utc"]].copy()
    for target in TARGETS:
        observed[target] = table[target].where(usable).astype("float64")

    local = table["hour_utc"].dt.tz_convert(LOCAL_TZ)
    features = pd.concat(
        [
            table[["station", "hour_utc", *TARGETS]],
            calendar_features(local),
            lag_columns(observed, horizon),
        ],
        axis=1,
    )
    features = features.merge(network_recent(observed, horizon), on="hour_utc", how="left")
    features = features.merge(weather[["hour_utc", *WEATHER_VARIABLES]], on="hour_utc", how="left")
    features["usable"] = usable.to_numpy()
    features["horizon"] = np.int16(horizon)
    return features


FEATURE_COLUMNS = [
    "station",
    "hour",
    "weekday",
    "is_holiday",
    "is_day_off",
    "is_semester",
    *(f"{target}_{kind}" for target in TARGETS for kind in ("last", "recent3", "recent24", "day", "week", "4w")),
    *(f"net_{target}_recent3" for target in TARGETS),
    *WEATHER_VARIABLES,
]
