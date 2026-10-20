"""Compare Tartu Smart Bike GPS traces with the OpenStreetMap network.

Offline pipeline, run once (or whenever new GPS data arrives):

    python -m backend.gps_coverage --locations data/raw/Locations

Each trip is cleaned and densified, then rasterised onto a 10 m grid in
EPSG:3301 (metres). Every grid cell counts the distinct trips and riders
that passed through it and is compared with OSM:

- ``cycle_infra``       near a cycleway or a road with a cycle lane/track
- ``road_no_infra``     near a way bikes may use, but without cycle infra
- ``osm_not_bikeable``  near an OSM way bikes are not allowed on (footway, steps, ...)
- ``no_osm_way``        not near any OSM way: a path missing from OSM

Neighbouring busy cells of the last two classes are grouped into ranked
candidate segments for expanding the OSM bike map. Only cells with enough
trips *and* riders are exported, so the outputs are aggregated and do not
reveal individual trips.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
import pandas as pd
import shapely
from pyproj import Transformer
from shapely.geometry import MultiPoint, Polygon

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_LOCATIONS_DIR = ROOT_DIR / "data" / "raw" / "Locations"
DEFAULT_OUTPUT_DIR = ROOT_DIR / "cache" / "gps_coverage"
PLACE = "Tartu, Estonia"
METRIC_CRS = "EPSG:3301"
CELL_ID_FACTOR = 10_000_000

CELL_CLASSES = ("cycle_infra", "road_no_infra", "osm_not_bikeable", "no_osm_way")
CANDIDATE_CLASSES = ("osm_not_bikeable", "no_osm_way")

# OSM tags needed to tell cycle infrastructure apart; osmnx drops them by default.
EXTRA_WAY_TAGS = [
    "bicycle",
    "cycleway",
    "cycleway:both",
    "cycleway:left",
    "cycleway:right",
    "surface",
]
INFRA_CYCLEWAY_VALUES = {"lane", "track", "opposite_lane", "opposite_track", "share_busway"}
BIKE_ALLOWED_VALUES = {"yes", "designated", "permissive"}
BIKE_FORBIDDEN_VALUES = {"no", "dismount", "use_sidepath"}
NOT_BIKEABLE_HIGHWAYS = {
    "bridleway",
    "bus_guideway",
    "corridor",
    "elevator",
    "escalator",
    "footway",
    "motorway",
    "motorway_link",
    "platform",
    "steps",
}


@dataclass(frozen=True)
class CoverageSettings:
    cell_size_m: float = 10.0
    sample_step_m: float = 5.0
    max_gap_m: float = 200.0
    max_gap_s: float = 60.0
    max_speed_ms: float = 12.0
    near_m: float = 15.0
    min_trips: int = 10
    min_users: int = 5
    neighbour_cells: int = 2
    min_candidate_cells: int = 5
    area_buffer_m: float = 500.0


# --- OSM reference network -------------------------------------------------


def _tag_values(value: Any) -> set[str]:
    """OSM tag values as a lower-case set; simplified edges may hold lists."""

    if value is None or (isinstance(value, float) and np.isnan(value)):
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).lower() for item in value}
    return {part.strip().lower() for part in str(value).split(";") if part.strip()}


def classify_way(tags: dict[str, Any]) -> str:
    """Classify one OSM way as ``cycle_infra``, ``bikeable`` or ``not_bikeable``."""

    highway = _tag_values(tags.get("highway"))
    bicycle = _tag_values(tags.get("bicycle"))
    access = _tag_values(tags.get("access"))
    cycleway = set().union(
        *(_tag_values(tags.get(key)) for key in EXTRA_WAY_TAGS if key.startswith("cycleway"))
    )
    if "cycleway" in highway or "designated" in bicycle or cycleway & INFRA_CYCLEWAY_VALUES:
        return "cycle_infra"
    if bicycle & BIKE_ALLOWED_VALUES:
        return "bikeable"
    if bicycle & BIKE_FORBIDDEN_VALUES or access & {"no", "private"}:
        return "not_bikeable"
    if highway and highway <= NOT_BIKEABLE_HIGHWAYS:
        return "not_bikeable"
    return "bikeable"


def study_area(place: str, buffer_m: float) -> Polygon:
    """The city boundary, buffered, in EPSG:3301."""

    boundary = ox.geocode_to_gdf(place).to_crs(METRIC_CRS)
    return boundary.geometry.iloc[0].buffer(buffer_m)


def download_osm_edges(area: Polygon) -> gpd.GeoDataFrame:
    """All OSM ways in the area (not only the bike network), classified, in EPSG:3301."""

    ox.settings.useful_tags_way = list(
        dict.fromkeys([*ox.settings.useful_tags_way, *EXTRA_WAY_TAGS])
    )
    polygon = gpd.GeoSeries([area], crs=METRIC_CRS).to_crs("EPSG:4326").iloc[0]
    graph = ox.graph_from_polygon(polygon, network_type="all", retain_all=True)
    edges = ox.graph_to_gdfs(graph, nodes=False).reset_index(drop=True)
    edges["edge_class"] = [classify_way(row) for row in edges.to_dict("records")]
    edges = edges.to_crs(METRIC_CRS)
    # Two-way streets appear once per direction; keep one copy.
    wkb = pd.Series(shapely.to_wkb(shapely.normalize(edges.geometry.values)))
    edges = edges[~wkb.duplicated().to_numpy()]
    joined = lambda column: [
        ",".join(sorted(_tag_values(value))) for value in edges.get(column, [None] * len(edges))
    ]
    return gpd.GeoDataFrame(
        {"highway": joined("highway"), "name": joined("name"), "edge_class": edges["edge_class"].to_numpy()},
        geometry=edges.geometry.values,
        crs=METRIC_CRS,
    ).reset_index(drop=True)


def load_osm_reference(
    output_dir: Path, settings: CoverageSettings, refresh: bool = False
) -> tuple[Polygon, gpd.GeoDataFrame]:
    """Load the cached study area and OSM edges, downloading them once if needed."""

    area_file = output_dir / "study_area.geojson"
    edges_file = output_dir / "osm_edges.gpkg"
    if refresh or not area_file.exists() or not edges_file.exists():
        print("Downloading OSM reference network (one-off)...")
        area = study_area(PLACE, settings.area_buffer_m)
        edges = download_osm_edges(area)
        output_dir.mkdir(parents=True, exist_ok=True)
        gpd.GeoDataFrame(geometry=[area], crs=METRIC_CRS).to_file(area_file, driver="GeoJSON")
        edges.to_file(edges_file, driver="GPKG")
        return area, edges
    area = gpd.read_file(area_file).to_crs(METRIC_CRS).geometry.iloc[0]
    return area, gpd.read_file(edges_file).to_crs(METRIC_CRS)


# --- GPS traces -------------------------------------------------------------


def densify_tracks(
    route: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    t: np.ndarray,
    settings: CoverageSettings,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Resample GPS tracks every ``sample_step_m`` along each plausible segment.

    Inputs must be sorted by route, then time. A segment between consecutive
    points is kept only if both belong to the same route, the gap is short in
    space and time, and the implied speed is realistic. Isolated points (GPS
    spikes, single-point trips) produce no samples. Returns ``(route, x, y)``.
    """

    dx = np.diff(x)
    dy = np.diff(y)
    dt = np.diff(t)
    distance = np.hypot(dx, dy)
    valid = (
        (route[1:] == route[:-1])
        & (distance <= settings.max_gap_m)
        & (dt <= settings.max_gap_s)
        & (distance <= settings.max_speed_ms * np.maximum(dt, 1.0))
    )
    segments = np.flatnonzero(valid)
    steps = np.maximum(1, np.ceil(distance[segments] / settings.sample_step_m)).astype(np.int64)
    owner = np.repeat(segments, steps)
    offset = np.arange(steps.sum()) - np.repeat(np.cumsum(steps) - steps, steps)
    fraction = offset / np.repeat(steps, steps)
    sample_x = np.concatenate([x[owner] + dx[owner] * fraction, x[segments + 1]])
    sample_y = np.concatenate([y[owner] + dy[owner] * fraction, y[segments + 1]])
    sample_route = np.concatenate([route[owner], route[segments + 1]])
    return sample_route, sample_x, sample_y


def cell_ids(x: np.ndarray, y: np.ndarray, cell_size_m: float) -> np.ndarray:
    """Integer id of the grid cell holding each EPSG:3301 point."""

    column = np.floor(x / cell_size_m).astype(np.int64)
    row = np.floor(y / cell_size_m).astype(np.int64)
    return column * CELL_ID_FACTOR + row


def cell_centres(ids: np.ndarray, cell_size_m: float) -> tuple[np.ndarray, np.ndarray]:
    """Inverse of :func:`cell_ids`: the EPSG:3301 centre of each cell."""

    column, row = np.divmod(ids, CELL_ID_FACTOR)
    return (column + 0.5) * cell_size_m, (row + 0.5) * cell_size_m


def read_locations(path: Path) -> pd.DataFrame:
    """One monthly ``locations_*.csv`` as route, user, lon, lat and epoch seconds."""

    frame = pd.read_csv(
        path,
        usecols=["RouteCode", "UserId", "Latitude", "Longitude", "CoordinatesDate", "CoordinatesTime"],
        dtype={"RouteCode": "int64", "UserId": "string", "CoordinatesDate": "string", "CoordinatesTime": "string"},
    )
    timestamp = pd.to_datetime(
        frame["CoordinatesDate"] + " " + frame["CoordinatesTime"].str.slice(0, 8),
        format="%Y-%m-%d %H:%M:%S",
        errors="coerce",
    )
    frame["seconds"] = (timestamp - pd.Timestamp("1970-01-01")) / pd.Timedelta(seconds=1)
    frame = frame.dropna(subset=["Latitude", "Longitude", "seconds", "UserId"])
    return frame[["RouteCode", "UserId", "Longitude", "Latitude", "seconds"]]


def trip_cells(frame: pd.DataFrame, settings: CoverageSettings) -> pd.DataFrame:
    """Distinct (cell, route, user) visits for every trip in ``frame``."""

    frame = frame.sort_values(["RouteCode", "seconds"], kind="stable")
    to_metres = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True)
    x, y = to_metres.transform(frame["Longitude"].to_numpy(), frame["Latitude"].to_numpy())
    routes = frame["RouteCode"].to_numpy()
    sample_route, sample_x, sample_y = densify_tracks(
        routes, np.asarray(x), np.asarray(y), frame["seconds"].to_numpy(), settings
    )
    user_by_route = frame.drop_duplicates("RouteCode").set_index("RouteCode")["UserId"]
    visits = pd.DataFrame(
        {"cell": cell_ids(sample_x, sample_y, settings.cell_size_m), "route": sample_route}
    ).drop_duplicates()
    visits["user"] = visits["route"].map(user_by_route).to_numpy()
    return visits


def aggregate_locations(files: Iterable[Path], settings: CoverageSettings) -> pd.DataFrame:
    """Per-cell distinct trip and rider counts over all location files."""

    trips = pd.Series(dtype="int64")
    cell_users = None
    for path in files:
        print(f"  {path.name}")
        visits = trip_cells(read_locations(path), settings)
        trips = trips.add(visits["cell"].value_counts(), fill_value=0)
        month_users = visits[["cell", "user"]].drop_duplicates()
        cell_users = (
            month_users
            if cell_users is None
            else pd.concat([cell_users, month_users], ignore_index=True).drop_duplicates()
        )
    if cell_users is None:
        raise ValueError("No location files to aggregate.")
    users = cell_users.groupby("cell")["user"].nunique()
    cells = pd.DataFrame({"trips": trips.astype("int64")})
    cells["users"] = users.reindex(cells.index).fillna(0).astype("int64")
    cells.index.name = "cell"
    return cells.reset_index()


# --- Comparison with OSM ----------------------------------------------------


def classify_cells(
    distance_infra: np.ndarray,
    distance_bikeable: np.ndarray,
    distance_any: np.ndarray,
    near_m: float,
) -> np.ndarray:
    """Cell class from the distance to the nearest way of each OSM layer."""

    return np.select(
        [distance_infra <= near_m, distance_bikeable <= near_m, distance_any <= near_m],
        CELL_CLASSES[:3],
        CELL_CLASSES[3],
    )


def _nearest(
    geometries: np.ndarray, points: np.ndarray, max_distance: float
) -> tuple[np.ndarray, np.ndarray]:
    """Distance to and index of the nearest geometry (``inf`` / -1 beyond ``max_distance``)."""

    distance = np.full(len(points), np.inf)
    index = np.full(len(points), -1, dtype=np.int64)
    if len(geometries) == 0 or len(points) == 0:
        return distance, index
    pairs, found = shapely.STRtree(geometries).query_nearest(
        points, max_distance=max_distance, return_distance=True, all_matches=False
    )
    distance[pairs[0]] = found
    index[pairs[0]] = pairs[1]
    return distance, index


def compare_with_osm(
    cells: pd.DataFrame, edges: gpd.GeoDataFrame, settings: CoverageSettings
) -> pd.DataFrame:
    """Add EPSG:3301 centres, the cell class and the nearest OSM edge to ``cells``."""

    x, y = cell_centres(cells["cell"].to_numpy(), settings.cell_size_m)
    points = shapely.points(x, y)
    geometry = edges.geometry.values
    infra = (edges["edge_class"] == "cycle_infra").to_numpy()
    bikeable = infra | (edges["edge_class"] == "bikeable").to_numpy()
    search = settings.near_m * 2
    distance_infra, _ = _nearest(geometry[infra], points, search)
    distance_bikeable, _ = _nearest(geometry[bikeable], points, search)
    distance_any, nearest_edge = _nearest(geometry, points, search)
    result = cells.copy()
    result["x"] = x
    result["y"] = y
    result["cell_class"] = classify_cells(
        distance_infra, distance_bikeable, distance_any, settings.near_m
    )
    result["edge"] = np.where(distance_any <= settings.near_m, nearest_edge, -1)
    return result


def summarise(cells: pd.DataFrame, settings: CoverageSettings) -> dict[str, Any]:
    """Share of ridden distance per class, computed before the privacy filter.

    One trip passing one 10 m cell is ~10 m ridden, so trip-cells summed per
    class approximate the ridden distance in that class.
    """

    trip_cells_by_class = cells.groupby("cell_class")["trips"].sum().reindex(CELL_CLASSES, fill_value=0)
    total = max(1, int(trip_cells_by_class.sum()))
    return {
        "ridden_km_approx": {
            name: round(int(value) * settings.cell_size_m / 1000, 1)
            for name, value in trip_cells_by_class.items()
        },
        "share_of_ridden_distance": {
            name: round(int(value) / total, 4) for name, value in trip_cells_by_class.items()
        },
        "settings": asdict(settings),
    }


def find_candidates(cells: pd.DataFrame, settings: CoverageSettings) -> gpd.GeoDataFrame:
    """Group nearby busy off-network cells into ranked candidate segments.

    Cells of the same candidate class within ``neighbour_cells`` grid steps
    of each other form one segment. ``score`` is trips × segment length, so
    long, busy segments rank first.
    """

    columns = ["cell_class", "cells", "length_m", "median_trips", "max_trips", "score", "geometry"]
    rows = []
    reach = range(-settings.neighbour_cells, settings.neighbour_cells + 1)
    offsets = [dx * CELL_ID_FACTOR + dy for dx in reach for dy in reach if dx or dy]
    for cell_class in CANDIDATE_CLASSES:
        subset = cells[cells["cell_class"] == cell_class].set_index("cell")
        graph = nx.Graph()
        graph.add_nodes_from(subset.index)
        members = set(subset.index)
        graph.add_edges_from(
            (cell, cell + offset) for cell in subset.index for offset in offsets if cell + offset in members
        )
        for component in nx.connected_components(graph):
            if len(component) < settings.min_candidate_cells:
                continue
            group = subset.loc[sorted(component)]
            centres = MultiPoint(list(zip(group["x"], group["y"])))
            rectangle = centres.minimum_rotated_rectangle
            side_lengths = (
                np.hypot(*np.diff(np.asarray(rectangle.exterior.coords)[:3], axis=0).T)
                if isinstance(rectangle, Polygon)
                else [rectangle.length]
            )
            length = float(max(side_lengths)) + settings.cell_size_m
            median_trips = float(group["trips"].median())
            rows.append(
                {
                    "cell_class": cell_class,
                    "cells": len(group),
                    "length_m": round(length),
                    "median_trips": int(median_trips),
                    "max_trips": int(group["trips"].max()),
                    "score": round(median_trips * length),
                    "geometry": centres.buffer(settings.cell_size_m * 0.75, quad_segs=2).simplify(1.0),
                }
            )
    candidates = gpd.GeoDataFrame(
        pd.DataFrame(rows, columns=columns), geometry="geometry", crs=METRIC_CRS
    ).sort_values("score", ascending=False)
    candidates.insert(0, "rank", np.arange(1, len(candidates) + 1))
    return candidates.reset_index(drop=True)


def edge_usage(
    cells: pd.DataFrame, edges: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """OSM edges with the median trip count of the busy cells matched to them."""

    matched = cells[cells["edge"] >= 0]
    usage = matched.groupby("edge")["trips"].median().astype("int64")
    used = edges.iloc[usage.index.to_numpy()].copy()
    used["trips"] = usage.to_numpy()
    return used.sort_values("trips")


# --- Pipeline -----------------------------------------------------------------


def _write_geojson(frame: gpd.GeoDataFrame, path: Path) -> None:
    frame.to_crs("EPSG:4326").to_file(path, driver="GeoJSON", COORDINATE_PRECISION=6)


def build(
    locations_dir: Path,
    output_dir: Path,
    settings: CoverageSettings,
    refresh_osm: bool = False,
) -> dict[str, Any]:
    files = sorted(locations_dir.glob("locations_*.csv"))
    if not files:
        raise FileNotFoundError(f"No locations_*.csv files in {locations_dir}")
    area, edges = load_osm_reference(output_dir, settings, refresh_osm)
    print(f"Rasterising {len(files)} GPS files onto a {settings.cell_size_m:g} m grid...")
    cells = aggregate_locations(files, settings)
    x, y = cell_centres(cells["cell"].to_numpy(), settings.cell_size_m)
    cells = cells[shapely.contains_xy(area, x, y)]
    print(f"Comparing {len(cells)} cells with {len(edges)} OSM ways...")
    cells = compare_with_osm(cells, edges, settings)
    summary = summarise(cells, settings)

    busy = cells[(cells["trips"] >= settings.min_trips) & (cells["users"] >= settings.min_users)]
    candidates = find_candidates(busy, settings)
    summary["exported_cells"] = len(busy)
    summary["candidates"] = candidates["cell_class"].value_counts().to_dict()

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_geojson(
        gpd.GeoDataFrame(
            busy[["trips", "cell_class"]].reset_index(drop=True),
            geometry=shapely.points(busy["x"], busy["y"]),
            crs=METRIC_CRS,
        ),
        output_dir / "cells.geojson",
    )
    _write_geojson(edge_usage(busy, edges), output_dir / "edges.geojson")
    _write_geojson(candidates, output_dir / "candidates.geojson")
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--locations", type=Path, default=DEFAULT_LOCATIONS_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--refresh-osm", action="store_true", help="re-download OSM ways")
    parser.add_argument("--near", type=float, default=CoverageSettings.near_m, help="metres from a way that still count as on it")
    parser.add_argument("--min-trips", type=int, default=CoverageSettings.min_trips)
    parser.add_argument("--min-users", type=int, default=CoverageSettings.min_users)
    args = parser.parse_args()
    settings = CoverageSettings(near_m=args.near, min_trips=args.min_trips, min_users=args.min_users)
    summary = build(args.locations, args.out, settings, args.refresh_osm)
    print(json.dumps(summary["share_of_ridden_distance"], indent=2))
    print(f"Wrote results to {args.out}")


if __name__ == "__main__":
    main()
