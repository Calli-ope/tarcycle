"""Calendar features: Estonian public holidays and University of Tartu semesters.

All functions take local (Europe/Tallinn) dates, because holidays and
lectures follow the local clock.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd


# Fixed-date public holidays (riigipühad), as (month, day).
FIXED_HOLIDAYS = [
    (1, 1),  # New Year's Day
    (2, 24),  # Independence Day
    (5, 1),  # Spring Day
    (6, 23),  # Victory Day
    (6, 24),  # Midsummer Day (jaanipäev)
    (8, 20),  # Day of Restoration of Independence
    (12, 24),  # Christmas Eve
    (12, 25),  # Christmas Day
    (12, 26),  # Boxing Day
]

# University of Tartu teaching periods (lectures, without the exam sessions and
# breaks), from the UT academic calendar. 
SEMESTERS = [
    (date(2025, 2, 3), date(2025, 6, 1)),  # spring 2025
    (date(2025, 9, 1), date(2025, 12, 21)),  # autumn 2025
    (date(2026, 2, 2), date(2026, 5, 31)),  # spring 2026
    (date(2026, 8, 31), date(2026, 12, 20)),  # autumn 2026
]


def easter_sunday(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""

    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def estonian_holidays(year: int) -> set[date]:
    """Estonian public holidays: the fixed dates plus Good Friday, Easter Sunday and Pentecost."""

    easter = easter_sunday(year)
    moving = {easter - timedelta(days=2), easter, easter + timedelta(days=49)}
    return {date(year, month, day) for month, day in FIXED_HOLIDAYS} | moving


def calendar_features(local_times: pd.Series) -> pd.DataFrame:
    """Hour, weekday and day-type features for local timestamps.

    ``is_day_off`` marks weekends and holidays together. On those days the
    commute peaks disappear in the same way, so the model can share what it
    learns about Sundays with the few holidays.
    """

    dates = local_times.dt.date
    years = {day.year for day in dates.dropna().unique()}
    holidays = set().union(*(estonian_holidays(year) for year in years)) if years else set()
    is_holiday = dates.isin(holidays)
    weekday = local_times.dt.weekday
    day = local_times.dt.normalize().dt.tz_localize(None)
    in_semester = pd.Series(False, index=local_times.index)
    for start, end in SEMESTERS:
        in_semester |= day.between(pd.Timestamp(start), pd.Timestamp(end))
    return pd.DataFrame(
        {
            "hour": local_times.dt.hour,
            "weekday": weekday,
            "is_holiday": is_holiday.astype("int8"),
            "is_day_off": (is_holiday | (weekday >= 5)).astype("int8"),
            "is_semester": in_semester.astype("int8"),
        },
        index=local_times.index,
    )
