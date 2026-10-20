import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import networkx as nx
import osmnx as ox
import geopandas as gpd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pyproj import Transformer
from shapely.geometry import LineString
from shapely.ops import unary_union

from .models import RoutingPreferences
from .router import NetworkXRouter
from .stations import load_stations
from .terrain import TerrainModel


ROOT_DIR = Path(__file__).resolve().parent.parent
STATIONS_FILE = ROOT_DIR / "data" / "Rattaparklad.csv"
GRAPH_FILE = ROOT_DIR / "cache" / "tartu_bike.graphml"
WATER_FILE = ROOT_DIR / "cache" / "tartu_water.geojson"
FRONTEND_DIR = ROOT_DIR / "frontend"
TERRAIN_DIR = ROOT_DIR / "data"
COVERAGE_DIR = ROOT_DIR / "data" / "gps_coverage"
COVERAGE_FILES = {
    "cells": "cells.geojson",
    "edges": "edges.geojson",
    "ways": "gps_ways.geojson",
    "cycleways": "osm_cycleways.geojson",
    "router": "router_graph.geojson",
    "candidates": "candidates.geojson",
    "summary": "summary.json",
}


app = FastAPI(title="Tarcycle routing API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
# Coverage GeoJSON layers are several MB; gzip shrinks them roughly tenfold.
app.add_middleware(GZipMiddleware, minimum_size=1000)


@lru_cache(maxsize=1)
def stations():
    return load_stations(STATIONS_FILE)


@lru_cache(maxsize=1)
def router() -> NetworkXRouter:
    GRAPH_FILE.parent.mkdir(parents=True, exist_ok=True)
    if GRAPH_FILE.exists():
        graph = ox.load_graphml(GRAPH_FILE)
    else:
        graph = ox.graph_from_place("Tartu, Estonia", network_type="bike")
        ox.save_graphml(graph, GRAPH_FILE)
    if not isinstance(graph, nx.MultiDiGraph):
        raise TypeError("The cached OSM graph must be a directed multigraph.")
    if graph.graph.get("terrain_model") != "dtm_5m_v2" or graph.graph.get("water_model") != "osm_water_v3":
        add_terrain_attributes(graph)
        add_water_attributes(graph)
        graph.graph["terrain_model"] = "dtm_5m_v2"
        graph.graph["water_model"] = "osm_water_v3"
        ox.save_graphml(graph, GRAPH_FILE)
    return NetworkXRouter(graph)


def add_terrain_attributes(graph: nx.MultiDiGraph) -> None:
    terrain = TerrainModel(TERRAIN_DIR)
    if not terrain.available:
        return
    try:
        for start, end, _key, data in graph.edges(keys=True, data=True):
            geometry = data.get("geometry")
            if geometry is None:
                coordinates = [
                    (graph.nodes[start]["x"], graph.nodes[start]["y"]),
                    (graph.nodes[end]["x"], graph.nodes[end]["y"]),
                ]
            else:
                coordinates = list(geometry.coords)
            data.update(terrain.edge_attributes(coordinates, float(data["length"])))
    finally:
        terrain.close()


def water_geometry():
    if WATER_FILE.exists():
        return gpd.read_file(WATER_FILE)
    water = ox.features_from_place(
        "Tartu, Estonia",
        tags={"natural": "water", "waterway": ["river", "canal"]},
    )
    if water.empty:
        raise RuntimeError("OpenStreetMap returned no water features for Tartu.")
    WATER_FILE.parent.mkdir(parents=True, exist_ok=True)
    water.to_file(WATER_FILE, driver="GeoJSON")
    return water


def add_water_attributes(graph: nx.MultiDiGraph) -> None:
    water = water_geometry()
    projected = water.to_crs("EPSG:3301")
    water_shape = unary_union(projected.geometry)
    to_meters = Transformer.from_crs("EPSG:4326", "EPSG:3301", always_xy=True)
    for start, end, _key, data in graph.edges(keys=True, data=True):
        geometry = data.get("geometry")
        coordinates = (
            list(geometry.coords)
            if isinstance(geometry, LineString)
            else [
                (graph.nodes[start]["x"], graph.nodes[start]["y"]),
                (graph.nodes[end]["x"], graph.nodes[end]["y"]),
            ]
        )
        projected_coordinates = [to_meters.transform(x, y) for x, y in coordinates]
        data["near_water"] = water_shape.distance(LineString(projected_coordinates)) <= 250


@app.get("/api/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "backend": "networkx",
        "terrain": "dtm_5m" if list(TERRAIN_DIR.glob("*_dtm_5m.tif")) else "none",
    }


@app.get("/api/stations")
def station_list() -> list[dict[str, int | str | float]]:
    return [station.as_dict() for station in stations()]


@lru_cache(maxsize=128)
def geocode(address: str) -> tuple[float, float]:
    """Resolve one Tartu address through Nominatim."""

    if not address.strip():
        raise HTTPException(status_code=400, detail="Address cannot be empty.")
    params = urlencode(
        {"q": f"{address}, Tartu, Estonia", "format": "jsonv2", "limit": 1}
    )
    request = Request(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers={"User-Agent": "Tarcycle/0.1 (cycling research prototype)"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            results = json.load(response)
    except OSError as error:
        raise HTTPException(status_code=502, detail="Address lookup failed.") from error
    if not results:
        raise HTTPException(status_code=404, detail=f"Address not found: {address}")
    return float(results[0]["lat"]), float(results[0]["lon"])


@app.get("/api/route")
def station_route(
    mode: str = Query("stations", pattern="^(stations|addresses)$"),
    from_station: int | None = Query(None, alias="from"),
    to_station: int | None = Query(None, alias="to"),
    from_address: str | None = None,
    to_address: str | None = None,
) -> dict:
    if mode == "stations":
        station_by_id = {station.id: station for station in stations()}
        start = station_by_id.get(from_station)
        end = station_by_id.get(to_station)
        if start is None or end is None:
            raise HTTPException(status_code=404, detail="Unknown station id.")
        start_point = (start.latitude, start.longitude)
        end_point = (end.latitude, end.longitude)
    else:
        if not from_address or not to_address:
            raise HTTPException(
                status_code=400,
                detail="Both from_address and to_address are required.",
            )
        start_point = geocode(from_address)
        end_point = geocode(to_address)
    try:
        return router().route_geojson(
            start_point[0],
            start_point[1],
            end_point[0],
            end_point[1],
            RoutingPreferences(),
        )
    except nx.NetworkXNoPath as error:
        raise HTTPException(status_code=422, detail="No bicycle route found.") from error


@app.get("/api/coverage/{layer}")
def coverage_layer(layer: str) -> FileResponse:
    """Precomputed GPS-vs-OSM comparison layers from ``backend.gps_coverage``."""

    filename = COVERAGE_FILES.get(layer)
    if filename is None:
        raise HTTPException(status_code=404, detail=f"Unknown coverage layer: {layer}")
    path = COVERAGE_DIR / filename
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="Coverage not built yet. Run: python -m backend.gps_coverage",
        )
    media_type = "application/json" if path.suffix == ".json" else "application/geo+json"
    return FileResponse(path, media_type=media_type)


app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
