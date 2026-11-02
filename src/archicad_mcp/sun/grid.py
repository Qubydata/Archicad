"""Measurement points on a regular grid inside room outlines (with holes)."""

from __future__ import annotations

import numpy as np


def _inside(points: np.ndarray, ring: np.ndarray) -> np.ndarray:
    """Even-odd point-in-polygon test for many points against one closed ring."""
    x, y = points[:, 0][:, None], points[:, 1][:, None]
    x1, y1 = ring[:, 0][None, :], ring[:, 1][None, :]
    x2, y2 = np.roll(ring[:, 0], -1)[None, :], np.roll(ring[:, 1], -1)[None, :]
    crosses = (y1 > y) != (y2 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        x_at = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
    return (crosses & (x < x_at)).sum(axis=1) % 2 == 1


def grid_in_polygon(outline: list[tuple[float, float]], holes: list[list[tuple[float, float]]] | None,
                    spacing: float, margin: float = 0.0) -> np.ndarray:
    """Cell centers (M, 2) of a square grid inside the outline and outside the holes.

    The grid is aligned to the outline's bounding box and centred in it, so a room gets a
    symmetric grid. margin keeps cell centers at least that far inside the outline's bounding box.
    """
    ring = np.asarray(outline, dtype=np.float64)
    if len(ring) < 3 or spacing <= 0:
        return np.zeros((0, 2))
    lo, hi = ring.min(axis=0) + margin, ring.max(axis=0) - margin
    counts = np.maximum(np.floor((hi - lo) / spacing).astype(int), 1)
    start = (lo + hi) / 2 - (counts - 1) * spacing / 2
    xs = start[0] + spacing * np.arange(counts[0])
    ys = start[1] + spacing * np.arange(counts[1])
    points = np.array(np.meshgrid(xs, ys)).reshape(2, -1).T
    keep = _inside(points, ring)
    for hole in holes or []:
        if len(hole) >= 3:
            keep &= ~_inside(points, np.asarray(hole, dtype=np.float64))
    return points[keep]


def polygon_area(outline: list[tuple[float, float]], holes: list[list[tuple[float, float]]] | None = None) -> float:
    def shoelace(ring):
        ring = np.asarray(ring, dtype=np.float64)
        return abs(np.dot(ring[:, 0], np.roll(ring[:, 1], -1)) - np.dot(ring[:, 1], np.roll(ring[:, 0], -1))) / 2

    return shoelace(outline) - sum(shoelace(hole) for hole in holes or [] if len(hole) >= 3)


def polygon_perimeter(outline: list[tuple[float, float]]) -> float:
    ring = np.asarray(outline, dtype=np.float64)
    return float(np.linalg.norm(np.roll(ring, -1, axis=0) - ring, axis=1).sum())
