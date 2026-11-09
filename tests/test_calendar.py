from datetime import date

import pandas as pd

from prediction.calendar import calendar_features, easter_sunday, estonian_holidays


def test_easter_dates():
    assert easter_sunday(2025) == date(2025, 4, 20)
    assert easter_sunday(2026) == date(2026, 4, 5)
    assert easter_sunday(2024) == date(2024, 3, 31)


def test_moving_holidays_2025():
    holidays = estonian_holidays(2025)
    assert date(2025, 4, 18) in holidays  # Good Friday
    assert date(2025, 6, 8) in holidays  # Pentecost
    assert date(2025, 6, 24) in holidays  # jaanipäev
    assert len(holidays) == 12


def test_features_use_local_dates():
    # 23 Jun 2025 21:30 UTC is already 24 Jun 00:30 in Tartu (UTC+3).
    local = pd.Series(pd.to_datetime(["2025-06-23 21:30", "2025-03-04 08:00"], utc=True)).dt.tz_convert("Europe/Tallinn")
    features = calendar_features(local)
    assert features["hour"].tolist() == [0, 10]
    assert features["is_holiday"].tolist() == [1, 0]
    assert features["is_day_off"].tolist() == [1, 0]  # Tuesday 4 March is a normal workday
    assert features["is_semester"].tolist() == [0, 1]
