"""The colour scale of the analysis tiles: dark blue (little light) through green and yellow to red."""

from __future__ import annotations

import numpy as np

STOPS = np.array([
    (4, 25, 145), (0, 105, 230), (0, 190, 240), (60, 200, 120), (250, 225, 0), (255, 140, 0), (225, 30, 30),
], dtype=np.float64)

# Default legends per metric: (low, high, unit, label).
SCALES = {
    "illuminance": (0.0, 2000.0, "lux", "Daylight"),
    "sun_hours": (0.0, 8.0, "h", "Direct sun"),
    "daylight_factor": (0.0, 5.0, "%", "Daylight factor"),
}


def colorize(values: np.ndarray, low: float, high: float) -> np.ndarray:
    """(N, 4) RGBA colours, 0-255, for values on the low..high scale (clamped at both ends)."""
    t = np.clip((np.asarray(values, dtype=np.float64) - low) / max(high - low, 1e-9), 0.0, 1.0)
    position = t * (len(STOPS) - 1)
    index = np.minimum(position.astype(int), len(STOPS) - 2)
    fraction = (position - index)[:, None]
    rgb = STOPS[index] * (1 - fraction) + STOPS[index + 1] * fraction
    return np.column_stack([np.round(rgb), np.full(len(t), 255)]).astype(int)


def legend(low: float, high: float, steps: int = 7) -> list[dict]:
    values = np.linspace(low, high, steps)
    return [{"value": float(v), "color": c[:3].tolist()} for v, c in zip(values, colorize(values, low, high))]
