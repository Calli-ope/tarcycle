import networkx as nx
import pytest

from backend.router import NetworkXRouter, OutsideNetwork


LAT, LON = 58.38, 26.72
START = (LAT - 0.001, LON)
END = (LAT + 0.010, LON)


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


def test_point_far_from_the_network_has_no_route():
    router = NetworkXRouter(road_graph())
    with pytest.raises(OutsideNetwork, match="from the bicycle network"):
        router.route_geojson(*START, LAT + 0.05, LON)
