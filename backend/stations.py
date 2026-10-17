import csv
from pathlib import Path

from pyproj import Transformer

from .models import Station


_TO_WGS84 = Transformer.from_crs("EPSG:3301", "EPSG:4326", always_xy=True)


def _coordinate(value: str) -> float:
    return float(value.strip().replace(",", "."))


def _capacity(value: str) -> int:
    return int(value) if value.strip() else 0


def load_stations(path: Path) -> list[Station]:
    """Load bike rack locations and convert Estonian grid coordinates."""

    stations: list[Station] = []
    with path.open(encoding="utf-8-sig", newline="") as file:
        for row in csv.DictReader(file):
            x = _coordinate(row["x"].strip('"'))
            y = _coordinate(row["y"].strip('"'))
            longitude, latitude = _TO_WGS84.transform(x, y)
            stations.append(
                Station(
                    id=int(row["OBJECTID"]),
                    name=row["Asukoht"].strip(),
                    capacity=_capacity(row["Rataste_arv"]),
                    longitude=longitude,
                    latitude=latitude,
                )
            )
    if not stations:
        raise ValueError(f"No stations found in {path}.")
    return stations
