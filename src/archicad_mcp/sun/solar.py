"""Sun position and clear-sky daylight.

Sun position: the NOAA solar position algorithm (accurate to a fraction of a
degree, which is plenty for shading studies).

Clear-sky light: the ASHRAE clear-sky model, which gives direct normal and
diffuse horizontal irradiance from the sun's altitude and the month, turned
into illuminance with standard luminous efficacies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import numpy as np

# ASHRAE clear-sky coefficients per month: A (W/m2), B (optical depth), C (diffuse factor).
ASHRAE = {
    1: (1230, 0.142, 0.058), 2: (1215, 0.144, 0.060), 3: (1186, 0.156, 0.071), 4: (1136, 0.180, 0.097),
    5: (1104, 0.196, 0.121), 6: (1088, 0.205, 0.134), 7: (1085, 0.207, 0.136), 8: (1107, 0.201, 0.122),
    9: (1151, 0.177, 0.092), 10: (1192, 0.160, 0.073), 11: (1221, 0.149, 0.063), 12: (1233, 0.142, 0.057),
}
EFFICACY_DIRECT = 93.0  # lm/W for clear-sky beam light
EFFICACY_DIFFUSE = 125.0  # lm/W for clear-sky diffuse light


@dataclass(frozen=True)
class Location:
    latitude: float  # degrees, north positive
    longitude: float  # degrees, east positive
    north: float = math.pi / 2  # Archicad project north: radians from the model's +X axis, counterclockwise
    utc_offset: float | None = None  # hours; None: estimated from the longitude

    @property
    def offset(self) -> float:
        return self.utc_offset if self.utc_offset is not None else round(self.longitude / 15.0)


@dataclass(frozen=True)
class SunSample:
    time: datetime  # local clock time
    altitude: float  # radians above the horizon
    azimuth: float  # radians clockwise from north
    direction: tuple[float, float, float]  # unit vector toward the sun, in model coordinates
    direct_normal: float  # W/m2
    diffuse_horizontal: float  # W/m2


def sun_position(location: Location, when: datetime) -> tuple[float, float]:
    """(altitude, azimuth) in radians for a local clock time; azimuth is clockwise from north."""
    utc = when - timedelta(hours=location.offset)
    day_of_year = utc.timetuple().tm_yday
    hour = utc.hour + utc.minute / 60 + utc.second / 3600
    gamma = 2 * math.pi / 365 * (day_of_year - 1 + (hour - 12) / 24)
    equation_of_time = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                                 - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    declination = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
                   - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
                   - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    true_solar_minutes = hour * 60 + equation_of_time + 4 * location.longitude
    hour_angle = math.radians(true_solar_minutes / 4 - 180)
    lat = math.radians(location.latitude)
    cos_zenith = math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(declination) * math.cos(hour_angle)
    zenith = math.acos(max(-1.0, min(1.0, cos_zenith)))
    altitude = math.pi / 2 - zenith
    azimuth = math.atan2(math.sin(hour_angle), math.cos(hour_angle) * math.sin(lat) - math.tan(declination) * math.cos(lat))
    return altitude, (azimuth + math.pi) % (2 * math.pi)


def direction_in_model(location: Location, altitude: float, azimuth: float) -> tuple[float, float, float]:
    """Unit vector toward the sun in model coordinates, using the project north."""
    north = np.array([math.cos(location.north), math.sin(location.north)])
    east = np.array([math.sin(location.north), -math.cos(location.north)])
    horizontal = math.cos(altitude) * (math.sin(azimuth) * east + math.cos(azimuth) * north)
    return (float(horizontal[0]), float(horizontal[1]), math.sin(altitude))


def clear_sky(month: int, altitude: float) -> tuple[float, float]:
    """(direct normal, diffuse horizontal) irradiance in W/m2 for a clear day."""
    if altitude <= 0:
        return 0.0, 0.0
    a, b, c = ASHRAE[month]
    direct_normal = a * math.exp(-b / max(math.sin(altitude), 0.05))
    return direct_normal, c * direct_normal


def day_samples(location: Location, day: date, step_minutes: int = 15) -> list[SunSample]:
    """Daytime samples of one day, at regular clock times."""
    samples = []
    start = datetime(day.year, day.month, day.day)
    for minute in range(0, 24 * 60, step_minutes):
        when = start + timedelta(minutes=minute)
        altitude, azimuth = sun_position(location, when)
        if altitude <= math.radians(0.5):
            continue
        dni, dhi = clear_sky(day.month, altitude)
        samples.append(SunSample(when, altitude, azimuth, direction_in_model(location, altitude, azimuth), dni, dhi))
    return samples
