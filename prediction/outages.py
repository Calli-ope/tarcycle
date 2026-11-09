"""Find hours where the trip export is missing data, so they are not learned as "no demand".

From 25 Jul to 13 Aug 2025 the raw export has blocks of hours with no trips at
all (e.g. 29 Jul: 57 trips, all 10-11 h; 31 Jul: none), while the days around
them are normal. The cause is a service outage or an export gap, not cleaning
and not low demand. Those hours are dropped from training and evaluation, and
lag features that would read them become missing.

Detection is network-wide: an hour is an outage when all stations together have
far fewer pickups than usual for that weekday and local hour. The 01-05 h night
closure is not an outage, because its usual count is 0.
"""

from __future__ import annotations

import pandas as pd


# Search window around the known gap. The rule could flag a few extreme-weather
# hours elsewhere (e.g. a snowstorm in January), which are real low demand.
OUTAGE_WINDOW = (pd.Timestamp("2025-07-20", tz="UTC"), pd.Timestamp("2025-08-20", tz="UTC"))
# Weeks before and after the window that define the usual count per weekday and hour.
REFERENCE_WEEKS = 4
# Flag an hour if it has fewer than this share of the usual pickups ...
MAX_SHARE = 0.2
# ... and the usual count is large enough for a shortfall to mean anything.
MIN_EXPECTED = 15


def network_hourly(demand: pd.DataFrame) -> pd.DataFrame:
    """Pickups of all stations together per hour, with local weekday and hour."""

    network = demand.groupby("hour_utc", as_index=False)["pickups"].sum()
    local = network["hour_utc"].dt.tz_convert("Europe/Tallinn")
    return network.assign(weekday=local.dt.weekday, hour=local.dt.hour)


def outage_hours(
    demand: pd.DataFrame,
    window: tuple[pd.Timestamp, pd.Timestamp] = OUTAGE_WINDOW,
    reference_weeks: int = REFERENCE_WEEKS,
    max_share: float = MAX_SHARE,
    min_expected: float = MIN_EXPECTED,
) -> pd.DatetimeIndex:
    """UTC hours inside ``window`` with far fewer network pickups than usual."""

    network = network_hourly(demand)
    start, end = window
    span = pd.Timedelta(weeks=reference_weeks)
    times = network["hour_utc"]
    in_window = (times >= start) & (times < end)
    reference = network[((times >= start - span) & (times < start)) | ((times >= end) & (times < end + span))]
    usual = reference.groupby(["weekday", "hour"])["pickups"].median().rename("usual")

    candidates = network[in_window].join(usual, on=["weekday", "hour"])
    flagged = (candidates["usual"] >= min_expected) & (candidates["pickups"] < max_share * candidates["usual"])
    return pd.DatetimeIndex(candidates.loc[flagged, "hour_utc"], name="hour_utc")
