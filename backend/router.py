from __future__ import annotations

import heapq
import math
import re
from itertools import count
from typing import Any, Callable

import networkx as nx
import numpy as np
import osmnx as ox
import shapely
from shapely.geometry import Point
from shapely.ops import substring

from .gps_layer import GpsWay, edge_gps_trips
from .graph import edge_line
from .models import RoutingPreferences


Edge = tuple[Any, Any, Any]
CYCLEWAY_KEYS = ("cycleway", "cycleway:left", "cycleway:right", "cycleway:both")
MAJOR_ROADS = ("primary", "secondary", "tertiary", "trunk")
NOT_ROADS = ("cycleway", "path", "footway", "pedestrian", "track", "steps", "bridleway")
ROUGH_SURFACES = ("cobblestone", "sett", "unpaved", "gravel", "dirt", "ground", "grass", "sand")
# Estonian urban default where maxspeed is not tagged.
URBAN_SPEED_KMH = 50.0
# Travel time: riders slow down on sidewalks, rough surfaces and climbs (e-bike
# assistance makes hills matter less), and lose time at junctions.
SIDEWALK_SPEED_KMH = 12.0
PATH_SPEED_SHARE = 0.85
ROUGH_SPEED_SHARE = 0.7
HILL_SLOWDOWN = 3.0
CROSSING_DELAY_S = 8.0
LEFT_TURN_DELAY_S = 5.0
ROADSIDE_DISTANCE_M = 25.0
# GPS layer cells need at least this many trips (privacy filter).
MINIMUM_TRIPS = 10
# The alternative route is searched with the best route's edges this much more
# expensive, and is dropped if it shares more than this share of its length.
ALTERNATIVE_PENALTY = 3.0
MAXIMUM_SHARED_SHARE = 0.7


def _as_text(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item) for item in value).lower()
    return str(value or "").lower()


def _has_any(value: str, options: tuple[str, ...]) -> bool:
    return any(option in value for option in options)


def _number(value: Any) -> float | None:
    """Largest number in an OSM value such as "50", "['30', '50']" or "2"."""

    numbers = re.findall(r"\d+(?:\.\d+)?", _as_text(value))
    return max(float(number) for number in numbers) if numbers else None


def facility_type(attributes: dict[str, Any]) -> str:
    highway = _as_text(attributes.get("highway"))
    cycleway = ",".join(_as_text(attributes.get(key)) for key in CYCLEWAY_KEYS)
    bicycle = _as_text(attributes.get("bicycle"))
    if "cycleway" in highway or (
        _has_any(highway, ("path", "footway")) and "designated" in bicycle
    ):
        return "dedicated_cycleway"
    if _has_any(cycleway, ("track", "lane", "shared")):
        return "road_cycleway"
    if _has_any(highway, ("path", "track", "pedestrian", "footway")):
        return "path"
    return "road"


def edge_cost(
    attributes: dict[str, Any],
    preferences: RoutingPreferences,
    facility: str | None = None,
    sidewalk: bool = False,
    trips: int | None = None,
) -> float:
    highway = _as_text(attributes.get("highway"))
    if "motorway" in highway:
        return math.inf
    facility = facility or facility_type(attributes)
    factor = {
        "dedicated_cycleway": preferences.dedicated_cycleway_factor,
        "road_cycleway": preferences.cycleway_factor,
        "path": preferences.path_factor,
        "road": 1.0,
    }[facility]
    if sidewalk:
        factor = preferences.sidewalk_factor
    if _has_any(_as_text(attributes.get("surface")), ("cobblestone", "sett")):
        factor *= preferences.cobblestone_factor
    if _has_any(highway, MAJOR_ROADS):
        factor *= preferences.major_road_factor
    if not _has_any(highway, NOT_ROADS):
        maxspeed = _number(attributes.get("maxspeed")) or URBAN_SPEED_KMH
        lanes = _number(attributes.get("lanes")) or 1
        if "living_street" in highway or maxspeed <= 30:
            factor *= preferences.calm_street_factor
        elif maxspeed >= 60 or lanes >= 3:
            factor *= preferences.fast_road_factor
    uphill_grade = float(attributes.get("average_uphill_grade", 0.0))
    factor *= 1.0 + preferences.uphill_factor * uphill_grade
    if trips:
        # 0 at the 10-trip privacy threshold, 1 at saturation, log-scaled.
        popularity = math.log(max(trips, MINIMUM_TRIPS) / MINIMUM_TRIPS) / math.log(
            preferences.popularity_saturation_trips / MINIMUM_TRIPS
        )
        factor *= 1.0 - preferences.popularity_weight * min(1.0, popularity)
    return float(attributes["length"]) * factor


def edge_speed(
    attributes: dict[str, Any],
    preferences: RoutingPreferences,
    facility: str,
    sidewalk: bool = False,
) -> float:
    """Riding speed in m/s."""

    speed = preferences.cycling_speed_kmh
    if sidewalk:
        speed = min(speed, SIDEWALK_SPEED_KMH)
    elif facility == "path":
        speed *= PATH_SPEED_SHARE
    if _has_any(_as_text(attributes.get("surface")), ROUGH_SURFACES):
        speed *= ROUGH_SPEED_SHARE
    grade = float(attributes.get("average_uphill_grade", 0.0))
    speed *= max(0.5, 1.0 - HILL_SLOWDOWN * grade)
    return speed / 3.6


def _bearing(start: tuple[float, ...], end: tuple[float, ...], x_scale: float) -> float:
    return math.degrees(math.atan2(end[1] - start[1], (end[0] - start[0]) * x_scale))


class NetworkXRouter:
    """Routing backend boundary; ValhallaRouter implements the same API."""

    backend_name = "networkx"

    def __init__(
        self,
        graph: nx.MultiDiGraph,
        preferences: RoutingPreferences | None = None,
        gps_ways: dict[int, GpsWay] | None = None,
    ) -> None:
        self.graph = graph
        self.preferences = preferences or RoutingPreferences()
        self.edges: list[Edge] = list(graph.edges(keys=True))
        self.lines = {
            edge: edge_line(graph, edge[0], edge[1], graph.edges[edge])
            for edge in self.edges
        }
        self.length = {edge: float(graph.edges[edge]["length"]) for edge in self.edges}
        # Lookups in roughly metric space (1° lon ≈ cos(lat) · 1° lat).
        latitude = sum(y for _, y in graph.nodes(data="y")) / len(graph)
        self.x_scale = math.cos(math.radians(latitude))
        self.tree = shapely.STRtree(
            [self._scaled(self.lines[edge]) for edge in self.edges]
        )
        self.facility = {edge: facility_type(graph.edges[edge]) for edge in self.edges}
        highway = {edge: _as_text(graph.edges[edge].get("highway")) for edge in self.edges}
        roads = [
            edge
            for edge in self.edges
            if self.facility[edge] == "road" and "service" not in highway[edge]
        ]
        # Cycle tracks beside a major road expose riders to the same traffic as
        # a painted lane, so they are priced like one.
        cycleways = [e for e in self.edges if self.facility[e] == "dedicated_cycleway"]
        major = [edge for edge in roads if _has_any(highway[edge], MAJOR_ROADS)]
        for edge in self._beside(cycleways, major):
            self.facility[edge] = "road_cycleway"
        # Footways without explicit bicycle access: beside a road they are
        # sidewalks; elsewhere (parks, riverside) ordinary paths.
        footways = [
            edge
            for edge in self.edges
            if self.facility[edge] == "path"
            and "footway" in highway[edge]
            and not _has_any(
                _as_text(graph.edges[edge].get("bicycle")), ("yes", "permissive")
            )
        ]
        self.sidewalks = set(self._beside(footways, roads))
        self.cost = {
            edge: edge_cost(
                graph.edges[edge],
                self.preferences,
                self.facility[edge],
                edge in self.sidewalks,
                edge_gps_trips(graph.edges[edge], gps_ways) if gps_ways else None,
            )
            for edge in self.edges
        }
        self.seconds = {
            edge: self.length[edge]
            / edge_speed(
                graph.edges[edge],
                self.preferences,
                self.facility[edge],
                edge in self.sidewalks,
            )
            for edge in self.edges
        }
        # Junctions: traffic lights, nodes on major roads, and turn directions.
        self.on_major = {e for e in self.edges if _has_any(highway[e], MAJOR_ROADS)}
        self.major_nodes = {node for edge in self.on_major for node in edge[:2]}
        self.signals = {
            node
            for node, data in graph.nodes(data=True)
            if data.get("highway") == "traffic_signals"
            or "traffic_signals" in _as_text(data.get("crossing"))
            or _as_text(data.get("crossing:signals")) == "yes"
        }
        self.bearing_start = {
            edge: _bearing(*self.lines[edge].coords[:2], self.x_scale)
            for edge in self.edges
        }
        self.bearing_end = {
            edge: _bearing(*self.lines[edge].coords[-2:], self.x_scale)
            for edge in self.edges
        }
        self.successors = {
            node: list(graph.out_edges(node, keys=True)) for node in graph
        }
        # A* lower bound: straight-line metres times the cheapest cost per metre
        # (slightly reduced, as positions use a flat-earth approximation).
        self.position = {
            node: (data["x"] * self.x_scale * 111_320, data["y"] * 111_320)
            for node, data in graph.nodes(data=True)
        }
        self.minimum_factor = 0.98 * min(
            self.cost[edge] / self.length[edge]
            for edge in self.edges
            if self.length[edge] > 0
        )

    def _scaled(self, geometry):
        return shapely.transform(geometry, lambda coords: coords * [self.x_scale, 1])

    def _beside(self, edges: list[Edge], roads: list[Edge]) -> list[Edge]:
        """Edges whose quarter, middle and three-quarter points all lie within
        ROADSIDE_DISTANCE_M of one of the roads."""

        tree = shapely.STRtree([self._scaled(self.lines[edge]) for edge in roads])
        lines = self._scaled(np.array([self.lines[edge] for edge in edges]))
        points = shapely.line_interpolate_point(
            lines[:, None], [0.25, 0.5, 0.75], normalized=True
        )
        near = np.zeros(points.size, dtype=bool)
        near[
            tree.query(
                points.ravel(),
                predicate="dwithin",
                distance=ROADSIDE_DISTANCE_M / 111_320,
            )[0]
        ] = True
        return [
            edge
            for edge, beside_road in zip(edges, near.reshape(points.shape).all(axis=1))
            if beside_road
        ]

    def _snap(self, latitude: float, longitude: float):
        """Map both ends of the nearest edge to (distance, connector from point)."""

        point = Point(longitude, latitude)
        edge = self.edges[int(self.tree.query_nearest(self._scaled(point))[0])]
        line = self.lines[edge]
        position = line.project(point)
        connectors = {
            edge[0]: list(substring(line, 0, position).coords)[::-1],
            edge[1]: list(substring(line, position, line.length).coords),
        }
        return {
            node: (
                float(
                    ox.distance.great_circle(
                        latitude,
                        longitude,
                        self.graph.nodes[node]["y"],
                        self.graph.nodes[node]["x"],
                    )
                ),
                connector,
            )
            for node, connector in connectors.items()
        }

    def _junction(self, previous: Edge, edge: Edge) -> tuple[float, float]:
        """Cost (metres of riding) and delay (seconds) of passing from one edge
        into the next: traffic lights, crossing a major road without them, and
        left turns across traffic at major junctions."""

        node, preferences = edge[0], self.preferences
        if node in self.signals:
            delay = preferences.signal_delay_s
            return delay * preferences.cycling_speed_kmh / 3.6, delay
        if node not in self.major_nodes:
            return 0.0, 0.0
        cost = delay = 0.0
        if previous not in self.on_major and edge not in self.on_major:
            cost, delay = preferences.major_crossing_cost, CROSSING_DELAY_S
        turn = (self.bearing_start[edge] - self.bearing_end[previous] + 180) % 360 - 180
        if 30 < turn < 150:
            cost += preferences.left_turn_cost
            delay += LEFT_TURN_DELAY_S
        return cost, delay

    def _preferred_weight(self, previous: Edge | None, edge: Edge) -> float:
        if previous is None:
            return self.cost[edge]
        transition = (
            self.preferences.facility_transition_cost
            if self.facility[previous] != self.facility[edge]
            else 0.0
        )
        return self.cost[edge] + transition + self._junction(previous, edge)[0]

    def _search(
        self,
        starts: dict[Any, float],
        ends: dict[Any, float],
        weight: Callable[[Edge | None, Edge], float],
        factor: float,
    ) -> tuple[list[Edge], Any, Any]:
        """A* over edges rather than nodes, so turns between facility types can
        be priced. Start/end offsets act as virtual endpoints. `factor` must not
        exceed the weight per metre of any edge, so the result stays optimal."""

        targets = [(self.position[node], offset) for node, offset in ends.items()]
        estimates: dict[Any, float] = {}

        def estimate(node) -> float:
            if node not in estimates:
                x, y = self.position[node]
                estimates[node] = min(
                    factor * math.hypot(x - target_x, y - target_y) + offset
                    for (target_x, target_y), offset in targets
                )
            return estimates[node]

        tie = count()
        heap = []
        for node, offset in starts.items():
            for edge in self.successors[node]:
                distance = offset + weight(None, edge)
                heap.append((distance + estimate(edge[1]), next(tie), edge, None, distance))
        # A None edge marks reaching the destination; its last item is (last edge, node).
        for node in starts.keys() & ends.keys():
            distance = starts[node] + ends[node]
            heap.append((distance, next(tie), None, (None, node), distance))
        heapq.heapify(heap)
        previous: dict[Edge, Edge | None] = {}
        while heap:
            _, _, edge, before, distance = heapq.heappop(heap)
            if edge is None:
                last, end = before
                route = []
                while last is not None:
                    route.append(last)
                    last = previous[last]
                route.reverse()
                return route, route[0][0] if route else end, end
            if edge in previous:
                continue
            previous[edge] = before
            head = edge[1]
            if head in ends:
                total = distance + ends[head]
                heapq.heappush(heap, (total, next(tie), None, (edge, head), total))
            for following in self.successors[head]:
                if following not in previous:
                    step = weight(edge, following)
                    if step < math.inf:
                        total = distance + step
                        heapq.heappush(
                            heap,
                            (
                                total + estimate(following[1]),
                                next(tie),
                                following,
                                edge,
                                total,
                            ),
                        )
        raise nx.NetworkXNoPath

    def route_geojson(
        self,
        start_lat: float,
        start_lon: float,
        end_lat: float,
        end_lon: float,
    ) -> dict[str, Any]:
        """Best route first, then a clearly different alternative if one exists."""

        starts = self._snap(start_lat, start_lon)
        ends = self._snap(end_lat, end_lon)
        start_offsets = {node: value[0] for node, value in starts.items()}
        end_offsets = {node: value[0] for node, value in ends.items()}

        def total_length(result) -> float:
            route, start, end = result
            return (
                start_offsets[start]
                + end_offsets[end]
                + sum(self.length[edge] for edge in route)
            )

        shortest = self._search(
            start_offsets, end_offsets, lambda _previous, edge: self.length[edge], 0.98
        )
        preferred = self._search(
            start_offsets, end_offsets, self._preferred_weight, self.minimum_factor
        )
        shortest_length = total_length(shortest)
        maximum_length = shortest_length * self.preferences.maximum_detour_factor
        best = preferred if total_length(preferred) <= maximum_length else shortest
        routes = [best]
        # Alternative: the cheapest route once the best one's edges cost more.
        used = set(best[0])
        alternative = self._search(
            start_offsets,
            end_offsets,
            lambda previous, edge: self._preferred_weight(previous, edge)
            * (ALTERNATIVE_PENALTY if edge in used else 1.0),
            self.minimum_factor,
        )
        shared = sum(self.length[edge] for edge in alternative[0] if edge in used)
        alternative_length = total_length(alternative)
        if (
            alternative_length <= maximum_length
            and shared <= MAXIMUM_SHARED_SHARE * alternative_length
        ):
            routes.append(alternative)

        def feature(result) -> dict[str, Any]:
            route, start, end = result
            route_length = total_length(result)
            coordinates = [(start_lon, start_lat), *starts[start][1]]
            for edge in route:
                coordinates.extend(self.lines[edge].coords)
            coordinates.extend([*reversed(ends[end][1]), (end_lon, end_lat)])
            coordinates = [
                [float(x), float(y)]
                for index, (x, y) in enumerate(coordinates)
                if index == 0 or (x, y) != coordinates[index - 1]
            ]
            data = [self.graph.edges[edge] for edge in route]
            seconds = (
                (start_offsets[start] + end_offsets[end])
                / (self.preferences.cycling_speed_kmh / 3.6)
                + sum(self.seconds[edge] for edge in route)
                + sum(
                    self._junction(previous, edge)[1]
                    for previous, edge in zip(route, route[1:])
                )
            )
            dedicated_length = sum(
                self.length[edge]
                for edge in route
                if self.facility[edge] == "dedicated_cycleway"
            )
            return {
                "type": "Feature",
                "properties": {
                    "backend": self.backend_name,
                    "node_count": len(route) + 1,
                    "route_length_m": round(route_length, 1),
                    "time_minutes": round(seconds / 60, 1),
                    "traffic_lights": sum(edge[0] in self.signals for edge in route[1:]),
                    "elevation_gain_m": round(
                        sum(float(item.get("elevation_gain", 0.0)) for item in data),
                        1,
                    ),
                    "maximum_uphill_grade": round(
                        max(
                            (
                                float(item.get("average_uphill_grade", 0.0))
                                for item in data
                            ),
                            default=0.0,
                        ),
                        4,
                    ),
                    "start_snap_distance_m": round(start_offsets[start], 1),
                    "end_snap_distance_m": round(end_offsets[end], 1),
                    "dedicated_cycleway_length_m": round(dedicated_length, 1),
                    "dedicated_cycleway_share": round(
                        dedicated_length / max(1.0, route_length), 3
                    ),
                    "shortest_viable_route_length_m": round(shortest_length, 1),
                    "maximum_detour_factor": self.preferences.maximum_detour_factor,
                },
                "geometry": {"type": "LineString", "coordinates": coordinates},
            }

        return {
            "type": "FeatureCollection",
            "features": [feature(result) for result in routes],
        }
