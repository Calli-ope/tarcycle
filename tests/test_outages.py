import numpy as np
import pandas as pd

from prediction.outages import outage_hours


def network_demand(start: str, weeks: int, gap: tuple[str, str] | None = None) -> pd.DataFrame:
    """One station with 50 pickups every hour except a 0-pickup night (01-04 local) and an optional gap."""

    hours = pd.date_range(start, periods=weeks * 168, freq="h", tz="UTC")
    local_hour = hours.tz_convert("Europe/Tallinn").hour
    pickups = np.where((local_hour >= 1) & (local_hour <= 4), 0, 50)
    demand = pd.DataFrame({"station": 1, "hour_utc": hours, "pickups": pickups})
    if gap:
        in_gap = (demand["hour_utc"] >= pd.Timestamp(gap[0], tz="UTC")) & (demand["hour_utc"] < pd.Timestamp(gap[1], tz="UTC"))
        demand.loc[in_gap, "pickups"] = 2
    return demand


WINDOW = (pd.Timestamp("2025-07-20", tz="UTC"), pd.Timestamp("2025-08-20", tz="UTC"))


def test_gap_hours_are_flagged_but_not_the_night_closure():
    demand = network_demand("2025-06-01", weeks=16, gap=("2025-07-30 06:00", "2025-07-30 12:00"))
    flagged = outage_hours(demand, window=WINDOW)
    assert list(flagged) == list(pd.date_range("2025-07-30 06:00", "2025-07-30 11:00", freq="h", tz="UTC"))


def test_gaps_outside_the_window_are_ignored():
    demand = network_demand("2025-06-01", weeks=16, gap=("2025-06-10 06:00", "2025-06-10 12:00"))
    assert len(outage_hours(demand, window=WINDOW)) == 0
