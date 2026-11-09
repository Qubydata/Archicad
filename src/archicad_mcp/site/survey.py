"""A surveyor's data: point lists (CSV/TXT) and drawings with contours (DXF).

Point lists come in many layouts: with or without a header, separated by
commas, semicolons, tabs or spaces, and with the columns in survey orders such
as PENZ (point, easting, northing, height) or PNEZ (northing first). A header
names the columns; without one, `order` says which layout it is.

DXF drawings give points (POINT), contour lines (LWPOLYLINE with an elevation,
3D POLYLINE, LINE) and spot heights written as numbers (TEXT/MTEXT). DWG files
must first be saved as DXF (AutoCAD, Archicad and most CAD programs can).
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

import numpy as np

ORDERS = {"xyz": (None, 0, 1, 2), "penz": (0, 1, 2, 3), "pnez": (0, 2, 1, 3), "enz": (None, 0, 1, 2), "nez": (None, 1, 0, 2)}
X_NAMES = {"x", "e", "east", "easting", "eastings"}
Y_NAMES = {"y", "n", "north", "northing", "northings"}
Z_NAMES = {"z", "h", "height", "elev", "elevation", "level", "rl", "alt", "altitude"}
UNITS = {"m": 1.0, "cm": 0.01, "mm": 0.001, "ft": 0.3048, "in": 0.0254}
DXF_UNITS = {1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}
NUMBER = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")


@dataclass
class Survey:
    points: np.ndarray  # N x 3
    lines: list[np.ndarray] = field(default_factory=list)  # each M x 3
    units: str = "m"
    notes: list[str] = field(default_factory=list)

    def scaled(self, factor: float) -> Survey:
        return Survey(self.points * factor, [line * factor for line in self.lines], self.units, self.notes)


def _number(text: str) -> float | None:
    text = text.strip().replace(" ", "")
    if not NUMBER.match(text):
        return None
    return float(text.replace(",", "."))


def read_points(text: str, order: str = "auto") -> Survey:
    """Points from CSV/TXT text."""
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith(("#", "//"))]
    if not lines:
        raise ValueError("The file has no points.")
    delimiter = None  # whitespace
    for candidate in ("\t", ";", ","):
        if sum(candidate in line for line in lines[:50]) >= 0.8 * min(len(lines), 50):
            delimiter = candidate
            break
    if delimiter == ",":
        rows = list(csv.reader(io.StringIO("\n".join(lines)), skipinitialspace=True))
    elif delimiter:
        rows = [line.split(delimiter) for line in lines]
    else:
        rows = [line.split() for line in lines]
    rows = [[c.strip() for c in row if c.strip() != ""] for row in rows]
    header = rows[0]
    columns = None
    if sum(_number(c) is None for c in header) >= 2:  # a header row
        names = [re.sub(r"[^a-z]", "", c.lower()) for c in header]
        find = lambda wanted: next((k for k, n in enumerate(names) if n in wanted), None)  # noqa: E731
        columns = (None, find(X_NAMES), find(Y_NAMES), find(Z_NAMES))
        if None in columns[1:]:
            raise ValueError(f"Could not tell which columns are x, y and height from the header: {', '.join(header)}.")
        rows = rows[1:]
    notes = []
    if columns is None:
        key = order.lower()
        if key == "auto":
            numeric = [sum(_number(c) is not None for c in row) for row in rows[:20]]
            first_is_id = all(len(row) >= 4 for row in rows[:20])
            key = "penz" if first_is_id else "xyz"
            notes.append("Columns read as " + ("point, easting, northing, height" if key == "penz" else "x, y, height")
                         + ". If that is wrong, pick the column order." if max(numeric, default=0) else "")
        if key not in ORDERS:
            raise ValueError(f"Unknown column order {order!r}.")
        columns = ORDERS[key]
    points, skipped = [], 0
    for row in rows:
        try:
            values = [_number(row[k]) for k in columns[1:]]
        except IndexError:
            skipped += 1
            continue
        if None in values:
            skipped += 1
            continue
        points.append(values)
    if skipped:
        notes.append(f"{skipped} lines without three numbers were skipped.")
    if len(points) < 3:
        raise ValueError("The file needs at least three points with x, y and height.")
    return Survey(np.array(points, dtype=float), [], "m", [n for n in notes if n])


def read_dxf(data: bytes) -> Survey:
    """Points, level lines and spot heights from a DXF drawing (model space)."""
    import ezdxf
    from ezdxf import recover

    try:
        doc, _auditor = recover.read(io.BytesIO(data))
    except Exception as exc:
        raise ValueError(f"This DXF file could not be read ({exc}). Save it again as DXF (2013 or later).") from exc
    units = DXF_UNITS.get(int(doc.header.get("$INSUNITS", 6) or 6), "m")
    points: list[tuple[float, float, float]] = []
    lines: list[np.ndarray] = []
    flat_lines = 0
    for entity in doc.modelspace():
        kind = entity.dxftype()
        try:
            if kind == "POINT":
                p = entity.dxf.location
                points.append((p.x, p.y, p.z))
            elif kind == "LWPOLYLINE":
                z = float(entity.dxf.elevation or 0.0)
                coords = [(x, y, z) for x, y, *_ in entity.get_points("xy")]
                lines.append(np.array(coords + ([coords[0]] if entity.closed else []), dtype=float))
            elif kind == "POLYLINE":
                coords = [(v.dxf.location.x, v.dxf.location.y, v.dxf.location.z) for v in entity.vertices]
                if coords:
                    lines.append(np.array(coords + ([coords[0]] if entity.is_closed else []), dtype=float))
            elif kind == "LINE":
                s, e = entity.dxf.start, entity.dxf.end
                lines.append(np.array([(s.x, s.y, s.z), (e.x, e.y, e.z)], dtype=float))
            elif kind in ("TEXT", "MTEXT"):
                text = entity.plain_text() if kind == "MTEXT" else entity.dxf.text
                value = _number(re.sub(r"^(?:\+|RL|EL|Z)\s*[:=]?\s*", "", str(text).strip(), flags=re.I))
                if value is not None:
                    p = entity.dxf.insert
                    points.append((p.x, p.y, value if abs(p.z) < 1e-9 else p.z))
        except (AttributeError, ezdxf.DXFError):
            continue
    has_heights = any(abs(p[2]) > 1e-9 for p in points) or any(np.any(np.abs(line[:, 2]) > 1e-9) for line in lines)
    notes = []
    if has_heights:
        kept = [line for line in lines if np.any(np.abs(line[:, 2]) > 1e-9)]
        flat_lines = len(lines) - len(kept)
        lines = kept
        points = [p for p in points if abs(p[2]) > 1e-9]
    if flat_lines:
        notes.append(f"{flat_lines} lines at height 0 (boundaries, roads and other drawing lines) were left out.")
    if not has_heights:
        raise ValueError("No heights were found in this DXF: contour lines need an elevation, and spot heights "
                         "need 3D points or numbers as text.")
    return Survey(np.array(points, dtype=float).reshape(-1, 3), lines, units, notes)


def read(name: str, data: bytes, order: str = "auto") -> Survey:
    suffix = name.lower().rsplit(".", 1)[-1] if "." in name else ""
    if suffix == "dxf":
        return read_dxf(data)
    if suffix == "dwg":
        raise ValueError("DWG files cannot be read directly. Open the drawing in AutoCAD or Archicad and save it as DXF.")
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    return read_points(text, order)


def unit_factor(survey: Survey, units: str = "auto") -> tuple[float, str | None]:
    """Metres per file unit, and a note when the units were guessed."""
    if units != "auto":
        return UNITS[units], None
    if survey.units != "m":
        return UNITS[survey.units], f"The drawing's units are {survey.units}."
    everything = np.vstack([survey.points] + survey.lines) if survey.lines else survey.points
    spread = float(np.ptp(everything[:, 2])) if len(everything) else 0.0
    if spread > 2000:  # more than 2 km of height difference on a site: millimetres
        return 0.001, "The heights look like millimetres, so they were read as millimetres."
    return 1.0, None


def to_project(survey: Survey, survey_point: dict | None, coordinates: str = "auto") -> tuple[Survey, list[str]]:
    """Move map coordinates (eastings, northings) to the project's, using Archicad's survey point."""
    notes: list[str] = []
    everything = np.vstack([survey.points] + survey.lines) if survey.lines else survey.points
    far = float(np.max(np.abs(everything[:, :2]))) > 50_000 if len(everything) else False
    position = (survey_point or {}).get("position") or {}
    has_survey_point = any(abs(position.get(k, 0.0)) > 1e-6 for k in ("eastings", "northings"))
    mode = coordinates
    if mode == "auto":
        mode = "map" if far else "project"
    if mode == "project":
        return survey, notes

    def move(array: np.ndarray, matrix, origin_map, origin_project, z_shift) -> np.ndarray:
        out = array.copy()
        d = array[:, :2] - origin_map
        out[:, :2] = d @ matrix.T + origin_project
        out[:, 2] = array[:, 2] + z_shift
        return out

    if has_survey_point:
        params = (survey_point or {}).get("geoReferencingParameters") or {}
        a, o = params.get("xAxisAbscissa", 1.0) or 1.0, params.get("xAxisOrdinate", 0.0) or 0.0
        length = float(np.hypot(a, o)) or 1.0
        a, o = a / length, o / length
        scale = params.get("scale", 1.0) or 1.0
        matrix = np.array([[a, o], [-o, a]]) / scale  # map east/north to the project's x/y
        in_project = (survey_point or {}).get("positionInProject") or {}
        origin_map = np.array([position.get("eastings", 0.0), position.get("northings", 0.0)])
        origin_project = np.array([in_project.get("x", 0.0), in_project.get("y", 0.0)])
        z_shift = in_project.get("z", 0.0) - position.get("elevation", 0.0)
        notes.append("Map coordinates were placed with the project's survey point.")
    else:
        matrix = np.eye(2)
        lo, hi = everything[:, :2].min(axis=0), everything[:, :2].max(axis=0)
        origin_map, origin_project, z_shift = (lo + hi) / 2, np.zeros(2), 0.0
        notes.append("The survey uses map coordinates, but the project's survey point is not set, so the survey was "
                     "centred on the project origin. Set the survey point (Options > Project Preferences > Location) "
                     "to place it exactly.")
    moved = Survey(move(survey.points, matrix, origin_map, origin_project, z_shift) if len(survey.points) else survey.points,
                   [move(line, matrix, origin_map, origin_project, z_shift) for line in survey.lines], "m", survey.notes)
    return moved, notes
