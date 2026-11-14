"""Local web server behind the Tevavid Tools panel.

It only listens on 127.0.0.1. Every API call must carry the session token that
is handed to the page in its URL, and the Host header must be the loopback
address, so other web pages open in a browser cannot drive Archicad through it.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import secrets
import tempfile
import threading
import time
from importlib import resources
from pathlib import Path
from typing import Any

import anthropic
import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from mcp.server.mcpserver.exceptions import ToolError
from starlette.routing import Route

from .. import __version__, catalog, claude_code, claude_desktop
from .. import server as mcp_server
from ..connection import ArchicadError
from ..updater import UpdateError, Updater
from . import config, panel
from .agent import ChatAgent
from .history import ConversationStore

TOKEN_HEADER = "x-tevavid-token"
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


def create_app(port: int, token: str | None = None, updater: Updater | None = None) -> Starlette:
    token = token or secrets.token_urlsafe(24)
    settings = config.load()
    updater = updater or Updater(config.CONFIG_DIR)
    update_job: dict[str, Any] = {"running": False, "error": None, "installing": False}
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
        return JSONResponse({"app": "tevavid-tools"})

    async def status(request: Request) -> Response:
        return JSONResponse({
            "archicad": await anyio.to_thread.run_sync(_archicad_status),
            "settings": {
                "model": settings.model,
                "effort": settings.effort,
                "autoOpenPanel": settings.auto_open_panel,
                "autoUpdate": settings.auto_update,
                "hasKey": bool(settings.resolved_api_key),
                "keyFromEnvironment": not settings.api_key and bool(settings.resolved_api_key),
            },
            "claudeDesktop": claude_desktop.status(),
            "claudeCode": claude_code.status(),
            "models": config.MODELS,
            "efforts": config.EFFORTS,
            "busy": agent.busy,
            "tools": settings.tools,
            "version": __version__,
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
        if isinstance(data.get("autoUpdate"), bool):
            settings.auto_update = data["autoUpdate"]
        config.save(settings)
        return JSONResponse({"ok": True})

    async def connect_claude_desktop(request: Request) -> Response:
        try:
            claude_desktop.connect()
        except claude_desktop.ClaudeDesktopError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, **claude_desktop.status()})

    async def connect_claude_code(request: Request) -> Response:
        try:
            claude_code.connect()
        except claude_code.ClaudeCodeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, **claude_code.status()})

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

    # --- Sun Analysis -----------------------------------------------------------

    sun = mcp_server.sun
    site = mcp_server.site

    async def sun_state(request: Request) -> Response:
        state = sun.state()
        try:
            state["projectLocation"] = await anyio.to_thread.run_sync(sun.location)
        except ArchicadError as exc:
            state["locationError"] = str(exc)
        return JSONResponse(state)

    async def sun_location(request: Request) -> Response:
        data = await request.json()
        if data.get("reset"):
            sun.location_override = None
        else:
            try:
                override = {"latitude": float(data["latitude"]), "longitude": float(data["longitude"])}
                if data.get("utcOffset") not in (None, ""):
                    override["utcOffset"] = float(data["utcOffset"])
            except (KeyError, TypeError, ValueError):
                return JSONResponse({"error": "Latitude and longitude must be numbers."}, status_code=400)
            if not (-90 <= override["latitude"] <= 90 and -180 <= override["longitude"] <= 180):
                return JSONResponse({"error": "Latitude is -90 to 90 and longitude -180 to 180 degrees."}, status_code=400)
            sun.location_override = override
        return JSONResponse({"ok": True})

    async def sun_run(request: Request) -> Response:
        data = await request.json()
        if sun.lock.locked():
            return JSONResponse({"error": "An analysis is already running."}, status_code=409)
        updates: queue.Queue = queue.Queue()

        def work():
            try:
                reflect = {k: float(data[k]) for k in ("floor_reflectance", "wall_reflectance", "ceiling_reflectance") if k in data}
                answer = sun.run(data.get("scope", "story"), data.get("date") or None, data.get("metric", "illuminance"),
                                 float(data.get("grid", 0.5)), float(data.get("workPlane", 0.0)),
                                 bool(data.get("includeFurniture")), reflect,
                                 lambda fraction, message: updates.put({"type": "progress", "fraction": fraction, "message": message}))
                updates.put({"type": "result", "state": answer})
            except ArchicadError as exc:
                updates.put({"type": "error", "message": str(exc)})
            except Exception as exc:  # reported in the panel
                logging.getLogger("archicad_mcp.sun").exception("Sun analysis failed")
                updates.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            updates.put(None)

        threading.Thread(target=work, daemon=True).start()

        async def events():
            while True:
                try:
                    item = updates.get_nowait()
                except queue.Empty:
                    await anyio.sleep(0.1)
                    continue
                if item is None:
                    break
                yield json.dumps(item) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")

    async def sun_show(request: Request) -> Response:
        data = await request.json()
        scale = (data["low"], data["high"]) if data.get("low") is not None and data.get("high") is not None else None
        time_index = data.get("timeIndex")
        try:
            answer = await anyio.to_thread.run_sync(
                lambda: sun.show(data.get("metric"), int(time_index) if time_index is not None else None, scale))
        except ArchicadError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(answer)

    async def sun_clear(request: Request) -> Response:
        try:
            return JSONResponse(await anyio.to_thread.run_sync(sun.clear))
        except ArchicadError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    def update_status() -> dict[str, Any]:
        return {**updater.status(), "autoUpdate": settings.auto_update, "job": dict(update_job)}

    async def update_state(request: Request) -> Response:
        return JSONResponse(await anyio.to_thread.run_sync(update_status))

    async def update_check(request: Request) -> Response:
        await anyio.to_thread.run_sync(lambda: updater.check(force=True))
        return JSONResponse(update_status())

    async def update_install(request: Request) -> Response:
        """Download the update (the panel polls /api/update for progress), then let setup replace this app."""
        if update_job["running"]:
            return JSONResponse(update_status())

        def work():
            update_job.update(running=True, error=None)
            try:
                updater.download()
                try:
                    archicad_open = bool(mcp_server.archicad.discover())
                except Exception:
                    archicad_open = True
                update_job["installing"] = True
                updater.install(relaunch=archicad_open)  # setup stops this service and opens the new panel
            except UpdateError as exc:
                update_job.update(error=str(exc), installing=False)
            except Exception as exc:  # shown in the panel
                logging.getLogger("archicad_mcp.updater").exception("Update failed")
                update_job.update(error=f"{type(exc).__name__}: {exc}", installing=False)
            finally:
                update_job["running"] = False

        threading.Thread(target=work, daemon=True).start()
        return JSONResponse({**update_status(), "job": {**update_job, "running": True}})

    def progress_stream(work) -> StreamingResponse:
        """Run work(progress) in a thread and stream its progress, then its result or error, as NDJSON."""
        updates: queue.Queue = queue.Queue()

        def run():
            try:
                answer = work(lambda fraction, message: updates.put({"type": "progress", "fraction": fraction,
                                                                     "message": message}))
                updates.put({"type": "result", "state": answer})
            except ToolError as exc:  # ArchicadError, ModelImportError: their messages are for the user
                updates.put({"type": "error", "message": str(exc)})
            except Exception as exc:  # reported in the panel
                logging.getLogger("archicad_mcp").exception("Panel job failed")
                updates.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            updates.put(None)

        threading.Thread(target=run, daemon=True).start()

        async def events():
            while True:
                try:
                    item = updates.get_nowait()
                except queue.Empty:
                    await anyio.sleep(0.1)
                    continue
                if item is None:
                    break
                yield json.dumps(item) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")

    async def site_state(request: Request) -> Response:
        state = site.state()
        try:
            place = await anyio.to_thread.run_sync(site.location)
            place.pop("surveyPoint", None)
            state["projectLocation"] = place
        except ArchicadError as exc:
            state["locationError"] = str(exc)
        return JSONResponse(state)

    async def site_map(request: Request) -> Response:
        data = await request.json()
        if site.lock.locked():
            return JSONResponse({"error": "The site is already being built."}, status_code=409)
        try:
            size = float(data.get("size", 300))
            spacing = float(data["spacing"]) if data.get("spacing") else None
            level = float(data.get("originLevel", 0) or 0)
        except (TypeError, ValueError):
            return JSONResponse({"error": "Size, spacing and level must be numbers."}, status_code=400)
        ground = "altitude" if data.get("ground") == "altitude" else "origin"
        return progress_stream(lambda progress: site.from_map(size, spacing, bool(data.get("buildings", True)),
                                                              ground, level, progress))

    async def site_survey(request: Request) -> Response:
        data = await request.json()
        if site.lock.locked():
            return JSONResponse({"error": "The site is already being built."}, status_code=409)
        try:
            content = base64.b64decode(data["content"], validate=True)
        except (KeyError, ValueError, TypeError):
            return JSONResponse({"error": "Choose a survey file."}, status_code=400)
        name = str(data.get("name") or "survey.csv")
        options = {k: str(data.get(k) or "auto") for k in ("coordinates", "order", "units")}
        return progress_stream(lambda progress: site.from_survey(name, content, options["coordinates"],
                                                                 options["order"], options["units"], progress))

    async def site_clear(request: Request) -> Response:
        data = await request.json()
        what = data.get("what") if data.get("what") in ("terrain", "buildings") else "all"
        try:
            return JSONResponse(await anyio.to_thread.run_sync(lambda: site.clear(what)))
        except ArchicadError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    # --- Model Import -------------------------------------------------------------

    importer = mcp_server.model_import

    def placements_of(data) -> int:
        try:
            return max(1, int(data.get("placements", 1)))
        except (TypeError, ValueError):
            return 1

    async def import_call(function) -> Response:
        try:
            return JSONResponse(await anyio.to_thread.run_sync(function))
        except ToolError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def import_state(request: Request) -> Response:
        placements = placements_of(request.query_params)
        return await import_call(lambda: {**importer.state(placements), "converter": importer.converter_status()})

    async def import_open(request: Request) -> Response:
        data = await request.json()
        if data.get("path"):  # a file from the Downloads list
            path = Path(str(data["path"]))
            if not any(path == Path(item["path"]) for item in importer.new_downloads(0)):
                return JSONResponse({"error": "Only models in the Downloads folder can be opened by path."}, status_code=400)
        else:
            try:
                content = base64.b64decode(data["content"], validate=True)
            except (KeyError, ValueError, TypeError):
                return JSONResponse({"error": "Choose a 3D model file."}, status_code=400)
            name = Path(str(data.get("name") or "model")).name
            folder = Path(tempfile.mkdtemp(prefix="upload-", dir=_work_dir()))
            path = folder / name
            path.write_bytes(content)
        placements = placements_of(data)
        return progress_stream(lambda progress: importer.open(path, data.get("unit") or None, data.get("upAxis") or None,
                                                              placements, progress))

    def _work_dir() -> Path:
        importer.work_dir.mkdir(parents=True, exist_ok=True)
        return importer.work_dir

    async def import_edit(request: Request) -> Response:
        data = await request.json()
        placements = placements_of(data)
        action = data.get("action")
        actions = {
            "delete": lambda: importer.delete_parts(list(data.get("ids") or []), placements),
            "restore": lambda: importer.restore_parts(data.get("ids"), placements),
            "removeSmall": lambda: importer.remove_small_parts(float(data.get("fraction", 0.01)), placements),
            "undo": lambda: importer.undo(placements),
            "unit": lambda: importer.set_unit(str(data.get("value")), placements),
            "upAxis": lambda: importer.set_up_axis(str(data.get("value")), placements),
            "turn": lambda: importer.set_turn(float(data.get("value", 0)), placements),
            "level": lambda: importer.set_level(int(data.get("value", 0)), placements),
            "placements": lambda: importer.state(placements),
            "close": lambda: importer.close(),
        }
        if action not in actions:
            return JSONResponse({"error": f"Unknown action {action!r}."}, status_code=400)
        return await import_call(actions[action])

    async def import_small(request: Request) -> Response:
        fraction = float(request.query_params.get("fraction", 0.01))
        return await import_call(lambda: {"ids": importer.small_parts(fraction)})

    async def import_preview(request: Request) -> Response:
        level = request.query_params.get("level")
        try:
            png = await anyio.to_thread.run_sync(lambda: importer.preview(320, int(level) if level else None))
        except ToolError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})

    async def import_downloads(request: Request) -> Response:
        since = float(request.query_params.get("since", 0) or 0)
        return await import_call(lambda: {"files": importer.new_downloads(since)})

    async def import_library(request: Request) -> Response:
        target = "embedded" if request.query_params.get("target") == "embedded" else "shared"
        return await import_call(lambda: importer.library_tree(target))

    async def import_folder(request: Request) -> Response:
        data = await request.json()
        target = "embedded" if data.get("target") == "embedded" else "shared"
        return await import_call(lambda: importer.create_folder(str(data.get("folder", "")), target))

    async def import_save(request: Request) -> Response:
        data = await request.json()
        target = "embedded" if data.get("target") == "embedded" else "shared"
        return progress_stream(lambda progress: importer.save(
            str(data.get("name") or "") or None, str(data.get("folder") or ""), target, bool(data.get("place")),
            None, bool(data.get("pickPoint")), placements_of(data), progress))

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
        ("/api/claude-desktop/connect", connect_claude_desktop, ["POST"]),
        ("/api/claude-code/connect", connect_claude_code, ["POST"]),
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
        ("/api/sun", sun_state, ["GET"]),
        ("/api/sun/location", sun_location, ["POST"]),
        ("/api/sun/run", sun_run, ["POST"]),
        ("/api/sun/show", sun_show, ["POST"]),
        ("/api/sun/clear", sun_clear, ["POST"]),
        ("/api/site", site_state, ["GET"]),
        ("/api/site/map", site_map, ["POST"]),
        ("/api/site/survey", site_survey, ["POST"]),
        ("/api/site/clear", site_clear, ["POST"]),
        ("/api/import", import_state, ["GET"]),
        ("/api/import/open", import_open, ["POST"]),
        ("/api/import/edit", import_edit, ["POST"]),
        ("/api/import/small", import_small, ["GET"]),
        ("/api/import/preview", import_preview, ["GET"]),
        ("/api/import/downloads", import_downloads, ["GET"]),
        ("/api/import/library", import_library, ["GET"]),
        ("/api/import/folder", import_folder, ["POST"]),
        ("/api/import/save", import_save, ["POST"]),
        ("/api/update", update_state, ["GET"]),
        ("/api/update/check", update_check, ["POST"]),
        ("/api/update/install", update_install, ["POST"]),
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
    app.state.updater = updater
    return app
