"""Entry point of the packaged apps.

    ArchicadMCPSetup            the setup wizard (also: setup --silent [--no-addon] [--no-desktop-app])
    ArchicadMCP panel [...]     open the Archicad MCP panel (see archicad-mcp-panel --help)
    ArchicadMCP uninstall       remove Archicad MCP (--quiet: no dialogs)
    archicad-mcp-server         the stdio MCP server for Claude Desktop and other MCP clients
    ... mcp                     the same server, from any of the executables
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import __version__


def _message(title: str, text: str, question: bool = False) -> bool:
    """A native message box; returns True for OK / Yes."""
    if sys.platform == "win32":
        import ctypes

        flags = (0x04 | 0x20) if question else 0x40  # MB_YESNO | MB_ICONQUESTION, or MB_ICONINFORMATION
        return ctypes.windll.user32.MessageBoxW(None, text, title, flags) in (1, 6)
    if sys.platform == "darwin":
        import subprocess

        buttons = '{"Cancel", "Uninstall"}' if question else '{"OK"}'
        script = f'display dialog "{text}" with title "{title}" buttons {buttons} default button {2 if question else 1}'
        return subprocess.run(["osascript", "-e", script], capture_output=True).returncode == 0
    print(text)
    return True


def _setup(args: list[str]) -> int:
    from .installer.system import System

    system = System()
    system.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(system.data_dir / "setup.log", encoding="utf8"), logging.StreamHandler()],
    )
    if "--silent" in args:
        from .installer.gui import run_silent

        return run_silent(system, addon="--no-addon" not in args, desktop_app="--no-desktop-app" not in args)
    from .installer.gui import SetupApp

    SetupApp(system).run()
    return 0


def _uninstall(args: list[str]) -> int:
    from .installer.install import uninstall
    from .installer.system import System

    quiet = "--quiet" in args
    if not quiet and not _message("Uninstall Archicad MCP", "Remove Archicad MCP from this computer? Your settings and conversations are kept.", question=True):
        return 1
    uninstall(System())
    if not quiet:
        _message("Archicad MCP", "Archicad MCP was removed. The Tapir add-on stays installed in Archicad.")
    return 0


def main(argv: list[str] | None = None) -> int:
    if sys.stdout is None or sys.stderr is None:  # windowed builds have no console streams
        import os

        sys.stdout = sys.stdout or open(os.devnull, "w")
        sys.stderr = sys.stderr or open(os.devnull, "w")
    args = list(sys.argv[1:] if argv is None else argv)
    executable = Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]).stem.lower()
    command = args.pop(0) if args and not args[0].startswith("-") else None
    if command is None:
        if "server" in executable:
            command = "mcp"
        elif "setup" in executable:
            command = "setup"
        else:
            command = "panel"

    if command == "mcp":
        from .server import main as server_main

        server_main()
        return 0
    if command == "panel":
        from .chat.app import main as panel_main

        panel_main(args)
        return 0
    if command == "setup":
        return _setup(args)
    if command == "uninstall":
        return _uninstall(args)
    if command in ("version", "--version"):
        print(__version__)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
