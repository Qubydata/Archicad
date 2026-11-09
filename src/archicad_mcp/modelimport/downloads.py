"""New 3D models in the Downloads folder, so the panel can offer "New model downloaded: X, import it?"."""

from __future__ import annotations

import sys
import time
import zipfile
from pathlib import Path

from .reader import FORMATS, SUPPORTED_EXTENSIONS

WATCHED = tuple(SUPPORTED_EXTENSIONS) + (".skp",)  # SketchUp too, to point the user at Archicad's own import
SETTLE_SECONDS = 2.0  # a file changed more recently than this may still be downloading


def downloads_folder(home: Path | None = None) -> Path:
    """The user's Downloads folder (the real one on Windows, also when it was moved)."""
    if sys.platform == "win32" and home is None:
        known = _windows_downloads_folder()
        if known:
            return known
    return (home or Path.home()) / "Downloads"


def _windows_downloads_folder() -> Path | None:
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                        ("Data4", wintypes.BYTE * 8)]

        tail = (wintypes.BYTE * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B)
        downloads = GUID(0x374DE290, 0x123F, 0x4565, tail)  # FOLDERID_Downloads
        path = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(downloads), 0, None, ctypes.byref(path)) == 0:
            result = Path(path.value)
            ctypes.windll.ole32.CoTaskMemFree(path)
            return result
    except Exception:
        pass
    return None


def _zip_models(path: Path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            return [n for n in archive.namelist() if Path(n).suffix.lower() in FORMATS or n.lower().endswith(".skp")]
    except (zipfile.BadZipFile, OSError):
        return []


def new_downloads(since: float, folder: str | Path | None = None, now: float | None = None,
                  settle: float = SETTLE_SECONDS) -> list[dict]:
    """3D model files (and ZIPs holding one) that appeared in the folder after ``since`` (a Unix time), newest first.

    Files still being written (changed in the last ``settle`` seconds) are left
    for the next call. ZIPs without a model inside are skipped.
    """
    folder = Path(folder) if folder else downloads_folder()
    now = time.time() if now is None else now
    found = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    for entry in entries:
        ext = entry.suffix.lower()
        if ext not in WATCHED or entry.name.startswith("."):
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        if not entry.is_file() or stat.st_mtime <= since or stat.st_mtime > now - settle:
            continue
        item = {"path": str(entry), "name": entry.name, "size": stat.st_size, "modified": stat.st_mtime,
                "format": FORMATS.get(ext, {"zip": "ZIP", "skp": "SketchUp"}.get(ext[1:], ext[1:].upper())),
                "readable": ext != ".skp"}
        if ext == ".zip":
            models = _zip_models(entry)
            if not models:
                continue
            item["contains"] = [Path(m).name for m in models]
            item["readable"] = any(not m.lower().endswith(".skp") for m in models)
        found.append(item)
    return sorted(found, key=lambda i: -i["modified"])
