"""Do our routes follow the paths real Smart Bike riders took?

    python -m backend.trace_match --sample      # once: pick trips from the raw GPS data
    python -m backend.trace_match               # route them with every engine and score

Step 1 samples real 2025 trips from the Locations files and keeps their GPS
traces in ``cache/evaluation/traces.csv.gz`` (gitignored: individual traces
are private). Step 2 routes each trip from its first to its last GPS point
with every engine and measures, on 10 m samples:

- ``route_on_trace``: share of the route within ``MATCH_M`` of the trace
  (how much of what we suggest riders actually rode),
- ``trace_on_route``: share of the trace within ``MATCH_M`` of the route
  (how much of what riders rode we suggest).

Only aggregate scores are written (``data/evaluation/trace_match*.{csv,json}``):
no coordinates, route codes or user ids. Unlike the "share on popular ways"
metric, this is not circular: each route is compared with one rider's own
trace, not with the popularity layer the router was tuned on. The GPS layer
does include the sampled trips themselves, but each is one of thousands of
trips on a busy way (and ways need >= 10 trips to be in the layer at all).
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import fields, replace
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
import shapely
from pyproj import Transformer
from shapely.geometry import LineString

from .app import COVERAGE_DIR, VALHALLA_URL
from .gps_coverage import DEFAULT_LOCATIONS_DIR, METRIC_CRS, read_location_chunks, valid_route_codes
from .gps_layer import load_gps_ways
from .graph import ROOT_DIR, load_graph
from .models import RoutingPreferences
from .router import NetworkXRouter
from .valhalla import ValhallaRouter, ValhallaUnavailable


TRACES_FILE = ROOT_DIR / "cache" / "evaluation" / "traces.csv.gz"
OUTPUT_FILE = ROOT_DIR / "data" / "evaluation" / "trace_match.csv"
SUMMARY_FILE = ROOT_DIR / "data" / "evaluation" / "trace_match_summary.json"
DEFAULT_ROUTES_FILE = ROOT_DIR.parent / "Routes" / "routes_2025.csv"

MATCH_M = 20.0
SAMPLE_M = 10.0
MAX_SPEED_MS = 12.0
MIN_POINTS = 10
MIN_DIRECT_M = 800.0
# Traces much longer than the direct distance are leisure loops, not trips
# from A to B; no router is meant to reproduce them.
MAX_TRACE_TO_DIRECT = 2.0
# Keep RouteCodes whose hash falls below this, out of 10,000 (~0.4% of trips).
SAMPLE_PER_10K = 40

TO_METRES = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True)


# --- Step 1: sample traces ---------------------------------------------------


def sampled(route_codes: np.ndarray, per_10k: int) -> np.ndarray:
    """Deterministic pseudo-random pick of RouteCodes, without knowing them in advance."""

    mixed = (route_codes.astype(np.uint64) * np.uint64(2654435761)) % np.uint64(2**32)
    return (mixed % np.uint64(10_000)) < np.uint64(per_10k)


def clean_trace(x: np.ndarray, y: np.ndarray, seconds: np.ndarray, max_speed_ms: float) -> np.ndarray:
    """Indices of GPS points to keep: drop points that jump faster than a bike can ride."""

    keep = [0]
    for index in range(1, len(x)):
        last = keep[-1]
        distance = np.hypot(x[index] - x[last], y[index] - y[last])
        elapsed = max(seconds[index] - seconds[last], 1.0)
        if distance / elapsed <= max_speed_ms:
            keep.append(index)
    return np.asarray(keep, dtype=int)


def usable_trip(x: np.ndarray, y: np.ndarray) -> bool:
    """A trip from A to B that is long enough to need a route."""

    if len(x) < MIN_POINTS:
        return False
    direct = float(np.hypot(x[-1] - x[0], y[-1] - y[0]))
    length = float(np.hypot(np.diff(x), np.diff(y)).sum())
    return direct >= MIN_DIRECT_M and length <= MAX_TRACE_TO_DIRECT * direct


def sample_traces(locations_dir: Path, routes_file: Path, per_10k: int) -> pd.DataFrame:
    """Cleaned GPS points (trip, lon, lat) of a sample of real A-to-B trips."""

    valid = valid_route_codes(routes_file, min_trip_minutes=2.0)
    files = sorted(locations_dir.glob("locations_2025*.csv"))
    kept: list[pd.DataFrame] = []
    for path in files:
        print(f"Reading {path.name}...")
        for chunk in read_location_chunks(path):
            codes = chunk["RouteCode"].to_numpy()
            chunk = chunk[sampled(codes, per_10k) & np.isin(codes, valid)]
            for code, trip in chunk.sort_values("seconds", kind="stable").groupby("RouteCode"):
                lon, lat = trip["Longitude"].to_numpy(), trip["Latitude"].to_numpy()
                x, y = TO_METRES.transform(lon, lat)
                keep = clean_trace(np.asarray(x), np.asarray(y), trip["seconds"].to_numpy(), MAX_SPEED_MS)
                if usable_trip(np.asarray(x)[keep], np.asarray(y)[keep]):
                    kept.append(pd.DataFrame({"trip": code, "lon": lon[keep], "lat": lat[keep]}))
    traces = pd.concat(kept, ignore_index=True)
    # Replace RouteCodes with plain numbers so nothing links back to the raw data.
    traces["trip"] = pd.factorize(traces["trip"])[0]
    return traces


# --- Step 2: route and score ---------------------------------------------------


def to_metric_line(coordinates: list[list[float]] | np.ndarray) -> LineString:
    points = np.asarray(coordinates, dtype=float)
    x, y = TO_METRES.transform(points[:, 0], points[:, 1])
    return LineString(np.column_stack([x, y]))


def share_within(line: LineString, other: LineString, distance_m: float, step_m: float) -> float:
    """Share of ``line`` (sampled every ``step_m``) lying within ``distance_m`` of ``other``."""

    count = max(2, int(line.length // step_m) + 1)
    samples = shapely.line_interpolate_point(line, np.linspace(0, line.length, count))
    return float(np.mean(shapely.dwithin(samples, other, distance_m)))


def score(route: LineString, trace: LineString) -> dict[str, float]:
    return {
        "route_on_trace": round(share_within(route, trace, MATCH_M, SAMPLE_M), 3),
        "trace_on_route": round(share_within(trace, route, MATCH_M, SAMPLE_M), 3),
        "route_to_trace_length": round(route.length / trace.length, 3),
    }


def shortest_preferences() -> RoutingPreferences:
    """Every factor neutral and no extra costs: the shortest route."""

    neutral = {f.name: 1.0 for f in fields(RoutingPreferences) if f.name.endswith("_factor")}
    zero = {
        name: 0.0
        for name in ("facility_transition_cost", "uphill_factor", "popularity_weight")
        if name in {f.name for f in fields(RoutingPreferences)}
    }
    return replace(RoutingPreferences(), **{**neutral, **zero})


def engines(use_valhalla: bool) -> dict[str, Any]:
    graph = load_graph()
    gps_ways = load_gps_ways(COVERAGE_DIR / "gps_ways.geojson")
    result: dict[str, Any] = {
        "networkx": NetworkXRouter(graph, gps_ways=gps_ways),
        "networkx_no_gps": NetworkXRouter(graph, replace(RoutingPreferences(), popularity_weight=0.0)),
        "shortest": NetworkXRouter(graph, shortest_preferences()),
    }
    if use_valhalla:
        valhalla = ValhallaRouter(VALHALLA_URL)
        try:  # one test route, so an unreachable server is reported once
            valhalla.route_geojson(58.3780, 26.7290, 58.3800, 26.7200)
            result["valhalla"] = valhalla
        except ValhallaUnavailable as error:
            print(f"Valhalla not reachable at {VALHALLA_URL}, skipping it: {error}")
    return result


def evaluate(traces: pd.DataFrame, routers: dict[str, Any], log: Callable[[str], None] = print) -> pd.DataFrame:
    rows = []
    trips = list(traces.groupby("trip"))
    for number, (trip, points) in enumerate(trips, start=1):
        coordinates = points[["lon", "lat"]].to_numpy()
        trace = to_metric_line(coordinates)
        (start_lon, start_lat), (end_lon, end_lat) = coordinates[0], coordinates[-1]
        for name, router in routers.items():
            try:
                collection = router.route_geojson(start_lat, start_lon, end_lat, end_lon)
            except ValhallaUnavailable as error:
                log(f"  {name} skipped for trip {trip}: {error}")
                continue
            except Exception as error:  # no path between the snapped nodes, etc.
                log(f"  {name} failed for trip {trip}: {error}")
                continue
            best = collection["features"][0]["geometry"]["coordinates"]
            rows.append({"trip": trip, "engine": name, "trace_km": round(trace.length / 1000, 2), **score(to_metric_line(best), trace)})
        if number % 50 == 0:
            log(f"Routed {number}/{len(trips)} trips")
    return pd.DataFrame(rows)


def paired_difference(results: pd.DataFrame, metric: str, engine: str, reference: str, seed: int = 0) -> dict[str, float]:
    """Mean of ``engine - reference`` over trips both routed, with a bootstrap 95% interval."""

    table = results.pivot(index="trip", columns="engine", values=metric)[[engine, reference]].dropna()
    differences = (table[engine] - table[reference]).to_numpy()
    rng = np.random.default_rng(seed)
    means = rng.choice(differences, size=(2000, len(differences))).mean(axis=1)
    low, high = np.percentile(means, [2.5, 97.5])
    return {"mean": round(float(differences.mean()), 3), "ci95": [round(float(low), 3), round(float(high), 3)], "trips": int(len(differences))}


def summarise(results: pd.DataFrame) -> dict[str, Any]:
    metrics = ["route_on_trace", "trace_on_route", "route_to_trace_length"]
    per_engine = results.groupby("engine")[metrics].agg(["mean", "median"]).round(3)
    summary: dict[str, Any] = {
        "trips": int(results["trip"].nunique()),
        "match_distance_m": MATCH_M,
        "per_engine": {
            engine: {f"{metric}_{stat}": float(per_engine.loc[engine, (metric, stat)]) for metric in metrics for stat in ("mean", "median")}
            for engine in per_engine.index
        },
        "networkx_minus_other": {
            other: {metric: paired_difference(results, metric, "networkx", other) for metric in metrics[:2]}
            for other in sorted(set(results["engine"]) - {"networkx"})
        },
    }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sample", action="store_true", help="sample traces from the raw Locations data first")
    parser.add_argument("--locations", type=Path, default=DEFAULT_LOCATIONS_DIR)
    parser.add_argument("--routes", type=Path, default=DEFAULT_ROUTES_FILE)
    parser.add_argument("--trips", type=int, default=400, help="how many sampled trips to route")
    parser.add_argument("--no-valhalla", action="store_true")
    args = parser.parse_args()

    if args.sample:
        traces = sample_traces(args.locations, args.routes, SAMPLE_PER_10K)
        TRACES_FILE.parent.mkdir(parents=True, exist_ok=True)
        traces.to_csv(TRACES_FILE, index=False)
        print(f"Kept {traces['trip'].nunique()} trips in {TRACES_FILE}")
    traces = pd.read_csv(TRACES_FILE)
    trips = sorted(traces["trip"].unique())
    chosen = set(random.Random(0).sample(trips, min(args.trips, len(trips))))
    traces = traces[traces["trip"].isin(chosen)]

    routers = engines(use_valhalla=not args.no_valhalla)
    results = evaluate(traces, routers)
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    results.to_csv(OUTPUT_FILE, index=False)
    summary = summarise(results)
    SUMMARY_FILE.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
