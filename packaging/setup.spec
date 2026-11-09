# PyInstaller spec of the one-file setup program (TevavidToolsSetup.exe / "Tevavid Tools Setup.app").
#
# It carries the built app as payload/app.zip, made by packaging/build.py
# before this spec runs.

import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "src" / "archicad_mcp" / "__init__.py").read_text()).group(1)
PAYLOAD = ROOT / "build" / "payload" / "app.zip"
ICON = str(ROOT / "packaging" / "icons" / ("tevavid-tools.icns" if sys.platform == "darwin" else "tevavid-tools.ico"))

if not PAYLOAD.exists():
    raise SystemExit(f"{PAYLOAD} is missing: run python packaging/build.py")

analysis = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    datas=collect_data_files("archicad_mcp") + [(str(PAYLOAD), "payload")],
    hiddenimports=collect_submodules("archicad_mcp.installer"),
)
# Windows: a picture while the one-file setup unpacks itself (it takes a few seconds,
# more while antivirus scans it); the setup closes it when its window shows.
splash = []
if sys.platform == "win32":
    # No text_pos: with text, the splash lists every file it unpacks ("zlib1.dll"...).
    splash_screen = Splash(str(ROOT / "packaging" / "icons" / "splash.png"), binaries=analysis.binaries,
                           datas=analysis.datas, text_pos=None, always_on_top=False)
    splash = [splash_screen, splash_screen.binaries]

exe = EXE(
    PYZ(analysis.pure), analysis.scripts, *splash, analysis.binaries, analysis.datas, [],
    name="TevavidToolsSetup", console=False, icon=ICON, upx=False,
    uac_admin=False,  # elevation is asked only for the Tapir add-on step
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="Tevavid Tools Setup.app",
        icon=ICON,
        bundle_identifier="com.tevavid.setup",
        info_plist={"CFBundleName": "Tevavid Tools Setup", "CFBundleShortVersionString": VERSION, "NSHighResolutionCapable": True},
    )
