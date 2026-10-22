from __future__ import annotations

import json
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


# Valhalla bicycle costing, see
# https://valhalla.github.io/valhalla/api/turn-by-turn/api-reference/#bicycle-costing-options
BICYCLE_COSTING = {
    "bicycle_type": "Hybrid",
    "use_roads": 0.1,  # strongly prefer cycleways and paths over roads
    "use_hills": 0.3,  # needs tiles built with elevation (build_elevation)
    "avoid_bad_surfaces": 0.5,  # cobblestones, sett, unpaved
    "use_living_streets": 0.5,
}


class ValhallaUnavailable(RuntimeError):
    """The configured Valhalla service could not route the request."""


def _decode_polyline(shape: str, precision: int = 6) -> list[list[float]]:
    coordinates: list[list[float]] = []
    index = latitude = longitude = 0
    while index < len(shape):
        values = []
        for _ in range(2):
            result = shift = 0
            while True:
                byte = ord(shape[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            values.append(~(result >> 1) if result & 1 else result >> 1)
        latitude += values[0]
        longitude += values[1]
        coordinates.append([longitude / 10**precision, latitude / 10**precision])
    return coordinates


class ValhallaRouter:
    backend_name = "valhalla"

    def __init__(self, endpoint: str, timeout: float = 15.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def route_geojson(
        self,
        start_lat: float,
        start_lon: float,
        end_lat: float,
        end_lon: float,
    ) -> dict[str, Any]:
        payload = {
            "locations": [
                {"lat": start_lat, "lon": start_lon},
                {"lat": end_lat, "lon": end_lon},
            ],
            "costing": "bicycle",
            "costing_options": {"bicycle": BICYCLE_COSTING},
            "directions_type": "none",
            "alternates": 1,
        }
        request = Request(
            f"{self.endpoint}/route",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
            trips = [result["trip"]] + [
                alternate["trip"] for alternate in result.get("alternates", [])
            ]
            features = [self._feature(trip) for trip in trips]
        except (URLError, TimeoutError, ValueError, KeyError, IndexError) as error:
            raise ValhallaUnavailable(f"Valhalla route request failed: {error}") from error
        return {"type": "FeatureCollection", "features": features}

    def _feature(self, trip: dict[str, Any]) -> dict[str, Any]:
        coordinates = [
            point for leg in trip["legs"] for point in _decode_polyline(leg["shape"])
        ]
        return {
            "type": "Feature",
            "properties": {
                "backend": self.backend_name,
                "node_count": len(coordinates),
                "route_length_m": round(trip["summary"]["length"] * 1000, 1),
                "time_minutes": round(trip["summary"]["time"] / 60, 1),
            },
            "geometry": {"type": "LineString", "coordinates": coordinates},
        }
