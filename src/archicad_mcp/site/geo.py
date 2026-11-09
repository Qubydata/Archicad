"""From latitude and longitude to model coordinates, and the web map tile grid.

Archicad's project location gives the latitude and longitude of the project
origin and the direction of north in the model (radians from the x axis;
pi/2, the default, means north is +y). Around a site (a few kilometres) a flat
local frame is accurate to centimetres, so no map projection library is needed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

TILE = 256  # pixels per web map tile


@dataclass(frozen=True)
class Frame:
    latitude: float
    longitude: float
    north: float = math.pi / 2  # direction of north in the model, radians from +x

    @property
    def metres_per_degree(self) -> tuple[float, float]:
        """(metres per degree of latitude, metres per degree of longitude) here, on the WGS84 ellipsoid."""
        phi = math.radians(self.latitude)
        per_lat = 111132.92 - 559.82 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi) - 0.0023 * math.cos(6 * phi)
        per_lon = 111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi) + 0.118 * math.cos(5 * phi)
        return per_lat, per_lon

    @property
    def axes(self) -> tuple[tuple[float, float], tuple[float, float]]:
        """The model directions of east and north."""
        north = (math.cos(self.north), math.sin(self.north))
        east = (math.sin(self.north), -math.cos(self.north))  # north turned 90 degrees clockwise
        return east, north

    def to_model(self, latitude, longitude):
        """Model x, y (metres) of geographic points; works on numbers and numpy arrays."""
        per_lat, per_lon = self.metres_per_degree
        e_m = (np.asarray(longitude, dtype=float) - self.longitude) * per_lon
        n_m = (np.asarray(latitude, dtype=float) - self.latitude) * per_lat
        (ex, ey), (nx, ny) = self.axes
        return e_m * ex + n_m * nx, e_m * ey + n_m * ny

    def to_geo(self, x, y):
        """Latitude and longitude of model points; the inverse of to_model."""
        per_lat, per_lon = self.metres_per_degree
        x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
        (ex, ey), (nx, ny) = self.axes
        e_m, n_m = x * ex + y * ey, x * nx + y * ny
        return self.latitude + n_m / per_lat, self.longitude + e_m / per_lon


def global_pixel(latitude, longitude, zoom: int):
    """Position on the whole-world web mercator image at a zoom level, in pixels (floats)."""
    size = TILE * 2 ** zoom
    lat = np.radians(np.clip(np.asarray(latitude, dtype=float), -85.05112878, 85.05112878))
    x = (np.asarray(longitude, dtype=float) + 180.0) / 360.0 * size
    y = (1.0 - np.log(np.tan(lat) + 1.0 / np.cos(lat)) / math.pi) / 2.0 * size
    return x, y


def ground_resolution(latitude: float, zoom: int) -> float:
    """Metres covered by one map pixel at this latitude and zoom."""
    return 156543.03392 * math.cos(math.radians(latitude)) / 2 ** zoom
