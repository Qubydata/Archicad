"""Entry point of the packaged apps.

    TevavidToolsSetup            the setup wizard; for IT: --silent [--tools assistant,sun] [--no-addon] [--no-desktop-app]
                                 updating an installed copy with its own choices: --silent --update [--relaunch]
    TevavidTools panel [...]     open the Tevavid Tools panel (see tevavid-panel --help)
    TevavidTools uninstall       remove Tevavid Tools (--quiet: no dialogs)
    TevavidTools update          check for a new version and install it (--check: only check)
    tevavid-mcp-server         the stdio MCP server for Claude Desktop and other MCP clients
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
    logging.getLogger("archicad_mcp.installer").info("Tevavid Tools Setup %s starting %s", __version__, args)
    if "--addon-files" in args:  # the part of setup that runs with administrator rights
        from .installer.addon_files import run_job_file

        return run_job_file(args[args.index("--addon-files") + 1])
    if "--silent" in args:
        from .installer.gui import run_silent

        tools = None
        if "--tools" in args and args.index("--tools") + 1 < len(args):
            tools = [t.strip() for t in args[args.index("--tools") + 1].split(",") if t.strip()]
        return run_silent(system, addon="--no-addon" not in args, desktop_app="--no-desktop-app" not in args, tools=tools,
                          update="--update" in args, relaunch="--relaunch" in args)
    try:
        from .installer.gui import SetupApp

        if "--selftest" in args:  # used by the build: open the window, check it, close it
            return 0 if SetupApp(system).run(selftest=True) else 1
        SetupApp(system).run()
    except Exception as exc:  # a windowed program has no console: say what went wrong
        logging.getLogger("archicad_mcp.installer").exception("The setup window could not start")
        try:
            from .installer.gui import close_splash

            close_splash()
        except Exception:
            pass
        _message("Tevavid Tools Setup",
                 f"Setup could not open its window ({type(exc).__name__}: {exc}).\n\n"
                 f"Details are in {system.data_dir / 'setup.log'}. "
                 "On Windows, installing the Microsoft Edge WebView2 Runtime often fixes this.")
        return 1
    return 0


def _uninstall(args: list[str]) -> int:
    from .installer.install import uninstall
    from .installer.system import System

    quiet = "--quiet" in args
    if not quiet and not _message("Uninstall Tevavid Tools", "Remove Tevavid Tools from this computer? Your settings and conversations are kept.", question=True):
        return 1
    uninstall(System())
    if not quiet:
        _message("Tevavid Tools", "Tevavid Tools was removed. The Tapir add-on stays installed in Archicad.")
    return 0


def _update(args: list[str]) -> int:
    from .installer.system import System
    from .updater import Updater, UpdateError

    updater = Updater(System().data_dir)
    status = updater.check(force=True)
    if status["error"]:
        print(status["error"])
        return 1
    if not status["available"]:
        print(f"Tevavid Tools {status['current']} is up to date.")
        return 0
    print(f"Version {status['latest']} is available (you have {status['current']}).")
    if "--check" in args:
        return 0
    try:
        print("Downloading...")
        updater.download()
        updater.install(relaunch=False)
    except UpdateError as exc:
        print(exc)
        return 1
    print("Installing in the background. Tevavid Tools starts again when it is done.")
    return 0


def main(argv: list[str] | None = None) -> int:
    if sys.stdout is None or sys.stderr is None:  # windowed builds have no console streams
        import os

        sys.stdout = sys.stdout or open(os.devnull, "w")
        sys.stderr = sys.stderr or open(os.devnull, "w")
    args = list(sys.argv[1:] if argv is None else argv)
    # The whole file name: a stem would cut "0.3.0-TevavidToolsSetup" at its first dot.
    executable = Path(sys.executable if getattr(sys, "frozen", False) else sys.argv[0]).name.lower()
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
    if command == "update":
        return _update(args)
    if command in ("version", "--version"):
        print(__version__)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
