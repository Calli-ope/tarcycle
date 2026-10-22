"""Join the Smart Bike GPS-usage layer onto an OSM routing graph.

``python -m backend.gps_coverage`` writes ``data/gps_coverage/gps_ways.geojson``:
one row per OSM way that riders use, keyed by the OSM way id. Every osmnx
graph stores the way id(s) of each edge in its ``osmid`` attribute, so the
layer can be joined onto any graph, including the router's.

Example (for ``edge_cost`` in ``router.py``)::

    gps_ways = load_gps_ways(GPS_WAYS_FILE)          # once, at startup
    trips = edge_gps_trips(attributes, gps_ways)     # per edge
    if trips is not None:
        factor *= popularity_factor(trips)           # e.g. cheaper if popular

How strongly popularity should count is a routing decision, not made here.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GpsWay:
    trips: int
    covered_share: float
    in_bike_graph: bool
    edge_class: str


def load_gps_ways(path: Path) -> dict[int, GpsWay]:
    """The GPS layer as ``{osm_way_id: GpsWay}``."""

    features = json.loads(Path(path).read_text())["features"]
    return {
        int(feature["properties"]["way_id"]): GpsWay(
            trips=int(feature["properties"]["trips"]),
            covered_share=float(feature["properties"]["covered_share"]),
            in_bike_graph=bool(feature["properties"]["in_bike_graph"]),
            edge_class=str(feature["properties"]["edge_class"]),
        )
        for feature in features
    }


def way_ids(osmid: Any) -> list[int]:
    """OSM way ids of one graph edge.

    osmnx stores a single id, a list (simplified edges spanning several ways),
    or, after a ``load_graphml`` round trip, sometimes the string ``"[1, 2]"``.
    """

    if osmid is None:
        return []
    if isinstance(osmid, str):
        osmid = ast.literal_eval(osmid) if osmid.strip().startswith("[") else int(osmid)
    if isinstance(osmid, (list, tuple, set)):
        return [int(value) for value in osmid]
    return [int(osmid)]


def edge_gps_trips(edge_data: dict[str, Any], gps_ways: dict[int, GpsWay]) -> int | None:
    """Smart Bike trips on one edge: the busiest of its ways, or ``None`` if unridden."""

    trips = [gps_ways[way].trips for way in way_ids(edge_data.get("osmid")) if way in gps_ways]
    return max(trips) if trips else None
