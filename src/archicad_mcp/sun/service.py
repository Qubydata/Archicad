"""Sun Analysis in the open Archicad project.

Reads the project location, the rooms (zones) and the 3D model, runs the
daylight analysis, and shows the result as thin coloured tiles on the room
floors: real Archicad elements on their own layer, so they turn and pan with
the model in the 3D window. The colours are element highlights, so changing
the time of day only recolours the tiles, which is quick enough to scrub or
play through the day live.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime
from typing import Any

import numpy as np

from ..connection import ArchicadConnection, ArchicadError
from . import colors
from .daylight import Result, Room, Settings, analyze, summary
from .geometry import build_scene, surface_transmission
from .solar import Location

log = logging.getLogger("archicad_mcp.sun")

LAYER_NAME = "Tevavid Sun Analysis"
OBSTRUCTING = ("Wall", "Slab", "Roof", "Shell", "Column", "Beam", "Mesh", "Morph", "CurtainWall",
               "Window", "Door", "Skylight", "Stair", "Railing")
METRICS = ("illuminance", "sun_hours", "daylight_factor")
MAX_POINTS = 6000
TILE_THICKNESS = 0.01
BATCH = 300

Progress = Callable[[float, str], None]


def _ids(guids: list[str]) -> list[dict]:
    return [{"elementId": {"guid": g}} for g in guids]


def _chunks(items: list, size: int = BATCH):
    for start in range(0, len(items), size):
        yield items[start:start + size]


class SunAnalysis:
    def __init__(self, connection: ArchicadConnection, state_file=None) -> None:
        self.connection = connection
        self.state_file = state_file  # remembers the tiles, so they can be cleared after a restart
        self.result: Result | None = None
        self.tiles: list[str] = []
        self.metric = "illuminance"
        self.time_index: int | None = None
        self.scale: tuple[float, float] | None = None
        self.location_override: dict[str, float] | None = None
        self.notes: list[str] = []
        self.lock = threading.Lock()
        self._load_tiles()

    # --- persistence of the tile list ----------------------------------------

    def _load_tiles(self) -> None:
        try:
            self.tiles = list(json.loads(self.state_file.read_text(encoding="utf8")).get("tiles", []))
        except (AttributeError, OSError, ValueError):
            self.tiles = []

    def _save_tiles(self) -> None:
        if self.state_file is None:
            return
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"tiles": self.tiles}), encoding="utf8")
        except OSError:
            log.warning("Could not save the analysis tile list")

    # --- reading the project -------------------------------------------------

    def location(self) -> dict[str, Any]:
        """The location the analysis uses: the project's, unless overridden in the panel."""
        project = self.connection.tapir("GetGeoLocation").get("projectLocation", {})
        place = {"latitude": project.get("latitude", 0.0), "longitude": project.get("longitude", 0.0),
                 "north": project.get("north", np.pi / 2), "source": "project"}
        if self.location_override:
            place.update(self.location_override)
            place["source"] = "override"
        place.setdefault("utcOffset", round(place["longitude"] / 15.0))
        return place

    def _rooms(self, scope: str) -> tuple[list[Room], list[int]]:
        stories = self.connection.tapir("GetStories")
        heights = {s["index"]: s.get("height") or 3.0 for s in stories.get("stories", [])}
        current = stories.get("actStory", 0)
        if scope == "selection":
            guids = [e["elementId"]["guid"] for e in self.connection.command("API.GetSelectedElements").get("elements", [])]
        else:
            guids = [e["elementId"]["guid"] for e in
                     self.connection.command("API.GetElementsByType", {"elementType": "Zone"}).get("elements", [])]
        rooms, floors = [], []
        for chunk in _chunks(guids):
            details = self.connection.tapir("GetDetailsOfElements", {"elements": _ids(chunk), "fields": ["type", "floorIndex", "details"]})
            for guid, item in zip(chunk, details.get("detailsOfElements", [])):
                if item.get("type") != "Zone":
                    continue
                floor = int(item.get("floorIndex", 0))
                if scope == "story" and floor != current:
                    continue
                zone = item.get("details", {})
                outline = [(p["x"], p["y"]) for p in zone.get("polygonOutline", [])]
                if len(outline) < 3:
                    continue
                holes = [[(p["x"], p["y"]) for p in (h.get("polygonOutline") or h.get("polygonCoordinates") or [])]
                         for h in zone.get("holes", [])]
                name = " ".join(part for part in (zone.get("numberStr"), zone.get("name")) if part) or "Room"
                rooms.append(Room(guid, name, outline, holes, float(zone.get("zCoordinate", 0.0)), float(heights.get(floor, 3.0))))
                floors.append(floor)
        return rooms, floors

    def _scene(self, include_objects: bool, progress: Progress):
        kinds = OBSTRUCTING + (("Object", "Lamp") if include_objects else ())
        tiles = set(self.tiles)
        elements: list[tuple[str, str]] = []
        for kind in kinds:
            found = self.connection.command("API.GetElementsByType", {"elementType": kind, "filters": ["IsVisibleIn3D"]})
            elements += [(e["elementId"]["guid"], kind) for e in found.get("elements", []) if e["elementId"]["guid"] not in tiles]
        geometries = []
        done = 0
        for chunk in _chunks(elements, 100):
            answer = self.connection.tapir("Get3DGeometryOfElements", {"elements": _ids([g for g, _ in chunk])})
            for (_, kind), geometry in zip(chunk, answer.get("geometries3D", [])):
                if "bodies" in geometry:
                    geometries.append({"type": kind, "bodies": geometry["bodies"]})
            done += len(chunk)
            progress(0.1 + 0.2 * done / max(len(elements), 1), f"Reading the 3D model ({done} of {len(elements)} elements)")
        surfaces = sorted({p["surfaceId"]["guid"] for g in geometries for b in g["bodies"] for p in b.get("polygons", [])
                           if p.get("surfaceId", {}).get("guid")})
        transmissions: dict[str, float] = {}
        for chunk in _chunks(surfaces):
            answer = self.connection.tapir("GetSurfaces", {"attributeIds": [{"attributeId": {"guid": g}} for g in chunk],
                                                           "fields": ["transparency"]})
            for guid, surface in zip(chunk, answer.get("surfaces", [])):
                if "error" not in surface:
                    transmissions[guid] = surface_transmission(surface)
        return build_scene(geometries, transmissions), len(elements)

    # --- tiles -----------------------------------------------------------------

    def _layer_index(self) -> int | None:
        def find():
            for layer in self.connection.tapir("GetAttributesByType", {"attributeType": "Layer"}).get("attributes", []):
                if layer.get("name") == LAYER_NAME:
                    return int(layer["index"])
            return None

        index = find()
        if index is None:
            self.connection.tapir("CreateLayers", {"layerDataArray": [{"name": LAYER_NAME}]})
            index = find()
        return index

    def _delete_tiles(self) -> None:
        if not self.tiles:
            return
        try:
            self.connection.tapir("HighlightElements", {"elements": [], "highlightedColors": []})
        except ArchicadError:
            pass
        for chunk in _chunks(self.tiles):
            try:
                self.connection.tapir("DeleteElements", {"elements": _ids(chunk)})
            except ArchicadError as exc:
                log.warning("Could not delete analysis tiles: %s", exc)
        self.tiles = []
        self._save_tiles()

    def _create_tiles(self, result: Result, floors: list[int], progress: Progress) -> None:
        size = result.settings.grid * 0.92
        data = []
        for point, room in zip(result.points, result.room_of_point):
            z = result.rooms[room].floor_z + result.settings.work_plane + 0.01
            data.append({"basePoint": {"x": float(point[0] - size / 2), "y": float(point[1] - size / 2), "z": float(z)},
                         "size": {"x": size, "y": size, "z": TILE_THICKNESS},
                         "castShadow": False, "receiveShadow": False, "floorIndex": int(floors[room])})
        created: list[str] = []
        for chunk in _chunks(data):
            answer = self.connection.tapir("CreateMorphs", {"morphsData": chunk})
            created += [e["elementId"]["guid"] for e in answer.get("elements", []) if "elementId" in e]
            progress(0.88 + 0.08 * len(created) / max(len(data), 1), f"Placing tiles ({len(created)} of {len(data)})")
        self.tiles = created
        self._save_tiles()
        if len(created) != len(data):
            self.notes.append(f"{len(data) - len(created)} tiles could not be placed.")
        layer = self._layer_index()
        if layer is not None:
            for chunk in _chunks(created):
                self.connection.tapir("SetDetailsOfElements", {"elementsWithDetails": [
                    {"elementId": {"guid": g}, "details": {"layerIndex": layer}} for g in chunk]})

    # --- the analysis ------------------------------------------------------------

    def run(self, scope: str = "story", day: str | None = None, metric: str = "illuminance", grid: float = 0.5,
            work_plane: float = 0.0, include_objects: bool = False, reflectances: dict[str, float] | None = None,
            progress: Progress | None = None) -> dict[str, Any]:
        report = progress or (lambda fraction, message: None)
        if metric not in METRICS:
            raise ArchicadError(f"Unknown metric {metric}; use one of {', '.join(METRICS)}.")
        with self.lock:
            started = time.time()
            self.notes = []
            report(0.0, "Reading the project location and rooms")
            place = self.location()
            location = Location(place["latitude"], place["longitude"], place["north"], place["utcOffset"])
            rooms, floors = self._rooms(scope)
            if not rooms:
                where = {"story": "on the current story", "all": "in the project", "selection": "in the selection"}[scope]
                raise ArchicadError(f"No rooms (zones) {where}. Sun Analysis measures the light on room floors: "
                                    "place zones first, or select some.")
            settings = Settings(grid=grid, work_plane=work_plane, **(reflectances or {}))
            area = sum(abs(np.prod(np.ptp(np.asarray(r.outline), axis=0))) for r in rooms)
            while area / settings.grid ** 2 > MAX_POINTS:
                settings.grid = round(settings.grid * 1.25, 2)
            if settings.grid != grid:
                self.notes.append(f"The grid was coarsened to {settings.grid} m to keep the analysis fast.")
            report(0.1, "Reading the 3D model")
            scene, element_count = self._scene(include_objects, report)
            self.notes += scene.notes
            when = date.fromisoformat(day) if day else date.today()
            result = analyze(scene, rooms, location, when, settings,
                             lambda f, message: report(0.3 + 0.55 * f, message))
            report(0.86, "Placing tiles in the model")
            self._delete_tiles()
            self.result = result
            self._create_tiles(result, floors, report)
            self.metric = metric
            self.scale = None
            self.time_index = self._noon_index()
            report(0.97, "Colouring")
            shown = self._apply()
            report(1.0, "Done")
            log.info("Sun analysis: %d rooms, %d points, %d elements, %.1f s", len(rooms), len(result.points),
                     element_count, time.time() - started)
            return {**self.state(), "shown": shown, "seconds": round(time.time() - started, 1), "elements": element_count}

    def _noon_index(self) -> int | None:
        if not self.result or not self.result.samples:
            return None
        noon = datetime.combine(self.result.day, datetime.min.time()).replace(hour=12)
        return min(range(len(self.result.samples)), key=lambda i: abs((self.result.samples[i].time - noon).total_seconds()))

    def values(self) -> np.ndarray:
        if self.result is None:
            return np.zeros(0)
        if self.metric == "sun_hours":
            return self.result.sun_hours()
        if self.metric == "daylight_factor":
            return self.result.daylight_factor()
        if self.time_index is None:
            return np.zeros(len(self.result.points))
        return self.result.illuminance(self.time_index)

    def _range(self, values: np.ndarray) -> tuple[float, float]:
        if self.scale:
            return self.scale
        low, high = colors.SCALES[self.metric][:2]
        if self.metric == "sun_hours" and len(values):
            high = max(1.0, float(np.ceil(values.max())))
        return low, high

    def _apply(self) -> dict[str, Any]:
        values = self.values()
        low, high = self._range(values)
        if self.tiles and len(values) == len(self.tiles):
            self.connection.tapir("HighlightElements", {
                "elements": _ids(self.tiles), "highlightedColors": colors.colorize(values, low, high).tolist(),
                "wireframe3D": False})
        unit = colors.SCALES[self.metric][2]
        target = {"illuminance": 300.0, "daylight_factor": 2.0, "sun_hours": 1.5}[self.metric]
        return {"stats": summary(values, unit, target), "legend": colors.legend(low, high), "low": low, "high": high}

    def show(self, metric: str | None = None, time_of_day: str | int | None = None,
             scale: tuple[float, float] | None = None) -> dict[str, Any]:
        """Recolour the tiles for another metric, time of day or colour scale. Quick: no new analysis."""
        if self.result is None:
            raise ArchicadError("Run a sun analysis first.")
        with self.lock:
            if metric:
                if metric not in METRICS:
                    raise ArchicadError(f"Unknown metric {metric}; use one of {', '.join(METRICS)}.")
                if metric != self.metric:
                    self.scale = None
                self.metric = metric
            if time_of_day is not None and self.result.samples:
                if isinstance(time_of_day, int):
                    self.time_index = max(0, min(time_of_day, len(self.result.samples) - 1))
                else:
                    hours, minutes = (int(part) for part in str(time_of_day).split(":")[:2])
                    target = hours * 60 + minutes
                    self.time_index = min(range(len(self.result.samples)), key=lambda i: abs(
                        self.result.samples[i].time.hour * 60 + self.result.samples[i].time.minute - target))
            if scale:
                self.scale = (float(scale[0]), float(scale[1]))
            return {**self.state(), "shown": self._apply()}

    def clear(self) -> dict[str, Any]:
        with self.lock:
            count = len(self.tiles)
            self._delete_tiles()
            self.result = None
            return {"removed": count}

    def state(self) -> dict[str, Any]:
        r = self.result
        state: dict[str, Any] = {"hasResult": r is not None, "tiles": len(self.tiles), "metric": self.metric,
                                 "notes": self.notes}
        if r is not None:
            state.update({
                "day": r.day.isoformat(),
                "location": {"latitude": r.location.latitude, "longitude": r.location.longitude,
                             "north": r.location.north, "utcOffset": r.location.offset},
                "rooms": [{"id": room.id, "name": room.name} for room in r.rooms],
                "points": int(len(r.points)),
                "times": r.times,
                "timeIndex": self.time_index,
                "settings": asdict(r.settings),
            })
        return state
