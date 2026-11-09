"""Polygon reduction and the polygon budget.

Each part (one mesh of one material) is reduced on its own with quadric edge
collapse (fast-simplification), its open borders locked where that still
reaches the target, so parts keep their outline and materials do not bleed
into each other. A handful of levels are computed up front, each from the one
before, so the slider in the panel snaps between them instantly.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

LEVELS = 8
MIN_TRIANGLES = 1000  # the coarsest level aims at this (or 0.2 % of the model, if that is more)
MIN_FRACTION = 0.002
PART_FLOOR = 12  # a part is never reduced below this (a box has 12 triangles)

# Archicad polygons per placed object. With many copies the limits shrink.
BUDGETS = ((20, 1_000, 5_000), (10, 5_000, 25_000), (1, 20_000, 100_000))  # (copies from, green to, amber to)

Progress = Callable[[float, str], None]


def level_targets(total: int, levels: int = LEVELS, floor: int = MIN_TRIANGLES) -> list[int]:
    """Triangle counts to aim at, log-spaced from the full model down to the coarsest level."""
    if total <= floor * 1.5 or levels < 2:
        return [int(total)]
    low = max(floor, int(total * MIN_FRACTION))
    targets = [int(round(total * (low / total) ** (k / (levels - 1)))) for k in range(levels)]
    return sorted(set(targets), reverse=True)


def simplify_mesh(vertices: np.ndarray, faces: np.ndarray, target: int) -> tuple[np.ndarray, np.ndarray]:
    """Reduce a triangle mesh to about ``target`` triangles. Returns the mesh unchanged if it is small already."""
    target = max(int(target), PART_FLOOR)
    if len(faces) <= target:
        return vertices, faces
    import fast_simplification

    points = np.ascontiguousarray(vertices, dtype=np.float64)
    triangles = np.ascontiguousarray(faces, dtype=np.int64)
    best = (vertices, faces)
    for lock in (True, False):
        try:
            v, f = fast_simplification.simplify(points, triangles, target_count=target, preserve_border=lock)
        except TypeError:  # an old fast-simplification without preserve_border
            v, f = fast_simplification.simplify(points, triangles, target_count=target)
        except Exception:
            continue
        v, f = _compact(np.asarray(v, dtype=np.float64), np.asarray(f, dtype=np.int64))
        if len(f) and len(f) < len(best[1]):
            best = (v, f)
        if len(best[1]) <= max(target * 1.5, target + 50):
            break  # close enough with the borders kept
    return best


def _compact(vertices: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    faces = faces.reshape(-1, 3)
    faces = faces[(faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])]
    if not len(faces):
        return vertices[:0], faces
    used, inverse = np.unique(faces, return_inverse=True)
    return vertices[used], inverse.reshape(-1, 3)


class Levels:
    """The reduced versions of every part, per level. Level 0 is the full model."""

    def __init__(self, parts, levels: int = LEVELS, floor: int = MIN_TRIANGLES) -> None:
        self.parts = {p.id: p for p in parts}
        total = sum(p.triangles for p in parts)
        self.targets = level_targets(total, levels, floor)
        self.ratios = [t / total if total else 1.0 for t in self.targets]
        self.meshes: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {}

    def __len__(self) -> int:
        return len(self.targets)

    @property
    def computed(self) -> bool:
        return len(self.meshes) == len(self.parts)

    def compute(self, progress: Progress | None = None) -> None:
        report = progress or (lambda fraction, message: None)
        total = sum(p.triangles for p in self.parts.values()) or 1
        done = 0
        for part in self.parts.values():
            if part.id in self.meshes:
                continue
            meshes = [(part.vertices, part.faces)]
            for ratio in self.ratios[1:]:
                previous = meshes[-1]
                target = max(int(round(part.triangles * ratio)), min(part.triangles, PART_FLOOR))
                meshes.append(simplify_mesh(*previous, target))
            self.meshes[part.id] = meshes
            done += part.triangles
            report(done / total, f"Reducing polygons ({done:,} of {total:,} triangles)")

    def mesh(self, part_id: str, level: int) -> tuple[np.ndarray, np.ndarray]:
        if level <= 0:
            part = self.parts[part_id]
            return part.vertices, part.faces
        self.compute()
        meshes = self.meshes[part_id]
        return meshes[min(level, len(meshes) - 1)]

    def counts(self, part_ids) -> list[int]:
        """Triangles of the given parts at each level."""
        ids = list(part_ids)
        if len(self) > 1:
            self.compute()
        return [sum(len(self.mesh(i, level)[1]) for i in ids) for level in range(len(self))]


def budget(triangles: int, placements: int = 1) -> dict:
    """The colour of the polygon budget bar (green, amber or red) and a short explanation."""
    placements = max(1, int(placements))
    _, green, amber = next(b for b in BUDGETS if placements >= b[0])
    count = f"{int(triangles):,} polygons"
    copies = "" if placements == 1 else f" for {placements} copies"
    if triangles <= green:
        colour = "green"
        message = f"Light: {count}. Fine to use{copies} in any project."
    elif triangles <= amber:
        colour = "amber"
        message = (f"Medium: {count}. Fine for a few copies, but reduce it{copies} if you will place it often "
                   f"(green is up to {green:,}).")
    else:
        colour = "red"
        message = (f"Heavy: {count}. It will slow down the 3D window, sections and renders{copies}. "
                   f"Reduce it to {amber:,} or less, ideally {green:,}.")
    return {"colour": colour, "triangles": int(triangles), "placements": placements, "green": green, "amber": amber,
            "fraction": round(min(1.0, triangles / (amber * 1.5)), 3), "message": message}
