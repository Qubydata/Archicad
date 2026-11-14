"""The Tevavid Tools entry in Claude Code's settings.

Claude Code is Claude in VS Code, in JetBrains and in the terminal. All of them
read the MCP servers in ~/.claude.json ("user" servers, for every project), so
one entry there lets Claude in VS Code work on the Archicad model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .claude_desktop import ClaudeConfigError, add_server, has_server

APP = "Claude Code"
ClaudeCodeError = ClaudeConfigError


def _system():
    from .installer.system import System

    return System()


def config_path() -> Path:
    return _system().claude_code_config_path


def is_installed(path: Path | None = None) -> bool:
    """Claude Code keeps ~/.claude.json and the ~/.claude folder once it has run."""
    path = path or config_path()
    return path.exists() or (path.parent / ".claude").is_dir()


def is_connected(path: Path | None = None) -> bool:
    return has_server(path or config_path(), APP)


def connect(command: list[str] | None = None, path: Path | None = None) -> dict[str, Any] | None:
    """Add (or update) the archicad server; returns the entry it replaced, if any."""
    path = path or config_path()
    if not is_installed(path):
        raise ClaudeConfigError("Claude Code is not installed on this computer.")
    command = command or _system().server_command()
    return add_server(path, {"type": "stdio", "command": command[0], "args": command[1:], "env": {}}, APP)


def status() -> dict[str, Any]:
    path = config_path()
    return {"installed": is_installed(path), "connected": is_connected(path)}
