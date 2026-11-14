"""The Tevavid Tools entry in Claude Desktop's settings file.

With it, people can work on their Archicad model from Claude Desktop with
their own Claude plan (Pro, Max, Team...), at no extra cost.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

SERVER_NAME = "archicad"


class ClaudeConfigError(Exception):
    pass


ClaudeDesktopError = ClaudeConfigError


def _system():
    from .installer.system import System

    return System()


def config_path() -> Path:
    return _system().claude_config_path


def is_installed(path: Path | None = None) -> bool:
    return (path or config_path()).parent.exists()


def read_config(path: Path, app: str = "Claude Desktop") -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf8")) if path.exists() else {}
    except (OSError, ValueError) as exc:
        raise ClaudeConfigError(f"{app}'s settings file could not be read.") from exc
    if not isinstance(config, dict):
        raise ClaudeConfigError(f"{app}'s settings file has an unexpected format.")
    return config


def write_config(path: Path, config: dict[str, Any]) -> None:
    """Write the whole file at once, so the app never reads it half written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf8") as file:
            json.dump(config, file, indent=2)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def has_server(path: Path, app: str = "Claude Desktop") -> bool:
    try:
        return SERVER_NAME in (read_config(path, app).get("mcpServers") or {})
    except ClaudeConfigError:
        return False


def add_server(path: Path, entry: dict[str, Any], app: str = "Claude Desktop") -> dict[str, Any] | None:
    """Add (or update) the archicad server in a settings file; returns the entry it replaced, if any."""
    config = read_config(path, app)
    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = config["mcpServers"] = {}
    previous = servers.get(SERVER_NAME)
    servers[SERVER_NAME] = entry
    try:
        write_config(path, config)
    except OSError as exc:
        raise ClaudeConfigError(f"{app}'s settings file could not be saved.") from exc
    return previous


def is_connected(path: Path | None = None) -> bool:
    return has_server(path or config_path())


def connect(command: list[str] | None = None, path: Path | None = None) -> dict[str, Any] | None:
    """Add (or update) the archicad server; returns the entry it replaced, if any."""
    path = path or config_path()
    if not is_installed(path):
        raise ClaudeConfigError("Claude Desktop is not installed on this computer.")
    command = command or _system().server_command()
    return add_server(path, {"command": command[0], "args": command[1:]})


def status() -> dict[str, Any]:
    path = config_path()
    return {"installed": is_installed(path), "connected": is_connected(path)}
