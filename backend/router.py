from __future__ import annotations

from typing import Any, Iterable

import networkx as nx
import osmnx as ox
from shapely.geometry import LineString, Point
from shapely.ops import substring

from .models import RoutingPreferences


def _as_text(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item) for item in value).lower()
    return str(value or "").lower()


def _has_any(value: str, options: tuple[str, ...]) -> bool:
    return any(option in value for option in options)


def _is_true(value: Any) -> bool:
    return value is True or str(value).lower() == "true"


def _edge_attributes(data: dict[Any, Any]) -> Iterable[dict[str, Any]]:
    if "length" in data:
        return (data,)
    return (item for item in data.values() if isinstance(item, dict))


def _distance_meters(graph: nx.MultiDiGraph, start: Any, end: Any) -> float:
    return float(
        ox.distance.great_circle(
            graph.nodes[start]["y"],
            graph.nodes[start]["x"],
            graph.nodes[end]["y"],
            graph.nodes[end]["x"],
        )
    )


def _point_to_node_distance(
    graph: nx.MultiDiGraph,
    latitude: float,
    longitude: float,
    node: Any,
) -> float:
    return float(
        ox.distance.great_circle(
            latitude,
            longitude,
            graph.nodes[node]["y"],
            graph.nodes[node]["x"],
        )
    )


def _endpoint_candidates(
    graph: nx.MultiDiGraph,
    latitude: float,
    longitude: float,
) -> list[tuple[Any, float, list[tuple[float, float]]]]:
    edge, _ = ox.distance.nearest_edges(
        graph,
        longitude,
        latitude,
        return_dist=True,
    )
    candidates = {edge[0], edge[1]}
    edge_data = graph.get_edge_data(edge[0], edge[1])[edge[2]]
    edge_geometry = edge_data.get("geometry")
    point = Point(longitude, latitude)
    result = []
    for node in candidates:
        if isinstance(edge_geometry, LineString):
            position = edge_geometry.project(point)
            if node == edge[0]:
                connector = substring(edge_geometry, 0, position)
                connector_points = list(connector.coords)[::-1]
            else:
                connector = substring(edge_geometry, position, edge_geometry.length)
                connector_points = list(connector.coords)
        else:
            connector_points = [
                (longitude, latitude),
                (graph.nodes[node]["x"], graph.nodes[node]["y"]),
            ]
        distance = _point_to_node_distance(graph, latitude, longitude, node)
        result.append((node, distance, connector_points))
    return result


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
        elif _has_any(cycleway, ("track", "lane", "shared")):
            factor *= preferences.cycleway_factor
        elif _has_any(highway, ("path", "track", "pedestrian")):
            factor *= preferences.path_factor
        if _is_true(attributes.get("near_water")) and _has_any(
            highway, ("cycleway", "path", "track", "pedestrian")
        ):
            factor *= preferences.riverside_path_factor
        if "cobblestone" in surface or "sett" in surface:
            factor *= preferences.cobblestone_factor
        is_major_road = _has_any(
            highway, ("primary", "secondary", "tertiary", "trunk")
        )
        if is_major_road:
            factor *= preferences.major_road_factor
        if _is_true(attributes.get("near_water")) and is_major_road:
            factor *= preferences.riverside_road_factor
        if _has_any(highway, ("motorway",)):
            factor = float("inf")
        uphill_grade = float(attributes.get("average_uphill_grade", 0.0))
        factor *= 1.0 + preferences.uphill_factor * uphill_grade
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
        minimum_factor = min(
            preferences.cycleway_factor,
            preferences.path_factor,
            1.0,
        )
        weight = lambda u, v, data: edge_cost(u, v, data, preferences)
        heuristic = lambda node, target: (
            _distance_meters(self.graph, node, target) * minimum_factor
        )
        start_candidates = _endpoint_candidates(self.graph, start_lat, start_lon)
        end_candidates = _endpoint_candidates(self.graph, end_lat, end_lon)
        best = None
        for origin, start_snap_distance, start_connector in start_candidates:
            for destination, end_snap_distance, end_connector in end_candidates:
                try:
                    candidate = nx.astar_path(
                        self.graph,
                        origin,
                        destination,
                        weight=weight,
                        heuristic=heuristic,
                    )
                except nx.NetworkXNoPath:
                    continue
                route_cost = sum(
                    edge_cost(
                        start,
                        end,
                        self.graph.get_edge_data(start, end),
                        preferences,
                    )
                    for start, end in zip(candidate, candidate[1:])
                )
                score = route_cost + start_snap_distance + end_snap_distance
                if best is None or score < best[0]:
                    best = (
                        score,
                        candidate,
                        start_snap_distance,
                        end_snap_distance,
                        start_connector,
                        end_connector,
                    )
        if best is None:
            raise nx.NetworkXNoPath
        (
            _,
            route,
            start_snap_distance,
            end_snap_distance,
            start_connector,
            end_connector,
        ) = best
        coordinates = []
        elevation_gain = 0.0
        maximum_grade = 0.0
        covered_edges = 0
        for start, end in zip(route, route[1:]):
            edge = _best_edge(self.graph, start, end, preferences)
            elevation_gain += float(edge.get("elevation_gain", 0.0))
            maximum_grade = max(
                maximum_grade,
                float(edge.get("average_uphill_grade", 0.0)),
            )
            covered_edges += int(float(edge.get("terrain_covered", 0.0)))
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
        connector_start = [
            [float(x), float(y)] for x, y in start_connector
        ]
        connector_end = [
            [float(x), float(y)] for x, y in reversed(end_connector)
        ]
        coordinates = (
            [[start_lon, start_lat]]
            + connector_start
            + coordinates
            + connector_end
            + [[end_lon, end_lat]]
        )
        return {
            "type": "Feature",
            "properties": {
                "backend": self.backend_name,
                "node_count": len(route),
                "elevation_gain_m": round(elevation_gain, 1),
                "maximum_uphill_grade": round(maximum_grade, 4),
                "terrain_coverage": round(
                    covered_edges / max(1, len(route) - 1),
                    3,
                ),
                "start_snap_distance_m": round(start_snap_distance, 1),
                "end_snap_distance_m": round(end_snap_distance, 1),
            },
            "geometry": {"type": "LineString", "coordinates": coordinates},
        }
