import json
import os
import re
import threading
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode
from typing import Any
from urllib.request import Request, urlopen

import networkx as nx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .gps_layer import load_gps_ways
from .graph import TERRAIN_DIR, GraphUnavailable, load_graph
from .router import NetworkXRouter
from .stations import load_stations
from .brouter import BRouterRouter, BRouterUnavailable
from .valhalla import ValhallaRouter, ValhallaUnavailable


ROOT_DIR = Path(__file__).resolve().parent.parent
STATIONS_FILE = ROOT_DIR / "data" / "Rattaparklad.csv"
FRONTEND_DIR = ROOT_DIR / "frontend"
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
VALHALLA_URL = os.getenv("TARCYCLE_VALHALLA_URL", "http://127.0.0.1:8002")
BROUTER_URL = os.getenv("TARCYCLE_BROUTER_URL", "http://127.0.0.1:17777")
BROUTER_PROFILE = os.getenv("TARCYCLE_BROUTER_PROFILE", "trekking")
USER_AGENT = {"User-Agent": "Tarcycle/0.1 (cycling research prototype)"}
# "lat, lon", as typed or set by clicking the map.
COORDINATES = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
# Autocomplete stays within Tartu (west, south, east, north).
TARTU_BBOX = "26.60,58.33,26.86,58.43"


# Engines that run as separate servers; NetworkX is the fallback for both.
external_engines = {
    "valhalla": ValhallaRouter(VALHALLA_URL),
    "brouter": BRouterRouter(BROUTER_URL, BROUTER_PROFILE),
}
_router_lock = threading.Lock()


@lru_cache(maxsize=1)
def stations():
    return load_stations(STATIONS_FILE)


@lru_cache(maxsize=1)
def _networkx_router() -> NetworkXRouter:
    gps_ways_file = COVERAGE_DIR / "gps_ways.geojson"
    gps_ways = load_gps_ways(gps_ways_file) if gps_ways_file.exists() else None
    return NetworkXRouter(load_graph(), gps_ways=gps_ways)


def networkx_router() -> NetworkXRouter:
    with _router_lock:
        return _networkx_router()


def _warm_up() -> None:
    try:
        networkx_router()
    except GraphUnavailable:
        pass  # reported again on the first route request


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Load (or build) the graph before the first route request needs it.
    threading.Thread(target=_warm_up, daemon=True).start()
    yield


app = FastAPI(title="Tarcycle routing API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
# Coverage GeoJSON layers are several MB; gzip shrinks them roughly tenfold.
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "valhalla_url": VALHALLA_URL,
        "brouter_url": BROUTER_URL,
        "terrain": "dtm_5m" if any(TERRAIN_DIR.glob("*_dtm_5m.tif")) else "none",
    }


@app.get("/api/stations")
def station_list() -> list[dict[str, int | str | float]]:
    return [station.as_dict() for station in stations()]


@lru_cache(maxsize=128)
def geocode(address: str) -> tuple[float, float]:
    """Resolve one Tartu address through Nominatim, or read "lat, lon"."""

    coordinates = COORDINATES.match(address)
    if coordinates:
        return float(coordinates[1]), float(coordinates[2])
    if not address.strip():
        raise HTTPException(status_code=400, detail="Address cannot be empty.")
    params = urlencode(
        {"q": f"{address}, Tartu, Estonia", "format": "jsonv2", "limit": 1}
    )
    request = Request(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers=USER_AGENT,
    )
    try:
        with urlopen(request, timeout=10) as response:
            results = json.load(response)
    except OSError as error:
        raise HTTPException(status_code=502, detail="Address lookup failed.") from error
    if not results:
        raise HTTPException(status_code=404, detail=f"Address not found: {address}")
    return float(results[0]["lat"]), float(results[0]["lon"])


@lru_cache(maxsize=512)
def _suggestions(query: str) -> tuple[dict[str, Any], ...]:
    params = urlencode({"q": query, "limit": 6, "bbox": TARTU_BBOX})
    request = Request(f"https://photon.komoot.io/api/?{params}", headers=USER_AGENT)
    with urlopen(request, timeout=5) as response:
        features = json.load(response)["features"]
    suggestions = {}
    for feature in features:
        properties = feature["properties"]
        street = " ".join(
            filter(None, (properties.get("street"), properties.get("housenumber")))
        )
        place = properties.get("district") or properties.get("city")
        label = ", ".join(dict.fromkeys(filter(None, (properties.get("name"), street, place))))
        longitude, latitude = feature["geometry"]["coordinates"]
        suggestions.setdefault(label, {"label": label, "lat": latitude, "lon": longitude})
    return tuple(suggestions.values())


@app.get("/api/suggest")
def suggest(q: str = Query(..., min_length=3)) -> list[dict[str, Any]]:
    """Address autocomplete through Photon (Nominatim does not allow it)."""

    try:
        return list(_suggestions(q.strip().lower()))
    except (OSError, ValueError, KeyError):
        return []


def route(
    start: tuple[float, float],
    end: tuple[float, float],
    backend: str,
) -> dict:
    fallback_reason = None
    if backend in external_engines:
        try:
            return external_engines[backend].route_geojson(*start, *end)
        except (ValhallaUnavailable, BRouterUnavailable) as error:
            fallback_reason = str(error)
    try:
        routes = networkx_router().route_geojson(*start, *end)
    except GraphUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except nx.NetworkXNoPath as error:
        raise HTTPException(status_code=422, detail="No bicycle route found.") from error
    if fallback_reason:
        for feature in routes["features"]:
            feature["properties"].update(
                requested_backend=backend,
                fallback=True,
                fallback_reason=fallback_reason,
            )
    return routes


@app.get("/api/route")
def station_route(
    mode: str = Query("stations", pattern="^(stations|addresses)$"),
    backend: str = Query("networkx", pattern="^(networkx|valhalla|brouter)$"),
    from_station: int | None = Query(None, alias="from"),
    to_station: int | None = Query(None, alias="to"),
    from_address: str | None = None,
    to_address: str | None = None,
) -> dict:
    if mode == "addresses":
        if not from_address or not to_address:
            raise HTTPException(
                status_code=400,
                detail="Both from_address and to_address are required.",
            )
        return route(geocode(from_address), geocode(to_address), backend)
    station_by_id = {station.id: station for station in stations()}
    start = station_by_id.get(from_station)
    end = station_by_id.get(to_station)
    if start is None or end is None:
        raise HTTPException(status_code=404, detail="Unknown station id.")
    return route(
        (start.latitude, start.longitude), (end.latitude, end.longitude), backend
    )


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
