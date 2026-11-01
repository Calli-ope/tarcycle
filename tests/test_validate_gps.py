import numpy as np
import pandas as pd
import pytest
from pyproj import Transformer

from backend.evaluate import Scorer
from backend.gps_coverage import METRIC_CRS, CoverageSettings
from backend.router import NetworkXRouter
from backend.validate_gps import (
    clean_track,
    compare_with_tracks,
    is_directed_trip,
    iter_tracks,
    report,
    sample_tracks,
    track_overlap,
)
from test_evaluate import LAT, LON, scoring_graph


SETTINGS = CoverageSettings()
ORIGIN_X, ORIGIN_Y = 659_000.0, 6_474_000.0


def straight_track(length: float = 2000.0, step: float = 50.0) -> np.ndarray:
    x = np.arange(0.0, length + step, step)
    return np.column_stack([ORIGIN_X + x, np.full(len(x), ORIGIN_Y)])


def test_clean_track_removes_a_gps_spike():
    x = np.array([0.0, 40.0, 5000.0, 80.0, 120.0])
    track = clean_track(x, np.zeros(5), np.arange(5) * 10.0, SETTINGS)
    assert list(track[:, 0]) == [0.0, 40.0, 80.0, 120.0]


def test_clean_track_rejects_lost_signal():
    x = np.array([0.0, 40.0, 500.0])
    assert clean_track(x, np.zeros(3), np.array([0.0, 10.0, 200.0]), SETTINGS) is None


@pytest.mark.parametrize(
    ("track", "expected"),
    [
        (straight_track(2000.0), True),
        (straight_track(500.0), False),
        (straight_track(7000.0), False),
        # Out and most of the way back: not choosing a way from A to B.
        (np.vstack([straight_track(2000.0), straight_track(2000.0)[::-1][:-25]]), False),
    ],
)
def test_is_directed_trip(track, expected):
    assert is_directed_trip(track) is expected


def test_sample_tracks_is_uniform_and_bounded():
    tracks = [np.array([[i, 0.0]]) for i in range(1000)]
    sample = sample_tracks(tracks, 100, seed=1)
    assert len(sample) == 100
    assert 30 < sum(track[0, 0] >= 500 for track in sample) < 70


def test_iter_tracks_reads_only_directed_trips_of_real_rentals(tmp_path):
    to_lonlat = Transformer.from_crs(METRIC_CRS, "EPSG:4326", always_xy=True)

    def rows(route, track):
        longitude, latitude = to_lonlat.transform(track[:, 0], track[:, 1])
        return pd.DataFrame(
            {
                "RouteCode": route,
                "UserId": f"user-{route}",
                "Latitude": latitude,
                "Longitude": longitude,
                "CoordinatesDate": "2025-09-01",
                "CoordinatesTime": [f"08:{i // 6:02d}:{i % 6 * 10:02d}+00" for i in range(len(track))],
            }
        )

    path = tmp_path / "locations_202509.csv"
    pd.concat(
        [
            rows(1, straight_track(2000.0)),
            rows(2, straight_track(2000.0)),
            rows(3, straight_track(300.0)),
        ]
    ).to_csv(path, index=False)
    tracks = list(iter_tracks([path], SETTINGS, valid_routes=np.array([1, 3])))
    assert len(tracks) == 1
    assert tracks[0][0, 0] == pytest.approx(ORIGIN_X, abs=0.01)


class FixedEngine:
    def __init__(self, coordinates):
        self.coordinates = coordinates

    def route_geojson(self, *_points):
        return {"features": [{"geometry": {"coordinates": self.coordinates}}]}


@pytest.fixture
def scorer():
    return Scorer(NetworkXRouter(scoring_graph()))


def test_track_overlap_of_same_and_different_ways(scorer):
    track = [[LON, LAT], [LON + 0.03, LAT]]
    assert track_overlap(scorer, track, track) == pytest.approx(1.0)
    assert track_overlap(scorer, [[LON, LAT + 0.01], [LON + 0.03, LAT + 0.01]], track) == 0.0
    half = track_overlap(scorer, [[LON, LAT], [LON + 0.015, LAT]], track)
    assert half == pytest.approx(2 / 3, abs=0.03)


def test_report_ranks_the_engine_closer_to_the_riders(scorer):
    to_metres = Transformer.from_crs("EPSG:4326", METRIC_CRS, always_xy=True).transform
    x, y = to_metres(np.array([LON, LON + 0.04]), np.array([LAT, LAT]))
    tracks = [
        np.column_stack([np.linspace(x[0], x[1], 40), np.linspace(y[0], y[1], 40) + i * 0.1])
        for i in range(25)
    ]
    engines = {
        "networkx": FixedEngine([[LON, LAT], [LON + 0.04, LAT]]),
        "networkx_no_gps": FixedEngine([[LON, LAT + 0.02], [LON + 0.04, LAT + 0.02]]),
    }
    table = compare_with_tracks(tracks, engines, scorer)
    assert set(table.columns) == {"pair", "engine", "overlap"}
    summary = report(table)
    assert "25 held-out trips" in summary
    assert "+1.000 [+1.000, +1.000]" in summary
