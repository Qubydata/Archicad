# PyInstaller spec of the one-file setup program (ArchicadMCPSetup.exe / "Archicad MCP Setup.app").
#
# It carries the built app as payload/app.zip, made by packaging/build.py
# before this spec runs.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
PAYLOAD = ROOT / "build" / "payload" / "app.zip"
ICON = str(ROOT / "packaging" / "icons" / ("archicad-mcp.icns" if sys.platform == "darwin" else "archicad-mcp.ico"))

if not PAYLOAD.exists():
    raise SystemExit(f"{PAYLOAD} is missing: run python packaging/build.py")

analysis = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    datas=collect_data_files("archicad_mcp") + [(str(PAYLOAD), "payload")],
    hiddenimports=collect_submodules("archicad_mcp.installer"),
)
exe = EXE(
    PYZ(analysis.pure), analysis.scripts, analysis.binaries, analysis.datas, [],
    name="ArchicadMCPSetup", console=False, icon=ICON, upx=False,
    uac_admin=False,  # elevation is asked only for the Tapir add-on step
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="Archicad MCP Setup.app",
        icon=ICON,
        bundle_identifier="com.archicadmcp.setup",
        info_plist={"CFBundleName": "Archicad MCP Setup", "CFBundleShortVersionString": "0.1.0", "NSHighResolutionCapable": True},
    )
