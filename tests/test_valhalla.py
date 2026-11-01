from backend.valhalla import ValhallaRouter


def test_feature_carries_valhallas_own_directions():
    trip = {
        "summary": {"length": 1.25, "time": 300},
        "legs": [
            {
                # One point, encoded at Valhalla's precision.
                "shape": "_ibE_seK",
                "maneuvers": [
                    {"instruction": "Bike north on Riia.", "length": 1.25},
                    {"instruction": "You have arrived at your destination.", "length": 0.0},
                ],
            }
        ],
    }
    properties = ValhallaRouter("http://unused")._feature(trip)["properties"]
    assert properties["directions"] == [
        {"instruction": "Bike north on Riia.", "length_m": 1250.0},
        {"instruction": "You have arrived at your destination.", "length_m": 0.0},
    ]
