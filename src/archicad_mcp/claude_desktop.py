"""The Archicad MCP entry in Claude Desktop's settings file.

With it, people can work on their Archicad model from Claude Desktop with
their own Claude plan (Pro, Max, Team...), at no extra cost.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SERVER_NAME = "archicad"


class ClaudeDesktopError(Exception):
    pass


def _system():
    from .installer.system import System

    return System()


def config_path() -> Path:
    return _system().claude_config_path


def is_installed(path: Path | None = None) -> bool:
    return (path or config_path()).parent.exists()


def _read(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf8")) if path.exists() else {}
    except (OSError, ValueError) as exc:
        raise ClaudeDesktopError("Claude Desktop's settings file could not be read.") from exc
    if not isinstance(config, dict):
        raise ClaudeDesktopError("Claude Desktop's settings file has an unexpected format.")
    return config


def is_connected(path: Path | None = None) -> bool:
    path = path or config_path()
    try:
        return SERVER_NAME in (_read(path).get("mcpServers") or {})
    except ClaudeDesktopError:
        return False


def connect(command: list[str] | None = None, path: Path | None = None) -> dict[str, Any] | None:
    """Add (or update) the archicad server; returns the entry it replaced, if any."""
    path = path or config_path()
    if not is_installed(path):
        raise ClaudeDesktopError("Claude Desktop is not installed on this computer.")
    command = command or _system().server_command()
    config = _read(path)
    servers = config.setdefault("mcpServers", {})
    previous = servers.get(SERVER_NAME)
    servers[SERVER_NAME] = {"command": command[0], "args": command[1:]}
    path.write_text(json.dumps(config, indent=2), encoding="utf8")
    return previous


def status() -> dict[str, Any]:
    path = config_path()
    return {"installed": is_installed(path), "connected": is_connected(path)}
