"""Opening the chat as a palette inside Archicad (Tapir Script UI) or in a browser."""

from __future__ import annotations

import json
import logging
import webbrowser
from typing import Literal

from .. import server as mcp_server
from ..connection import ArchicadError

log = logging.getLogger("archicad_mcp.panel")

PanelMode = Literal["full", "compact"]
PANEL_SIZES = {"full": (420, 760), "compact": (92, 460)}
TABS = ("chat", "tools", "library", "settings")


def panel_url(ui_port: int, token: str, mode: PanelMode = "full", palette: bool = True, tab: str = "chat") -> str:
    url = f"http://127.0.0.1:{ui_port}/?t={token}&mode={mode}&tab={tab}"
    return url + "&palette=1" if palette else url


def _redirect_page(url: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>Archicad MCP</title></head>'
        '<body style="font-family:-apple-system,Segoe UI,sans-serif;padding:16px;color:#555">'
        f'Opening Archicad MCP... <a href="{url}">Click here if nothing happens.</a>'
        f"<script>location.replace({json.dumps(url)});</script></body></html>"
    )


def show_palette(ui_port: int, token: str, mode: PanelMode = "full", tab: str = "chat") -> None:
    """Show (or resize) the Archicad MCP palette in the connected Archicad. Raises ArchicadError."""
    width, height = PANEL_SIZES[mode]
    mcp_server.archicad.tapir(
        "ShowScriptUI",
        {
            "htmlContent": _redirect_page(panel_url(ui_port, token, mode, tab=tab)),
            "title": "MCP",
            "width": width,
            "height": height,
            "contextMenuEnabled": False,
            "scrollBarsVisible": False,
        },
    )


def open_panel(ui_port: int, token: str, mode: PanelMode = "full", browser: bool = False) -> str:
    """Show the chat in an Archicad palette, falling back to the web browser."""
    if not browser:
        try:
            show_palette(ui_port, token, mode)
            return "palette"
        except ArchicadError as exc:
            log.info("Archicad palette not available (%s), using the browser", exc)
    webbrowser.open(panel_url(ui_port, token, "full", palette=False))
    return "browser"
