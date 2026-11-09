"""Compare LightGBM with the baselines on held-out months (RQ2).

    python -m prediction.evaluate --horizons 1 3 6 24

Needs ``demand_hourly_2025.csv.gz`` (``prediction.demand``) and
``weather_2025.csv`` (``prediction.weather``) in ``cache/prediction/``.

Split by time, never at random. With a random split, an hour's neighbours
(which share weather and events) would sit in the training data:

- train: 1 Jan - 31 Aug 2025
- validation: September (early stopping only; baselines and the final model
  are then fitted on train + validation)
- test: 1 Oct - 31 Dec 2025

Outputs (in ``cache/prediction/``): ``metrics.csv`` (aggregate scores, safe to
share), ``importance.csv`` (feature gain per horizon and target) and
``test_predictions_h{h}.csv.gz`` (station-hour forecasts, private like the trips).
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .baselines import BASELINES, baseline_predictions
from .demand import DEMAND_FILE
from .features import TARGETS, build_features
from .model import fit
from .outages import outage_hours
from .trips import LOCAL_TZ
from .weather import WEATHER_FILE, read_weather


OUTPUT_DIR = DEMAND_FILE.parent
VALID_START = pd.Timestamp("2025-09-01", tz=LOCAL_TZ)
TEST_START = pd.Timestamp("2025-10-01", tz=LOCAL_TZ)
TEST_END = pd.Timestamp("2026-01-01", tz=LOCAL_TZ)
DEFAULT_HORIZONS = (1, 3, 6, 24)


def read_demand(path: Path = DEMAND_FILE) -> pd.DataFrame:
    demand = pd.read_csv(path, usecols=["station", "hour_utc", "pickups", "returns"])
    demand["hour_utc"] = pd.to_datetime(demand["hour_utc"], utc=True)
    return demand


def split(
    features: pd.DataFrame,
    valid_start: pd.Timestamp = VALID_START,
    test_start: pd.Timestamp = TEST_START,
    test_end: pd.Timestamp = TEST_END,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Usable rows of train (before ``valid_start``), validation and test, by local time."""

    rows = features[features["usable"]]
    hour = rows["hour_utc"]
    train = rows[hour < valid_start]
    valid = rows[(hour >= valid_start) & (hour < test_start)]
    test = rows[(hour >= test_start) & (hour < test_end)]
    return train, valid, test


def scores(actual: pd.Series, predicted: pd.Series) -> dict[str, float]:
    """MAE and RMSE per station-hour, WAPE (total error / total trips), and bias (total forecast / total trips - 1)."""

    error = predicted.to_numpy(dtype=float) - actual.to_numpy(dtype=float)
    total = float(actual.sum())
    return {
        "mae": float(np.mean(np.abs(error))),
        "rmse": float(np.sqrt(np.mean(error**2))),
        "wape": float(np.sum(np.abs(error)) / total) if total else np.nan,
        "bias": float(predicted.sum() / total - 1) if total else np.nan,
    }


def score_table(test: pd.DataFrame, predictions: dict[tuple[str, str], pd.Series], horizon: int) -> pd.DataFrame:
    """One row per (method, target), including ``net_flow`` = returns - pickups."""

    rows = []
    methods = sorted({method for method, _ in predictions})
    for method in methods:
        for target in TARGETS:
            rows.append({"horizon": horizon, "method": method, "target": target, **scores(test[target], predictions[(method, target)])})
        net_actual = test["returns"] - test["pickups"]
        net_predicted = predictions[(method, "returns")] - predictions[(method, "pickups")]
        net = scores(net_actual, net_predicted)
        rows.append({"horizon": horizon, "method": method, "target": "net_flow", "mae": net["mae"], "rmse": net["rmse"]})
    return pd.DataFrame(rows)


def monthly_mae(test: pd.DataFrame, predictions: dict[tuple[str, str], pd.Series], target: str) -> pd.DataFrame:
    month = test["hour_utc"].dt.tz_convert(LOCAL_TZ).dt.strftime("%Y-%m")
    out = {}
    for (method, name), predicted in predictions.items():
        if name == target:
            out[method] = (predicted - test[target]).abs().groupby(month).mean()
    return pd.DataFrame(out)


def evaluate_horizon(demand: pd.DataFrame, weather: pd.DataFrame, outages: pd.DatetimeIndex, horizon: int):
    features = build_features(demand, weather, outages, horizon)
    train, valid, test = split(features)
    history = pd.concat([train, valid])

    predictions = baseline_predictions(history, test)
    importance = []
    for target in TARGETS:
        model = fit(train, valid, target)
        predictions[("lightgbm", target)] = model.predict(test)
        importance.append(model.importance().rename("gain_share").rename_axis("feature").reset_index().assign(horizon=horizon, target=target, trees=model.best_rounds))
    return test, predictions, pd.concat(importance, ignore_index=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--horizons", type=int, nargs="+", default=list(DEFAULT_HORIZONS))
    parser.add_argument("--demand", type=Path, default=DEMAND_FILE)
    parser.add_argument("--weather", type=Path, default=WEATHER_FILE)
    args = parser.parse_args()

    demand = read_demand(args.demand)
    weather = read_weather(args.weather)
    outages = outage_hours(demand)
    print(f"{len(outages)} outage hours masked")

    all_scores, all_importance = [], []
    for horizon in args.horizons:
        started = time.time()
        test, predictions, importance = evaluate_horizon(demand, weather, outages, horizon)
        table = score_table(test, predictions, horizon)
        all_scores.append(table)
        all_importance.append(importance)

        forecasts = test[["station", "hour_utc", *TARGETS]].copy()
        for (method, target), predicted in predictions.items():
            forecasts[f"{target}_{method}"] = predicted.round(4)
        forecasts.to_csv(OUTPUT_DIR / f"test_predictions_h{horizon}.csv.gz", index=False)

        print(f"\n## Horizon {horizon} h ({len(test):,} test station-hours, {time.time() - started:.0f} s)\n")
        print(table.drop(columns="horizon").to_string(index=False, float_format="{:.4f}".format))
        print("\nPickups MAE by month:\n")
        print(monthly_mae(test, predictions, "pickups").to_string(float_format="{:.4f}".format))
        top = importance[importance["target"] == "pickups"].head(8)
        print("\nTop features (pickups, gain share): " + ", ".join(f"{row.feature} {row.gain_share:.0%}" for row in top.itertuples()))

    pd.concat(all_scores).to_csv(OUTPUT_DIR / "metrics.csv", index=False)
    pd.concat(all_importance).to_csv(OUTPUT_DIR / "importance.csv", index=False)
    print(f"\nWrote {OUTPUT_DIR / 'metrics.csv'} and {OUTPUT_DIR / 'importance.csv'}")


if __name__ == "__main__":
    main()
