from __future__ import annotations

from typing import Any, Iterable

import networkx as nx
import osmnx as ox
from shapely.geometry import LineString

from .models import RoutingPreferences


def _as_text(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item) for item in value).lower()
    return str(value or "").lower()


def _edge_attributes(data: dict[Any, Any]) -> Iterable[dict[str, Any]]:
    if "length" in data:
        return (data,)
    return (item for item in data.values() if isinstance(item, dict))


def edge_cost(
    _u: Any,
    _v: Any,
    data: dict[Any, Any],
    preferences: RoutingPreferences,
) -> float:
    costs = []
    for attributes in _edge_attributes(data):
        base_cost = float(attributes.get("length", 1.0))
        highway = _as_text(attributes.get("highway"))
        surface = _as_text(attributes.get("surface"))
        cycleway = _as_text(attributes.get("cycleway"))
        factor = 1.0
        if "cycleway" in highway:
            factor *= preferences.cycleway_factor
        elif any(value in cycleway for value in ("track", "lane", "shared")):
            factor *= preferences.cycleway_factor
        elif any(kind in highway for kind in ("path", "track", "pedestrian")):
            factor *= preferences.path_factor
        if "cobblestone" in surface or "sett" in surface:
            factor *= preferences.cobblestone_factor
        if highway in {"primary", "secondary", "tertiary", "trunk"}:
            factor *= preferences.major_road_factor
        costs.append(base_cost * factor)
    if not costs:
        raise ValueError("OSM edge has no usable attributes.")
    return min(costs)


def _best_edge(
    graph: nx.MultiDiGraph,
    start: Any,
    end: Any,
    preferences: RoutingPreferences,
) -> dict[str, Any]:
    edges = graph.get_edge_data(start, end)
    if not edges:
        raise ValueError(f"Missing edge between {start} and {end}.")
    return min(
        edges.values(),
        key=lambda data: edge_cost(start, end, data, preferences),
    )


class NetworkXRouter:
    """Routing backend boundary; a ValhallaRouter can implement the same API."""

    backend_name = "networkx"

    def __init__(self, graph: nx.MultiDiGraph) -> None:
        self.graph = graph

    def route_geojson(
        self,
        start_lat: float,
        start_lon: float,
        end_lat: float,
        end_lon: float,
        preferences: RoutingPreferences | None = None,
    ) -> dict[str, Any]:
        preferences = preferences or RoutingPreferences()
        origin = ox.distance.nearest_nodes(self.graph, start_lon, start_lat)
        destination = ox.distance.nearest_nodes(self.graph, end_lon, end_lat)
        route = nx.shortest_path(
            self.graph,
            origin,
            destination,
            weight=lambda u, v, data: edge_cost(u, v, data, preferences),
        )
        coordinates = []
        for start, end in zip(route, route[1:]):
            edge = _best_edge(self.graph, start, end, preferences)
            geometry = edge.get("geometry")
            if isinstance(geometry, LineString):
                points = list(geometry.coords)
            else:
                points = [
                    (self.graph.nodes[start]["x"], self.graph.nodes[start]["y"]),
                    (self.graph.nodes[end]["x"], self.graph.nodes[end]["y"]),
                ]
            if coordinates and coordinates[-1] == [float(value) for value in points[0]]:
                points = points[1:]
            coordinates.extend([[float(x), float(y)] for x, y in points])
        return {
            "type": "Feature",
            "properties": {
                "backend": self.backend_name,
                "node_count": len(route),
            },
            "geometry": {"type": "LineString", "coordinates": coordinates},
        }
