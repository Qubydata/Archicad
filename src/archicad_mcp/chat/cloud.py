"""Talking to the Archicad MCP cloud service: sign-in, account and billing links."""

from __future__ import annotations

import platform
from typing import Any

import httpx

from .config import cloud_url


TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests route requests to an in-process service


class CloudError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


async def call(method: str, path: str, token: str = "", json: dict[str, Any] | None = None,
               transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    base = cloud_url()
    if not base:
        raise CloudError("Subscriptions are not set up in this version of Archicad MCP.")
    headers = {"x-api-key": token} if token else {}
    try:
        async with httpx.AsyncClient(transport=transport or TRANSPORT, timeout=30) as client:
            response = await client.request(method, f"{base}{path}", json=json, headers=headers)
    except httpx.HTTPError as exc:
        raise CloudError(f"Cannot reach the Archicad MCP service ({type(exc).__name__}). Check the internet connection.") from exc
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code >= 400:
        message = data.get("error")
        if isinstance(message, dict):
            message = message.get("message")
        raise CloudError(message or f"The Archicad MCP service answered {response.status_code}.", response.status_code)
    return data


def device_name() -> str:
    return f"Archicad MCP on {platform.node() or 'this computer'}"
