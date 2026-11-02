# PyInstaller spec of the installed Tevavid Tools app.
#
# Two executables share one folder:
#   TevavidTools          windowed: the panel service, background mode and uninstaller
#   tevavid-mcp-server  console:  the stdio MCP server that Claude Desktop starts
# On macOS the folder is wrapped into "Tevavid Tools.app".
#
# Build with:  python packaging/build.py

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
ENTRY = str(ROOT / "packaging" / "entry.py")
ICON = str(ROOT / "packaging" / "icons" / ("tevavid-tools.icns" if sys.platform == "darwin" else "tevavid-tools.ico"))

datas = collect_data_files("archicad_mcp")
hidden = collect_submodules("archicad_mcp") + collect_submodules("uvicorn")


def analysis():
    return Analysis([ENTRY], pathex=[str(ROOT / "src")], datas=datas, hiddenimports=hidden, noarchive=False)


app_analysis = analysis()
server_analysis = analysis()

app_exe = EXE(
    PYZ(app_analysis.pure), app_analysis.scripts, [],
    exclude_binaries=True, name="TevavidTools", console=False, icon=ICON,
    upx=False,
)
server_exe = EXE(
    PYZ(server_analysis.pure), server_analysis.scripts, [],
    exclude_binaries=True, name="tevavid-mcp-server", console=True, icon=ICON,
    upx=False,
)
folder = COLLECT(
    app_exe, app_analysis.binaries, app_analysis.datas,
    server_exe, server_analysis.binaries, server_analysis.datas,
    name="Tevavid Tools", upx=False,
)

if sys.platform == "darwin":
    app = BUNDLE(
        folder,
        name="Tevavid Tools.app",
        icon=ICON,
        bundle_identifier="com.tevavid.app",
        info_plist={
            "CFBundleName": "Tevavid Tools",
            "CFBundleDisplayName": "Tevavid Tools",
            "CFBundleShortVersionString": "0.1.0",
            "LSUIElement": True,  # a background service with a panel, no Dock icon
            "NSHighResolutionCapable": True,
        },
    )
