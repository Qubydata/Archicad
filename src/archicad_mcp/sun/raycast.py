"""How much light reaches each point from each direction, through the scene.

For one direction (toward the sun, or toward a patch of sky) every point and
triangle is projected onto a plane facing that direction. A triangle shades a
point when the point's projection falls inside the triangle's projection and
the triangle lies on the light's side of the point. A uniform grid on that
plane limits the tests to the triangles near each point, so a few thousand
points against tens of thousands of triangles take a fraction of a second per
direction.

Opaque triangles block the light. Transparent ones let their transmission
through, counted once per glass body (a pane is a thin solid: the ray crosses
its front and back face, which is one pane, not two).
"""

from __future__ import annotations

import numpy as np

from .geometry import Scene

EPSILON = 1e-6
MAX_PAIRS = 4_000_000  # candidate point-triangle pairs handled at once


def _basis(direction: np.ndarray) -> np.ndarray:
    d = direction / np.linalg.norm(direction)
    helper = np.array([1.0, 0.0, 0.0]) if abs(d[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = np.cross(d, helper)
    u /= np.linalg.norm(u)
    v = np.cross(d, u)
    return np.stack([u, v, d])  # rows: u, v, depth along the direction


def transmission_along(points: np.ndarray, direction: np.ndarray, scene: Scene, offset: float = 0.0) -> np.ndarray:
    """The share of light arriving at each point from `direction` (1 = unobstructed, 0 = blocked)."""
    count = len(points)
    if count == 0 or scene.size == 0:
        return np.ones(count)
    frame = _basis(np.asarray(direction, dtype=np.float64))
    p = points @ frame.T  # (N, 3): u, v, depth
    p[:, 2] += offset
    t = scene.triangles @ frame.T  # (T, 3, 3)

    # Only triangles at least partly on the light's side of some point can shade anything.
    in_front = t[:, :, 2].max(axis=1) > p[:, 2].min() + EPSILON
    lo = t[:, :, :2].min(axis=1)
    hi = t[:, :, :2].max(axis=1)
    p_lo, p_hi = p[:, :2].min(axis=0), p[:, :2].max(axis=0)
    overlaps = in_front & np.all(hi >= p_lo - EPSILON, axis=1) & np.all(lo <= p_hi + EPSILON, axis=1)
    candidates = np.nonzero(overlaps)[0]
    if len(candidates) == 0:
        return np.ones(count)
    t, lo, hi = t[candidates], lo[candidates], hi[candidates]

    # Grid on the projection plane, sized from the spread of the points.
    extent = np.maximum(p_hi - p_lo, 1e-3)
    cells = int(np.clip(np.sqrt(count) * 2, 8, 512))
    size = float(max(extent.max() / cells, 0.05))
    origin = p_lo
    shape = np.ceil(extent / size).astype(np.int64) + 1

    def cell_of(xy: np.ndarray) -> np.ndarray:
        return np.clip(np.floor((xy - origin) / size).astype(np.int64), 0, shape - 1)

    c_lo, c_hi = cell_of(lo), cell_of(hi)
    spans = (c_hi - c_lo + 1)
    per_triangle = spans[:, 0] * spans[:, 1]
    tri_ids = np.repeat(np.arange(len(t)), per_triangle)
    starts = np.repeat(np.cumsum(per_triangle) - per_triangle, per_triangle)
    local = np.arange(len(tri_ids)) - starts
    width = spans[tri_ids, 0]
    cx = c_lo[tri_ids, 0] + local % width
    cy = c_lo[tri_ids, 1] + local // width
    pair_cells = cx * shape[1] + cy
    order = np.argsort(pair_cells, kind="stable")
    pair_cells, tri_ids = pair_cells[order], tri_ids[order]

    point_cells = cell_of(p[:, :2])
    point_keys = point_cells[:, 0] * shape[1] + point_cells[:, 1]
    first = np.searchsorted(pair_cells, point_keys, side="left")
    last = np.searchsorted(pair_cells, point_keys, side="right")
    per_point = last - first

    log_light = np.zeros(count)
    if per_point.sum() == 0:
        return np.ones(count)
    # Points are handled in groups whose candidate pairs fit in MAX_PAIRS, to bound memory.
    group = (np.cumsum(per_point) - per_point) // MAX_PAIRS
    for g in np.unique(group):
        idx = np.nonzero(group == g)[0]
        n = per_point[idx]
        if not n.sum():
            continue
        pi = np.repeat(idx, n)
        offsets = np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n)
        ti = tri_ids[np.repeat(first[idx], n) + offsets]
        _accumulate(p, t, candidates, scene, pi, ti, log_light)
    return np.exp(log_light)


def _accumulate(p, t, candidates, scene, pi, ti, log_light) -> None:
    """Point-in-triangle and depth test for candidate pairs; add each pair's shading to log_light."""
    a, b, c = t[ti, 0], t[ti, 1], t[ti, 2]
    q = p[pi]
    v0, v1, v2 = b[:, :2] - a[:, :2], c[:, :2] - a[:, :2], q[:, :2] - a[:, :2]
    denominator = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
    valid = np.abs(denominator) > 1e-12  # triangles seen edge-on cast no shadow
    safe = np.where(valid, denominator, 1.0)
    s = (v2[:, 0] * v1[:, 1] - v1[:, 0] * v2[:, 1]) / safe
    r = (v0[:, 0] * v2[:, 1] - v2[:, 0] * v0[:, 1]) / safe
    inside = valid & (s >= -EPSILON) & (r >= -EPSILON) & (s + r <= 1 + EPSILON)
    depth = a[:, 2] + s * (b[:, 2] - a[:, 2]) + r * (c[:, 2] - a[:, 2])
    hits = inside & (depth > q[:, 2] + 1e-4)
    if not np.any(hits):
        return
    pi, global_tri = pi[hits], candidates[ti[hits]]
    tau = scene.transmission[global_tri]
    opaque = tau <= 0
    if np.any(opaque):
        np.subtract.at(log_light, np.unique(pi[opaque]), 1e3)  # fully blocked
    clear = ~opaque
    if np.any(clear):
        # One pane per (point, glass body), however many of its faces the ray crossed.
        keys = np.unique(np.stack([pi[clear], scene.body[global_tri[clear]], np.round(tau[clear] * 1e6).astype(np.int64)], axis=1), axis=0)
        np.add.at(log_light, keys[:, 0], np.log(np.maximum(keys[:, 2] / 1e6, 1e-9)))


def transmission_matrix(points: np.ndarray, directions: np.ndarray, scene: Scene, progress=None) -> np.ndarray:
    """(N, D) share of light reaching each point from each direction."""
    result = np.empty((len(points), len(directions)))
    for k, direction in enumerate(directions):
        result[:, k] = transmission_along(points, direction, scene)
        if progress:
            progress((k + 1) / len(directions))
    return result
