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
