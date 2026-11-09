"""Experiments that check modelling choices (beyond the main scores of ``prediction.evaluate``).

    python -m prediction.experiments calibration --horizons 1 24   # seconds, reads evaluate's output
    python -m prediction.experiments ablation --horizons 1 24      # ~15 min
    python -m prediction.experiments rolling --horizons 1 24       # ~30 min

- ``calibration``: do the forecasts produce as many zero (and 1, 2, 3+) station-hours
  as observed? If a Poisson forecast with mean ``mu`` is right, the share of hours
  with ``k`` trips is the average of ``Poisson(k; mu)`` over all hours. Many zeros are
  then expected, not an "imbalance" to fix.
- ``ablation``: retrain without one feature group at a time and see how much the
  test MAE gets worse. Shows what each group adds *on top of* the others, which
  neither a correlation nor gain importance shows.
- ``rolling``: rolling-origin evaluation. For each test month M (Jun-Dec 2025), train
  on everything before month M-1, early-stop on M-1, predict M. This is how the model
  would be used (retrained regularly, forecasting the near future) and gives a score
  per season. Also compares recency weighting (recent weeks count more in training).

Outputs go to ``cache/prediction/`` (aggregate tables only).
"""

from __future__ import annotations

import argparse
import time

import numpy as np
import pandas as pd

from .baselines import baseline_predictions
from .evaluate import OUTPUT_DIR, TEST_END, read_demand, scores, split
from .features import FEATURE_COLUMNS, TARGETS, build_features
from .model import fit
from .outages import outage_hours
from .trips import LOCAL_TZ
from .weather import VARIABLES as WEATHER_VARIABLES, read_weather


FEATURE_GROUPS = {
    "calendar": ["hour", "weekday", "is_holiday", "is_day_off", "is_semester"],
    "weather": list(WEATHER_VARIABLES),
    "station_level": [f"{t}_{k}" for t in TARGETS for k in ("day", "week", "4w")],
    "recent_hours": [f"{t}_{k}" for t in TARGETS for k in ("last", "recent3", "recent24")],
    "network": [f"net_{t}_recent3" for t in TARGETS],
}
COUNT_CLASSES = ("0", "1", "2", "3+")
ROLLING_MONTHS = range(6, 13)
HALF_LIVES = (None, 56, 28)


# ---------- calibration ----------

def poisson_class_shares(mean: np.ndarray) -> pd.Series:
    """Expected share of hours with 0, 1, 2 and 3+ trips if each hour is Poisson(``mean``)."""

    mean = np.clip(np.asarray(mean, dtype=float), 0, None)
    p0 = np.exp(-mean)
    p1 = p0 * mean
    p2 = p1 * mean / 2
    probs = np.stack([p0, p1, p2, 1 - p0 - p1 - p2]).mean(axis=1)
    return pd.Series(probs, index=COUNT_CLASSES)


def observed_class_shares(counts: pd.Series) -> pd.Series:
    labels = counts.clip(upper=3).astype(int).map({0: "0", 1: "1", 2: "2", 3: "3+"})
    return labels.value_counts(normalize=True).reindex(COUNT_CLASSES, fill_value=0.0)


def calibration(horizons: list[int]) -> pd.DataFrame:
    rows = []
    for horizon in horizons:
        forecasts = pd.read_csv(OUTPUT_DIR / f"test_predictions_h{horizon}.csv.gz")
        for target in TARGETS:
            row = {"horizon": horizon, "target": target}
            row.update({f"observed_{k}": v for k, v in observed_class_shares(forecasts[target]).items()})
            for method in ("lightgbm", "mean_4w"):
                expected = poisson_class_shares(forecasts[f"{target}_{method}"])
                row.update({f"{method}_{k}": v for k, v in expected.items()})
            rows.append(row)
    return pd.DataFrame(rows)


# ---------- ablation ----------

def ablation(demand, weather, outages, horizons: list[int]) -> pd.DataFrame:
    rows = []
    for horizon in horizons:
        train, valid, test = split(build_features(demand, weather, outages, horizon))
        for dropped in ["none", *FEATURE_GROUPS]:
            started = time.time()
            removed = set(FEATURE_GROUPS.get(dropped, []))
            columns = [c for c in FEATURE_COLUMNS if c not in removed]
            predicted = {target: fit(train, valid, target, features=columns).predict(test) for target in TARGETS}
            row = {"horizon": horizon, "dropped": dropped}
            for target in TARGETS:
                row[f"{target}_mae"] = scores(test[target], predicted[target])["mae"]
            net_error = (predicted["returns"] - predicted["pickups"]) - (test["returns"] - test["pickups"])
            row["net_flow_mae"] = float(net_error.abs().mean())
            rows.append(row)
            print(f"h={horizon} without {dropped}: pickups MAE {row['pickups_mae']:.4f} ({time.time() - started:.0f} s)", flush=True)
    table = pd.DataFrame(rows)
    reference = table[table["dropped"] == "none"].set_index("horizon")
    for column in ("pickups_mae", "returns_mae", "net_flow_mae"):
        base = table["horizon"].map(reference[column])
        table[column.replace("_mae", "_worse_pct")] = (table[column] / base - 1) * 100
    return table


# ---------- rolling origin ----------

def month_start(year: int, month: int) -> pd.Timestamp:
    return pd.Timestamp(year=year, month=month, day=1, tz=LOCAL_TZ)


def rolling(demand, weather, outages, horizons: list[int], months=ROLLING_MONTHS, half_lives=HALF_LIVES) -> pd.DataFrame:
    rows = []
    for horizon in horizons:
        features = build_features(demand, weather, outages, horizon)
        for month in months:
            test_start = month_start(2025, month)
            test_end = month_start(2025, month + 1) if month < 12 else TEST_END
            train, valid, test = split(features, month_start(2025, month - 1), test_start, test_end)
            predictions = baseline_predictions(pd.concat([train, valid]), test)
            for half_life in half_lives:
                method = "lightgbm" if half_life is None else f"lightgbm_hl{half_life}d"
                for target in TARGETS:
                    predictions[(method, target)] = fit(train, valid, target, half_life_days=half_life).predict(test)
            for (method, target), predicted in predictions.items():
                if method == "last_week":
                    continue
                rows.append({"horizon": horizon, "month": f"2025-{month:02d}", "method": method, "target": target,
                             "rows": len(test), **scores(test[target], predicted)})
            summary = ", ".join(f"{r['method']} {r['mae']:.3f}" for r in rows if r["horizon"] == horizon
                                and r["month"] == f"2025-{month:02d}" and r["target"] == "pickups")
            print(f"h={horizon} {test_start:%b} pickups MAE: {summary}", flush=True)
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("experiment", choices=["calibration", "ablation", "rolling"])
    parser.add_argument("--horizons", type=int, nargs="+", default=[1, 24])
    args = parser.parse_args()
    pd.set_option("display.width", 200)

    if args.experiment == "calibration":
        table = calibration(args.horizons)
    else:
        demand = read_demand()
        weather = read_weather()
        outages = outage_hours(demand)
        run = ablation if args.experiment == "ablation" else rolling
        table = run(demand, weather, outages, args.horizons)

    path = OUTPUT_DIR / f"experiment_{args.experiment}.csv"
    table.to_csv(path, index=False)
    print(table.to_string(index=False, float_format="{:.4f}".format))
    print(f"\nWrote {path}")


if __name__ == "__main__":
    main()
