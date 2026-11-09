"""Model Import in the open Archicad project.

:class:`ModelImport` is what the panel and the MCP tools call: open a
downloaded model, clean it up, pick a polygon level, and save it as a library
object (.gsm) into the shared Tevavid Library or the project's embedded
library, optionally placing it in the model. Without LP_XMLConverter the
model is placed as Morph elements instead, and the result says so.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np

from ..connection import ArchicadConnection, ArchicadError
from . import library
from .cleanup import SMALL_PART_FRACTION, ImportSession
from .downloads import new_downloads
from .errors import ModelImportError
from .gdl import library_part_guid, library_part_xml, safe_file_name
from .preview import render_preview
from .reader import read_model
from .simplify import budget

log = logging.getLogger("archicad_mcp.modelimport")

TARGETS = ("shared", "embedded")
PREVIEW_TRIANGLES = 30_000  # the preview is drawn from the first level at or below this
MORPH_FACES = 3000  # faces per Morph in the fallback
MORPHS_PER_CALL = 4

Progress = Callable[[float, str], None]


def _quiet(fraction: float, message: str) -> None:
    pass


class ModelImport:
    def __init__(self, connection: ArchicadConnection | None, library_root: str | Path | None = None,
                 work_dir: str | Path | None = None, system=None,
                 converter: str | Path | Sequence[str] | None = None) -> None:
        """``converter`` overrides the LP_XMLConverter search (a path or a command line, for tests)."""
        self.connection = connection
        self.system = system
        self.library_root = Path(library_root) if library_root else library.default_library_root(system)
        self.work_dir = Path(work_dir) if work_dir else Path(tempfile.gettempdir()) / "tevavid-model-import"
        self.converter_override = converter
        self.session: ImportSession | None = None
        self.lock = threading.RLock()

    # --- the model -------------------------------------------------------------

    def _session(self) -> ImportSession:
        if self.session is None:
            raise ModelImportError("Open a model first.")
        return self.session

    def open(self, path: str | Path, unit: str | None = None, up_axis: str | None = None,
             placements: int = 1, progress: Progress | None = None) -> dict[str, Any]:
        """Read a model file (or a ZIP with one) and compute its polygon levels."""
        report = progress or _quiet
        with self.lock:
            started = time.time()
            report(0.0, f"Reading {Path(path).name}")
            model = read_model(path)
            try:
                session = ImportSession(model, unit=unit, up_axis=up_axis)
                report(0.3, "Reducing polygons")
                session.levels.compute(lambda f, message: report(0.3 + 0.65 * f, message))
            except BaseException:
                model.close()
                raise
            self.close()
            self.session = session
            report(1.0, "Done")
            log.info("Model import: %s, %d parts, %d triangles, %.1f s", model.path.name, len(model.parts),
                     model.triangles, time.time() - started)
            return self.state(placements)

    def close(self) -> dict[str, Any]:
        with self.lock:
            if self.session is not None:
                self.session.model.close()
            self.session = None
            return self.state()

    def state(self, placements: int = 1) -> dict[str, Any]:
        with self.lock:
            base = {"hasModel": self.session is not None, "licenseReminder": library.LICENSE_REMINDER,
                    "libraryRoot": str(self.library_root), "suggestedFolders": list(library.SUGGESTED_FOLDERS)}
            if self.session is None:
                return base
            return {**base, **self.session.summary(placements)}

    def _edit(self, change: Callable[[ImportSession], Any], placements: int = 1, key: str = "changed") -> dict:
        with self.lock:
            result = change(self._session())
            return {**self.state(placements), key: result}

    def delete_parts(self, ids, placements: int = 1) -> dict[str, Any]:
        return self._edit(lambda s: s.delete_parts(ids), placements, "deleted")

    def restore_parts(self, ids=None, placements: int = 1) -> dict[str, Any]:
        return self._edit(lambda s: s.restore_parts(ids), placements, "restored")

    def small_parts(self, fraction: float = SMALL_PART_FRACTION) -> list[str]:
        """The parts "remove small parts" would delete, to highlight them first."""
        with self.lock:
            return self._session().small_parts(fraction)

    def remove_small_parts(self, fraction: float = SMALL_PART_FRACTION, placements: int = 1) -> dict[str, Any]:
        return self._edit(lambda s: s.remove_small_parts(fraction), placements, "deleted")

    def undo(self, placements: int = 1) -> dict[str, Any]:
        return self._edit(lambda s: s.undo(), placements, "undone")

    def set_unit(self, unit: str, placements: int = 1) -> dict[str, Any]:
        return self._edit(lambda s: s.set_unit(unit), placements)

    def set_up_axis(self, axis: str, placements: int = 1) -> dict[str, Any]:
        return self._edit(lambda s: s.set_up_axis(axis), placements)

    def set_turn(self, degrees: float, placements: int = 1) -> dict[str, Any]:
        return self._edit(lambda s: s.set_turn(degrees), placements)

    def set_level(self, level: int, placements: int = 1) -> dict[str, Any]:
        return self._edit(lambda s: s.set_level(level), placements, "level")

    def budget(self, placements: int = 1, level: int | None = None) -> dict[str, Any]:
        with self.lock:
            return budget(self._session().triangles(level), placements)

    def preview(self, size: int = 256, level: int | None = None) -> bytes:
        """A PNG of the model as it would be saved (the current level unless given)."""
        with self.lock:
            session = self._session()
            level = self._preview_level(session, session.level if level is None else level)
            return render_preview(session.meshes(level), session.model.materials, size)

    @staticmethod
    def _preview_level(session: ImportSession, level: int) -> int:
        for candidate in range(level, len(session.levels)):
            if session.triangles(candidate) <= PREVIEW_TRIANGLES:
                return candidate
        return len(session.levels) - 1

    # --- Downloads ---------------------------------------------------------------

    def new_downloads(self, since: float, folder: str | Path | None = None) -> list[dict]:
        return new_downloads(since, folder)

    # --- library folders -----------------------------------------------------------

    def _embedded_root(self) -> Path:
        answer = self._tapir("GetSpecialFolders", {"folderTypes": ["EmbeddedProjectLibrary"]})
        paths = answer.get("folderPaths") or []
        if not paths or "path" not in paths[0]:
            raise ModelImportError("Archicad did not say where the project's embedded library is.")
        return Path(paths[0]["path"])

    def library_tree(self, target: str = "shared") -> dict:
        """The folders (and objects) of the shared library or of the project's embedded library."""
        self._check_target(target)
        if target == "embedded":
            return {**library.folder_tree(self._embedded_root(), create=False), "target": target}
        return {**library.folder_tree(self.library_root), "target": target}

    def create_folder(self, folder: str, target: str = "shared") -> dict:
        self._check_target(target)
        if target == "embedded":
            relative = library.clean_folder(folder)
            if not relative:
                raise ModelImportError("Give the folder a name.")
            self._check_result(self._tapir("CreateFolderInEmbeddedLibrary", {"folderPath": relative}),
                               f"create {relative} in the embedded library")
            return {"folder": relative, "target": target}
        return {**library.create_folder(self.library_root, folder), "target": target}

    @staticmethod
    def _check_target(target: str) -> None:
        if target not in TARGETS:
            raise ModelImportError(f"Unknown library {target}; use shared or embedded.")

    # --- Archicad --------------------------------------------------------------

    def _tapir(self, command: str, parameters: dict | None = None) -> dict:
        if self.connection is None:
            raise ModelImportError("Archicad is not connected.")
        return self.connection.tapir(command, parameters)

    @staticmethod
    def _check_result(result: dict, what: str) -> None:
        if isinstance(result, dict) and result.get("success") is False:
            error = result.get("error") or {}
            raise ModelImportError(f"Archicad could not {what}: {error.get('message', 'unknown error')}")

    def find_converter(self) -> str | Path | Sequence[str] | None:
        """LP_XMLConverter of the running Archicad (or an installed one), or None."""
        if self.converter_override is not None:
            return self.converter_override
        location, version = None, None
        if self.connection is not None:
            try:
                location = self.connection.tapir("GetArchicadLocation").get("archicadLocation")
                version = self.connection.command("API.GetProductInfo").get("version")
            except ArchicadError as exc:
                log.info("Could not ask Archicad where it is: %s", exc)
        return library.find_converter(location, version, self.system)

    def converter_status(self) -> dict:
        converter = self.find_converter()
        if converter is None:
            return {"found": False, "path": None, "message": (
                "Archicad's LP_XMLConverter was not found, so models will be placed as Morph elements "
                "instead of library objects.")}
        shown = converter if isinstance(converter, (str, Path)) else " ".join(map(str, converter))
        return {"found": True, "path": str(shown), "message": "Models are saved as Archicad library objects."}

    def _position(self, position: dict | Sequence[float] | None, pick_point: bool, name: str) -> dict:
        if pick_point:
            prompt = f"Click where to place {name}".encode("ascii", "replace").decode("ascii")
            try:
                clicked = self._tapir("GetPointFromUser", {"prompt": prompt})["position"]
            except ArchicadError as exc:
                raise ModelImportError(f"No point was picked ({exc}).") from exc
            return {k: float(clicked.get(k, 0.0)) for k in "xyz"}
        if position is None:
            return {"x": 0.0, "y": 0.0, "z": 0.0}
        if isinstance(position, dict):
            return {k: float(position.get(k, 0.0)) for k in "xyz"}
        values = [float(v) for v in position] + [0.0] * 3
        return {"x": values[0], "y": values[1], "z": values[2]}

    def _register_shared_library(self, notes: list[str]) -> bool:
        """Make sure the shared library is loaded in the project, and reload it so the new object shows."""
        try:
            loaded = self._tapir("GetLibraries").get("libraries", [])
            if not any(_same_path(lib.get("path", ""), self.library_root) for lib in loaded):
                self._check_result(self._tapir("AddLibraries", {"libraries": [{"path": str(self.library_root)}]}),
                                   "add the Tevavid Library to the project")
                notes.append(f"Added {self.library_root} to the project's libraries.")
            else:
                self._tapir("ReloadLibraries")
            return True
        except (ArchicadError, ModelImportError) as exc:
            notes.append(f"Saved, but it could not be added to the open project ({exc}). Add the folder "
                         f"{self.library_root} with File > Libraries and Objects > Library Manager.")
            return False

    def place(self, name: str, position=None, pick_point: bool = False, size=None) -> str:
        """Place a library object in the model; returns the new element's GUID."""
        where = self._position(position, pick_point, name)
        data: dict[str, Any] = {"libraryPartName": name, "coordinates": where}
        if size is not None:
            data["dimensions"] = {"x": float(size[0]), "y": float(size[1]), "z": float(size[2])}
        created = self._tapir("CreateObjects", {"objectsData": [data]}).get("elements", [])
        if not created or "elementId" not in created[0]:
            error = (created[0].get("error") if created else None) or {}
            reason = error.get("message", "no element was created")
            raise ModelImportError(f"Archicad could not place {name}: {reason}. Is the library loaded in this project?")
        return created[0]["elementId"]["guid"]

    # --- saving ----------------------------------------------------------------

    def export_xml(self, name: str | None = None, folder: str = "", target: str = "shared",
                   out_dir: str | Path | None = None, placements: int = 1,
                   progress: Progress | None = None) -> dict[str, Any]:
        """Write the library part's XML and preview PNG (``<out_dir>/<name>.xml``, ``<out_dir>/images/<name>.png``).

        This is the first half of :meth:`save`; on its own it gives the files for
        a manual ``LP_XMLConverter xml2libpart -img <out_dir>/images ...``.
        """
        report = progress or _quiet
        self._check_target(target)
        with self.lock:
            session = self._session()
            file_name = safe_file_name(name or session.model.name)
            relative = library.clean_folder(folder)
            report(0.05, "Preparing the geometry")
            meshes = session.meshes()
            triangles = sum(len(m.faces) for m in meshes)
            size = session.size()

            report(0.15, "Drawing the preview")
            xml_dir = Path(out_dir) if out_dir else self.work_dir / "xml"
            images = xml_dir / "images"
            images.mkdir(parents=True, exist_ok=True)
            preview_file = images / f"{file_name}.png"
            preview_file.write_bytes(render_preview(session.meshes(self._preview_level(session, session.level)),
                                                    session.model.materials, 256))

            report(0.3, "Writing the GDL library part")
            guid = library_part_guid(f"{target}:{relative}/{file_name}")
            source = Path(session.model.archive or session.model.path).name
            xml_text = library_part_xml(file_name, meshes, session.model.materials, guid=guid,
                                        preview=preview_file.name, source=source)
            xml_file = xml_dir / f"{file_name}.xml"
            xml_file.write_text(xml_text, encoding="utf8")
            return {
                "name": file_name, "target": target, "folder": relative, "triangles": triangles,
                "size": {"x": size[0], "y": size[1], "z": size[2]}, "budget": budget(triangles, placements),
                "guid": guid, "xml": str(xml_file), "images": str(images), "preview": str(preview_file),
                "notes": [], "licenseReminder": library.LICENSE_REMINDER, "placed": None,
            }

    def save(self, name: str | None = None, folder: str = "", target: str = "shared", place: bool = False,
             position=None, pick_point: bool = False, placements: int = 1,
             progress: Progress | None = None) -> dict[str, Any]:
        """Save the cleaned-up model as a library object and optionally place it.

        Without LP_XMLConverter the model is placed as Morph elements instead
        (``mode`` is then ``"morphs"``); the XML is kept for a later conversion.
        """
        report = progress or _quiet
        with self.lock:
            result = self.export_xml(name, folder, target, placements=placements, progress=report)
            session = self._session()
            file_name, relative, notes = result["name"], result["folder"], result["notes"]
            xml_file, images = Path(result["xml"]), Path(result["images"])
            meshes = session.meshes()
            size = session.size()

            converter = self.find_converter()
            if converter is None and self.connection is None:
                raise ModelImportError("Archicad's LP_XMLConverter was not found and Archicad is not connected, "
                                       f"so no library object could be made. The GDL XML is in {xml_file}.")
            if converter is None:
                report(0.4, "LP_XMLConverter not found: placing Morph elements")
                return {**result, **self._place_morphs(session, file_name, meshes, position, pick_point, report)}

            report(0.4, "Converting to a .gsm library part")
            if target == "shared":
                gsm = library.object_path(self.library_root, relative, file_name)
            else:
                gsm = xml_file.parent / "gsm" / f"{file_name}.gsm"
            conversion = library.convert(converter, xml_file, gsm, images)
            result.update(mode="library", file=str(gsm), converter=conversion)

            report(0.8, "Adding it to the library")
            if target == "shared":
                result["libraryLoaded"] = self._register_shared_library(notes) if self.connection else False
            else:
                self._add_to_embedded(gsm, relative, file_name)
                result["libraryLoaded"] = True
                result["file"] = f"{relative}/{file_name}.gsm" if relative else f"{file_name}.gsm"
            if place:
                report(0.9, "Placing it in the model")
                result["placed"] = self.place(file_name, position, pick_point, size)
            report(1.0, "Done")
            return result

    def _add_to_embedded(self, gsm: Path, folder: str, file_name: str) -> None:
        if folder:
            self._check_result(self._tapir("CreateFolderInEmbeddedLibrary", {"folderPath": folder}),
                               f"create {folder} in the embedded library")
        output = f"{folder}/{file_name}.gsm" if folder else f"{file_name}.gsm"
        files = [{"inputPath": str(gsm), "outputPath": output, "type": "Object"}]
        try:
            answer = self._tapir("AddFilesToEmbeddedLibrary", {"files": files, "overwriteExisting": True})
        except ArchicadError:  # overwriteExisting needs Archicad 27
            answer = self._tapir("AddFilesToEmbeddedLibrary", {"files": files})
        results = answer.get("executionResults") or [{}]
        self._check_result(results[0], f"add {file_name} to the embedded library")

    # --- the Morph fallback --------------------------------------------------------

    def _surfaces(self, session: ImportSession, file_name: str) -> dict[str, dict]:
        """One Surface attribute per material, named after the object; material id -> attribute id."""
        used = session.used_materials()
        materials = [session.model.materials[m] for m in used]
        data = [{"name": f"{file_name} - {m.name}"[:120],
                 "surfaceColor": {"red": m.color[0], "green": m.color[1], "blue": m.color[2]},
                 "transparency": round((1.0 - m.opacity) * 100.0, 1)} for m in materials]
        try:
            answer = self._tapir("CreateSurfaces", {"surfaceDataArray": data, "overwriteExisting": True})
        except ArchicadError as exc:
            log.info("Could not create surfaces: %s", exc)
            return {}
        return {mid: item["attributeId"] for mid, item in zip(used, answer.get("attributeIds", []))
                if "attributeId" in item}

    def _place_morphs(self, session: ImportSession, file_name: str, meshes, position, pick_point: bool,
                      report: Progress) -> dict:
        where = self._position(position, pick_point, file_name)
        surfaces = self._surfaces(session, file_name)
        morphs = []
        for mesh in meshes:
            surface = surfaces.get(mesh.material)
            for start in range(0, len(mesh.faces), MORPH_FACES):
                morphs.append(_morph(mesh.vertices, mesh.faces[start:start + MORPH_FACES], where, surface))
        created: list[str] = []
        failed = 0
        for start in range(0, len(morphs), MORPHS_PER_CALL):
            answer = self._tapir("CreateMorphs", {"morphsData": morphs[start:start + MORPHS_PER_CALL]})
            for item in answer.get("elements", []):
                if "elementId" in item:
                    created.append(item["elementId"]["guid"])
                else:
                    failed += 1
            report(0.4 + 0.55 * min(1.0, (start + MORPHS_PER_CALL) / len(morphs)),
                   f"Placing Morph elements ({len(created)} of {len(morphs)})")
        notes = [f"Archicad's LP_XMLConverter was not found, so {file_name} was placed as {len(created)} Morph "
                 "elements, not as a library object. They are not in any library. Install or repair Archicad "
                 "(LP_XMLConverter comes with it) to save library objects."]
        if failed:
            notes.append(f"{failed} of {len(morphs)} Morph elements could not be created.")
        report(1.0, "Done")
        return {"mode": "morphs", "morphs": created, "placed": created[0] if created else None,
                "position": where, "notes": notes}


def _same_path(a, b) -> bool:
    def norm(p) -> str:
        return os.path.normcase(os.path.abspath(str(p))).rstrip("\\/")

    return bool(str(a)) and norm(a) == norm(b)


def _morph(vertices: np.ndarray, faces: np.ndarray, where: dict, surface: dict | None) -> dict:
    used, inverse = np.unique(faces, return_inverse=True)
    local = vertices[used]
    inverse = inverse.reshape(-1, 3)
    edges = np.sort(np.concatenate([inverse[:, [0, 1]], inverse[:, [1, 2]], inverse[:, [2, 0]]]), axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    polygon_extra = {"surfaceId": surface} if surface else {}
    body = {
        "bodyType": "Solid" if (counts == 2).all() else "Surface",
        "edgeDefault": "HardHidden",  # a triangulated body shows no triangle edges (Archicad 29 and later)
        "vertices": [{"x": float(x), "y": float(y), "z": float(z)} for x, y, z in local],
        "polygons": [{"vertexIds": [int(a), int(b), int(c)], **polygon_extra} for a, b, c in inverse],
    }
    data = {"basePoint": where, "body": body}
    if surface:
        data["surfaceId"] = surface
    return data
