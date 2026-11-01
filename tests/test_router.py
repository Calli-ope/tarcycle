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
