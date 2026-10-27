import numpy as np
import pandas as pd
from shapely.geometry import LineString

from backend.models import RoutingPreferences
from backend.trace_match import (
    clean_trace,
    paired_difference,
    sampled,
    share_within,
    shortest_preferences,
    usable_trip,
)


def test_sampled_is_deterministic_and_roughly_the_requested_share():
    codes = np.arange(1_735_000_000_000, 1_735_000_100_000, dtype=np.int64)
    first, second = sampled(codes, 40), sampled(codes, 40)
    assert (first == second).all()
    assert 0.003 < first.mean() < 0.005


def test_clean_trace_drops_a_gps_jump():
    x = np.array([0.0, 50.0, 5000.0, 100.0, 150.0])
    y = np.zeros(5)
    seconds = np.array([0.0, 10.0, 20.0, 30.0, 40.0])
    assert clean_trace(x, y, seconds, max_speed_ms=12.0).tolist() == [0, 1, 3, 4]


def test_usable_trip_rejects_short_trips_and_loops():
    straight = np.linspace(0, 2000, 20)
    assert usable_trip(straight, np.zeros(20))
    assert not usable_trip(straight[:5], np.zeros(5))  # too few points
    assert not usable_trip(np.linspace(0, 500, 20), np.zeros(20))  # too short
    out_and_back = np.concatenate([np.linspace(0, 3000, 10), np.linspace(3000, 900, 10)])
    assert not usable_trip(out_and_back, np.zeros(20))  # 5.1 km ridden for 0.9 km


def test_share_within_counts_the_part_near_the_other_line():
    route = LineString([(0, 0), (100, 0)])
    trace = LineString([(0, 5), (50, 5)])  # follows the first half; samples 0-60 m are within 20 m
    assert abs(share_within(route, trace, 20, 10) - 7 / 11) < 1e-9
    assert share_within(trace, route, 20, 10) == 1.0


def test_shortest_preferences_are_neutral():
    preferences = shortest_preferences()
    assert preferences.major_road_factor == 1.0
    assert preferences.popularity_weight == 0.0
    assert RoutingPreferences().popularity_weight > 0


def test_paired_difference_uses_only_trips_both_engines_routed():
    results = pd.DataFrame(
        {
            "trip": [0, 0, 1, 1, 2],
            "engine": ["networkx", "shortest"] * 2 + ["networkx"],
            "route_on_trace": [0.9, 0.6, 0.8, 0.7, 0.1],
        }
    )
    difference = paired_difference(results, "route_on_trace", "networkx", "shortest")
    assert difference["trips"] == 2
    assert difference["mean"] == 0.2
