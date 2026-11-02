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
    glass: list[dict[str, Any]] = field(default_factory=list)  # glazing and openings: area, centroid, normal, transmission
    notes: list[str] = field(default_factory=list)  # assumptions worth telling the user

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


OPENING_KINDS = frozenset({"Window", "Door", "Skylight", "CurtainWall"})
HOST_KINDS = frozenset({"Wall", "CurtainWall", "Slab", "Roof", "Shell"})
PANE_MAX_THICKNESS = 0.03  # glass units are a few millimetres to about 28 mm; door leaves are thicker
PANE_MIN_AREA = 0.05
FILLER_REACH = 0.35  # how far an opening's face may be from the window or door that fills it


def _normal_and_area(points: np.ndarray) -> tuple[np.ndarray, float]:
    normal = np.zeros(3)
    for a, b in zip(points, np.roll(points, -1, axis=0)):
        normal += np.cross(a, b)
    length = np.linalg.norm(normal)
    return (normal / length if length else normal), length / 2


def _pane_entry(tris: np.ndarray, taus: np.ndarray, body: int) -> dict[str, Any]:
    clear = tris[taus > 0]
    normals = np.cross(clear[:, 1] - clear[:, 0], clear[:, 2] - clear[:, 0])
    # A pane is a thin solid whose front and back cancel out: use its biggest face instead.
    # Which side is outside is decided later, from which side sees more sky.
    biggest = normals[np.argmax(np.linalg.norm(normals, axis=1))]
    return {
        "area": float(np.linalg.norm(normals, axis=1).sum() / 4),  # half the faces, half the cross product
        "centroid": clear.mean(axis=(0, 1)).tolist(),
        "normal": (biggest / (np.linalg.norm(biggest) or 1.0)).tolist(),
        "transmission": float(np.mean(taus[taus > 0])),
        "body": body,
        "open": False,
    }


def build_scene(geometries: list[dict[str, Any]], surface_transmissions: dict[str, float] | None = None,
                glass_default: float = DEFAULT_GLASS_TRANSMISSION) -> Scene:
    """Build the light-blocking scene.

    geometries: one entry per element, {"type": element type, "bodies": [Body3D...]} as Tapir's
    Get3DGeometryOfElements returns them. surface_transmissions maps surface attribute GUIDs to
    their transmission (see surface_transmission); faces with other known surfaces are opaque.

    Window glass often has a material defined inside the window object, which Archicad reports
    without a surface: in windows, doors, skylights and curtain walls, a thin flat body of unknown
    material is taken as a glass pane. The see-through bodies, and the openings in walls, slabs and
    roofs that hold no glass and no door, are listed as glazing for the light entering the rooms.
    """
    surface_transmissions = surface_transmissions or {}
    triangles: list[np.ndarray] = []
    bodies: list[int] = []
    transmissions: list[float] = []
    glass: list[dict[str, Any]] = []
    openings: list[dict[str, Any]] = []
    fillers: list[dict[str, Any]] = []  # windows and doors: their extent, and whether glass was found in them
    body_index = 0
    for element in geometries:
        kind = element.get("type")
        element_points: list[np.ndarray] = []
        element_glass = False
        for body in element.get("bodies", []):
            vertices = np.array([[v["x"], v["y"], v["z"]] for v in body.get("vertices", [])], dtype=np.float64)
            if len(vertices) < 3:
                continue
            element_points.append(vertices)
            body_triangles: list[np.ndarray] = []
            body_transmissions: list[float] = []
            known = False
            for polygon in body.get("polygons", []):
                rings = [polygon["vertexIds"]] + [hole["vertexIds"] for hole in polygon.get("holes", [])]
                surface = (polygon.get("surfaceId") or {}).get("guid")
                known = known or surface in surface_transmissions
                tau = surface_transmissions.get(surface, 0.0) if surface else 0.0
                if kind in HOST_KINDS:
                    for hole in polygon.get("holes", []):
                        normal, area = _normal_and_area(vertices[hole["vertexIds"]])
                        if area > 0.05:
                            openings.append({"centroid": vertices[hole["vertexIds"]].mean(axis=0), "normal": normal, "area": area})
                for a, b, c in _triangulate(vertices, rings):
                    body_triangles.append(vertices[[a, b, c]])
                    body_transmissions.append(tau)
            if not body_triangles:
                continue
            tris = np.array(body_triangles)
            taus = np.array(body_transmissions)
            if kind in OPENING_KINDS and not known and not np.any(taus > 0):
                normal, _ = _normal_and_area(tris[np.argmax(np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1))])
                thickness = np.ptp(vertices @ normal) if np.any(normal) else 1.0
                face_area = np.linalg.norm(np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]), axis=1).sum() / 4
                if thickness <= PANE_MAX_THICKNESS and face_area >= PANE_MIN_AREA:
                    taus = np.full(len(tris), glass_default)
            triangles.append(tris)
            bodies.extend([body_index] * len(tris))
            transmissions.extend(taus.tolist())
            if np.any(taus > 0):
                glass.append(_pane_entry(tris, taus, body_index))
                element_glass = True
            body_index += 1
        if kind in OPENING_KINDS - {"CurtainWall"} and element_points:
            every = np.concatenate(element_points)
            # The wall faces holding the opening sit up to a wall thickness in front of or behind the window.
            fillers.append({"lo": every.min(axis=0) - FILLER_REACH, "hi": every.max(axis=0) + FILLER_REACH,
                            "kind": kind, "glass": element_glass})

    # Openings: the two faces of a wall report the same hole; keep one, and only those not holding glass or a door.
    notes: list[str] = []
    assumed = 0
    kept: list[dict[str, Any]] = []
    for opening in openings:
        if any(np.linalg.norm(opening["centroid"] - k["centroid"]) < 0.7 and abs(opening["normal"] @ k["normal"]) > 0.95
               for k in kept):
            continue
        kept.append(opening)
        filler = next((f for f in fillers if np.all(opening["centroid"] >= f["lo"]) and np.all(opening["centroid"] <= f["hi"])), None)
        if filler is None:
            tau = 1.0
        elif filler["glass"] or filler["kind"] == "Door":
            continue  # its glass is counted already, or it is a closed door
        else:
            tau = glass_default  # a window without recognizable glass: assume standard glazing
            assumed += 1
        glass.append({"area": float(opening["area"]), "centroid": opening["centroid"].tolist(),
                      "normal": opening["normal"].tolist(), "transmission": tau, "body": -1, "open": filler is None})
    if assumed:
        notes.append(f"{assumed} window(s) had no recognizable glass: standard glazing was assumed for the light "
                     "they let in, so direct light through them may be underestimated.")
    if not triangles:
        return Scene(np.zeros((0, 3, 3)), np.zeros(0, dtype=np.int64), np.zeros(0), glass, notes)
    return Scene(np.concatenate(triangles), np.array(bodies, dtype=np.int64), np.array(transmissions), glass, notes)
