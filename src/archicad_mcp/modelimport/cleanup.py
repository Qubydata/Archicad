"""Cleaning up an imported model before it becomes a library part.

An :class:`ImportSession` holds a read model and the user's edits: parts
deleted, the unit, the up axis, a turn about the vertical axis and the
polygon reduction level. Every edit except the level can be undone. The
geometry it hands out is in metres, Z up, with its origin at the bottom
centre of the kept parts, which is where Archicad places the object.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

from .errors import ModelImportError
from .reader import UNITS, UP_AXES, Model, Part
from .simplify import Levels, budget

HISTORY = 100
SMALL_PART_FRACTION = 0.01  # of the model's bounding-box diagonal
UNIT_PRIOR = {"m": 0.0, "cm": 0.15, "mm": 0.15, "inch": 0.4, "ft": 0.6}
UNIT_LABELS = {"mm": "millimetres", "cm": "centimetres", "m": "metres", "inch": "inches", "ft": "feet",
               "file": "the file's unit"}

# Rows: where the file's axes go so that its up axis becomes Archicad's Z (right-handed, so nothing is mirrored).
ROTATIONS = {
    "Z": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    "Y": ((1, 0, 0), (0, 0, -1), (0, 1, 0)),  # glTF and most OBJ: the front (+Z) faces -Y, Archicad's front
    "X": ((0, 1, 0), (0, 0, 1), (1, 0, 0)),
    "-Z": ((1, 0, 0), (0, -1, 0), (0, 0, -1)),
    "-Y": ((1, 0, 0), (0, 0, 1), (0, -1, 0)),
    "-X": ((0, 1, 0), (0, 0, -1), (-1, 0, 0)),
}


@dataclass(frozen=True)
class Edits:
    deleted: frozenset = frozenset()
    unit: str = "m"  # a key of UNITS, or "file" for a unit the file states that is not in UNITS
    up_axis: str = "Z"
    turn: float = 0.0  # degrees about the vertical axis, counterclockwise seen from above


@dataclass
class ExportMesh:
    part_id: str
    name: str
    material: str
    vertices: np.ndarray  # metres, Z up, origin at the bottom centre
    faces: np.ndarray


def guess_unit(model: Model) -> tuple[str, str]:
    """The unit of the model's numbers and where that comes from: ``"file"`` (stated in it) or ``"size"`` (guessed)."""
    if model.unit:
        return model.unit, "file"
    if model.unit_scale:
        return "file", "file"
    vertices = np.concatenate([p.vertices for p in model.parts]) if model.parts else np.zeros((1, 3))
    extent = float(np.ptp(vertices, axis=0).max())
    if extent <= 0:
        return "m", "size"
    # Furniture and fittings are mostly 0.3 to 3 m: pick the unit that makes the model closest to 1 m,
    # with a small preference for the common units.
    return min(UNITS, key=lambda u: abs(math.log10(extent * UNITS[u])) + UNIT_PRIOR[u]), "size"


class ImportSession:
    def __init__(self, model: Model, unit: str | None = None, up_axis: str | None = None) -> None:
        self.model = model
        self.unit_guess, self.unit_source = guess_unit(model)
        self.edits = Edits(unit=unit or self.unit_guess, up_axis=up_axis or model.up_axis)
        self._check(self.edits)
        self.history: list[Edits] = []
        self.level = 0
        self.levels = Levels(model.parts)
        self._parts = {p.id: p for p in model.parts}

    # --- the transformation -------------------------------------------------

    @property
    def scale(self) -> float:
        """Metres per file unit."""
        if self.edits.unit == "file":
            return float(self.model.unit_scale or 1.0)
        return UNITS[self.edits.unit]

    def rotation(self, edits: Edits | None = None) -> np.ndarray:
        edits = edits or self.edits
        axes = np.asarray(ROTATIONS[edits.up_axis], dtype=np.float64)
        angle = math.radians(edits.turn)
        c, s = math.cos(angle), math.sin(angle)
        return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]]) @ axes

    def _oriented(self, vertices: np.ndarray) -> np.ndarray:
        return (vertices @ self.rotation().T) * self.scale

    def kept(self) -> list[Part]:
        return [p for p in self.model.parts if p.id not in self.edits.deleted]

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        """The bounding box of the kept parts, oriented and in metres, before moving it to the origin."""
        points = np.concatenate([self._oriented(p.vertices) for p in self.kept()])
        return points.min(axis=0), points.max(axis=0)

    def offset(self) -> np.ndarray:
        low, high = self.bounds()
        return np.array([-(low[0] + high[0]) / 2, -(low[1] + high[1]) / 2, -low[2]])

    def size(self) -> tuple[float, float, float]:
        low, high = self.bounds()
        return tuple(float(v) for v in high - low)

    def diagonal(self) -> float:
        return float(np.linalg.norm(self.size()))

    def meshes(self, level: int | None = None) -> list[ExportMesh]:
        """The kept parts at a reduction level (the current one by default), ready to export."""
        level = self.level if level is None else level
        offset = self.offset()
        result = []
        for part in self.kept():
            vertices, faces = self.levels.mesh(part.id, level)
            result.append(ExportMesh(part.id, part.name, part.material, self._oriented(vertices) + offset, faces))
        return result

    # --- reading ------------------------------------------------------------

    def part_size(self, part: Part) -> tuple[float, float, float]:
        oriented = self._oriented(part.vertices)
        return tuple(float(v) for v in np.ptp(oriented, axis=0))

    def parts(self) -> list[dict]:
        """Every part with its triangle count, size in metres and material, deleted ones marked."""
        diagonal = self.diagonal() or 1.0
        info = []
        for part in self.model.parts:
            size = self.part_size(part)
            material = self.model.materials.get(part.material)
            info.append({
                "id": part.id, "name": part.name, "triangles": part.triangles,
                "size": {"x": round(size[0], 4), "y": round(size[1], 4), "z": round(size[2], 4)},
                "relativeSize": round(float(np.linalg.norm(size)) / diagonal, 4),
                "material": part.material, "materialName": material.name if material else "",
                "deleted": part.id in self.edits.deleted,
            })
        return info

    def small_parts(self, fraction: float = SMALL_PART_FRACTION) -> list[str]:
        """The kept parts whose bounding-box diagonal is below ``fraction`` of the whole model's."""
        limit = self.diagonal() * float(fraction)
        return [p.id for p in self.kept() if float(np.linalg.norm(self.part_size(p))) < limit]

    def triangles(self, level: int | None = None) -> int:
        level = self.level if level is None else level
        return sum(len(self.levels.mesh(p.id, level)[1]) for p in self.kept())

    def level_list(self, placements: int = 1) -> list[dict]:
        counts = self.levels.counts(p.id for p in self.kept())
        return [{"index": i, "target": target, "triangles": count, "colour": budget(count, placements)["colour"]}
                for i, (target, count) in enumerate(zip(self.levels.targets, counts))]

    def used_materials(self) -> list[str]:
        seen: dict[str, None] = {}
        for part in self.kept():
            seen.setdefault(part.material, None)
        return list(seen)

    def summary(self, placements: int = 1) -> dict:
        size = self.size()
        triangles = self.triangles()
        used = set(self.used_materials())
        return {
            "file": str(self.model.archive or self.model.path), "name": self.model.name,
            "format": self.model.format, "reader": self.model.reader, "notes": list(self.model.notes),
            "unit": self.edits.unit, "unitGuess": self.unit_guess, "unitSource": self.unit_source,
            "units": [{"id": u, "label": UNIT_LABELS[u]} for u in self.unit_choices()],
            "upAxis": self.edits.up_axis, "upAxes": list(UP_AXES), "turn": self.edits.turn,
            "size": {"x": round(size[0], 4), "y": round(size[1], 4), "z": round(size[2], 4)},
            "triangles": triangles, "fullTriangles": self.triangles(0),
            "parts": self.parts(),
            "materials": [m.to_dict() for m in self.model.materials.values() if m.id in used],
            "levels": self.level_list(placements), "level": self.level,
            "budget": budget(triangles, placements),
            "canUndo": bool(self.history),
        }

    def unit_choices(self) -> list[str]:
        return list(UNITS) + (["file"] if self.model.unit_scale else [])

    # --- edits --------------------------------------------------------------

    def _check(self, edits: Edits) -> None:
        if edits.unit not in self.unit_choices():
            raise ModelImportError(f"Unknown unit {edits.unit}; use one of {', '.join(self.unit_choices())}.")
        if edits.up_axis not in ROTATIONS:
            raise ModelImportError(f"Unknown up axis {edits.up_axis}; use one of {', '.join(UP_AXES)}.")
        if not [p for p in self.model.parts if p.id not in edits.deleted]:
            raise ModelImportError("That would delete every part. Keep at least one.")

    def _apply(self, edits: Edits) -> bool:
        if edits == self.edits:
            return False
        self._check(edits)
        self.history.append(self.edits)
        del self.history[:-HISTORY]
        self.edits = edits
        return True

    def _known(self, ids) -> set[str]:
        ids = {str(i) for i in ids}
        unknown = ids - set(self._parts)
        if unknown:
            raise ModelImportError(f"Unknown part ids: {', '.join(sorted(unknown))}.")
        return ids

    def delete_parts(self, ids) -> list[str]:
        ids = self._known(ids)
        self._apply(replace(self.edits, deleted=self.edits.deleted | ids))
        return sorted(ids)

    def restore_parts(self, ids=None) -> list[str]:
        """Bring deleted parts back (all of them when ``ids`` is None)."""
        ids = set(self.edits.deleted) if ids is None else self._known(ids)
        self._apply(replace(self.edits, deleted=self.edits.deleted - ids))
        return sorted(ids)

    def remove_small_parts(self, fraction: float = SMALL_PART_FRACTION) -> list[str]:
        small = self.small_parts(fraction)
        if small and len(small) == len(self.kept()):
            raise ModelImportError("Every part is below that size. Use a smaller threshold.")
        if small:
            self._apply(replace(self.edits, deleted=self.edits.deleted | set(small)))
        return small

    def set_unit(self, unit: str) -> None:
        self._apply(replace(self.edits, unit=unit))

    def set_up_axis(self, axis: str) -> None:
        self._apply(replace(self.edits, up_axis=str(axis).upper()))

    def set_turn(self, degrees: float) -> None:
        self._apply(replace(self.edits, turn=float(degrees) % 360.0))

    def set_level(self, level: int) -> int:
        """Pick a reduction level (0 is the full model). Not part of the undo history."""
        self.level = max(0, min(int(level), len(self.levels) - 1))
        return self.level

    def undo(self) -> bool:
        if not self.history:
            return False
        self.edits = self.history.pop()
        return True
