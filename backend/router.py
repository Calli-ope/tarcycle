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


def _facility_type(attributes: dict[str, Any]) -> str:
    highway = _as_text(attributes.get("highway"))
    cycleway = _as_text(attributes.get("cycleway"))
    if "cycleway" in highway:
        return "dedicated_cycleway"
    if _has_any(cycleway, ("track", "lane", "shared")):
        return "road_cycleway"
    if _has_any(highway, ("path", "track", "pedestrian")):
        return "path"
    return "road"


def _is_true(value: Any) -> bool:
    return value is True or str(value).lower() == "true"


def _edge_attributes(data: dict[Any, Any]) -> Iterable[dict[str, Any]]:
    if "length" in data:
        return (data,)
    return (item for item in data.values() if isinstance(item, dict))


def _line_edge_data(
    graph: nx.MultiDiGraph,
    edge: tuple[Any, Any, Any],
) -> dict[str, Any]:
    return graph.get_edge_data(edge[0], edge[1])[edge[2]]


def _edge_length(graph: nx.MultiDiGraph, edge: tuple[Any, Any, Any]) -> float:
    return float(_line_edge_data(graph, edge).get("length", 1.0))


def _route_length(
    graph: nx.MultiDiGraph,
    edge_route: list[tuple[Any, Any, Any]],
    start_distance: float,
    end_distance: float,
) -> float:
    return (
        start_distance
        + end_distance
        + sum(_edge_length(graph, edge) for edge in edge_route)
    )


def _dedicated_cycleway_length(
    graph: nx.MultiDiGraph,
    edge_route: list[tuple[Any, Any, Any]],
) -> float:
    return sum(
        _edge_length(graph, edge)
        for edge in edge_route
        if _facility_type(_line_edge_data(graph, edge)) == "dedicated_cycleway"
    )


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
            factor *= preferences.dedicated_cycleway_factor
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


def _transition_cost(
    previous: dict[str, Any],
    current: dict[str, Any],
    preferences: RoutingPreferences,
) -> float:
    if _facility_type(previous) == _facility_type(current):
        return 0.0
    return preferences.facility_transition_cost


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
        line_graph = nx.line_graph(self.graph)
        start_candidates = _endpoint_candidates(self.graph, start_lat, start_lon)
        end_candidates = _endpoint_candidates(self.graph, end_lat, end_lon)
        source_target_pairs = []
        shortest_route_length = float("inf")
        shortest_baseline = None
        for origin, start_snap_distance, start_connector in start_candidates:
            for destination, end_snap_distance, end_connector in end_candidates:
                source_edges = [
                    edge for edge in line_graph.nodes if edge[0] == origin
                ]
                target_edges = [
                    edge for edge in line_graph.nodes if edge[1] == destination
                ]
                source_target_pairs.append(
                    (
                        origin,
                        destination,
                        start_snap_distance,
                        end_snap_distance,
                        start_connector,
                        end_connector,
                        source_edges,
                        target_edges,
                    )
                )
                for source_edge in source_edges:
                    for target_edge in target_edges:
                        try:
                            edge_route = nx.shortest_path(
                                line_graph,
                                source_edge,
                                target_edge,
                                weight=lambda _previous, current, _data: (
                                    _edge_length(self.graph, current)
                                ),
                                method="dijkstra",
                            )
                        except nx.NetworkXNoPath:
                            continue
                        route_length = _route_length(
                            self.graph,
                            edge_route,
                            start_snap_distance,
                            end_snap_distance,
                        )
                        if route_length < shortest_route_length:
                            shortest_route_length = route_length
                            shortest_baseline = (
                                edge_route,
                                start_snap_distance,
                                end_snap_distance,
                                start_connector,
                                end_connector,
                            )
        if shortest_route_length == float("inf"):
            raise nx.NetworkXNoPath

        if shortest_baseline is None:
            raise nx.NetworkXNoPath
        baseline_edge_route, baseline_start_distance, baseline_end_distance, (
            baseline_start_connector,
            baseline_end_connector,
        ) = (
            shortest_baseline[0],
            shortest_baseline[1],
            shortest_baseline[2],
            shortest_baseline[3:],
        )
        baseline_route = [baseline_edge_route[0][0]] + [
            edge[1] for edge in baseline_edge_route
        ]
        baseline_dedicated_length = _dedicated_cycleway_length(
            self.graph, baseline_edge_route
        )
        best = (
            shortest_route_length,
            baseline_route,
            baseline_edge_route,
            baseline_start_distance,
            baseline_end_distance,
            baseline_start_connector,
            baseline_end_connector,
            shortest_route_length,
            baseline_dedicated_length,
        )
        best_key = (-baseline_dedicated_length, shortest_route_length)
        fallback = None
        for (
            origin,
            destination,
            start_snap_distance,
            end_snap_distance,
            start_connector,
            end_connector,
            source_edges,
            target_edges,
        ) in source_target_pairs:
            for source_edge in source_edges:
                for target_edge in target_edges:
                    try:
                        edge_route = nx.shortest_path(
                            line_graph,
                            source_edge,
                            target_edge,
                            weight=lambda previous, current, _data: (
                                edge_cost(
                                    current[0],
                                    current[1],
                                    _line_edge_data(self.graph, current),
                                    preferences,
                                )
                                + _transition_cost(
                                    _line_edge_data(self.graph, previous),
                                    _line_edge_data(self.graph, current),
                                    preferences,
                                )
                            ),
                            method="dijkstra",
                        )
                    except nx.NetworkXNoPath:
                        continue
                    route_length = _route_length(
                        self.graph,
                        edge_route,
                        start_snap_distance,
                        end_snap_distance,
                    )
                    candidate = [source_edge[0]] + [
                        edge[1] for edge in edge_route
                    ]
                    route_cost = edge_cost(
                        source_edge[0],
                        source_edge[1],
                        _line_edge_data(self.graph, source_edge),
                        preferences,
                    ) + sum(
                        edge_cost(
                            edge[0],
                            edge[1],
                            _line_edge_data(self.graph, edge),
                            preferences,
                        )
                        + _transition_cost(
                            _line_edge_data(self.graph, previous),
                            _line_edge_data(self.graph, edge),
                            preferences,
                        )
                        for previous, edge in zip(edge_route, edge_route[1:])
                    )
                    score = route_cost + start_snap_distance + end_snap_distance
                    dedicated_length = _dedicated_cycleway_length(
                        self.graph, edge_route
                    )
                    result = (
                        score,
                        candidate,
                        edge_route,
                        start_snap_distance,
                        end_snap_distance,
                        start_connector,
                        end_connector,
                        route_length,
                        dedicated_length,
                    )
                    if fallback is None or score < fallback[0]:
                        fallback = result
                    if route_length <= (
                        shortest_route_length * preferences.maximum_detour_factor
                    ) and (
                        best_key is None
                        or (-dedicated_length, score) < best_key
                    ):
                        best = result
                        best_key = (-dedicated_length, score)
        if best is None:
            best = fallback
        if best is None:
            raise nx.NetworkXNoPath
        (
            _,
            route,
            edge_route,
            start_snap_distance,
            end_snap_distance,
            start_connector,
            end_connector,
            route_length,
            dedicated_length,
        ) = best
        coordinates = []
        elevation_gain = 0.0
        maximum_grade = 0.0
        covered_edges = 0
        for edge_state in edge_route:
            start, end, _key = edge_state
            edge = _line_edge_data(self.graph, edge_state)
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
                "route_length_m": round(route_length, 1),
                "dedicated_cycleway_length_m": round(dedicated_length, 1),
                "dedicated_cycleway_share": round(
                    dedicated_length / max(1.0, route_length), 3
                ),
                "shortest_viable_route_length_m": round(
                    shortest_route_length, 1
                ),
                "maximum_detour_factor": preferences.maximum_detour_factor,
            },
            "geometry": {"type": "LineString", "coordinates": coordinates},
        }
