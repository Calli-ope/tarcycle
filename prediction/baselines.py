"""Simple forecasts the model has to beat.

- ``historical_mean``: mean count per station x weekday x local hour over the
  training period (the "typical week" a planner would draw from history).
- ``last_week``: the same hour one week earlier.
- ``mean_4w``: mean of the same hour over the last 4 weeks. This follows
  seasonal drift that a whole-year mean misses.

The lag-based baselines fall back to ``historical_mean`` where the lag is
missing (a new station, or a lag that reads an outage). The model never sees
a missing forecast that way, and every baseline is scored on the same rows.
"""

from __future__ import annotations

import pandas as pd

from .features import TARGETS


BASELINES = ("historical_mean", "last_week", "mean_4w")


class HistoricalMean:
    """Mean per station x weekday x hour, falling back to the station x hour mean, then 0."""

    def fit(self, train: pd.DataFrame, target: str) -> "HistoricalMean":
        self.target = target
        self.by_weekday = train.groupby(["station", "weekday", "hour"])[target].mean().rename("by_weekday")
        self.by_hour = train.groupby(["station", "hour"])[target].mean().rename("by_hour")
        return self

    def predict(self, rows: pd.DataFrame) -> pd.Series:
        joined = rows[["station", "weekday", "hour"]].join(self.by_weekday, on=["station", "weekday", "hour"])
        joined = joined.join(self.by_hour, on=["station", "hour"])
        return joined["by_weekday"].fillna(joined["by_hour"]).fillna(0.0)


def baseline_predictions(train: pd.DataFrame, test: pd.DataFrame) -> dict[tuple[str, str], pd.Series]:
    """Forecasts of every baseline for every target, keyed by (baseline, target)."""

    predictions = {}
    for target in TARGETS:
        mean = HistoricalMean().fit(train, target).predict(test)
        predictions[("historical_mean", target)] = mean
        predictions[("last_week", target)] = test[f"{target}_week"].fillna(mean)
        predictions[("mean_4w", target)] = test[f"{target}_4w"].fillna(mean)
    return predictions
