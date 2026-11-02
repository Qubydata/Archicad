"""The sky as 145 patches (the Tregenza subdivision) with the CIE standard overcast distribution.

Each patch has a direction and a weight, its share of the illuminance an
unobstructed horizontal surface receives from the whole sky. The sky component
at a point is then the sum of the weights of the patches it sees, times their
transmission (glass), which is the daylight-factor definition of the sky
component. The same factor scales the clear-sky diffuse light during the day.
"""

from __future__ import annotations

import math

import numpy as np

# Tregenza bands: (lower altitude, upper altitude, patch count), 12 degrees each plus the zenith cap.
BANDS = [(0, 12, 30), (12, 24, 30), (24, 36, 24), (36, 48, 24), (48, 60, 18), (60, 72, 12), (72, 84, 6), (84, 90, 1)]
VERTICAL_UNOBSTRUCTED = 0.396  # vertical / horizontal illuminance under a CIE overcast sky


def patches() -> tuple[np.ndarray, np.ndarray]:
    """(directions (145, 3) as unit vectors, solid angles (145,)), z up, azimuths from +X."""
    directions, solid_angles = [], []
    for low, high, count in BANDS:
        lo, hi = math.radians(low), math.radians(high)
        altitude = math.radians(90) if count == 1 else (lo + hi) / 2
        omega = 2 * math.pi * (math.sin(hi) - math.sin(lo)) / count
        for k in range(count):
            azimuth = 2 * math.pi * (k + 0.5) / count
            directions.append((math.cos(altitude) * math.cos(azimuth), math.cos(altitude) * math.sin(azimuth), math.sin(altitude)))
            solid_angles.append(omega)
    return np.array(directions), np.array(solid_angles)


def overcast_weights(directions: np.ndarray, solid_angles: np.ndarray) -> np.ndarray:
    """Each patch's share of the unobstructed horizontal illuminance under a CIE overcast sky."""
    sin_altitude = directions[:, 2]
    luminance = (1 + 2 * sin_altitude) / 3  # CIE standard overcast sky, relative to the zenith
    contribution = luminance * sin_altitude * solid_angles
    return contribution / contribution.sum()


def plane_weights(directions: np.ndarray, solid_angles: np.ndarray, normal: np.ndarray) -> np.ndarray:
    """Each patch's share of the unobstructed horizontal illuminance, as received by a plane facing `normal`.

    For a vertical plane the weights add up to about 0.396, the CIE overcast ratio.
    """
    sin_altitude = directions[:, 2]
    luminance = (1 + 2 * sin_altitude) / 3
    horizontal = (luminance * sin_altitude * solid_angles).sum()
    cos_incidence = np.clip(directions @ (normal / np.linalg.norm(normal)), 0, None)
    return luminance * cos_incidence * solid_angles / horizontal
