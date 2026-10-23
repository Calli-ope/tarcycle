"""Compare route quality across routing engines on random station pairs.

    python -m backend.evaluate --pairs 100

Every route's geometry is matched to the router's graph every 10 m, so all
engines are measured the same way: share of length per facility class, rough
surfaces, ways popular with Smart Bike riders, major-road crossings, detour
and travel time. Writes one row per pair and engine to
`data/evaluation/route_quality.csv` and prints the mean per engine.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
from dataclasses import fields, replace
from pathlib import Path

import networkx as nx
import numpy as np
import shapely
from shapely.geometry import LineString

from .app import BROUTER_URL, COVERAGE_DIR, STATIONS_FILE, VALHALLA_URL
from .brouter import BRouterRouter, BRouterUnavailable
from .gps_layer import edge_gps_trips, load_gps_ways
from .graph import ROOT_DIR, load_graph
from .models import RoutingPreferences
from .router import ROUGH_SURFACES, NetworkXRouter, _as_text, _has_any
from .stations import load_stations
from .valhalla import ValhallaRouter, ValhallaUnavailable


OUTPUT_FILE = ROOT_DIR / "data" / "evaluation" / "route_quality.csv"
SAMPLE_M = 10.0
MATCH_M = 15.0
POPULAR_TRIPS = 1000
CLASSES = ("car_free", "lane_or_track", "sidewalk", "quiet_road", "major_road")


def shortest_preferences() -> RoutingPreferences:
    """Every factor neutral: the route with the least distance."""

    neutral = {
        field.name: 1.0
        for field in fields(RoutingPreferences)
        if field.name.endswith("_factor")
    }
    zero = {
        name: 0.0
        for name in (
            "facility_transition_cost",
            "uphill_factor",
            "signal_delay_s",
            "major_crossing_cost",
            "left_turn_cost",
            "popularity_weight",
        )
    }
    return replace(RoutingPreferences(), **{**neutral, **zero})


def edge_class(router: NetworkXRouter, edge) -> str:
    facility = router.facility[edge]
    if edge in router.sidewalks:
        return "sidewalk"
    if facility in ("dedicated_cycleway", "path"):
        return "car_free"
    if facility == "road_cycleway":
        return "lane_or_track"
    return "major_road" if edge in router.on_major else "quiet_road"


class Scorer:
    def __init__(self, router: NetworkXRouter, gps_ways) -> None:
        self.router = router
        self.gps_ways = gps_ways
        major = [router.lines[edge] for edge in router.on_major]
        self.major_roads = shapely.union_all(major)

    def score(self, coordinates: list[list[float]]) -> dict[str, float]:
        router = self.router
        line = LineString(coordinates)
        metric = shapely.transform(
            line, lambda xy: xy * [router.x_scale * 111_320, 111_320]
        )
        length = metric.length
        count = max(2, int(length // SAMPLE_M) + 1)
        samples = shapely.line_interpolate_point(
            metric, np.linspace(0, length, count)
        )
        scaled = shapely.transform(samples, lambda xy: xy / 111_320)
        sample_index, edge_index = router.tree.query_nearest(
            scaled, max_distance=MATCH_M / 111_320
        )
        nearest = dict(zip(sample_index.tolist(), edge_index.tolist()))
        matched = [router.edges[nearest[i]] for i in range(count) if i in nearest]
        result = {name: 0.0 for name in CLASSES}
        rough = popular = 0
        for edge in matched:
            result[edge_class(router, edge)] += 1
            attributes = router.graph.edges[edge]
            rough += _has_any(_as_text(attributes.get("surface")), ROUGH_SURFACES)
            trips = edge_gps_trips(attributes, self.gps_ways) if self.gps_ways else None
            popular += (trips or 0) >= POPULAR_TRIPS
        shares = {f"share_{name}": value / count for name, value in result.items()}
        # Crossing a major road meets it in a point; riding along it overlaps
        # (after snapping, so other engines' coordinates line up with OSM).
        snapped = shapely.snap(line, self.major_roads, 2 / 111_320)
        meeting = shapely.get_parts(snapped.intersection(self.major_roads))
        return {
            "length_m": round(length, 1),
            **{key: round(value, 3) for key, value in shares.items()},
            "share_unmatched": round(1 - len(matched) / count, 3),
            "share_rough_surface": round(rough / count, 3),
            "share_popular": round(popular / count, 3),
            "major_road_crossings": int(
                sum(part.geom_type == "Point" for part in meeting)
            ),
        }


def station_pairs(count: int, seed: int) -> list[tuple]:
    stations = load_stations(STATIONS_FILE)
    rng = random.Random(seed)
    pairs = []
    while len(pairs) < count:
        a, b = rng.sample(stations, 2)
        distance = math.dist(
            (a.longitude * 0.53 * 111_320, a.latitude * 111_320),
            (b.longitude * 0.53 * 111_320, b.latitude * 111_320),
        )
        if 1000 <= distance <= 6000:
            pairs.append((a, b))
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pairs", type=int, default=100)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", type=Path, default=OUTPUT_FILE)
    args = parser.parse_args()

    graph = load_graph()
    gps_file = COVERAGE_DIR / "gps_ways.geojson"
    gps_ways = load_gps_ways(gps_file) if gps_file.exists() else None
    engines = {
        "networkx": NetworkXRouter(graph, gps_ways=gps_ways),
        "networkx_no_gps": NetworkXRouter(
            graph, replace(RoutingPreferences(), popularity_weight=0.0)
        ),
        "shortest": NetworkXRouter(graph, shortest_preferences()),
        "valhalla": ValhallaRouter(VALHALLA_URL),
        "brouter_trekking": BRouterRouter(BROUTER_URL, "trekking"),
        "brouter_low_traffic": BRouterRouter(BROUTER_URL, "fastbike-verylowtraffic"),
    }
    scorer = Scorer(engines["networkx"], gps_ways)
    rows = []
    skipped = 0
    for index, (origin, destination) in enumerate(station_pairs(args.pairs, args.seed)):
        points = (origin.latitude, origin.longitude, destination.latitude, destination.longitude)
        pair_rows = []
        for name, engine in engines.items():
            try:
                feature = engine.route_geojson(*points)["features"][0]
            except (ValhallaUnavailable, BRouterUnavailable):
                continue
            except nx.NetworkXNoPath:
                pair_rows = []
                break
            row = {
                "pair": index,
                "origin": origin.name,
                "destination": destination.name,
                "engine": name,
                "time_min": feature["properties"].get("time_minutes"),
                **scorer.score(feature["geometry"]["coordinates"]),
            }
            pair_rows.append(row)
        if not pair_rows:
            skipped += 1
            continue
        shortest = next(row["length_m"] for row in pair_rows if row["engine"] == "shortest")
        for row in pair_rows:
            row["detour"] = round(row["length_m"] / shortest, 3)
        rows.extend(pair_rows)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    columns = [key for key in rows[0] if key not in ("pair", "origin", "destination", "engine")]
    print("| engine | " + " | ".join(columns) + " |")
    print("|---" * (len(columns) + 1) + "|")
    for name in engines:
        own = [row for row in rows if row["engine"] == name]
        if not own:
            continue
        means = [
            np.mean([row[column] for row in own if row[column] is not None])
            for column in columns
        ]
        print(f"| {name} | " + " | ".join(f"{value:.3g}" for value in means) + " |")
    print(f"\n{len(rows)} routes ({skipped} pairs without a route skipped), written to {args.out}")


if __name__ == "__main__":
    main()
