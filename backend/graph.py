"""Build and cache the Tartu bicycle graph: OSM via Overpass plus 5 m DTM grades.

Run `python -m backend.graph` once to build `cache/tartu_bike.graphml` before
starting the app; otherwise the app builds it in the background on startup.
"""

from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
import osmnx as ox
import rasterio
from pyproj import Transformer
from shapely.geometry import LineString, Point, Polygon

from .stations import load_stations


ROOT_DIR = Path(__file__).resolve().parent.parent
GRAPH_FILE = ROOT_DIR / "cache" / "tartu_bike.graphml"
TERRAIN_DIR = ROOT_DIR / "data"
STATIONS_FILE = ROOT_DIR / "data" / "Rattaparklad.csv"
OSM_MODEL = "osm_bike_footway_v4"
PLACE = "Tartu, Estonia"
# Around the convex hull of all stations, so routes can leave it a little.
NETWORK_BUFFER_M = 1500.0
TERRAIN_MODEL = "dtm_5m_v2"
OVERPASS_URLS = (
    "https://overpass-api.de/api",
    "https://overpass.private.coffee/api",
    "https://overpass.kumi.systems/api",
)
# osmnx's "bike" filter with "footway" removed from the excluded highway types:
# footways stay unless they carry bicycle=no.
BIKE_FILTER = (
    '["highway"]["area"!~"yes"]["access"!~"private"]'
    '["highway"!~"abandoned|bus_guideway|construction|corridor|elevator|'
    'escalator|motor|no|planned|platform|proposed|raceway|razed|rest_area|'
    'services|steps"]["bicycle"!~"no"]["service"!~"private"]'
)
BIKE_TAGS_WAY = (
    "cycleway",
    "cycleway:left",
    "cycleway:right",
    "cycleway:both",
    "surface",
    "bicycle",
)
# Whether a crossing has traffic lights is usually on the crossing node.
JUNCTION_TAGS_NODE = ("crossing", "crossing:signals")


class GraphUnavailable(RuntimeError):
    """The OSM graph is not cached and could not be downloaded."""


def edge_line(graph: nx.MultiDiGraph, start, end, data: dict) -> LineString:
    geometry = data.get("geometry")
    if isinstance(geometry, LineString):
        return geometry
    return LineString(
        [(graph.nodes[node]["x"], graph.nodes[node]["y"]) for node in (start, end)]
    )


def network_area() -> Polygon:
    """Tartu plus all its stations, some of which are in villages outside the city."""

    city = ox.geocode_to_gdf(PLACE).to_crs("EPSG:3301")
    stations = gpd.GeoSeries(
        [Point(s.longitude, s.latitude) for s in load_stations(STATIONS_FILE)],
        crs="EPSG:4326",
    ).to_crs(city.crs)
    area = stations.union_all().convex_hull.buffer(NETWORK_BUFFER_M)
    return (
        gpd.GeoSeries([area.union(city.geometry.iloc[0])], crs=city.crs)
        .to_crs("EPSG:4326")
        .iloc[0]
    )


def download() -> nx.MultiDiGraph:
    ox.settings.useful_tags_way = list(
        dict.fromkeys([*ox.settings.useful_tags_way, *BIKE_TAGS_WAY])
    )
    ox.settings.useful_tags_node = list(
        dict.fromkeys([*ox.settings.useful_tags_node, *JUNCTION_TAGS_NODE])
    )
    area = network_area()
    errors = []
    for url in OVERPASS_URLS:
        ox.settings.overpass_url = url
        # Only the main instance answers /status reliably.
        ox.settings.overpass_rate_limit = url == OVERPASS_URLS[0]
        try:
            return ox.graph_from_polygon(
                area, network_type="bike", custom_filter=BIKE_FILTER
            )
        except Exception as error:  # Overpass mirrors fail in many different ways.
            errors.append(f"{url}: {error}")
    raise GraphUnavailable("OSM download failed. " + " | ".join(errors))


def add_terrain(graph: nx.MultiDiGraph) -> None:
    """Store elevation gain and average uphill grade for every edge."""

    edges = list(graph.edges(keys=True, data=True))
    lines = [np.asarray(edge_line(graph, u, v, data).coords) for u, v, _, data in edges]
    points = np.concatenate(lines)
    elevation = np.full(len(points), np.nan)
    for path in sorted(TERRAIN_DIR.glob("*_dtm_5m.tif")):
        with rasterio.open(path) as dataset:
            to_grid = Transformer.from_crs("EPSG:4326", dataset.crs, always_xy=True)
            x, y = to_grid.transform(points[:, 0], points[:, 1])
            bounds = dataset.bounds
            inside = (
                np.isnan(elevation)
                & (bounds.left <= x) & (x <= bounds.right)
                & (bounds.bottom <= y) & (y <= bounds.top)
            )
            values = np.array(
                [value[0] for value in dataset.sample(zip(x[inside], y[inside]))],
                dtype=float,
            )
            if dataset.nodata is not None:
                values[np.isclose(values, dataset.nodata)] = np.nan
            elevation[inside] = values
    splits = np.cumsum([len(line) for line in lines])[:-1]
    for (_, _, _, data), heights in zip(edges, np.split(elevation, splits)):
        heights = heights[~np.isnan(heights)]
        gain = float(np.clip(np.diff(heights), 0, None).sum())
        length = float(data["length"])
        data["elevation_gain"] = gain
        data["average_uphill_grade"] = gain / length if length > 0 else 0.0


def load_graph(refresh: bool = False) -> nx.MultiDiGraph:
    """The cached graph; downloaded if missing, or if outdated and `refresh`.

    The app never refreshes an outdated cache itself, so a busy Overpass
    server cannot block routing; `python -m backend.graph` does.
    """

    graph = None
    if GRAPH_FILE.exists():
        graph = ox.load_graphml(
            GRAPH_FILE,
            edge_dtypes={"elevation_gain": float, "average_uphill_grade": float},
        )
    if graph is None or (refresh and graph.graph.get("osm_model") != OSM_MODEL):
        graph = download()
        graph.graph["osm_model"] = OSM_MODEL
    elif graph.graph.get("osm_model") != OSM_MODEL:
        print("Cached graph is outdated; run `python -m backend.graph` to update it.")
    terrain_model = TERRAIN_MODEL if any(TERRAIN_DIR.glob("*_dtm_5m.tif")) else "none"
    if graph.graph.get("terrain_model") != terrain_model:
        add_terrain(graph)
        graph.graph["terrain_model"] = terrain_model
        GRAPH_FILE.parent.mkdir(parents=True, exist_ok=True)
        ox.save_graphml(graph, GRAPH_FILE)
    return graph


if __name__ == "__main__":
    graph = load_graph(refresh=True)
    print(f"{GRAPH_FILE}: {graph.number_of_edges()} edges, {graph.graph}")
