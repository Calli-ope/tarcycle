import networkx as nx
import pytest
from shapely.geometry import LineString

from backend.graph import add_contraflow
from backend.router import NetworkXRouter, OutsideNetwork


LAT, LON = 58.38, 26.72
START = (LAT - 0.001, LON)
END = (LAT + 0.010, LON)
# The two stubs joining START and END to the network, as straight-line connectors.
STUBS_M = 2 * 111.3


def road_graph() -> nx.MultiDiGraph:
    """From A to B: a direct major road (1000 m), a plain road (1300 m) and a
    long cycleway (1700 m), which is the cheapest but over the 1.5× limit."""

    graph = nx.MultiDiGraph(crs="EPSG:4326")
    nodes = {
        "a": (LON, LAT),
        "b": (LON, LAT + 0.009),
        "plain": (LON + 0.004, LAT + 0.0045),
        "cycleway": (LON + 0.008, LAT + 0.0045),
    }
    for name, (x, y) in nodes.items():
        graph.add_node(name, x=x, y=y)

    def both_ways(start, end, length, **tags):
        graph.add_edge(start, end, length=length, **tags)
        graph.add_edge(end, start, length=length, **tags)

    both_ways("a", "b", 1000.0, highway="primary")
    both_ways("a", "plain", 650.0, highway="residential")
    both_ways("plain", "b", 650.0, highway="residential")
    both_ways("a", "cycleway", 850.0, highway="cycleway")
    both_ways("cycleway", "b", 850.0, highway="cycleway")
    # The router's roadside lookups need a footway to look at.
    graph.add_node("park", x=LON - 0.01, y=LAT)
    both_ways("a", "park", 500.0, highway="footway")
    graph.add_node("start", x=START[1], y=START[0])
    graph.add_node("end", x=END[1], y=END[0])
    both_ways("start", "a", 111.0, highway="residential")
    both_ways("b", "end", 111.0, highway="residential")
    return graph


def test_route_over_the_detour_limit_keeps_what_preference_fits():
    router = NetworkXRouter(road_graph())
    # Without the limit the cycleway would win; the major road is what the old
    # fallback to the shortest route chose.
    route = router.route_geojson(*START, *END)["features"][0]
    assert route["properties"]["route_length_m"] == pytest.approx(1300.0 + STUBS_M, abs=2)


def test_route_within_the_detour_limit_is_the_preferred_one():
    graph = road_graph()
    for _, _, data in graph.edges(data=True):
        if data["highway"] == "cycleway":
            data["length"] = 700.0
    route = NetworkXRouter(graph).route_geojson(*START, *END)["features"][0]
    assert route["properties"]["route_length_m"] == pytest.approx(1400.0 + STUBS_M, abs=2)


def test_point_far_from_the_network_has_no_route():
    router = NetworkXRouter(road_graph())
    with pytest.raises(OutsideNetwork, match="from the bicycle network"):
        router.route_geojson(*START, LAT + 0.05, LON)


def directions_graph() -> nx.MultiDiGraph:
    """Riia east to a junction, left into Pargi, ahead over a short unnamed
    crossing onto more of Pargi, then right onto a cycleway."""

    graph = nx.MultiDiGraph(crs="EPSG:4326")
    nodes = {
        "start": (LON, LAT),
        "junction": (LON + 0.01, LAT),
        "south": (LON + 0.01, LAT - 0.002),
        "kerb": (LON + 0.01, LAT + 0.004),
        "kerb2": (LON + 0.01, LAT + 0.00415),
        "corner": (LON + 0.01, LAT + 0.008),
        "end": (LON + 0.016, LAT + 0.008),
        "park": (LON, LAT + 0.02),
    }
    for name, (x, y) in nodes.items():
        graph.add_node(name, x=x, y=y)
    for start, end, length, tags in (
        ("start", "junction", 580.0, {"highway": "residential", "name": "Riia"}),
        ("junction", "south", 222.0, {"highway": "residential", "name": "Pargi"}),
        ("junction", "kerb", 445.0, {"highway": "residential", "name": "Pargi"}),
        ("kerb", "kerb2", 17.0, {"highway": "path"}),
        ("kerb2", "corner", 428.0, {"highway": "residential", "name": "Pargi"}),
        ("corner", "end", 350.0, {"highway": "cycleway"}),
        ("start", "park", 2000.0, {"highway": "footway"}),
    ):
        graph.add_edge(start, end, length=length, **tags)
        graph.add_edge(end, start, length=length, **tags)
    return graph


def test_directions_follow_the_route():
    route = NetworkXRouter(directions_graph()).route_geojson(
        LAT, LON + 0.005, LAT + 0.008, LON + 0.013
    )["features"][0]
    steps = route["properties"]["directions"]
    assert [step["instruction"] for step in steps] == [
        "Head east on Riia.",
        "Turn left onto Pargi.",
        "Turn right onto the cycleway.",
        "You have arrived.",
    ]
    assert steps[1]["length_m"] == pytest.approx(890, abs=5)
    assert sum(step["length_m"] for step in steps) == pytest.approx(
        route["properties"]["route_length_m"], abs=1
    )


def test_directions_name_a_cycleway_by_its_street_and_skip_straight_on_unnamed_ways():
    # A cycleway 11 m beside Riia, then straight on along a path through a park.
    graph = nx.MultiDiGraph(crs="EPSG:4326")
    nodes = {
        "a": (LON, LAT),
        "b": (LON + 0.01, LAT),
        "c": (LON, LAT + 0.0001),
        "d": (LON + 0.01, LAT + 0.0001),
        "e": (LON + 0.015, LAT + 0.0001),
        "park": (LON, LAT + 0.02),
    }
    for name, (x, y) in nodes.items():
        graph.add_node(name, x=x, y=y)
    for start, end, length, tags in (
        ("a", "b", 583.0, {"highway": "residential", "name": "Riia"}),
        ("c", "d", 583.0, {"highway": "cycleway"}),
        ("d", "e", 292.0, {"highway": "path"}),
        ("c", "park", 2200.0, {"highway": "footway"}),
    ):
        graph.add_edge(start, end, length=length, **tags)
        graph.add_edge(end, start, length=length, **tags)
    steps = NetworkXRouter(graph).route_geojson(LAT + 0.0001, LON, LAT + 0.0001, LON + 0.015)[
        "features"
    ][0]["properties"]["directions"]
    assert steps == [
        {"instruction": "Head east on the cycleway along Riia.", "length_m": 875.0},
        {"instruction": "You have arrived.", "length_m": 0.0},
    ]


def test_directions_skip_moving_onto_a_cycleway_along_the_same_street():
    graph = nx.MultiDiGraph(crs="EPSG:4326")
    nodes = {
        "a": (LON, LAT),
        "b": (LON + 0.005, LAT),
        "f": (LON + 0.01, LAT),
        "c": (LON + 0.005, LAT + 0.0001),
        "d": (LON + 0.01, LAT + 0.0001),
        "park": (LON, LAT + 0.02),
    }
    for name, (x, y) in nodes.items():
        graph.add_node(name, x=x, y=y)
    for start, end, length, tags in (
        ("a", "b", 292.0, {"highway": "residential", "name": "Riia"}),
        ("b", "f", 292.0, {"highway": "residential", "name": "Riia"}),
        ("b", "c", 11.0, {"highway": "path"}),
        ("c", "d", 292.0, {"highway": "cycleway"}),
        ("a", "park", 2200.0, {"highway": "footway"}),
    ):
        graph.add_edge(start, end, length=length, **tags)
        graph.add_edge(end, start, length=length, **tags)
    steps = NetworkXRouter(graph).route_geojson(LAT, LON, LAT + 0.0001, LON + 0.01)[
        "features"
    ][0]["properties"]["directions"]
    assert [step["instruction"] for step in steps] == ["Head east on Riia.", "You have arrived."]


def test_contraflow_adds_the_reverse_of_ways_open_to_bikes_both_ways():
    graph = nx.MultiDiGraph()
    line = LineString([(0, 0), (1, 1)])
    graph.add_edge("a", "b", **{"oneway:bicycle": "no"}, geometry=line, length=5.0)
    graph.add_edge("b", "c", cycleway="opposite_lane", length=5.0)
    graph.add_edge("c", "d", length=5.0)
    graph.add_edge("d", "e", **{"bicycle:backward": "yes"}, length=5.0)
    graph.add_edge("e", "f", **{"bicycle:backward": "no"}, length=5.0)
    add_contraflow(graph)
    assert graph.has_edge("b", "a")
    assert list(graph.edges["b", "a", 0]["geometry"].coords) == [(1, 1), (0, 0)]
    assert graph.has_edge("c", "b")
    assert not graph.has_edge("d", "c")
    assert graph.has_edge("e", "d")
    assert not graph.has_edge("f", "e")
