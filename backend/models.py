from dataclasses import dataclass


@dataclass(frozen=True)
class RoutingPreferences:
    # Kept within one order of magnitude of a plain road (1.0): much smaller
    # factors make the router zigzag after every short cycleway fragment.
    dedicated_cycleway_factor: float = 0.5
    cycleway_factor: float = 0.7
    path_factor: float = 0.6
    # Footways beside a road without bicycle=yes/designated; use float("inf")
    # to make them unroutable. Other footways count as paths.
    sidewalk_factor: float = 1.5
    cobblestone_factor: float = 2.5
    major_road_factor: float = 3.5
    facility_transition_cost: float = 20.0
    maximum_detour_factor: float = 1.50
    uphill_factor: float = 1.0
    # Smart Bike GPS usage (data/gps_coverage/gps_ways.geojson): ways ridden by
    # popularity_saturation_trips or more trips in 2025 cost popularity_weight
    # less, scaled logarithmically down to none at 10 trips. 0 disables it.
    popularity_weight: float = 0.3
    popularity_saturation_trips: float = 5000.0


@dataclass(frozen=True)
class Station:
    id: int
    name: str
    capacity: int
    longitude: float
    latitude: float

    def as_dict(self) -> dict[str, int | str | float]:
        return {
            "id": self.id,
            "name": self.name,
            "capacity": self.capacity,
            "longitude": self.longitude,
            "latitude": self.latitude,
        }
