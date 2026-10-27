"""HTTP client for the Archicad JSON API and the Tapir add-on commands.

Archicad (25 and later) runs a small JSON-over-HTTP server on localhost while it
is open. The first running instance listens on port 19723, further instances on
the following ports. Every request is a POST with a body of
``{"command": "API.Something", "parameters": {...}}`` and every answer has the
shape ``{"succeeded": bool, "result": {...}}`` or ``{"succeeded": false,
"error": {...}}``.

The Tapir add-on registers its commands under the ``TapirCommand`` namespace;
they are reached through the built-in ``API.ExecuteAddOnCommand`` command.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

DEFAULT_HOST = "127.0.0.1"
FIRST_PORT = 19723
LAST_PORT = 19744
TAPIR_NAMESPACE = "TapirCommand"


class ArchicadError(ToolError):
    """Archicad could not be reached or rejected a command.

    A ToolError, so the message is shown to the model as a tool error result.
    """


@dataclass
class ArchicadInstance:
    port: int
    product_info: dict[str, Any]


class ArchicadConnection:
    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.host = host or os.environ.get("ARCHICAD_HOST", DEFAULT_HOST)
        env_port = os.environ.get("ARCHICAD_PORT")
        self._port: int | None = port or (int(env_port) if env_port else None)
        self.timeout = timeout or float(os.environ.get("ARCHICAD_TIMEOUT", "120"))

    @property
    def port(self) -> int:
        if self._port is None:
            instances = self.discover()
            if not instances:
                raise ArchicadError(
                    f"No running Archicad found on {self.host} ports {FIRST_PORT}-{LAST_PORT}. "
                    "Start Archicad and open a project, or set ARCHICAD_PORT."
                )
            self._port = instances[0].port
        return self._port

    @port.setter
    def port(self, value: int) -> None:
        self._port = value

    def _url(self, port: int) -> str:
        return f"http://{self.host}:{port}"

    def _post(self, port: int, command: str, parameters: dict[str, Any] | None, timeout: float) -> Any:
        body = {"command": command, "parameters": parameters or {}}
        try:
            response = httpx.post(self._url(port), json=body, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ArchicadError(f"Archicad did not answer {command} within {timeout:g} s.") from exc
        except httpx.HTTPError as exc:
            raise ArchicadError(f"Cannot reach Archicad at {self._url(port)}: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ArchicadError(f"Archicad sent a non-JSON answer to {command}: {response.text[:200]}") from exc
        if not payload.get("succeeded", False):
            error = payload.get("error") or {}
            raise ArchicadError(
                f"{command} failed: {error.get('message', 'unknown error')} (code {error.get('code', '?')})"
            )
        return payload.get("result", {})

    def discover(self) -> list[ArchicadInstance]:
        """Find every Archicad instance that answers on the usual port range."""
        instances = []
        for port in range(FIRST_PORT, LAST_PORT + 1):
            try:
                alive = self._post(port, "API.IsAlive", None, timeout=0.5)
            except ArchicadError:
                continue
            if not alive.get("isAlive"):
                continue
            try:
                info = self._post(port, "API.GetProductInfo", None, timeout=2)
            except ArchicadError:
                info = {}
            instances.append(ArchicadInstance(port=port, product_info=info))
        return instances

    def command(self, command: str, parameters: dict[str, Any] | None = None) -> Any:
        """Run a built-in Archicad command such as ``API.GetSelectedElements``."""
        if not command.startswith("API."):
            command = f"API.{command}"
        return self._post(self.port, command, parameters, self.timeout)

    def tapir(self, command: str, parameters: dict[str, Any] | None = None) -> Any:
        """Run a Tapir add-on command such as ``CreateWalls``."""
        result = self.command(
            "API.ExecuteAddOnCommand",
            {
                "addOnCommandId": {"commandNamespace": TAPIR_NAMESPACE, "commandName": command},
                "addOnCommandParameters": parameters or {},
            },
        )
        response = result.get("addOnCommandResponse", result)
        if isinstance(response, dict) and "error" in response and len(response) == 1:
            error = response["error"]
            raise ArchicadError(
                f"Tapir {command} failed: {error.get('message', 'unknown error')} (code {error.get('code', '?')})"
            )
        return response

    def is_tapir_available(self) -> bool:
        try:
            result = self.command(
                "API.IsAddOnCommandAvailable",
                {"addOnCommandId": {"commandNamespace": TAPIR_NAMESPACE, "commandName": "GetAddOnVersion"}},
            )
        except ArchicadError:
            return False
        return bool(result.get("available"))
