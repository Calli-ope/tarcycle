import json
from dataclasses import replace

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from pyproj import Transformer
from shapely.geometry import LineString, box

from backend.gps_layer import edge_gps_trips, load_gps_ways, way_ids
from backend.gps_coverage import (
    CoverageSettings,
    METRIC_CRS,
    build,
    cell_centres,
    cell_ids,
    classify_cells,
    classify_way,
    densify_tracks,
    alongside_share,
    gap_group,
    infra_kind,
    in_osmnx_bike_network,
    rebuild_from_cache,
    valid_route_codes,
    read_location_chunks,
)

SETTINGS = CoverageSettings()
NO_TRIM = replace(SETTINGS, trim_ends_m=0.0)
ORIGIN_X, ORIGIN_Y = 659_000.0, 6_474_000.0


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"highway": "cycleway"}, "cycle_infra"),
        ({"highway": "footway", "bicycle": "designated"}, "cycle_infra"),
        ({"highway": "secondary", "cycleway:right": "lane"}, "cycle_infra"),
        ({"highway": "secondary", "cycleway": "separate"}, "bikeable"),
        ({"highway": ["residential", "service"]}, "bikeable"),
        ({"highway": "footway"}, "not_bikeable"),
        ({"highway": "footway", "bicycle": "yes"}, "bikeable"),
        ({"highway": "steps"}, "not_bikeable"),
        ({"highway": "residential", "bicycle": "no"}, "not_bikeable"),
        ({"highway": ["footway", "path"]}, "bikeable"),
    ],
)
def test_classify_way(tags, expected):
    assert classify_way(tags) == expected


def test_densify_samples_every_step_and_breaks_between_routes():
    route = np.array([1, 1, 2, 2])
    x = np.array([0.0, 20.0, 1000.0, 1000.0])
    y = np.array([0.0, 0.0, 0.0, 10.0])
    t = np.array([0.0, 10.0, 0.0, 10.0])
    sample_route, sample_x, _ = densify_tracks(route, x, y, t, NO_TRIM)
    route_one = np.sort(sample_x[sample_route == 1])
    np.testing.assert_allclose(route_one, [0, 5, 10, 15, 20])
    # No sample interpolated across the route boundary (x between 20 and 1000).
    assert not np.any((sample_x > 20) & (sample_x < 1000))


def test_densify_drops_gps_spikes_and_long_gaps():
    route = np.ones(4, dtype=int)
    x = np.array([0.0, 40.0, 900.0, 940.0])  # 40 m in 10 s is fine, 860 m is a spike
    y = np.zeros(4)
    t = np.array([0.0, 10.0, 20.0, 30.0])
    _, sample_x, _ = densify_tracks(route, x, y, t, NO_TRIM)
    assert not np.any((sample_x > 40) & (sample_x < 900))


def test_isolated_point_produces_no_samples():
    sample_route, _, _ = densify_tracks(
        np.array([7]), np.array([1.0]), np.array([1.0]), np.array([0.0]), NO_TRIM
    )
    assert len(sample_route) == 0


def test_cell_ids_round_trip_to_cell_centre():
    x = np.array([ORIGIN_X + 3.0, ORIGIN_X + 17.0])
    y = np.array([ORIGIN_Y + 9.9, ORIGIN_Y + 0.1])
    centre_x, centre_y = cell_centres(cell_ids(x, y, 10.0), 10.0)
    np.testing.assert_allclose(centre_x, [ORIGIN_X + 5, ORIGIN_X + 15])
    np.testing.assert_allclose(centre_y, [ORIGIN_Y + 5, ORIGIN_Y + 5])


def test_classify_cells_prefers_most_specific_layer():
    inf = np.inf
    result = classify_cells(
        np.array([3.0, 40.0, inf, inf]),
        np.array([3.0, 4.0, inf, inf]),
        np.array([3.0, 4.0, 10.0, inf]),
        near_m=15.0,
    )
    assert list(result) == ["cycle_infra", "road_no_infra", "osm_not_bikeable", "no_osm_way"]


def _synthetic_trips(users: int) -> pd.DataFrame:
    """Every user rides east along a cycleway, then back along an unmapped line."""

    to_lonlat = Transformer.from_crs(METRIC_CRS, "EPSG:4326", always_xy=True)
    rows = []
    for user in range(users):
        route = 1_000 + user
        xs = np.arange(0, 400, 40.0)
        legs = [(xs, 2.0), (xs[::-1], 100.0)]
        step = 0
        for leg_x, offset_y in legs:
            for value in leg_x:
                lon, lat = to_lonlat.transform(ORIGIN_X + value, ORIGIN_Y + offset_y)
                rows.append(
                    {
                        "RouteCode": route,
                        "UserId": f"user-{user}",
                        "CycleNumber": 1,
                        "Latitude": lat,
                        "Longitude": lon,
                        "CoordinatesDate": "2025-05-01",
                        "CoordinatesTime": f"08:{step // 6:02d}:{step % 6 * 10:02d}+00",
                    }
                )
                step += 1
    return pd.DataFrame(rows)


def test_chunked_reading_never_splits_a_trip(tmp_path):
    path = tmp_path / "locations_202505.csv"
    _synthetic_trips(users=5).to_csv(path, index=False)  # 20 rows per trip
    chunks = list(read_location_chunks(path, chunk_rows=7))
    routes_per_chunk = [set(chunk["RouteCode"]) for chunk in chunks]
    assert sum(len(routes) for routes in routes_per_chunk) == 5
    assert sum(len(chunk) for chunk in chunks) == 100


def test_build_end_to_end(tmp_path):
    locations = tmp_path / "Locations"
    locations.mkdir()
    _synthetic_trips(users=12).to_csv(locations / "locations_202505.csv", index=False)
    out = tmp_path / "out"
    out.mkdir()
    area = box(ORIGIN_X - 500, ORIGIN_Y - 500, ORIGIN_X + 1000, ORIGIN_Y + 1000)
    gpd.GeoDataFrame(geometry=[area], crs=METRIC_CRS).to_file(out / "study_area.geojson")
    gpd.GeoDataFrame(
        {"way_id": [101], "highway": ["cycleway"], "name": ["Test"], "edge_class": ["cycle_infra"], "in_bike_graph": [True]},
        geometry=[LineString([(ORIGIN_X - 50, ORIGIN_Y), (ORIGIN_X + 450, ORIGIN_Y)])],
        crs=METRIC_CRS,
    ).to_file(out / "osm_edges.gpkg", driver="GPKG")

    summary = build(locations, out, SETTINGS)

    shares = summary["share_of_ridden_distance"]
    assert shares["cycle_infra"] == pytest.approx(0.5, abs=0.1)
    assert shares["no_osm_way"] == pytest.approx(0.5, abs=0.1)
    candidates = json.loads((out / "candidates.geojson").read_text())["features"]
    assert len(candidates) == 1
    best = candidates[0]["properties"]
    assert best["cell_class"] == "no_osm_way"
    assert best["median_trips"] == 12
    assert 280 <= best["length_m"] <= 420
    lon, lat = candidates[0]["geometry"]["coordinates"][0][0]
    assert 26 < lon < 27 and 58 < lat < 59  # GeoJSON is [lng, lat]
    edges = json.loads((out / "edges.geojson").read_text())["features"]
    assert edges[0]["properties"]["trips"] == 12


def test_privacy_threshold_hides_rare_paths(tmp_path):
    locations = tmp_path / "Locations"
    locations.mkdir()
    _synthetic_trips(users=3).to_csv(locations / "locations_202505.csv", index=False)
    out = tmp_path / "out"
    out.mkdir()
    area = box(ORIGIN_X - 500, ORIGIN_Y - 500, ORIGIN_X + 1000, ORIGIN_Y + 1000)
    gpd.GeoDataFrame(geometry=[area], crs=METRIC_CRS).to_file(out / "study_area.geojson")
    gpd.GeoDataFrame(
        {"way_id": [101], "highway": ["cycleway"], "name": [""], "edge_class": ["cycle_infra"], "in_bike_graph": [True]},
        geometry=[LineString([(ORIGIN_X, ORIGIN_Y), (ORIGIN_X + 400, ORIGIN_Y)])],
        crs=METRIC_CRS,
    ).to_file(out / "osm_edges.gpkg", driver="GPKG")

    summary = build(locations, out, SETTINGS)

    assert summary["exported_cells"] == 0
    assert json.loads((out / "cells.geojson").read_text())["features"] == []


def test_densify_trims_trip_ends_near_stations():
    route = np.repeat([1, 2], 16)
    x = np.tile(np.arange(0, 320, 20.0), 2)
    y = np.repeat([0.0, 1000.0], 16)
    t = np.tile(np.arange(16) * 5.0, 2)
    sample_route, sample_x, _ = densify_tracks(route, x, y, t, SETTINGS)
    for trip in (1, 2):
        along = sample_x[sample_route == trip]
        assert along.min() >= 50 and along.max() <= 250


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ({"highway": "residential"}, True),
        ({"highway": "cycleway"}, True),
        ({"highway": "footway"}, False),
        ({"highway": "footway", "bicycle": "yes"}, False),  # osmnx drops every footway
        ({"highway": "steps"}, False),
        ({"highway": "motorway_link"}, False),
        ({"highway": "residential", "bicycle": "no"}, False),
        ({"highway": "service", "service": "private"}, False),
        ({"highway": "pedestrian", "area": "yes"}, False),
        ({"highway": "pedestrian"}, True),
    ],
)
def test_in_osmnx_bike_network(tags, expected):
    assert in_osmnx_bike_network(tags) is expected


def test_valid_route_codes_drops_aborted_rentals(tmp_path):
    path = tmp_path / "routes_2025.csv"
    pd.DataFrame(
        {
            "RouteCode": [1, 2, 3, 4],
            "Length": [2.1, 0.0, 0.3, 1.0],
            "DurationMinutes": [10.0, 5.0, 1.2, 2.0],
            "PersonalIdCode": [501, 502, 503, 504],
        }
    ).to_csv(path, index=False)
    assert list(valid_route_codes(path, min_trip_minutes=2.0)) == [1, 4]


def _write_osm(out, rows):
    area = box(ORIGIN_X - 500, ORIGIN_Y - 500, ORIGIN_X + 1000, ORIGIN_Y + 1000)
    gpd.GeoDataFrame(geometry=[area], crs=METRIC_CRS).to_file(out / "study_area.geojson")
    frame = pd.DataFrame(rows)
    gpd.GeoDataFrame(
        frame.drop(columns="geometry"), geometry=list(frame["geometry"]), crs=METRIC_CRS
    ).to_file(out / "osm_edges.gpkg", driver="GPKG")


def test_gps_layer_marks_ridden_ways_missing_from_router_graph(tmp_path):
    locations = tmp_path / "Locations"
    locations.mkdir()
    trips = _synthetic_trips(users=14)
    trips.to_csv(locations / "locations_202505.csv", index=False)
    routes = tmp_path / "routes_2025.csv"
    route_codes = sorted(trips["RouteCode"].unique())
    pd.DataFrame(
        {
            "RouteCode": route_codes,
            "Length": [0.0, 0.0] + [1.5] * (len(route_codes) - 2),  # two aborted rentals
            "DurationMinutes": [9.0] * len(route_codes),
        }
    ).to_csv(routes, index=False)
    out = tmp_path / "out"
    out.mkdir()
    _write_osm(
        out,
        [
            {"way_id": 101, "highway": "cycleway", "name": "Ride", "edge_class": "cycle_infra", "in_bike_graph": True,
             "geometry": LineString([(ORIGIN_X - 50, ORIGIN_Y), (ORIGIN_X + 450, ORIGIN_Y)])},
            # One footway in four pieces (unsimplified OSM); the last piece is never ridden.
            *[
                {"way_id": 202, "highway": "footway", "name": "", "edge_class": "not_bikeable", "in_bike_graph": False,
                 "geometry": LineString([(ORIGIN_X + a, ORIGIN_Y + 100), (ORIGIN_X + b, ORIGIN_Y + 100)])}
                for a, b in [(-50, 100), (100, 250), (250, 450), (450, 1000)]
            ],
        ],
    )

    summary = build(locations, out, SETTINGS, routes_file=routes)

    ways = {
        feature["properties"]["way_id"]: feature["properties"]
        for feature in json.loads((out / "gps_ways.geojson").read_text())["features"]
    }
    assert set(ways) == {101, 202}
    assert ways[202]["in_bike_graph"] is False
    assert ways[202]["trips"] == 12  # 14 riders minus the two aborted rentals
    assert ways[202]["length_m"] == 1050
    assert ways[202]["ridden_m"] == 500  # only the three ridden pieces are drawn
    assert ways[202]["gap_group"] == "separate"  # 100 m from the cycleway
    assert ways[101]["gap_group"] is None
    assert summary["gps_layer"]["ridden_ways_missing_from_router_graph"] == 1
    assert summary["router_graph_share_of_ridden_distance"]["other_osm_way"] == pytest.approx(0.5, abs=0.1)
    assert (out / "edges.geojson").exists()  # kept for comparison with the first run

    assert summary["gps_layer"]["gap_groups"]["separate"]["ways"] == 1
    assert (out / "osm_cycleways.geojson").exists()

    (out / "gps_ways.geojson").unlink()
    rebuild_from_cache(out, SETTINGS)
    rebuilt = {
        feature["properties"]["way_id"]: feature["properties"]
        for feature in json.loads((out / "gps_ways.geojson").read_text())["features"]
    }
    assert rebuilt == ways

    gps_ways = load_gps_ways(out / "gps_ways.geojson")
    assert edge_gps_trips({"osmid": [999, 202]}, gps_ways) == 12
    assert edge_gps_trips({"osmid": 999}, gps_ways) is None


@pytest.mark.parametrize(
    ("osmid", "expected"),
    [(5, [5]), ([5, 6], [5, 6]), ("[5, 6]", [5, 6]), ("7", [7]), (None, [])],
)
def test_way_ids_accepts_every_osmnx_osmid_form(osmid, expected):
    assert way_ids(osmid) == expected


@pytest.mark.parametrize(("share", "group"), [(0.95, "alongside"), (0.8, "alongside"), (0.6, "partly"), (0.2, "separate")])
def test_gap_group(share, group):
    assert gap_group(share) == group


def test_alongside_share_measures_parallel_sidewalks():
    road = np.array([LineString([(0, 0), (400, 0)])])
    sidewalk = LineString([(0, 10), (400, 10)])
    park_path = LineString([(0, 100), (400, 300)])
    half = LineString([(200, 10), (600, 10)])
    shares = alongside_share(np.array([sidewalk, park_path, half]), road, 25.0)
    # The corridor reaches sqrt(25² - 10²) ≈ 22.9 m past the road's end: (200 + 22.9) / 400.
    np.testing.assert_allclose(shares, [1.0, 0.0, 222.9 / 400], atol=0.01)


@pytest.mark.parametrize(
    ("highway", "kind"),
    [("cycleway", "separate"), ("footway", "separate"), ("path", "separate"),
     ("secondary", "on_road"), ("residential", "on_road"), ("cycleway,residential", "separate")],
)
def test_infra_kind(highway, kind):
    assert infra_kind(highway) == kind
