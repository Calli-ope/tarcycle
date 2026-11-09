# Station demand prediction

Forecast hourly **pickups** and **returns** per station some hours ahead, and
compare against simple baselines (RQ2). Code: `prediction/`. Data stays in
`cache/prediction/` (gitignored). Only aggregate scores belong in the repo or report.

## Pipeline

```text
routes_2025.csv ──prediction.trips──▶ trips_2025.csv.gz
                 ──prediction.demand─▶ stations_2025.csv, demand_hourly_2025.csv.gz
Open-Meteo      ──prediction.weather─▶ weather_2025.csv
                 ──prediction.evaluate▶ metrics.csv, importance.csv, test_predictions_h*.csv.gz
```

```bash
pip install -r requirements-prediction.txt
python -m prediction.trips --routes ../Routes/routes_2025.csv   # ~1 min
python -m prediction.demand
python -m prediction.weather                                    # one HTTP request
python -m prediction.evaluate --horizons 1 3 6 24               # ~5 min
pytest
```

| Module | What it does |
|---|---|
| `calendar.py` | hour, weekday, Estonian public holidays (Easter computed), `is_day_off`, UT semester |
| `weather.py` | hourly temperature, precipitation, snowfall, snow depth, wind, cloud cover, daylight from the Open-Meteo archive |
| `outages.py` | finds hours with missing trip data (Jul–Aug 2025 export gap) |
| `features.py` | model table per station × hour for one horizon (lags, calendar, weather) |
| `baselines.py` | historical mean, same hour last week, 4-week mean |
| `model.py` | LightGBM (Poisson), one global model per target |
| `evaluate.py` | time split, fits everything, writes scores |

## Forecast setup

A forecast for hour `T` with **horizon `h`** is made when hour `T − h` has just
finished: it may use trips up to and including `T − h`, never later.
`tests/test_features.py` checks this for each lag. Station hours are on a
regular UTC grid (local time has 23- and 25-hour days); hour and weekday
features use local time.

**Features.** Station (categorical), local hour, weekday, holiday, day off,
semester, weather at `T`, and for both pickups and returns of the station:
last known hour, 3 h and 24 h sums up to `T − h`, the same hour on the last
fully known day, one week back, and the 4-week mean of the same hour.
Plus the network-wide 3 h sum (all stations).

**Split (by time).** Train 1 Jan–31 Aug 2025, validation September (early
stopping only), test 1 Oct–31 Dec 2025. Baselines and the final model are
fitted on Jan–Sep. The test period is autumn and early winter, which the
training data barely covers, so the test is harder than a random split would be.

**Metrics** per station-hour: MAE, RMSE, WAPE (total absolute error / total
trips) and bias (total forecast / total trips − 1). `net_flow` = returns −
pickups is scored too, because a station empties when its net flow stays negative.

## Data issues handled

**Outage hours.** Between 25 Jul and 16 Aug 2025 the export has blocks of hours
with (almost) no trips while the days around them are normal (e.g. 31 Jul:
0 trips). `outages.py` flags an hour when the whole network has under 20 % of
the usual pickups for that weekday and hour (usual = median of the 4 weeks
before and after; hours whose usual count is under 15, e.g. the 01–05 h night
closure, are never flagged). The 2025 data has **220 such hours**. They are
dropped from training and testing, and lags that would read them are missing
(LightGBM handles missing values; baselines fall back to the historical mean).
Cause still to confirm with Kärt.

**Observed instead of forecast weather.** The model is trained and tested with
observed weather. Live, it would get Open-Meteo *forecasts*, which are worse,
so the test scores are slightly optimistic about weather. The effect is small
here: weather features carry little gain (see results).

## Limitation: censored demand

The targets are **trips that happened**, not **trips people wanted**. When a
station is empty, nobody can pick up a bike there, so demand in those hours is
recorded as 0 or as too low. The model learns from these hours as if demand were
low, and therefore under-predicts at stations that often run empty. These are
exactly the stations that matter for rebalancing. The same happens, rarely,
when a station has no free docks.

What we can and cannot do:

- 2025 has no station-status history, so censored hours cannot be found
  directly. They could be approximated by reconstructing each station's stock
  from bike IDs (last lock location per bike), and that stock would also be
  needed for RQ3.
- From Oct 2026 the poller records when each station is empty. Those intervals
  give (a) how often demand is censored and where, (b) a way to drop or
  down-weight censored hours in training, and (c) the empty-station hit rate as
  an evaluation of the risk forecast.
- So the reported errors measure how well we forecast **observed trips**. True
  demand at busy, often-empty stations is likely higher than the forecast.

## Results (2025, test Oct–Dec)

228,972 test station-hours (105 stations). Baselines do not depend on the horizon.
The average station-hour has 0.6 pickups, so errors are in trips per station-hour.

**MAE, pickups**

| Method | 1 h | 3 h | 6 h | 24 h |
|---|---|---|---|---|
| Historical mean (station × weekday × hour) | 0.643 | 0.643 | 0.643 | 0.643 |
| Same hour last week | 0.602 | 0.602 | 0.602 | 0.602 |
| 4-week mean, same hour | 0.583 | 0.583 | 0.583 | 0.583 |
| **LightGBM** | **0.470** | **0.489** | **0.492** | **0.502** |

**MAE, returns:** best baseline (4-week mean) 0.577; LightGBM 0.479 / 0.493 / 0.492 / 0.500.
**MAE, net flow:** best baseline (historical mean) 0.602; LightGBM 0.550 / 0.556 / 0.557 / 0.559.

**Bias, pickups** (total forecast vs total trips): historical mean +33 %, 4-week
mean +34 %, last week +14 %, LightGBM +5 % (1 h) to +14 % (24 h).

Observations:

- LightGBM beats the best baseline by **14–19 %** in MAE at every horizon and
  in every test month (Oct, Nov, Dec). Most of the gain holds even at 24 h.
- All methods over-forecast because demand falls through autumn. The
  whole-year mean is too high by a third. Short lags (recent hours, the
  network-wide sum) let LightGBM follow the decline at short horizons, so its
  bias grows with the horizon.
- The 4-week mean of the same hour is by far the most important feature (50–60 %
  of the gain). Weather adds only 3–4 %: recent counts already carry most of the
  seasonal and weather signal. Calendar features (`is_day_off`, `is_semester`)
  carry almost none, because one year has few holidays.
- Hourly counts per station are mostly 0–2 and largely random (Poisson noise),
  so some error remains for any method. WAPE stays around 0.8. For rebalancing,
  sums over several hours (e.g. net flow until evening) matter more and are
  much more predictable than single hours. This is the next thing to evaluate,
  together with the empty-station risk.

Reproduce: `python -m prediction.evaluate`. Full tables (incl. RMSE, WAPE) in
`cache/prediction/metrics.csv`.
