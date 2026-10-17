from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from shapely.geometry import LineString


class TerrainModel:
    """Read one or more Maa- ja Ruumiamet DTM GeoTIFF tiles."""

    def __init__(self, directory: Path) -> None:
        self.files = sorted(directory.glob("*_dtm_5m.tif"))
        self.datasets = [rasterio.open(path) for path in self.files]
        self.transformers = {
            dataset.crs: Transformer.from_crs(
                "EPSG:4326", dataset.crs, always_xy=True
            )
            for dataset in self.datasets
        }

    @property
    def available(self) -> bool:
        return bool(self.datasets)

    def elevation(self, longitude: float, latitude: float) -> float | None:
        for dataset in self.datasets:
            transformer = self.transformers[dataset.crs]
            x, y = transformer.transform(longitude, latitude)
            if not (
                dataset.bounds.left <= x <= dataset.bounds.right
                and dataset.bounds.bottom <= y <= dataset.bounds.top
            ):
                continue
            value = next(dataset.sample([(x, y)]))[0]
            if dataset.nodata is None or not np.isclose(value, dataset.nodata):
                return float(value)
        return None

    def edge_attributes(
        self,
        coordinates: list[tuple[float, float]],
        length_meters: float,
    ) -> dict[str, float]:
        if not self.available or length_meters <= 0:
            return {"elevation_gain": 0.0, "average_uphill_grade": 0.0}

        elevations = [
            self.elevation(longitude, latitude)
            for longitude, latitude in coordinates
        ]
        values = [value for value in elevations if value is not None]
        if len(values) < 2:
            return {
                "elevation_gain": 0.0,
                "average_uphill_grade": 0.0,
                "terrain_covered": 0.0,
            }

        gain = sum(
            max(0.0, current - previous)
            for previous, current in zip(values, values[1:])
        )
        return {
            "elevation_gain": gain,
            "average_uphill_grade": gain / length_meters,
            "terrain_covered": 1.0,
        }

    def close(self) -> None:
        for dataset in self.datasets:
            dataset.close()
