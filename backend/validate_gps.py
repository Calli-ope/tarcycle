"""Does the popularity factor bring routes closer to where riders actually ride?

    python -m backend.validate_gps --months 202509 202511

Builds the popularity layer without those months (once, ~15 min), then routes a
sample of their trips with every engine and measures the overlap with each track.
Needs the raw GPS data in `data/raw/`. Only aggregates over many trips are written.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path
from typing import Iterable, Iterator

import networkx as nx
import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import LineString

from .brouter import BRouterUnavailable
from .evaluate import OUTPUT_FILE, Scorer, _markdown, comparison_engines, paired_differences
from .gps_coverage import (
    DEFAULT_LOCATIONS_DIR,
    METRIC_CRS,
    CoverageSettings,
    build,
    read_location_chunks,
    valid_route_codes,
)
from .gps_layer import load_gps_ways
from .graph import ROOT_DIR, load_graph
from .valhalla import ValhallaUnavailable


REPORT_FILE = OUTPUT_FILE.with_name("gps_overlap.md")
RAW_DIR = ROOT_DIR / "data" / "raw"
SAMPLE_SEED = 2
# GPS tracks wander more than engines' geometry does.
OVERLAP_M = 20.0
# Trips like the evaluation's pairs. Loops and errands (long compared with the
# distance covered) were not choosing a way from A to B.
MINIMUM_TRIP_M = 1000.0
MAXIMUM_TRIP_M = 6000.0
MAXIMUM_TRACK_DETOUR = 1.6

_TO_METRES = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True)
_TO_LONLAT = Transformer.from_crs(METRIC_CRS, "EPSG:4326", always_xy=True)


def clean_track(
    x: np.ndarray, y: np.ndarray, seconds: np.ndarray, settings: CoverageSettings
) -> np.ndarray | None:
    """A track's points in metres without GPS spikes; None if the signal was lost
    for too long to know where the rider went."""

    kept = [0]
    for index in range(1, len(x)):
        distance = float(np.hypot(x[index] - x[kept[-1]], y[index] - y[kept[-1]]))
        elapsed = float(seconds[index] - seconds[kept[-1]])
        if distance > settings.max_speed_ms * max(elapsed, 1.0):
            continue
        if distance > settings.max_gap_m or elapsed > settings.max_gap_s:
            return None
        kept.append(index)
    return np.column_stack([x[kept], y[kept]]) if len(kept) >= 2 else None


def is_directed_trip(track: np.ndarray) -> bool:
    straight = float(np.hypot(*(track[-1] - track[0])))
    return (
        MINIMUM_TRIP_M <= straight <= MAXIMUM_TRIP_M
        and LineString(track).length <= MAXIMUM_TRACK_DETOUR * straight
    )


def iter_tracks(
    files: Iterable[Path],
    settings: CoverageSettings,
    valid_routes: np.ndarray | None = None,
) -> Iterator[np.ndarray]:
    for path in files:
        print(f"  {path.name}", flush=True)
        for frame in read_location_chunks(path):
            if valid_routes is not None:
                frame = frame[frame["RouteCode"].isin(valid_routes)]
            for _, trip in frame.sort_values("seconds", kind="stable").groupby("RouteCode"):
                x, y = _TO_METRES.transform(trip["Longitude"].to_numpy(), trip["Latitude"].to_numpy())
                track = clean_track(x, y, trip["seconds"].to_numpy(), settings)
                if track is not None and is_directed_trip(track):
                    yield track


def sample_tracks(tracks: Iterable[np.ndarray], count: int, seed: int) -> list[np.ndarray]:
    """A uniform sample without knowing how many tracks there are (reservoir sampling)."""

    rng = random.Random(seed)
    sample: list[np.ndarray] = []
    for seen, track in enumerate(tracks):
        if len(sample) < count:
            sample.append(track)
        elif (slot := rng.randrange(seen + 1)) < count:
            sample[slot] = track
    return sample


def track_overlap(scorer: Scorer, route, track) -> float:
    """Harmonic mean of how much of the route the rider rode and how much of the
    ride the route covers."""

    route_on_track = scorer.overlap(route, track, OVERLAP_M)
    track_on_route = scorer.overlap(track, route, OVERLAP_M)
    total = route_on_track + track_on_route
    return 2 * route_on_track * track_on_route / total if total else 0.0


def compare_with_tracks(tracks: Iterable[np.ndarray], engines: dict, scorer: Scorer) -> pd.DataFrame:
    """One row per trip and engine; holds no coordinates."""

    rows = []
    for number, track in enumerate(tracks):
        longitude, latitude = _TO_LONLAT.transform(track[:, 0], track[:, 1])
        coordinates = np.column_stack([longitude, latitude]).tolist()
        points = (latitude[0], longitude[0], latitude[-1], longitude[-1])
        for name, engine in engines.items():
            try:
                route = engine.route_geojson(*points)["features"][0]["geometry"]["coordinates"]
            except (ValhallaUnavailable, BRouterUnavailable, nx.NetworkXNoPath):
                continue
            rows.append(
                {"pair": number, "engine": name, "overlap": track_overlap(scorer, route, coordinates)}
            )
    return pd.DataFrame(rows)


def report(table: pd.DataFrame, reference: str = "networkx_no_gps") -> str:
    """Compares only trips every engine could route."""

    engines = table["engine"].nunique()
    table = table[table.groupby("pair")["engine"].transform("nunique") == engines]
    groups = table.groupby("engine", sort=False)["overlap"]
    overall = pd.DataFrame({"mean overlap": groups.mean(), "median overlap": groups.median()})
    overall[f"difference from {reference}"] = pd.Series(
        paired_differences(table, reference, "overlap")
    )
    return (
        f"## Overlap with {table['pair'].nunique()} held-out trips\n\n"
        f"{_markdown(overall.fillna('-'))}\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--months", nargs="+", required=True, help="held-out months, e.g. 202509")
    parser.add_argument("--trips", type=int, default=1000)
    args = parser.parse_args()

    settings = CoverageSettings()
    routes_file = next(RAW_DIR.rglob("routes_2025.csv"), None)
    months = tuple(args.months)
    # The raw folder is gitignored; the layer depends on which months are left out.
    layer = RAW_DIR / f"validation_{'_'.join(months)}"
    if not (layer / "gps_ways.geojson").exists():
        print(f"Building the popularity layer without {', '.join(months)}...")
        build(DEFAULT_LOCATIONS_DIR, layer, settings, routes_file=routes_file, exclude_months=months)

    files = [
        path
        for path in sorted(DEFAULT_LOCATIONS_DIR.glob("locations_*.csv"))
        if any(month in path.name for month in months)
    ]
    if not files:
        raise FileNotFoundError(f"No locations_*.csv files for {months} in {DEFAULT_LOCATIONS_DIR}")
    valid_routes = valid_route_codes(routes_file, settings.min_trip_minutes) if routes_file else None
    print(f"Sampling {args.trips} trips...")
    tracks = sample_tracks(iter_tracks(files, settings, valid_routes), args.trips, SAMPLE_SEED)
    engines = comparison_engines(load_graph(), load_gps_ways(layer / "gps_ways.geojson"))
    print(f"Routing {len(tracks)} trips...")
    summary = report(compare_with_tracks(tracks, engines, Scorer(engines["networkx"])))
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(summary, encoding="utf-8")
    print(summary)
    print(f"Written to {REPORT_FILE}")


if __name__ == "__main__":
    main()
