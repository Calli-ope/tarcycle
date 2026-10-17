from dataclasses import dataclass


@dataclass(frozen=True)
class RoutingPreferences:
    cycleway_factor: float = 0.45
    path_factor: float = 0.7
    riverside_path_factor: float = 0.2
    riverside_road_factor: float = 6.0
    cobblestone_factor: float = 2.5
    major_road_factor: float = 2.0
    uphill_factor: float = 6.0


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
