"""LightGBM forecast of hourly pickups and returns per station.

One global model per target for all stations, with ``station`` as a categorical
feature. A model per station would have too few trips at quiet stations. The
objective is Poisson because the targets are small counts (most station-hours
are 0, 1 or 2). With it the model predicts a non-negative expected count,
not a value pulled toward 0 by the many zeros.

Training follows the usual pattern for time series:

1. fit on the training months, with early stopping on the next month (validation),
2. refit on training + validation with the number of trees found in step 1,
3. predict the test months, which neither step has seen.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import lightgbm as lgb
import numpy as np
import pandas as pd

from .features import FEATURE_COLUMNS


PARAMS = {
    "objective": "poisson",
    "learning_rate": 0.05,
    "num_leaves": 63,
    "min_data_in_leaf": 100,
    "feature_fraction": 0.8,
    "bagging_fraction": 0.8,
    "bagging_freq": 1,
    "lambda_l2": 1.0,
    "verbosity": -1,
    "seed": 2025,
    "num_threads": 0,
}
MAX_ROUNDS = 3000
EARLY_STOPPING = 100


def recency_weights(hours: pd.Series, half_life_days: float) -> np.ndarray:
    """Weight 1 for the newest hour, halving every ``half_life_days`` going back in time."""

    age_days = (hours.max() - hours).dt.total_seconds().to_numpy() / 86400
    return 0.5 ** (age_days / half_life_days)


def _dataset(
    rows: pd.DataFrame,
    target: str,
    features: Sequence[str],
    reference: lgb.Dataset | None = None,
    half_life_days: float | None = None,
) -> lgb.Dataset:
    weight = recency_weights(rows["hour_utc"], half_life_days) if half_life_days else None
    return lgb.Dataset(
        rows[list(features)],
        label=rows[target],
        weight=weight,
        categorical_feature=["station"] if "station" in features else [],
        reference=reference,
        free_raw_data=False,
    )


@dataclass
class FittedModel:
    booster: lgb.Booster
    best_rounds: int
    features: tuple[str, ...] = tuple(FEATURE_COLUMNS)

    def predict(self, rows: pd.DataFrame) -> pd.Series:
        return pd.Series(self.booster.predict(rows[list(self.features)]), index=rows.index)

    def importance(self) -> pd.Series:
        """Total gain per feature, as a share of the sum."""

        gain = pd.Series(self.booster.feature_importance("gain"), index=self.booster.feature_name())
        return (gain / gain.sum()).sort_values(ascending=False)


def fit(
    train: pd.DataFrame,
    valid: pd.DataFrame,
    target: str,
    params: dict | None = None,
    features: Sequence[str] = FEATURE_COLUMNS,
    half_life_days: float | None = None,
) -> FittedModel:
    """Early-stop on ``valid``, then refit on ``train + valid`` with that many trees.

    ``features`` selects a subset of columns (for ablation). ``half_life_days``
    weights recent hours more (see ``recency_weights``); the validation month is
    scored unweighted.
    """

    params = {**PARAMS, **(params or {})}
    train_set = _dataset(train, target, features, half_life_days=half_life_days)
    valid_set = _dataset(valid, target, features, reference=train_set)
    tuned = lgb.train(
        params,
        train_set,
        num_boost_round=MAX_ROUNDS,
        valid_sets=[valid_set],
        callbacks=[lgb.early_stopping(EARLY_STOPPING, verbose=False)],
    )
    best_rounds = tuned.best_iteration or MAX_ROUNDS
    full = pd.concat([train, valid])
    booster = lgb.train(params, _dataset(full, target, features, half_life_days=half_life_days), num_boost_round=best_rounds)
    return FittedModel(booster, best_rounds, tuple(features))
