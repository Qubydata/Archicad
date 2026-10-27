"""`archicad-mcp-panel`: run the Archicad MCP service and open its panel.

The panel opens inside Archicad as a Tapir Script UI palette when the Tapir
add-on is loaded, otherwise in the default web browser. Running the command
again while the service is up just opens the panel again.

With --background the service keeps running (it is started at login by the
installer's "Desktop App" option) and opens the panel in every Archicad that
starts, when "Open this panel when Archicad starts" is on.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from .. import server as mcp_server
from ..connection import ArchicadConnection, ArchicadError
from . import config, panel
from .web import TOKEN_HEADER, create_app

SESSION_FILE = config.CONFIG_DIR / "session.json"
LOG_FILE = config.CONFIG_DIR / "panel.log"
log = logging.getLogger("archicad_mcp")


def running_session(ui_port: int) -> dict | None:
    """The session of an Archicad MCP service already listening on ui_port, if any."""
    try:
        session = json.loads(SESSION_FILE.read_text(encoding="utf8"))
        if session.get("port") != ui_port:
            return None
        with urllib.request.urlopen(f"http://127.0.0.1:{ui_port}/api/ping", timeout=1) as response:
            if json.loads(response.read()).get("app") == "archicad-mcp":
                return session
    except (OSError, ValueError):
        pass
    return None


def ask_running_service(session: dict, archicad_port: int | None, browser: bool) -> str:
    """Ask the running service to show its panel."""
    if not browser:
        body = json.dumps({"mode": "full", "archicadPort": archicad_port}).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{session['port']}/api/panel",
            data=body,
            headers={"Content-Type": "application/json", TOKEN_HEADER: session["token"], "Host": f"127.0.0.1:{session['port']}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if json.loads(response.read()).get("ok"):
                    return "palette"
        except (OSError, ValueError):
            pass
    webbrowser.open(panel.panel_url(session["port"], session["token"], palette=False))
    return "browser"


class ArchicadWatcher:
    """Opens the panel in each Archicad that appears, and optionally stops when all are gone."""

    def __init__(self, server: uvicorn.Server, ui_port: int, token: str, settings: config.Settings,
                 auto_open: bool, exit_when_gone: bool, interval: float = 5.0) -> None:
        self.server = server
        self.ui_port = ui_port
        self.token = token
        self.settings = settings
        self.auto_open = auto_open
        self.exit_when_gone = exit_when_gone
        self.interval = interval
        self.opened: set[int] = set()
        self.seen_any = False
        self.missing_since: float | None = None

    def scan_once(self) -> None:
        ports = {instance.port for instance in ArchicadConnection(host=mcp_server.archicad.host).discover()}
        self.opened &= ports  # an Archicad that quit gets the panel again when it restarts
        if ports:
            self.seen_any = True
            self.missing_since = None
        elif self.exit_when_gone and self.seen_any:
            self.missing_since = self.missing_since or time.monotonic()
            if time.monotonic() - self.missing_since > 60:
                log.info("Archicad is gone, stopping the Archicad MCP service")
                self.server.should_exit = True
        if not (self.auto_open and self.settings.auto_open_panel):
            return
        for port in sorted(ports - self.opened):
            previous = mcp_server.archicad._port
            mcp_server.archicad.port = port
            try:
                panel.show_palette(self.ui_port, self.token)
                self.opened.add(port)
                log.info("Opened the panel in the Archicad on port %s", port)
            except ArchicadError as exc:
                log.info("Panel not opened yet in the Archicad on port %s: %s", port, exc)
                if previous:
                    mcp_server.archicad.port = previous

    def run(self) -> None:
        while not self.server.should_exit:
            try:
                self.scan_once()
            except Exception:  # never let the watcher die
                log.exception("Archicad watcher failed")
            time.sleep(self.interval)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Open the Archicad MCP panel: chat with Claude to change your model.")
    parser.add_argument("--port", type=int, help="Archicad JSON API port (default: find it automatically)")
    parser.add_argument("--ui-port", type=int, default=int(os.environ.get("ARCHICAD_MCP_PANEL_PORT", "8765")))
    parser.add_argument("--browser", action="store_true", help="open in the web browser instead of an Archicad palette")
    parser.add_argument("--background", action="store_true", help="keep running and open the panel when Archicad starts")
    parser.add_argument("--no-open", action="store_true", help="only start the service")
    parser.add_argument("--exit-with-archicad", action="store_true", help="stop a minute after the last Archicad quits")
    args, _unknown = parser.parse_known_args(argv)  # Tapir passes extra flags such as --token

    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf8"), logging.StreamHandler()],
    )
    if args.port:
        mcp_server.archicad.port = args.port

    session = running_session(args.ui_port)
    if session:
        if not args.no_open and not args.background:
            print(f"Archicad MCP is already running, opened in the {ask_running_service(session, args.port, args.browser)}.")
        return

    token = secrets.token_urlsafe(24)
    app = create_app(args.ui_port, token)
    SESSION_FILE.write_text(json.dumps({"port": args.ui_port, "token": token, "pid": os.getpid()}), encoding="utf8")
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.ui_port, log_level="warning"))

    def open_when_ready() -> None:
        while not server.started and not server.should_exit:
            time.sleep(0.1)
        if server.started and not args.no_open and not args.background:
            where = panel.open_panel(args.ui_port, token, browser=args.browser)
            print(f"Archicad MCP is running on port {args.ui_port} (opened in the {where}).")

    threading.Thread(target=open_when_ready, daemon=True).start()
    watcher = ArchicadWatcher(
        server, args.ui_port, token, app.state.settings,
        auto_open=args.background, exit_when_gone=args.exit_with_archicad and not args.background,
    )
    if args.background or args.exit_with_archicad:
        threading.Thread(target=watcher.run, daemon=True).start()
    try:
        server.run()
    finally:
        try:
            if json.loads(SESSION_FILE.read_text(encoding="utf8")).get("token") == token:
                SESSION_FILE.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
