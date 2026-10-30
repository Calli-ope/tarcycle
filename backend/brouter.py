from __future__ import annotations

import json
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen


class BRouterUnavailable(RuntimeError):
    """The configured BRouter server could not route the request."""


class BRouterRouter:
    """BRouter HTTP server; profiles such as "trekking" or
    "fastbike-verylowtraffic" live in the server's profiles2 folder."""

    backend_name = "brouter"

    def __init__(self, endpoint: str, profile: str = "trekking", timeout: float = 15.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.profile = profile
        self.timeout = timeout

    def _feature(self, lonlats: str, alternative: int) -> dict[str, Any]:
        params = urlencode(
            {
                "lonlats": lonlats,
                "profile": self.profile,
                "alternativeidx": alternative,
                "format": "geojson",
            }
        )
        with urlopen(f"{self.endpoint}/brouter?{params}", timeout=self.timeout) as response:
            track = json.load(response)["features"][0]
        properties = track["properties"]
        # Coordinates are [lon, lat, elevation].
        coordinates = [[x, y] for x, y, *_ in track["geometry"]["coordinates"]]
        return {
            "type": "Feature",
            "properties": {
                "backend": self.backend_name,
                "profile": self.profile,
                "node_count": len(coordinates),
                "route_length_m": float(properties["track-length"]),
                "time_minutes": round(float(properties["total-time"]) / 60, 1),
            },
            "geometry": {"type": "LineString", "coordinates": coordinates},
        }

    def route_geojson(
        self,
        start_lat: float,
        start_lon: float,
        end_lat: float,
        end_lon: float,
    ) -> dict[str, Any]:
        lonlats = f"{start_lon},{start_lat}|{end_lon},{end_lat}"
        errors = (URLError, TimeoutError, ValueError, KeyError, IndexError)
        try:
            features = [self._feature(lonlats, 0)]
        except errors as error:
            raise BRouterUnavailable(f"BRouter route request failed: {error}") from error
        try:
            features.append(self._feature(lonlats, 1))
        except errors:
            pass  # no alternative for this trip
        return {"type": "FeatureCollection", "features": features}
