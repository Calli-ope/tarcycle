from dataclasses import dataclass


@dataclass(frozen=True)
class RoutingPreferences:
    dedicated_cycleway_factor: float = 0.05
    cycleway_factor: float = 0.2
    path_factor: float = 0.25
    riverside_path_factor: float = 0.2
    riverside_road_factor: float = 6.0
    cobblestone_factor: float = 2.5
    major_road_factor: float = 3.5
    facility_transition_cost: float = 35.0
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
