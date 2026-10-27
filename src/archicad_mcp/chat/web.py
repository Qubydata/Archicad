"""Local web server behind the Archicad MCP panel.

It only listens on 127.0.0.1. Every API call must carry the session token that
is handed to the page in its URL, and the Host header must be the loopback
address, so other web pages open in a browser cannot drive Archicad through it.
"""

from __future__ import annotations

import json
import secrets
import time
from importlib import resources
from typing import Any

import anthropic
import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .. import catalog
from .. import server as mcp_server
from ..connection import ArchicadError
from . import config, panel
from .agent import ChatAgent
from .history import ConversationStore

TOKEN_HEADER = "x-archicad-mcp-token"
LIBRARY_TYPES = {"Object", "Lamp", "Door", "Window"}
ASSETS = {"logo-128.png": "image/png", "logo-256.png": "image/png", "quickstart.html": "text/html; charset=utf-8"}


def _api_error_message(exc: Exception) -> str:
    if isinstance(exc, anthropic.AuthenticationError):
        return "The Anthropic API key was rejected. Open Settings and paste a valid key."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "This API key cannot use the selected model. Pick another model in Settings."
    if isinstance(exc, anthropic.RateLimitError):
        return "Rate limited by the Anthropic API. Wait a moment and try again."
    if isinstance(exc, anthropic.APIStatusError):
        return f"Anthropic API error {exc.status_code}: {exc.message}"
    if isinstance(exc, anthropic.APIConnectionError):
        return "Cannot reach the Anthropic API. Check the internet connection."
    return f"{type(exc).__name__}: {exc}"


def _archicad_status() -> dict[str, Any]:
    connection = mcp_server.archicad
    try:
        status: dict[str, Any] = {"connected": True, "port": connection.port}
        status["product"] = connection.command("API.GetProductInfo")
        status["tapir"] = connection.is_tapir_available()
        if status["tapir"]:
            status["tapirVersion"] = connection.tapir("GetAddOnVersion").get("version")
            status["project"] = connection.tapir("GetProjectInfo").get("projectName")
    except ArchicadError as exc:
        return {"connected": False, "message": str(exc)}
    return status


def _library_parts() -> list[dict[str, str]]:
    parts = mcp_server.archicad.tapir("GetAvailableLibraryParts").get("libraryParts", [])
    found = {
        (part.get("documentName") or part.get("fileName", ""), part.get("typeId", ""))
        for part in parts
        if part.get("typeId") in LIBRARY_TYPES
    }
    return [{"name": name, "type": kind} for name, kind in sorted(found) if name]


def create_app(port: int, token: str | None = None) -> Starlette:
    token = token or secrets.token_urlsafe(24)
    settings = config.load()
    agent = ChatAgent(mcp_server.mcp, settings, ConversationStore(config.CONFIG_DIR / "conversations"))
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    library_cache: dict[str, Any] = {}

    def authorized(request: Request) -> bool:
        return request.headers.get("host") in allowed_hosts and secrets.compare_digest(
            request.headers.get(TOKEN_HEADER, ""), token
        )

    def forbidden() -> Response:
        return JSONResponse({"error": "forbidden"}, status_code=403)

    def guarded(handler):
        async def endpoint(request: Request) -> Response:
            if not authorized(request):
                return forbidden()
            return await handler(request)

        return endpoint

    async def index(request: Request) -> Response:
        if request.headers.get("host") not in allowed_hosts:
            return forbidden()
        html = resources.files("archicad_mcp.chat").joinpath("static/index.html").read_text(encoding="utf8")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def asset(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in ASSETS or request.headers.get("host") not in allowed_hosts:
            return Response(status_code=404)
        data = resources.files("archicad_mcp").joinpath(f"assets/{name}").read_bytes()
        return Response(data, media_type=ASSETS[name], headers={"Cache-Control": "max-age=3600"})

    async def ping(request: Request) -> Response:
        return JSONResponse({"app": "archicad-mcp"})

    async def status(request: Request) -> Response:
        return JSONResponse({
            "archicad": await anyio.to_thread.run_sync(_archicad_status),
            "settings": {
                "model": settings.model,
                "effort": settings.effort,
                "autoOpenPanel": settings.auto_open_panel,
                "hasKey": bool(settings.resolved_api_key),
                "keyFromEnvironment": not settings.api_key and bool(settings.resolved_api_key),
            },
            "models": config.MODELS,
            "efforts": config.EFFORTS,
            "busy": agent.busy,
        })

    async def save_settings(request: Request) -> Response:
        data = await request.json()
        if data.get("apiKey"):
            settings.api_key = data["apiKey"].strip()
        if data.get("model") in config.MODELS:
            settings.model = data["model"]
        if data.get("effort") in config.EFFORTS:
            settings.effort = data["effort"]
        if isinstance(data.get("autoOpenPanel"), bool):
            settings.auto_open_panel = data["autoOpenPanel"]
        config.save(settings)
        return JSONResponse({"ok": True})

    async def history(request: Request) -> Response:
        return JSONResponse({
            "id": agent.conversation.id,
            "title": agent.conversation.title,
            "items": agent.transcript,
            "busy": agent.busy,
        })

    async def conversations(request: Request) -> Response:
        limit = int(request.query_params.get("limit", "0")) or None
        return JSONResponse({"conversations": agent.store.recent(limit), "now": time.time()})

    async def open_conversation(request: Request) -> Response:
        if agent.busy:
            return JSONResponse({"error": "Still working on the previous request."}, status_code=409)
        try:
            agent.open((await request.json()).get("id", ""))
        except (OSError, ValueError, KeyError):
            return JSONResponse({"error": "That conversation no longer exists."}, status_code=404)
        return JSONResponse({"ok": True})

    async def delete_conversation(request: Request) -> Response:
        conversation_id = (await request.json()).get("id", "")
        try:
            agent.store.delete(conversation_id)
        except ValueError:
            return JSONResponse({"error": "bad id"}, status_code=400)
        if agent.conversation.id == conversation_id:
            agent.reset()
        return JSONResponse({"ok": True})

    async def chat(request: Request) -> Response:
        text = ((await request.json()).get("message") or "").strip()
        if not text:
            return JSONResponse({"error": "empty message"}, status_code=400)
        if agent.busy:
            return JSONResponse({"error": "Still working on the previous request."}, status_code=409)

        async def events():
            agent.busy = True
            try:
                async for event in agent.send(text):
                    yield json.dumps(event) + "\n"
            except Exception as exc:  # reported in the chat instead of breaking the stream
                event = {"type": "error", "message": _api_error_message(exc)}
                agent._record(event)
                yield json.dumps(event) + "\n"
            finally:
                agent.busy = False
            yield json.dumps({"type": "done"}) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")

    async def stop(request: Request) -> Response:
        agent.stop_requested = True
        return JSONResponse({"ok": True})

    async def reset(request: Request) -> Response:
        agent.reset()
        return JSONResponse({"ok": True})

    async def library(request: Request) -> Response:
        if request.query_params.get("refresh") or "parts" not in library_cache:
            try:
                library_cache["parts"] = await anyio.to_thread.run_sync(_library_parts)
            except ArchicadError as exc:
                return JSONResponse({"error": str(exc), "parts": []})
        query = request.query_params.get("q", "").lower()
        kind = request.query_params.get("type", "")
        parts = [
            p for p in library_cache["parts"]
            if (not kind or p["type"] == kind) and all(word in p["name"].lower() for word in query.split())
        ]
        return JSONResponse({"parts": parts[:200], "total": len(parts)})

    async def commands(request: Request) -> Response:
        return JSONResponse({"commands": catalog.search(request.query_params.get("q", ""), limit=40)})

    async def panel_mode(request: Request) -> Response:
        """Resize the Archicad palette to the full or the compact panel, or open it."""
        data = await request.json()
        mode = data.get("mode") if data.get("mode") in panel.PANEL_SIZES else "full"
        tab = data.get("tab") if data.get("tab") in panel.TABS else "chat"
        if data.get("archicadPort"):
            mcp_server.archicad.port = int(data["archicadPort"])
        try:
            await anyio.to_thread.run_sync(lambda: panel.show_palette(port, token, mode, tab))
        except ArchicadError as exc:
            return JSONResponse({"ok": False, "error": str(exc)})
        return JSONResponse({"ok": True})

    api = [
        ("/api/status", status, ["GET"]),
        ("/api/settings", save_settings, ["POST"]),
        ("/api/history", history, ["GET"]),
        ("/api/conversations", conversations, ["GET"]),
        ("/api/conversations/open", open_conversation, ["POST"]),
        ("/api/conversations/delete", delete_conversation, ["POST"]),
        ("/api/chat", chat, ["POST"]),
        ("/api/stop", stop, ["POST"]),
        ("/api/reset", reset, ["POST"]),
        ("/api/library", library, ["GET"]),
        ("/api/commands", commands, ["GET"]),
        ("/api/panel", panel_mode, ["POST"]),
    ]
    app = Starlette(routes=[
        Route("/", index),
        Route("/api/ping", ping),
        Route("/assets/{name}", asset),
        *[Route(path, guarded(handler), methods=methods) for path, handler, methods in api],
    ])
    app.state.token = token
    app.state.agent = agent
    app.state.settings = settings
    return app
