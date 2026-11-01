"""Compare route quality across routing engines on random station pairs.

    python -m backend.evaluate --pairs 100

Every route's geometry is matched to the router's graph every 10 m, so all
engines are measured the same way. Writes one row per pair and engine to
`data/evaluation/route_quality.csv` and the comparison to `summary.md` beside it.
"""

from __future__ import annotations

import argparse
import math
import random
from dataclasses import fields, replace
from pathlib import Path

import networkx as nx
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import LineString

from .app import BROUTER_URL, COVERAGE_DIR, STATIONS_FILE, VALHALLA_URL
from .brouter import BRouterRouter, BRouterUnavailable
from .gps_layer import load_gps_ways
from .graph import CYCLEWAY_KEYS, ROOT_DIR, load_graph
from .models import RoutingPreferences
from .router import (
    CROSSING_DELAY_S,
    NOT_ROADS,
    ROUGH_SURFACES,
    URBAN_SPEED_KMH,
    NetworkXRouter,
    _as_text,
    _has_any,
    _number,
)
from .stations import load_stations
from .valhalla import ValhallaRouter, ValhallaUnavailable


OUTPUT_FILE = ROOT_DIR / "data" / "evaluation" / "route_quality.csv"
SAMPLE_M = 10.0
MATCH_M = 15.0
CLASSES = ("car_free", "lane_or_track", "sidewalk", "quiet_road", "major_road")
# Major-road crossings: other engines' coordinates differ slightly from OSM.
SNAP_M = 2.0
CROSSING_LOOK_M = 25.0
CROSSING_CLEARANCE_M = 3.0
# One crossing of a dual carriageway or junction meets several centre lines.
CROSSING_MERGE_M = 30.0
# Crossing at grade shares a node with the road; bridges and underpasses do not.
CROSSING_JUNCTION_M = 10.0
# OSMnx drops signal nodes in the middle of an edge, leaving others of the junction.
SIGNAL_CROSSING_M = 40.0
SIGNAL_PASSED_M = 15.0
SIGNAL_MERGE_M = 40.0
TURN_DEGREES = 45.0
TURN_WINDOW_SAMPLES = 2
MAXIMUM_UNMATCHED_SHARE = 0.05
BOOTSTRAP_SAMPLES = 4000
COMPARED = (
    "detour",
    "common_time_min",
    "share_lts_3plus",
    "share_major_road",
    "unsignalled_per_km",
    "signals_per_km",
    "share_rough_surface",
    "turns_per_km",
)


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


def traffic_stress(attributes: dict, sidewalk: bool = False) -> int:
    """Level of Traffic Stress, 1 (calm) to 4, after Mekuria, Furth & Nixon (2012).

    OSM rarely has lane widths or parking, so a painted lane never gets the
    lowest stress above 30 km/h. Sidewalks count as 2: no traffic, but pedestrians.
    """

    highway = _as_text(attributes.get("highway"))
    if sidewalk:
        return 2
    if _has_any(highway, NOT_ROADS):
        return 1
    cycleway = ",".join(_as_text(attributes.get(key)) for key in CYCLEWAY_KEYS)
    cycleway = cycleway.replace("shared_lane", "")
    if "track" in cycleway:
        return 1
    speed = _number(attributes.get("maxspeed")) or URBAN_SPEED_KMH
    # `lanes` on a one-way street is one direction; the tables count both.
    lanes = _number(attributes.get("lanes")) or 2
    if _as_text(attributes.get("oneway")) == "true":
        lanes *= 2
    if "lane" in cycleway:
        if lanes >= 4:
            return 3 if speed <= 50 else 4
        return 1 if speed <= 30 else 2 if speed <= 50 else 3 if speed <= 60 else 4
    residential = _has_any(highway, ("residential", "living_street", "service"))
    if lanes >= 6:
        return 4
    if lanes >= 4:
        return 3 if speed <= 40 else 4
    if speed <= 40:
        return 1 if residential else 2
    if speed <= 50:
        return 2 if residential else 3
    return 4


def major_crossings(route: LineString, roads) -> list[float]:
    """Where a route passes from one side of a major road to the other, as
    positions along it, in metres.

    A place where the route meets a road counts only if the route is clear of
    every major road a little before and after it, and the road lies between
    those two points. Riding along a road meets it many times as the two lines
    wander across each other; none of those places qualify. So neither does a
    junction passed while riding on a major road; traffic stress covers that.
    """

    snapped = shapely.snap(route, roads, SNAP_M)
    meeting = shapely.get_parts(snapped.intersection(roads))
    crossings = []
    last = -math.inf
    for position in sorted(route.project(part.centroid) for part in meeting if not part.is_empty):
        if position - last < CROSSING_MERGE_M:
            continue
        before = route.interpolate(max(0.0, position - CROSSING_LOOK_M))
        after = route.interpolate(position + CROSSING_LOOK_M)
        if (
            before.distance(roads) >= CROSSING_CLEARANCE_M
            and after.distance(roads) >= CROSSING_CLEARANCE_M
            and LineString([before, after]).intersects(roads)
        ):
            crossings.append(position)
            last = position
    return crossings


def turn_count(points: np.ndarray) -> int:
    """Separate changes of direction, in points spaced evenly along a route."""

    window = TURN_WINDOW_SAMPLES
    if len(points) <= 2 * window:
        return 0
    before = points[window:-window] - points[: -2 * window]
    after = points[2 * window :] - points[window:-window]
    change = np.arctan2(after[:, 1], after[:, 0]) - np.arctan2(before[:, 1], before[:, 0])
    change = np.abs((change + math.pi) % (2 * math.pi) - math.pi)
    turning = (change > math.radians(TURN_DEGREES)).astype(int)
    return int(turning[0] + np.count_nonzero(np.diff(turning) == 1))


class Scorer:
    def __init__(self, router: NetworkXRouter) -> None:
        self.router = router
        self.major_roads = self._metric(
            shapely.union_all([router.lines[edge] for edge in router.on_major])
        )
        self.signals = self._node_points(router.signals)
        self.signal_tree = shapely.STRtree(self.signals)
        self.junction_tree = shapely.STRtree(self._node_points(router.major_nodes))

    def _metric(self, geometry):
        router = self.router
        return shapely.transform(
            geometry, lambda xy: xy * [router.x_scale * 111_320, 111_320]
        )

    def _node_points(self, selected) -> np.ndarray:
        nodes = self.router.graph.nodes
        return np.array(
            [self._metric(shapely.Point(nodes[n]["x"], nodes[n]["y"])) for n in selected],
            dtype=object,
        )

    def _samples(self, metric: LineString) -> np.ndarray:
        count = max(2, int(metric.length // SAMPLE_M) + 1)
        return shapely.line_interpolate_point(metric, np.linspace(0, metric.length, count))

    def overlap(self, coordinates, other, distance: float = MATCH_M) -> float:
        """Share of a route within a distance of another route."""

        samples = self._samples(self._metric(LineString(coordinates)))
        return float(shapely.dwithin(samples, self._metric(LineString(other)), distance).mean())

    def _signals_passed(self, metric: LineString) -> int:
        near = self.signal_tree.query(metric, predicate="dwithin", distance=SIGNAL_PASSED_M)
        passed = 0
        last = -math.inf
        for position in sorted(metric.project(self.signals[i]) for i in near):
            if position - last >= SIGNAL_MERGE_M:
                passed += 1
                last = position
        return passed

    def score(self, coordinates: list[list[float]]) -> dict[str, float]:
        router = self.router
        metric = self._metric(LineString(coordinates))
        length = metric.length
        samples = self._samples(metric)
        count = len(samples)
        step = length / (count - 1)
        scaled = shapely.transform(samples, lambda xy: xy / 111_320)
        sample_index, edge_index = router.tree.query_nearest(
            scaled, max_distance=MATCH_M / 111_320
        )
        nearest = dict(zip(sample_index.tolist(), edge_index.tolist()))
        matched = [router.edges[index] for index in nearest.values()]

        result = {name: 0.0 for name in CLASSES}
        stress = {level: 0 for level in (1, 2, 3, 4)}
        rough = 0
        seconds = (count - len(matched)) * step / (router.preferences.cycling_speed_kmh / 3.6)
        for edge in matched:
            attributes = router.graph.edges[edge]
            result[edge_class(router, edge)] += 1
            stress[traffic_stress(attributes, edge in router.sidewalks)] += 1
            rough += _has_any(_as_text(attributes.get("surface")), ROUGH_SURFACES)
            if router.length[edge] > 0:
                seconds += router.seconds[edge] / router.length[edge] * step

        places = [metric.interpolate(p) for p in major_crossings(metric, self.major_roads)]
        crossings = [
            place
            for place in places
            if self.junction_tree.query(
                place, predicate="dwithin", distance=CROSSING_JUNCTION_M
            ).size
        ]
        unsignalled = sum(
            not self.signal_tree.query(
                place, predicate="dwithin", distance=SIGNAL_CROSSING_M
            ).size
            for place in crossings
        )
        signals = self._signals_passed(metric)
        seconds += signals * router.preferences.signal_delay_s + unsignalled * CROSSING_DELAY_S
        kilometres = max(length / 1000, 0.001)
        return {
            "length_m": round(length, 1),
            "common_time_min": round(seconds / 60, 1),
            **{f"share_{name}": round(value / count, 3) for name, value in result.items()},
            "share_unmatched": round(1 - len(matched) / count, 3),
            "share_lts_3plus": round((stress[3] + stress[4]) / count, 3),
            "share_rough_surface": round(rough / count, 3),
            "unsignalled_per_km": round(unsignalled / kilometres, 2),
            "signals_per_km": round(signals / kilometres, 2),
            "turns_per_km": round(
                turn_count(shapely.get_coordinates(samples)) / kilometres, 2
            ),
        }


def bootstrap_interval(values: np.ndarray, seed: int = 0) -> tuple[float, float]:
    """95 % interval of the mean of paired differences."""

    rng = np.random.default_rng(seed)
    means = rng.choice(values, (BOOTSTRAP_SAMPLES, len(values))).mean(axis=1)
    low, high = np.percentile(means, [2.5, 97.5])
    return float(low), float(high)


def paired_differences(
    table: pd.DataFrame, reference: str, column: str
) -> dict[str, str]:
    """Each engine's mean difference from a reference, with its 95 % interval."""

    pivot = table.pivot(index="pair", columns="engine", values=column)
    result = {}
    for engine in pivot.columns.drop(reference):
        difference = (pivot[engine] - pivot[reference]).dropna().to_numpy()
        low, high = bootstrap_interval(difference)
        result[engine] = f"{difference.mean():+.3f} [{low:+.3f}, {high:+.3f}]"
    return result


def _markdown(table: pd.DataFrame) -> str:
    lines = [
        "| | " + " | ".join(map(str, table.columns)) + " |",
        "|---" * (len(table.columns) + 1) + "|",
    ]
    for name, row in table.iterrows():
        cells = [f"{value:.3g}" if isinstance(value, float) else str(value) for value in row]
        lines.append("| " + " | ".join([str(name), *cells]) + " |")
    return "\n".join(lines)


def summarise(table: pd.DataFrame, reference: str = "networkx") -> str:
    """The comparison as Markdown. Routes mostly off the network, or beyond the
    detour limit, are counted as invalid and left out of everything else."""

    engines = list(dict.fromkeys(table["engine"]))
    valid = table[table["valid"].astype(bool)]
    groups = valid.groupby("engine", sort=False)[list(COMPARED)]
    means = groups.mean().reindex(engines)
    means.insert(0, "valid", table.groupby("engine", sort=False)["valid"].mean())
    worst = groups.quantile(0.9).reindex(engines)[["detour", "share_lts_3plus"]]
    paired = pd.DataFrame(
        {column: paired_differences(valid, reference, column) for column in COMPARED}
    )
    return (
        f"## Mean per engine ({table['pair'].nunique()} pairs)\n\n{_markdown(means)}\n\n"
        f"## 90th percentile\n\n{_markdown(worst)}\n\n"
        f"## Paired difference from {reference}, mean [95 % interval]\n\n{_markdown(paired)}\n"
    )


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


def comparison_engines(graph: nx.MultiDiGraph, gps_ways) -> dict:
    return {
        "networkx": NetworkXRouter(graph, gps_ways=gps_ways),
        "networkx_no_gps": NetworkXRouter(
            graph, replace(RoutingPreferences(), popularity_weight=0.0)
        ),
        "shortest": NetworkXRouter(graph, shortest_preferences()),
        "valhalla": ValhallaRouter(VALHALLA_URL),
        "brouter_trekking": BRouterRouter(BROUTER_URL, "trekking"),
        "brouter_low_traffic": BRouterRouter(BROUTER_URL, "fastbike-verylowtraffic"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pairs", type=int, default=100)
    # Seed 1 was used while tuning the router; report on pairs it has not seen.
    parser.add_argument("--seed", type=int, default=2)
    parser.add_argument("--out", type=Path, default=OUTPUT_FILE)
    args = parser.parse_args()

    graph = load_graph()
    gps_file = COVERAGE_DIR / "gps_ways.geojson"
    gps_ways = load_gps_ways(gps_file) if gps_file.exists() else None
    engines = comparison_engines(graph, gps_ways)
    scorer = Scorer(engines["networkx"])
    maximum_detour = engines["networkx"].preferences.maximum_detour_factor
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
                **scorer.score(feature["geometry"]["coordinates"]),
            }
            pair_rows.append(row)
        if not pair_rows:
            skipped += 1
            continue
        shortest = next(row["length_m"] for row in pair_rows if row["engine"] == "shortest")
        for row in pair_rows:
            row["detour"] = round(row["length_m"] / shortest, 3)
            row["valid"] = int(
                row["share_unmatched"] <= MAXIMUM_UNMATCHED_SHARE
                and row["detour"] <= maximum_detour
            )
        rows.extend(pair_rows)

    table = pd.DataFrame(rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.out, index=False, lineterminator="\n")
    summary = summarise(table)
    args.out.with_name("summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    print(f"{len(rows)} routes ({skipped} pairs without a route skipped), written to {args.out}")


if __name__ == "__main__":
    main()
