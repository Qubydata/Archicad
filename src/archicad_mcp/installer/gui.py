"""The setup wizard window (pywebview), its mini progress window and the completion popup."""

from __future__ import annotations

import base64
import json
import logging
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from ..connection import ArchicadConnection
from .install import STEPS, Installer, Options
from .system import SUPPORTED_VERSIONS, ArchicadInstall, System

log = logging.getLogger("archicad_mcp.installer")
UI = Path(__file__).resolve().parent / "ui"
MAIN_SIZE = (640, 520)
COMPLETE_SIZE = (640, 300)
MINI_SIZE = (380, 118)
TOAST_SIZE = (390, 126)


def page(name: str) -> str:
    """A UI page with its images inlined, so it works from any folder (and inside the packaged app)."""
    from importlib import resources

    html = (UI / name).read_text(encoding="utf8")
    assets = resources.files("archicad_mcp").joinpath("assets")
    for file, mime in (("logo-256.png", "image/png"), ("logo-128.png", "image/png")):
        data = base64.b64encode(assets.joinpath(file).read_bytes()).decode()
        html = html.replace(f"../../assets/{file}", f"data:{mime};base64,{data}")
    building = base64.b64encode((UI / "building.svg").read_bytes()).decode()
    return html.replace('src="building.svg"', f'src="data:image/svg+xml;base64,{building}"')


def payload_path() -> Path | None:
    """The app archive bundled into the setup executable (absent when running from source)."""
    base = Path(getattr(sys, "_MEIPASS", ""))
    candidate = base / "payload" / "app.zip"
    return candidate if getattr(sys, "frozen", False) and candidate.exists() else None


def archicad_rows(system: System) -> list[dict[str, Any]]:
    """Detected Archicad versions, plus one version to pick by hand like the design shows."""
    detected = system.detect_archicad()
    rows = [install.to_dict() for install in detected]
    versions = {install.version for install in detected}
    extra = [v for v in sorted(SUPPORTED_VERSIONS, reverse=True) if v not in versions]
    for version in extra[: 1 if detected else 2]:
        rows.append(ArchicadInstall(version, system.default_archicad_path(version), False).to_dict())
    return rows


class SetupApi:
    """Called from setup.html, mini.html and toast.html through window.pywebview.api."""

    def __init__(self, app: SetupApp) -> None:
        self._app = app

    def get_state(self) -> dict[str, Any]:
        return {
            "supported": [min(SUPPORTED_VERSIONS), max(SUPPORTED_VERSIONS)],
            "archicad": archicad_rows(self._app.system),
            "steps": [step.label for step in STEPS],
            "platform": self._app.system.platform,
        }

    def screen_changed(self, name: str) -> None:
        self._app.on_screen(name)

    def browse(self, current: str) -> dict[str, Any] | None:
        import webview

        start = current if Path(current).exists() else str(Path(current).anchor or "/")
        chosen = self._app.main.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
        if not chosen:
            return None
        path = chosen[0] if isinstance(chosen, (list, tuple)) else chosen
        version = self._app.system.version_from_path(path, max(SUPPORTED_VERSIONS))
        return {"path": path, "version": version}

    def install(self, options: dict[str, Any]) -> None:
        self._app.start_install(Options.from_dict(options))

    def confirm_cancel(self) -> bool:
        if self._app.installer is None:
            return False
        if self._app.main.create_confirmation_dialog("Cancel installation?", "Archicad MCP is not installed yet. Changes made so far will be undone."):
            self._app.installer.cancel()
            return True
        return False

    def finish(self, open_archicad: bool, show_guide: bool) -> None:
        self._app.finish(open_archicad, show_guide)

    def quit(self) -> None:
        self._app.quit()

    def show_main(self) -> None:
        self._app.show_main()

    def hide_mini(self) -> None:
        if self._app.mini:
            self._app.mini.hide()

    def close_toast(self) -> None:
        self._app.close_toast()

    def toast_clicked(self) -> None:
        self._app.close_toast()
        self._app.show_main()


class SetupApp:
    def __init__(self, system: System | None = None) -> None:
        self.system = system or System()
        self.api = SetupApi(self)
        self.main = None
        self.mini = None
        self.toast = None
        self.installer: Installer | None = None
        self.options: Options | None = None
        self.result: dict[str, Any] | None = None
        self.minimized = False

    # --- windows ------------------------------------------------------------

    def run(self) -> None:
        import webview

        self.main = webview.create_window(
            "Archicad MCP Setup", html=page("setup.html"), js_api=self.api,
            width=MAIN_SIZE[0], height=MAIN_SIZE[1], resizable=False, background_color="#FFFFFF",
        )
        self.main.events.minimized += self._on_minimized
        self.main.events.restored += self._on_restored
        self.main.events.closing += self._on_closing
        self.mini = webview.create_window(
            "Archicad MCP Installer", html=page("mini.html"), js_api=self.api,
            width=MINI_SIZE[0], height=MINI_SIZE[1], frameless=True, on_top=True, hidden=True,
            resizable=False, easy_drag=False, background_color="#FFFFFF",
        )
        icon = Path(__file__).resolve().parent.parent / "assets" / "logo-256.png"
        webview.start(debug=False, icon=str(icon))

    def _screen_size(self) -> tuple[int, int]:
        import webview

        try:
            screen = webview.screens[0]
            return screen.width, screen.height
        except Exception:
            return 1920, 1080

    def on_screen(self, name: str) -> None:
        if not self.main:
            return
        if name == "complete":
            warnings = len((self.result or {}).get("warnings") or [])
            self.main.resize(COMPLETE_SIZE[0], COMPLETE_SIZE[1] + 34 * warnings)
        else:
            self.main.resize(*MAIN_SIZE)

    def _on_minimized(self) -> None:
        self.minimized = True
        if self.installer is not None and self.result is None and self.mini:
            width, height = self._screen_size()
            self.mini.move((width - MINI_SIZE[0]) // 2, height - MINI_SIZE[1] - 72)
            self.mini.show()

    def _on_restored(self) -> None:
        self.minimized = False
        if self.mini:
            self.mini.hide()

    def _on_closing(self) -> bool:
        if self.installer is not None and self.result is None:
            # Same as pressing Cancel: ask (from the page, not from inside this event), then roll back.
            threading.Thread(target=lambda: self.main.evaluate_js("document.getElementById('cancelInstall').click()"),
                             daemon=True).start()
            return False
        self._destroy_others()
        return True

    def show_main(self) -> None:
        if self.mini:
            self.mini.hide()
        if self.main:
            self.main.restore()
            self.main.show()

    def _destroy_others(self) -> None:
        for window in (self.mini, self.toast):
            try:
                if window:
                    window.destroy()
            except Exception:
                pass

    def close_toast(self) -> None:
        if self.toast:
            try:
                self.toast.destroy()
            except Exception:
                pass
            self.toast = None

    def _show_toast(self) -> None:
        import webview

        width, height = self._screen_size()
        self.toast = webview.create_window(
            "Archicad MCP", html=page("toast.html"), js_api=self.api,
            width=TOAST_SIZE[0], height=TOAST_SIZE[1], frameless=True, on_top=True, resizable=False,
            x=width - TOAST_SIZE[0] - 16, y=height - TOAST_SIZE[1] - 64, background_color="#1F2328", focus=False,
        )
        threading.Timer(8.0, self.close_toast).start()

    # --- installing ---------------------------------------------------------

    def start_install(self, options: Options) -> None:
        self.options = options
        self.installer = Installer(self.system, options, payload_path(), self._on_progress)
        threading.Thread(target=self._install_thread, daemon=True).start()

    def _install_thread(self) -> None:
        state = self.installer.run()
        if state["status"] == "done":
            self._copy_guide()
            try:
                state["archicadRunning"] = bool(ArchicadConnection().discover())
            except Exception:
                state["archicadRunning"] = False
            if state["archicadRunning"] and self.options.install_addon:
                state["warnings"].append("Restart Archicad to load the add-on and the MCP panel.")
        self.result = state
        self._push(state)
        if self.mini:
            self.mini.hide()
        if state["status"] == "done":
            self._show_toast()

    def _on_progress(self, state: dict[str, Any]) -> None:
        if state["status"] == "running":
            self._push(state)

    def _push(self, state: dict[str, Any]) -> None:
        if self.main:
            self.main.evaluate_js(f"window.setup.progress({json.dumps(state)})")
        if self.mini:
            label = "Installing Archicad MCP..." if state["status"] == "running" else "Archicad MCP is installed."
            self.mini.evaluate_js(f"window.setPercent({int(state['percent'])}, {json.dumps(label)})")

    def _copy_guide(self) -> None:
        """Keep the quick start guide next to the settings, where it outlives the setup program."""
        from importlib import resources

        guide = self.system.data_dir / "guide"
        guide.mkdir(parents=True, exist_ok=True)
        assets = resources.files("archicad_mcp").joinpath("assets")
        for name in ("quickstart.html", "logo-128.png"):
            (guide / name).write_bytes(assets.joinpath(name).read_bytes())

    def finish(self, open_archicad: bool, show_guide: bool) -> None:
        if open_archicad and self.options and self.options.targets:
            try:
                self.system.open_archicad(self.options.targets[0])
            except Exception:
                log.exception("Could not start Archicad")
        if show_guide:
            webbrowser.open((self.system.data_dir / "guide" / "quickstart.html").as_uri())
        self.quit()

    def quit(self) -> None:
        self.installer = None
        self.result = self.result or {"status": "closed"}
        self._destroy_others()
        if self.main:
            self.main.destroy()


def run_silent(system: System, addon: bool, desktop_app: bool) -> int:
    """Install without windows, for IT deployment and automated tests. Returns the exit code."""
    options = Options([t for t in system.detect_archicad()], install_addon=addon, desktop_app=desktop_app)
    last = {"percent": -1}

    def report(state: dict[str, Any]) -> None:
        if state["percent"] != last["percent"]:
            last["percent"] = state["percent"]
            active = next((s["label"] for s in state["steps"] if s["state"] == "active"), "")
            print(f"{state['percent']:3d}% {active}", flush=True)

    state = Installer(system, options, payload_path(), report).run()
    for warning in state["warnings"]:
        print(f"warning: {warning}", flush=True)
    if state["status"] != "done":
        print(f"error: {state.get('error') or state['status']}", flush=True)
        return 1
    print("Archicad MCP is installed.", flush=True)
    return 0
