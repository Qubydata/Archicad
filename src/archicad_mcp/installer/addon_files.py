"""Putting the Tevavid add-on into Archicad's Add-Ons folders, and taking files out.

Archicad's folders (C:\\Program Files\\Graphisoft\\..., /Applications/GRAPHISOFT/...)
usually need administrator rights, so setup writes a small job file and runs
itself once with those rights to carry it out (`setup --addon-files <job>`),
which asks the user only once for all Archicad versions. The job's result is
written next to it, so the setup that started it can tell what worked.

A job is JSON: {"copy": [[source, destination], ...], "remove": [path, ...]}.
A source ending in .zip is a zipped macOS bundle and is unpacked with ditto.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger("archicad_mcp.installer")

SUBFOLDER = "Tevavid"
TAPIR_SUBFOLDER = "Tapir"
# The Add-Ons folder of each Archicad language version (from the Tapir installer).
LOCALIZED_ADDONS_FOLDERS = ["Add-Ons", "Extensões", "Doplnky ArchiCADu", "ArchiCAD-laajennukset", "Extensions",
                            "Kiegészítök", "Kiegészítők", "애드온", "Dodatki", "Расширения ArchiCAD",
                            "Extensiones ArchiCAD", "Add-On'lar"]


def addons_folder(archicad_folder: str | Path) -> Path:
    """The Add-Ons folder of an Archicad installation, whatever its language."""
    folder = Path(archicad_folder)
    known = {name.casefold() for name in LOCALIZED_ADDONS_FOLDERS}
    try:
        for entry in sorted(folder.iterdir()):
            if entry.is_dir() and entry.name.casefold() in known:
                return entry
    except OSError:
        pass
    return folder / "Add-Ons"


def asset_name(version: int, platform: str = sys.platform) -> str:
    return f"TevavidAddOn_AC{version}_Mac.zip" if platform == "darwin" else f"TevavidAddOn_AC{version}_Win.apx"


def installed_name(version: int, platform: str = sys.platform) -> str:
    """The add-on's name in the Add-Ons folder (the macOS bundle is unpacked)."""
    return f"TevavidAddOn_AC{version}_Mac.bundle" if platform == "darwin" else f"TevavidAddOn_AC{version}_Win.apx"


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix == ".zip":  # a zipped macOS bundle: ditto keeps its symlinks and signature
        if destination.exists():
            shutil.rmtree(destination)
        if subprocess.run(["ditto", "-x", "-k", str(source), str(destination.parent)]).returncode != 0:
            raise OSError(f"could not unpack {source.name}")
    else:
        shutil.copyfile(source, destination)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def apply(job: dict[str, Any]) -> dict[str, Any]:
    """Carry out a job; returns {"done": [...], "failed": {path: error}} for its destinations and removals."""
    done: list[str] = []
    failed: dict[str, str] = {}
    for source, destination in job.get("copy", []):
        try:
            _copy(Path(source), Path(destination))
            done.append(destination)
        except OSError as exc:
            failed[destination] = exc.strerror or str(exc)
    for path in job.get("remove", []):
        try:
            _remove(Path(path))
            done.append(path)
        except OSError as exc:
            failed[path] = exc.strerror or str(exc)
    return {"done": done, "failed": failed}


def run_job_file(path: str) -> int:
    """Entry point of `setup --addon-files <job>`: does the job and writes <job>.result."""
    job = json.loads(Path(path).read_text(encoding="utf8"))
    result = apply(job)
    Path(path + ".result").write_text(json.dumps(result), encoding="utf8")
    return 0 if not result["failed"] else 1


def self_command(*args: str) -> list[str]:
    """This program's command line with the given setup arguments (the setup and the app both take them)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "setup", *args]
    return [sys.executable, "-m", "archicad_mcp.launcher", "setup", *args]


def carry_out(system, job: dict[str, Any], work: Path) -> dict[str, Any]:
    """Do a job as this user where that is allowed, the rest once with administrator rights."""
    if not job.get("copy") and not job.get("remove"):
        return {"done": [], "failed": {}}
    result = apply(job)
    if not result["failed"]:
        return result
    denied = set(result["failed"])
    retry = {"copy": [pair for pair in job.get("copy", []) if pair[1] in denied],
             "remove": [p for p in job.get("remove", []) if p in denied]}
    work.mkdir(parents=True, exist_ok=True)
    job_file = work / "addon-job.json"
    job_file.write_text(json.dumps(retry), encoding="utf8")
    try:
        system.run_elevated(self_command("--addon-files", str(job_file)))
        elevated = json.loads(Path(str(job_file) + ".result").read_text(encoding="utf8"))
    except Exception as exc:  # declined, or the elevated run failed: keep the first errors
        log.warning("Could not change the Add-Ons folders with administrator rights: %s", exc)
        return result
    finally:
        job_file.unlink(missing_ok=True)
    return {"done": result["done"] + elevated["done"], "failed": elevated["failed"]}


def describe(failed: dict[str, str]) -> str:
    files = ", ".join(os.path.basename(p) or p for p in failed)
    return (f"Could not change {files} in Archicad's Add-Ons folder ({next(iter(failed.values()))}). "
            "Close Archicad, then run setup again.")
