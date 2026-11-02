"""The model as triangles that block or transmit light.

Archicad reports each element's 3D model as solid bodies with planar faces
(and holes). The faces are triangulated, and each triangle keeps the body it
belongs to and the share of light it lets through: 0 for opaque materials,
the surface's transparency for glass and other translucent materials.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import mapbox_earcut
import numpy as np

DEFAULT_GLASS_TRANSMISSION = 0.7  # typical visible transmittance of clear double glazing
GLASS_NAME = re.compile(r"glass|glazing|glazed|window|vitr|verre|glas\b|vidrio|translucent|polycarbonate", re.IGNORECASE)


@dataclass
class Scene:
    triangles: np.ndarray  # (T, 3, 3) world coordinates in meters
    body: np.ndarray  # (T,) index of the body each triangle belongs to
    transmission: np.ndarray  # (T,) 0 = opaque, otherwise the share of light passing through
    glass: list[dict[str, Any]] = field(default_factory=list)  # transparent bodies: area, centroid, normal, transmission

    @property
    def size(self) -> int:
        return len(self.triangles)


def surface_transmission(surface: dict[str, Any], glass_default: float = DEFAULT_GLASS_TRANSMISSION) -> float:
    """The share of light a surface attribute lets through, from its transparency or, failing that, its name."""
    transparency = surface.get("transparency")
    if isinstance(transparency, (int, float)) and transparency > 0:
        return max(0.0, min(1.0, transparency / 100.0))
    if GLASS_NAME.search(str(surface.get("name", ""))):
        return glass_default
    return 0.0


def _triangulate(points: np.ndarray, rings: list[list[int]]) -> list[tuple[int, int, int]]:
    """Triangulate a planar face (outer ring plus holes) given as vertex indices."""
    flat = [index for ring in rings for index in ring]
    coords = points[flat]
    normal = np.zeros(3)
    outer = points[rings[0]]
    for a, b in zip(outer, np.roll(outer, -1, axis=0)):  # Newell's method, robust for concave faces
        normal += np.cross(a, b)
    if not np.any(normal):
        return []
    drop = int(np.argmax(np.abs(normal)))
    keep = [axis for axis in range(3) if axis != drop]
    ends = np.cumsum([len(ring) for ring in rings]).astype(np.uint32)
    if len(rings) == 1 and len(rings[0]) == 3:
        return [tuple(rings[0])]
    try:
        local = mapbox_earcut.triangulate_float64(coords[:, keep].astype(np.float64), ends)
    except Exception:
        return []
    return [(flat[local[i]], flat[local[i + 1]], flat[local[i + 2]]) for i in range(0, len(local), 3)]


def build_scene(geometries: list[dict[str, Any]], surface_transmissions: dict[str, float] | None = None) -> Scene:
    """Build the light-blocking scene.

    geometries: one entry per element, {"type": element type, "bodies": [Body3D...]} as Tapir's
    Get3DGeometryOfElements returns them. surface_transmissions maps surface attribute GUIDs to
    their transmission (see surface_transmission); faces with other or unknown surfaces are opaque.
    Bodies with see-through faces are also listed as glazing, for the light entering the rooms.
    """
    surface_transmissions = surface_transmissions or {}
    triangles: list[np.ndarray] = []
    bodies: list[int] = []
    transmissions: list[float] = []
    glass: list[dict[str, Any]] = []
    body_index = 0
    for element in geometries:
        for body in element.get("bodies", []):
            vertices = np.array([[v["x"], v["y"], v["z"]] for v in body.get("vertices", [])], dtype=np.float64)
            if len(vertices) < 3:
                continue
            body_triangles: list[np.ndarray] = []
            body_transmissions: list[float] = []
            for polygon in body.get("polygons", []):
                rings = [polygon["vertexIds"]] + [hole["vertexIds"] for hole in polygon.get("holes", [])]
                surface = (polygon.get("surfaceId") or {}).get("guid")
                tau = surface_transmissions.get(surface, 0.0) if surface else 0.0
                for a, b, c in _triangulate(vertices, rings):
                    body_triangles.append(vertices[[a, b, c]])
                    body_transmissions.append(tau)
            if not body_triangles:
                continue
            tris = np.array(body_triangles)
            taus = np.array(body_transmissions)
            triangles.append(tris)
            bodies.extend([body_index] * len(tris))
            transmissions.extend(taus.tolist())
            if np.any(taus > 0):
                clear = tris[taus > 0]
                areas = 0.5 * np.linalg.norm(np.cross(clear[:, 1] - clear[:, 0], clear[:, 2] - clear[:, 0]), axis=1)
                normals = np.cross(clear[:, 1] - clear[:, 0], clear[:, 2] - clear[:, 0])
                # A pane is a thin solid whose front and back cancel out: use its biggest face instead.
                # Which side is outside is decided later, from which side sees more sky.
                biggest = normals[np.argmax(np.linalg.norm(normals, axis=1))]
                normal = biggest / (np.linalg.norm(biggest) or 1.0)
                glass.append({
                    "area": float(areas.sum() / 2),  # both faces of the pane were summed
                    "centroid": (clear.mean(axis=(0, 1))).tolist(),
                    "normal": normal.tolist(),
                    "transmission": float(np.mean(taus[taus > 0])),
                    "body": body_index,
                })
            body_index += 1
    if not triangles:
        return Scene(np.zeros((0, 3, 3)), np.zeros(0, dtype=np.int64), np.zeros(0), glass)
    return Scene(np.concatenate(triangles), np.array(bodies, dtype=np.int64), np.array(transmissions), glass)
