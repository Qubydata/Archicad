"""Site from Map in the open Archicad project.

Builds the real ground around the project location as an Archicad Mesh, adds
the neighbouring buildings as simple blocks, or turns a surveyor's points and
contours into the Mesh. The elements go on their own layers and are
remembered, so building the site again replaces them and Remove deletes them.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from ..connection import ArchicadConnection, ArchicadError
from . import buildings as osm
from . import elevation, survey
from .geo import Frame, ground_resolution
from .terrain import Grid, extrusion_body, grid_axis, grid_mesh, survey_mesh

log = logging.getLogger("archicad_mcp.site")

TERRAIN_LAYER = "Tevavid Site Terrain"
BUILDINGS_LAYER = "Tevavid Site Buildings"
MIN_SIZE, MAX_SIZE = 20.0, 3000.0
BATCH = 100

Progress = Callable[[float, str], None]


def _ids(guids: list[str]) -> list[dict]:
    return [{"elementId": {"guid": g}} for g in guids]


class SiteService:
    def __init__(self, connection: ArchicadConnection, data_dir: Path | None = None,
                 fetch_tile: elevation.Fetch | None = None, post: osm.Post | None = None) -> None:
        self.connection = connection
        self.data_dir = Path(data_dir) if data_dir else None
        self.tiles = elevation.TileCache(self.data_dir / "terrain-cache" if self.data_dir else None,
                                         fetch_tile or elevation.http_get)
        self.post = post or osm.http_post
        self.lock = threading.Lock()
        self.elements: dict[str, list[str]] = {"terrain": [], "buildings": []}
        self.last: dict[str, Any] | None = None
        self._load()

    # --- remembered elements --------------------------------------------------

    @property
    def state_file(self) -> Path | None:
        return self.data_dir / "site.json" if self.data_dir else None

    def _load(self) -> None:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf8"))
            self.elements = {k: list(data.get("elements", {}).get(k, [])) for k in ("terrain", "buildings")}
            self.last = data.get("last")
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self) -> None:
        if self.state_file:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"elements": self.elements, "last": self.last}), encoding="utf8")

    def _delete(self, kind: str) -> int:
        guids = self.elements.get(kind, [])
        for start in range(0, len(guids), BATCH):
            try:
                self.connection.tapir("DeleteElements", {"elements": _ids(guids[start:start + BATCH])})
            except ArchicadError as exc:
                log.warning("Could not delete site elements: %s", exc)
        self.elements[kind] = []
        self._save()
        return len(guids)

    # --- Archicad helpers -----------------------------------------------------

    def location(self) -> dict[str, Any]:
        geo = self.connection.tapir("GetGeoLocation")
        place = geo.get("projectLocation", {})
        return {"latitude": float(place.get("latitude", 0.0)), "longitude": float(place.get("longitude", 0.0)),
                "altitude": float(place.get("altitude", 0.0)), "north": float(place.get("north", math.pi / 2)),
                "surveyPoint": geo.get("surveyPoint")}

    def _ground_floor(self) -> tuple[int, float]:
        """The floor the site goes on (the one at level 0, else the lowest), and its level."""
        try:
            stories = self.connection.tapir("GetStories").get("stories", [])
        except ArchicadError:
            return 0, 0.0
        if not stories:
            return 0, 0.0
        story = min(stories, key=lambda s: (abs(float(s.get("level", 0.0))), s.get("index", 0)))
        return int(story.get("index", 0)), float(story.get("level", 0.0))

    def _layer(self, name: str) -> int | None:
        def find():
            for layer in self.connection.tapir("GetAttributesByType", {"attributeType": "Layer"}).get("attributes", []):
                if layer.get("name") == name:
                    return int(layer["index"])
            return None

        index = find()
        if index is None:
            self.connection.tapir("CreateLayers", {"layerDataArray": [{"name": name}]})
            index = find()
        return index

    def _create(self, command: str, key: str, items: list[dict], layer: str, progress: Progress,
                start: float, span: float, what: str) -> list[str]:
        created: list[str] = []
        for first in range(0, len(items), BATCH):
            answer = self.connection.tapir(command, {key: items[first:first + BATCH]})
            for item in answer.get("elements", []):
                if "elementId" in item:
                    created.append(item["elementId"]["guid"])
                else:
                    log.warning("%s: %s", command, item.get("error"))
            progress(start + span * min(1.0, (first + BATCH) / max(len(items), 1)), f"Placing {what}")
        index = self._layer(layer)
        if index is not None and created:
            for first in range(0, len(created), BATCH):
                self.connection.tapir("SetDetailsOfElements", {"elementsWithDetails": [
                    {"elementId": {"guid": g}, "details": {"layerIndex": index}} for g in created[first:first + BATCH]]})
        return created

    # --- from the map -----------------------------------------------------------

    def from_map(self, size: float = 300.0, spacing: float | None = None, include_buildings: bool = True,
                 ground: str = "origin", origin_level: float = 0.0, progress: Progress | None = None) -> dict[str, Any]:
        """Terrain (and buildings) of a square of `size` metres centred on the project origin.

        ground: "origin" puts the ground at the project origin at origin_level (most convenient);
        "altitude" uses the project's altitude, so heights are true heights above sea level minus it.
        """
        report = progress or (lambda fraction, message: None)
        if not self.lock.acquire(blocking=False):
            raise ArchicadError("The site is already being built.")
        try:
            size = float(min(max(size, MIN_SIZE), MAX_SIZE))
            place = self.location()
            if abs(place["latitude"]) < 1e-9 and abs(place["longitude"]) < 1e-9:
                raise ArchicadError("The project location is not set. Set it in Options > Project Location "
                                    "(type the address or the latitude and longitude), then try again.")
            frame = Frame(place["latitude"], place["longitude"], place["north"])
            half = size / 2
            zoom = elevation.choose_zoom(frame.latitude, half)
            resolution = ground_resolution(frame.latitude, zoom)
            spacing = float(spacing) if spacing else max(resolution, size / 80)
            axis = grid_axis(half, spacing)
            report(0.05, "Downloading the ground heights")
            try:
                heights = elevation.sample(frame, axis, axis, self.tiles, zoom,
                                           lambda f: report(0.05 + 0.45 * f, "Downloading the ground heights"))
            except OSError as exc:
                raise ArchicadError(f"The elevation data could not be downloaded ({exc}). Check the internet "
                                    "connection and try again.") from exc
            centre = Grid(axis, axis, heights).height_at(0.0, 0.0)
            floor, floor_level = self._ground_floor()
            if ground == "altitude":
                offset = -place["altitude"]
            else:
                offset = origin_level - float(centre)
            grid = Grid(axis, axis, heights + offset - floor_level)

            report(0.55, "Placing the terrain")
            self._delete("terrain")
            self.elements["terrain"] = self._create("CreateMeshes", "meshesData", [grid_mesh(grid, floor)],
                                                    TERRAIN_LAYER, report, 0.55, 0.1, "the terrain")
            self._save()
            if not self.elements["terrain"]:
                raise ArchicadError("Archicad did not create the terrain mesh.")

            notes = [f"Elevation data: {elevation.SOURCE} Accuracy is about +/- 2 to 5 m in height, enough for "
                     "context and slopes; for levels on a building plot, import your survey."]
            found: list[osm.Building] = []
            if include_buildings:
                report(0.7, "Downloading the neighbouring buildings")
                self._delete("buildings")
                try:
                    found = osm.fetch(frame, half, self.post)
                except ConnectionError as exc:
                    notes.append(f"Neighbouring buildings were left out: {exc}")
                morphs = []
                for building in found:
                    body = extrusion_body(building.outline, building.height)
                    if body is None:
                        continue
                    base = float(np.min(grid.height_at([p[0] for p in building.outline], [p[1] for p in building.outline])))
                    morphs.append({"basePoint": {"x": 0.0, "y": 0.0, "z": round(base + floor_level - 0.1, 4)},
                                   "body": {**body, "vertices": [{**v, "z": v["z"] + (0.1 if v["z"] > 0 else 0.0)}
                                                                 for v in body["vertices"]]},
                                   "floorIndex": floor})
                self.elements["buildings"] = self._create("CreateMorphs", "morphsData", morphs, BUILDINGS_LAYER,
                                                          report, 0.8, 0.18, "the neighbouring buildings")
                self._save()
                estimated = sum(b.estimated for b in found)
                if estimated:
                    notes.append(f"{estimated} of {len(found)} buildings have no height in OpenStreetMap and were "
                                 f"drawn {osm.DEFAULT_HEIGHT:.0f} m high.")
                notes.append(osm.ATTRIBUTION)
            notes.append(elevation.ATTRIBUTION)
            above_sea = heights
            self.last = {
                "source": "map", "size": size, "spacing": round(float(axis[1] - axis[0]), 2),
                "points": int(heights.size), "zoom": zoom, "resolution": round(resolution, 1),
                "location": {k: place[k] for k in ("latitude", "longitude", "altitude", "north")},
                "elevation": {"min": round(float(above_sea.min()), 1), "max": round(float(above_sea.max()), 1),
                              "origin": round(float(centre), 1)},
                "ground": ground, "originLevel": origin_level,
                "buildings": len(self.elements["buildings"]), "notes": notes,
            }
            self._save()
            report(1.0, "Done")
            return self.state()
        finally:
            self.lock.release()

    # --- from a survey -----------------------------------------------------------

    def from_survey(self, name: str, data: bytes, coordinates: str = "auto", order: str = "auto",
                    units: str = "auto", progress: Progress | None = None) -> dict[str, Any]:
        """Terrain from a surveyor's CSV/TXT points or DXF contours."""
        report = progress or (lambda fraction, message: None)
        if not self.lock.acquire(blocking=False):
            raise ArchicadError("The site is already being built.")
        try:
            report(0.1, "Reading the survey")
            try:
                data_set = survey.read(name, data, order)
                factor, unit_note = survey.unit_factor(data_set, units)
                data_set = data_set.scaled(factor)
                place = self.location()
                data_set, place_notes = survey.to_project(data_set, place.get("surveyPoint"), coordinates)
                floor, floor_level = self._ground_floor()
                shifted = survey.Survey(data_set.points - [0, 0, floor_level],
                                        [line - [0, 0, floor_level] for line in data_set.lines])
                mesh, mesh_notes = survey_mesh(shifted.points, shifted.lines, floor)
            except ValueError as exc:
                raise ArchicadError(str(exc)) from exc
            report(0.5, "Placing the terrain")
            self._delete("terrain")
            self.elements["terrain"] = self._create("CreateMeshes", "meshesData", [mesh], TERRAIN_LAYER,
                                                    report, 0.5, 0.45, "the terrain")
            self._save()
            if not self.elements["terrain"]:
                raise ArchicadError("Archicad did not create the terrain mesh.")
            everything = np.vstack([data_set.points.reshape(-1, 3)] + data_set.lines)
            notes = data_set.notes + ([unit_note] if unit_note else []) + place_notes + mesh_notes
            self.last = {
                "source": "survey", "file": name, "points": int(len(data_set.points)),
                "lines": len(data_set.lines), "size": round(float(max(np.ptp(everything[:, 0]), np.ptp(everything[:, 1]))), 1),
                "elevation": {"min": round(float(everything[:, 2].min()), 2), "max": round(float(everything[:, 2].max()), 2)},
                "buildings": len(self.elements["buildings"]), "notes": notes,
            }
            self._save()
            report(1.0, "Done")
            return self.state()
        finally:
            self.lock.release()

    # --- state ---------------------------------------------------------------------

    def clear(self, what: str = "all") -> dict[str, Any]:
        with self.lock:
            kinds = ("terrain", "buildings") if what == "all" else (what,)
            removed = {kind: self._delete(kind) for kind in kinds}
            if what == "all":
                self.last = None
                self._save()
            return {"removed": removed}

    def state(self) -> dict[str, Any]:
        return {"terrain": len(self.elements["terrain"]), "buildings": len(self.elements["buildings"]),
                "last": self.last}
