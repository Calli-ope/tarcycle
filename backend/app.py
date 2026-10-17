from functools import lru_cache

import networkx as nx
import osmnx as ox
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .config import GRAPH_FILE, STATIONS_FILE
from .models import RoutingPreferences
from .router import NetworkXRouter
from .stations import load_stations


app = FastAPI(title="Tarcycle routing API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


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
    return NetworkXRouter(graph)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "backend": "networkx"}


@app.get("/api/stations")
def station_list() -> list[dict[str, int | str | float]]:
    return [station.as_dict() for station in stations()]


@app.get("/api/route")
def station_route(
    from_station: int = Query(..., alias="from"),
    to_station: int = Query(..., alias="to"),
) -> dict:
    station_by_id = {station.id: station for station in stations()}
    start = station_by_id.get(from_station)
    end = station_by_id.get(to_station)
    if start is None or end is None:
        raise HTTPException(status_code=404, detail="Unknown station id.")
    try:
        return router().route_geojson(
            start.latitude,
            start.longitude,
            end.latitude,
            end.longitude,
            RoutingPreferences(),
        )
    except nx.NetworkXNoPath as error:
        raise HTTPException(status_code=422, detail="No bicycle route found.") from error
