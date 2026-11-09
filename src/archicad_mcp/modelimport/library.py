"""Library folders, and Archicad's LP_XMLConverter that turns the XML into a .gsm library part.

Imported objects go into a library folder the user's projects can all load,
``Documents/Tevavid Library`` by default, in sub-folders the user picks or
creates (``Furniture/Tables``). LP_XMLConverter ships with every Archicad:

* Windows: ``LP_XMLConverter.exe`` in the Archicad folder
* macOS: ``Archicad NN.app/Contents/MacOS/LP_XMLConverter.app/Contents/MacOS/LP_XMLConverter``

It is run as ``LP_XMLConverter xml2libpart -img <image folder> <xml> <gsm>``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath

from .errors import ModelImportError

LIBRARY_FOLDER_NAME = "Tevavid Library"
LICENSE_REMINDER = ("Check the model's license (e.g. D5 Works Personal vs Project license) "
                    "before using it in client work.")
SUGGESTED_FOLDERS = ("Furniture/Tables", "Furniture/Chairs", "Furniture/Sofas", "Furniture/Beds",
                     "Furniture/Storage", "Kitchen", "Bathroom", "Lighting", "Decoration", "Plants", "Office")
CONVERTER_ENV = "TEVAVID_LP_XMLCONVERTER"
CONVERT_TIMEOUT = 300.0
MAX_DEPTH = 8


class ConversionError(ModelImportError):
    """LP_XMLConverter failed; the message carries its output."""


# --- folders ------------------------------------------------------------------

def default_library_root(system=None) -> Path:
    """``Documents/Tevavid Library`` (the real Documents folder, also when Windows redirects it)."""
    if system is None:
        from ..installer.system import System

        system = System()
    return Path(system.documents_dir) / LIBRARY_FOLDER_NAME


def clean_folder(folder: str | None) -> str:
    """A library sub-folder as ``A/B``: accepts ``A > B``, ``A\\B`` or ``A/B``; refuses ``..`` and absolute paths."""
    text = str(folder or "").replace("\\", "/").replace(">", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise ModelImportError(f"{folder}: give a folder inside the library, like Furniture/Tables.")
    parts = [re.sub(r"\s+", " ", p).strip() for p in text.split("/")]
    parts = [p for p in parts if p]
    for part in parts:
        if part in (".", "..") or re.search(r'[<>:"|?*\x00-\x1f]', part) or part.endswith("."):
            raise ModelImportError(f"{part!r} cannot be a folder name.")
    if len(parts) > MAX_DEPTH:
        raise ModelImportError(f"Folders can be at most {MAX_DEPTH} levels deep.")
    return "/".join(parts)


def create_folder(root: str | Path, folder: str) -> dict:
    """Create a library sub-folder (and the library itself) if missing."""
    relative = clean_folder(folder)
    if not relative:
        raise ModelImportError("Give the folder a name.")
    path = Path(root) / relative
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ModelImportError(f"Could not create {path}: {exc}") from exc
    return {"folder": relative, "path": str(path)}


def folder_tree(root: str | Path, create: bool = True) -> dict:
    """The library's folders and the objects in each, for a folder picker."""
    root = Path(root)
    if create:
        root.mkdir(parents=True, exist_ok=True)

    def walk(path: Path, relative: str, depth: int) -> dict:
        folders, objects = [], []
        try:
            entries = sorted(path.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            entries = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir() and depth < MAX_DEPTH:
                folders.append(walk(entry, f"{relative}/{entry.name}" if relative else entry.name, depth + 1))
            elif entry.suffix.lower() == ".gsm":
                objects.append(entry.stem)
        return {"name": path.name, "folder": relative, "folders": folders, "objects": objects}

    tree = walk(root, "", 0)
    tree["path"] = str(root)
    return tree


def object_path(root: str | Path, folder: str, file_name: str) -> Path:
    relative = clean_folder(folder)
    return (Path(root) / relative if relative else Path(root)) / f"{file_name}.gsm"


# --- LP_XMLConverter ------------------------------------------------------------

def _converter_in_app(app: Path) -> Path:
    return app / "Contents" / "MacOS" / "LP_XMLConverter.app" / "Contents" / "MacOS" / "LP_XMLConverter"


def converter_candidates(location: str | Path | None, platform: str | None = None) -> list[Path]:
    """Where LP_XMLConverter is for an Archicad at ``location`` (its executable, app bundle or folder)."""
    if not location:
        return []
    platform = platform or sys.platform
    flavour = PureWindowsPath if platform == "win32" else PurePosixPath
    path = flavour(str(location))
    candidates = []
    app = next((flavour(*path.parts[:i + 1]) for i, part in enumerate(path.parts) if part.lower().endswith(".app")),
               None)
    if app is not None:
        candidates.append(_converter_in_app(app))
        folder = app.parent
    elif path.suffix.lower() == ".exe" or (path.name.lower().startswith("archicad") and not Path(str(path)).is_dir()):
        folder = path.parent
    else:
        folder = path
    if platform == "darwin":
        candidates += [_converter_in_app(app) for app in sorted(Path(str(folder)).glob("Archicad*.app"))]
    candidates += [folder / "LP_XMLConverter.exe", folder / "LP_XMLConverter"]
    return [Path(str(c)) for c in dict.fromkeys(candidates)]


def find_converter(archicad_location: str | Path | None = None, version: int | None = None, system=None) -> Path | None:
    """LP_XMLConverter: from ``TEVAVID_LP_XMLCONVERTER``, the running Archicad, or an installed Archicad.

    A converter of the same Archicad version as the project is preferred: a
    newer one writes library parts the older Archicad cannot open.
    """
    override = os.environ.get(CONVERTER_ENV)
    if override and Path(override).is_file():
        return Path(override)
    platform = getattr(system, "platform", None) or sys.platform
    for candidate in converter_candidates(archicad_location, platform):
        if candidate.is_file():
            return candidate
    if system is None:
        from ..installer.system import System

        system = System()
    installs = system.detect_archicad()
    if version is not None:
        installs.sort(key=lambda i: (i.version != version, -i.version))
    for install in installs:
        for candidate in converter_candidates(install.path, platform):
            if candidate.is_file():
                return candidate
    return None


def convert(converter: str | Path | Sequence[str], xml: Path, gsm: Path, images: Path | None = None,
            timeout: float = CONVERT_TIMEOUT) -> dict:
    """Run ``xml2libpart``.

    The converter writes into a temporary folder under the final file name (the
    file name is the library part's name); the .gsm is moved into the library
    only when the conversion worked, so a failure keeps the previous version.
    """
    command = [str(converter)] if isinstance(converter, (str, Path)) else [str(c) for c in converter]
    gsm = Path(gsm)
    kwargs = {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # CREATE_NO_WINDOW
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="tevavid-gsm-") as work:
        written = Path(work) / gsm.name
        command += ["xml2libpart"] + (["-img", str(images)] if images else []) + [str(xml), str(written)]
        try:
            done = subprocess.run(command, capture_output=True, timeout=timeout, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise ConversionError(f"LP_XMLConverter did not finish within {timeout:g} s.") from exc
        except OSError as exc:
            raise ConversionError(f"Could not run LP_XMLConverter ({command[0]}): {exc}") from exc
        output = (done.stdout or b"").decode("utf8", "replace") + (done.stderr or b"").decode("utf8", "replace")
        if done.returncode != 0 or not written.is_file() or written.stat().st_size == 0:
            detail = output.strip()[-1500:] or f"exit code {done.returncode}"
            raise ConversionError(f"LP_XMLConverter could not make the library part: {detail}")
        try:
            gsm.parent.mkdir(parents=True, exist_ok=True)
            staging = gsm.with_name(f".{gsm.name}.new")
            shutil.copyfile(written, staging)
            os.replace(staging, gsm)
        except OSError as exc:
            raise ConversionError(f"Could not write {gsm}: {exc}") from exc
    return {"command": command[:-1] + [str(gsm)], "output": output.strip(), "seconds": round(time.time() - started, 2)}
