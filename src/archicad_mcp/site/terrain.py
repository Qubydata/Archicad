"""Archicad Mesh elements from ground heights.

An Archicad Mesh is an outline polygon with a height at each corner, plus
level lines (sublines) inside it, each point with its own height; Archicad
triangulates the surface through all of them. A height grid becomes the
outline plus one level line per inner grid row. Survey points become level
points (very short level lines) and contour lines become level lines as they are.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MAX_SIDE = 121  # grid points per side: 14,641 heights at most
MAX_SURVEY_POINTS = 20000


@dataclass
class Grid:
    xs: np.ndarray  # model x of the columns
    ys: np.ndarray  # model y of the rows
    z: np.ndarray  # z[j, i] is the height at (xs[i], ys[j])

    def height_at(self, x, y):
        """Bilinear height inside the grid (clamped at its edges)."""
        x = np.clip(np.asarray(x, dtype=float), self.xs[0], self.xs[-1])
        y = np.clip(np.asarray(y, dtype=float), self.ys[0], self.ys[-1])
        i = np.clip(np.searchsorted(self.xs, x) - 1, 0, len(self.xs) - 2)
        j = np.clip(np.searchsorted(self.ys, y) - 1, 0, len(self.ys) - 2)
        fx = (x - self.xs[i]) / (self.xs[i + 1] - self.xs[i])
        fy = (y - self.ys[j]) / (self.ys[j + 1] - self.ys[j])
        z = self.z
        return (z[j, i] * (1 - fx) + z[j, i + 1] * fx) * (1 - fy) + (z[j + 1, i] * (1 - fx) + z[j + 1, i + 1] * fx) * fy


def grid_axis(half_size: float, spacing: float) -> np.ndarray:
    """Evenly spaced coordinates from -half_size to +half_size, at most MAX_SIDE of them."""
    count = int(round(2 * half_size / max(spacing, 0.1))) + 1
    return np.linspace(-half_size, half_size, max(3, min(count, MAX_SIDE)))


def _point(x, y, z) -> dict:
    return {"x": round(float(x), 4), "y": round(float(y), 4), "z": round(float(z), 4)}


def grid_mesh(grid: Grid, floor_index: int = 0) -> dict:
    """CreateMeshes data for a height grid: the outline plus a level line for each inner row."""
    xs, ys, z = grid.xs, grid.ys, grid.z
    n, m = len(xs), len(ys)
    ring = ([(i, 0) for i in range(n)] + [(n - 1, j) for j in range(1, m)]
            + [(i, m - 1) for i in range(n - 2, -1, -1)] + [(0, j) for j in range(m - 2, 0, -1)])
    outline = [_point(xs[i], ys[j], z[j, i]) for i, j in ring]  # counterclockwise
    sublines = [{"coordinates": [_point(xs[i], ys[j], z[j, i]) for i in range(1, n - 1)]} for j in range(1, m - 1)]
    data = {"floorIndex": floor_index, "level": 0.0, "polygonCoordinates": outline, "ridges": "AllSmooth",
            "skirtType": "WithSkirt", "skirtLevel": 1.0, "showLines": False}
    if sublines:
        data["sublines"] = sublines
    return data


def convex_hull(points: np.ndarray) -> list[int]:
    """Indices of the convex hull of 2D points, counterclockwise (monotone chain)."""
    order = sorted(range(len(points)), key=lambda k: (points[k][0], points[k][1]))

    def cross(o, a, b):
        return (points[a][0] - points[o][0]) * (points[b][1] - points[o][1]) - \
               (points[a][1] - points[o][1]) * (points[b][0] - points[o][0])

    lower: list[int] = []
    for k in order:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], k) <= 0:
            lower.pop()
        lower.append(k)
    upper: list[int] = []
    for k in reversed(order):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], k) <= 0:
            upper.pop()
        upper.append(k)
    return lower[:-1] + upper[:-1]


def thin_line(line: np.ndarray, min_step: float) -> np.ndarray:
    """Drop points of a polyline closer than min_step to the last kept one (keeps both ends)."""
    if len(line) <= 2:
        return line
    keep = [0]
    for k in range(1, len(line) - 1):
        if np.hypot(*(line[k, :2] - line[keep[-1], :2])) >= min_step:
            keep.append(k)
    keep.append(len(line) - 1)
    return line[keep]


def survey_mesh(points: np.ndarray, lines: list[np.ndarray], floor_index: int = 0,
                min_step: float = 0.25) -> tuple[dict, list[str]]:
    """CreateMeshes data from survey points (N x 3) and level lines (each M x 3), and notes."""
    notes: list[str] = []
    lines = [thin_line(np.asarray(line, dtype=float), min_step) for line in lines if len(line) >= 2]
    everything = np.vstack([points.reshape(-1, 3)] + [line for line in lines]) if lines or len(points) else np.zeros((0, 3))
    if len(everything) < 3:
        raise ValueError("The survey needs at least three points with heights.")
    hull = convex_hull(everything[:, :2])
    if len(hull) < 3:
        raise ValueError("The survey points lie on one line, so they do not describe a ground surface.")
    outline = [_point(*everything[k]) for k in hull]
    on_hull = {tuple(np.round(everything[k, :2], 4)) for k in hull}
    sublines = [{"coordinates": [_point(*p) for p in line]} for line in lines]
    loose = [p for p in points.reshape(-1, 3) if tuple(np.round(p[:2], 4)) not in on_hull]
    if len(loose) > MAX_SURVEY_POINTS:
        step = int(np.ceil(len(loose) / MAX_SURVEY_POINTS))
        notes.append(f"The survey has {len(loose)} points; every {step}th point was used to keep Archicad responsive.")
        loose = loose[::step]
    for x, y, z in loose:  # a level point is a level line 1 cm long
        sublines.append({"coordinates": [_point(x, y, z), _point(x + 0.01, y, z)]})
    data = {"floorIndex": floor_index, "level": 0.0, "polygonCoordinates": outline, "ridges": "AllSmooth",
            "skirtType": "WithSkirt", "skirtLevel": 1.0, "showLines": False}
    if sublines:
        data["sublines"] = sublines
    return data, notes


def extrusion_body(outline: list[tuple[float, float]], height: float) -> dict | None:
    """A Morph body for a building footprint raised to a height (vertices relative to the base point)."""
    pts = [tuple(map(float, p)) for p in outline]
    if len(pts) > 1 and np.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-6:
        pts = pts[:-1]
    cleaned: list[tuple[float, float]] = []
    for p in pts:
        if not cleaned or np.hypot(p[0] - cleaned[-1][0], p[1] - cleaned[-1][1]) > 0.05:
            cleaned.append(p)
    if len(cleaned) >= 2 and np.hypot(cleaned[0][0] - cleaned[-1][0], cleaned[0][1] - cleaned[-1][1]) <= 0.05:
        cleaned.pop()
    if len(cleaned) < 3:
        return None
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(cleaned, cleaned[1:] + cleaned[:1])) / 2
    if abs(area) < 1.0:
        return None
    if area < 0:
        cleaned.reverse()
    n = len(cleaned)
    vertices = [{"x": x, "y": y, "z": 0.0} for x, y in cleaned] + [{"x": x, "y": y, "z": float(height)} for x, y in cleaned]
    polygons = [{"vertexIds": list(range(n, 2 * n))},  # roof, counterclockwise from above
                {"vertexIds": list(range(n - 1, -1, -1))}]  # floor, counterclockwise from below
    polygons += [{"vertexIds": [i, (i + 1) % n, n + (i + 1) % n, n + i]} for i in range(n)]
    return {"bodyType": "Solid", "vertices": vertices, "polygons": polygons}
