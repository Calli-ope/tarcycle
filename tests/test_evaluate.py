import networkx as nx
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, MultiLineString

from backend.evaluate import (
    COMPARED,
    Scorer,
    major_crossings,
    summarise,
    traffic_stress,
    turn_count,
)
from backend.router import NetworkXRouter


ROAD = LineString([(0, 0), (1000, 0)])
LAT, LON = 58.38, 26.72


@pytest.mark.parametrize(
    ("route", "expected"),
    [
        # Straight across.
        (LineString([(500, -100), (500, 100)]), 1),
        # Up to the road and back on the same side: no crossing.
        (LineString([(400, -100), (500, 0), (600, -100)]), 0),
        # Starts on the road.
        (LineString([(500, 0), (500, 100)]), 0),
        # A track beside the road never meets it.
        (LineString([(0, 8), (1000, 8)]), 0),
        # Across, and later back.
        (LineString([(100, -100), (100, 100), (800, 100), (800, -100)]), 2),
    ],
)
def test_major_crossings(route, expected):
    assert len(major_crossings(route, ROAD)) == expected


def test_riding_along_a_road_with_slightly_different_coordinates_is_no_crossing():
    # Another engine's geometry wandering 4 m either side of the centre line
    # meets it dozens of times.
    x = np.arange(0, 1001, 20.0)
    y = 4.0 * np.where(np.arange(len(x)) % 2, 1, -1)
    route = LineString(np.column_stack([x, y]))
    assert len(list(route.intersection(ROAD).geoms)) > 40
    assert major_crossings(route, ROAD) == []


def test_dual_carriageway_counts_once():
    roads = MultiLineString([[(0, -6), (1000, -6)], [(0, 6), (1000, 6)]])
    assert len(major_crossings(LineString([(500, -100), (500, 100)]), roads)) == 1


@pytest.mark.parametrize(
    ("attributes", "sidewalk", "expected"),
    [
        ({"highway": "cycleway"}, False, 1),
        ({"highway": "footway"}, True, 2),
        ({"highway": "primary", "cycleway:right": "track"}, False, 1),
        ({"highway": "residential", "maxspeed": "30"}, False, 1),
        ({"highway": "residential"}, False, 2),
        ({"highway": "tertiary"}, False, 3),
        ({"highway": "secondary", "lanes": "4"}, False, 4),
        ({"highway": "primary", "cycleway": "lane"}, False, 2),
        ({"highway": "primary", "cycleway": "lane", "maxspeed": "70"}, False, 4),
        # A shared lane is mixed traffic, not a bike lane.
        ({"highway": "tertiary", "cycleway": "shared_lane"}, False, 3),
        # Two lanes on a one-way street are four in the tables' terms.
        ({"highway": "secondary", "lanes": "2", "oneway": True}, False, 4),
    ],
)
def test_traffic_stress(attributes, sidewalk, expected):
    assert traffic_stress(attributes, sidewalk) == expected


def test_turn_count_counts_a_corner_once():
    straight = np.column_stack([np.arange(0, 200, 10.0), np.zeros(20)])
    corner = np.vstack(
        [straight, np.column_stack([np.full(20, 190.0), np.arange(10, 210, 10.0)])]
    )
    assert turn_count(straight) == 0
    assert turn_count(corner) == 1


def scoring_graph() -> nx.MultiDiGraph:
    """A west-east street crossed by a major road at a junction and passing under
    another, and a footway for the router's roadside lookups."""

    graph = nx.MultiDiGraph(crs="EPSG:4326")
    for name, x in (("a", LON), ("x", LON + 0.002), ("b", LON + 0.01)):
        graph.add_node(name, x=x, y=LAT)
    graph.add_node("park", x=LON, y=LAT + 0.01)
    graph.add_node("south", x=LON + 0.002, y=LAT - 0.002)
    graph.add_node("north", x=LON + 0.002, y=LAT + 0.002)
    graph.add_node("signal", x=LON + 0.002, y=LAT + 0.01, highway="traffic_signals")
    graph.add_node("bridge_south", x=LON + 0.008, y=LAT - 0.002)
    graph.add_node("bridge_north", x=LON + 0.008, y=LAT + 0.002)
    for start, end, length, highway in (
        ("a", "x", 117.0, "residential"),
        ("x", "b", 467.0, "residential"),
        ("a", "park", 1100.0, "footway"),
        ("south", "x", 222.0, "primary"),
        ("x", "north", 222.0, "primary"),
        ("north", "signal", 890.0, "primary"),
        # The bridge has no node on the street below it.
        ("bridge_south", "bridge_north", 445.0, "primary"),
    ):
        graph.add_edge(start, end, length=length, highway=highway)
        graph.add_edge(end, start, length=length, highway=highway)
    return graph


@pytest.fixture
def scorer():
    return Scorer(NetworkXRouter(scoring_graph()))


def test_score_of_a_route_along_the_network(scorer):
    row = scorer.score([[LON, LAT], [LON + 0.01, LAT]])
    assert row["length_m"] == pytest.approx(584, abs=5)
    assert row["share_unmatched"] == 0
    # The junction, without a signal nearby, but not the bridge overhead.
    assert row["unsignalled_per_km"] == pytest.approx(1 / 0.584, abs=0.05)


def test_overlap_is_the_share_of_a_route_near_the_other(scorer):
    route = [[LON, LAT], [LON + 0.01, LAT]]
    half = [[LON, LAT], [LON + 0.005, LAT]]
    assert scorer.overlap(route, route) == 1.0
    assert scorer.overlap(route, half) == pytest.approx(0.5, abs=0.05)


def test_summary_compares_only_valid_routes():
    rows = []
    for pair in range(4):
        rows.append({"pair": pair, "engine": "networkx", "valid": 1, **dict.fromkeys(COMPARED, 1.0)})
        rows.append({"pair": pair, "engine": "other", "valid": 1, **dict.fromkeys(COMPARED, 2.0)})
    # This route's 99 must not reach any comparison.
    rows.append({"pair": 4, "engine": "other", "valid": 0, **dict.fromkeys(COMPARED, 99.0)})
    rows.append({"pair": 4, "engine": "networkx", "valid": 1, **dict.fromkeys(COMPARED, 1.0)})
    summary = summarise(pd.DataFrame(rows))
    assert "99" not in summary
    assert "| other | 0.8 | 2 |" in summary
    assert "+1.000 [+1.000, +1.000]" in summary
