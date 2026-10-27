"""Build the Archicad MCP installer for the current platform.

    python packaging/build.py

Steps: build the app folder (app.spec), zip it as the setup payload, build the
one-file setup (setup.spec), then copy the result to dist/release/:
    Windows: ArchicadMCPSetup.exe
    macOS:   ArchicadMCPSetup-macOS.zip (contains "Archicad MCP Setup.app")
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
RELEASE = DIST / "release"


def pyinstaller(spec: str, workpath: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(DIST), "--workpath", str(BUILD / workpath), str(ROOT / "packaging" / spec)],
        check=True,
    )


def zip_folder(folder: Path, archive: Path) -> None:
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(folder))


def main() -> None:
    shutil.rmtree(DIST, ignore_errors=True)
    (BUILD / "payload").mkdir(parents=True, exist_ok=True)
    RELEASE.mkdir(parents=True, exist_ok=True)
    payload = BUILD / "payload" / "app.zip"
    payload.unlink(missing_ok=True)

    pyinstaller("app.spec", "app")
    if sys.platform == "darwin":
        # ditto keeps the bundle's symlinks and code signature; the installer unpacks it the same way.
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(DIST / "Archicad MCP.app"), str(payload)], check=True)
    else:
        zip_folder(DIST / "Archicad MCP", payload)
    print(f"payload: {payload.stat().st_size / 1e6:.1f} MB")

    pyinstaller("setup.spec", "setup")
    if sys.platform == "win32":
        shutil.copy2(DIST / "ArchicadMCPSetup.exe", RELEASE / "ArchicadMCPSetup.exe")
    elif sys.platform == "darwin":
        subprocess.run(["ditto", "-c", "-k", "--keepParent", str(DIST / "Archicad MCP Setup.app"),
                        str(RELEASE / "ArchicadMCPSetup-macOS.zip")], check=True)
    else:
        shutil.copy2(DIST / "ArchicadMCPSetup", RELEASE / "ArchicadMCPSetup-linux")
    for path in RELEASE.iterdir():
        print(f"built {path} ({path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
