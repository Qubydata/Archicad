"""The install steps, their progress, cancel with rollback, and uninstall.

Every change the installer makes is written to a manifest
(~/.archicad-mcp/install.json). Rollback and uninstall undo the manifest's
actions in reverse order. The Tapir add-on is not removed on uninstall, since
other tools may use it.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import __version__
from . import system as sysmod
from .system import APP_NAME, ArchicadInstall, InstallerError, System

log = logging.getLogger("archicad_mcp.installer")


@dataclass(frozen=True)
class Step:
    key: str
    label: str
    weight: int


STEPS = [
    Step("download", "Downloading files", 25),
    Step("core", "Installing core components", 30),
    Step("addon", "Adding Archicad add-on", 25),
    Step("ai", "Configuring AI service", 10),
    Step("finalize", "Finalizing installation", 10),
]


@dataclass
class Options:
    targets: list[ArchicadInstall] = field(default_factory=list)
    install_addon: bool = True
    desktop_app: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Options:
        targets = [ArchicadInstall(int(t["version"]), str(t["path"]), bool(t.get("detected"))) for t in data.get("targets", [])]
        return cls(targets, bool(data.get("installAddon", True)), bool(data.get("desktopApp", True)))


class Cancelled(Exception):
    pass


Downloader = Callable[[str, Path, Callable[[float], None], Callable[[], bool]], None]


def download(url: str, destination: Path, progress: Callable[[float], None], cancelled: Callable[[], bool]) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": f"ArchicadMCPSetup/{__version__}"})
    with urllib.request.urlopen(request, timeout=60) as response, open(destination, "wb") as file:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while chunk := response.read(256 * 1024):
            if cancelled():
                raise Cancelled()
            file.write(chunk)
            done += len(chunk)
            if total:
                progress(done / total)
    progress(1.0)


LAUNCHER_SCRIPT = '''"""Opens the Archicad MCP panel. Installed by the Archicad MCP setup; run it from the Tapir palette."""

import subprocess
import sys

COMMAND = {command!r}

# Tapir passes --port <Archicad port>; the panel uses it to talk to this Archicad.
args = COMMAND + ["--exit-with-archicad"] + sys.argv[1:]
if sys.platform == "win32":
    subprocess.Popen(args, creationflags=0x00000008 | 0x00000200 | 0x08000000, close_fds=True)
else:
    subprocess.Popen(args, start_new_session=True, close_fds=True)
print("Opening the Archicad MCP panel...")
'''

LAUNCH_AGENT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>{arguments}</array>
  <key>RunAtLoad</key><true/>
  <key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
"""


class Installer:
    def __init__(
        self,
        system: System,
        options: Options,
        payload: Path | None,
        on_progress: Callable[[dict[str, Any]], None] = lambda state: None,
        downloader: Downloader = download,
    ) -> None:
        self.system = system
        self.options = options
        self.payload = payload
        self.on_progress = on_progress
        self.downloader = downloader
        self.actions: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.work = Path(tempfile.mkdtemp(prefix="archicad-mcp-setup-"))
        self.downloads: dict[str, Path] = {}
        self._cancel = threading.Event()
        self.state: dict[str, Any] = {
            "status": "running",
            "percent": 0,
            "steps": [{"key": s.key, "label": s.label, "state": "pending"} for s in STEPS],
            "warnings": self.warnings,
            "error": None,
        }

    # --- progress -----------------------------------------------------------

    def cancel(self) -> None:
        self._cancel.set()

    def _check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def _report(self, index: int, fraction: float) -> None:
        total = sum(s.weight for s in STEPS)
        done = sum(s.weight for s in STEPS[:index]) + STEPS[index].weight * max(0.0, min(1.0, fraction))
        percent = int(round(100 * done / total))
        if percent != self.state["percent"] or fraction in (0.0, 1.0):
            self.state["percent"] = percent
            self.on_progress(self.state)

    def run(self) -> dict[str, Any]:
        handlers = {
            "download": self._download,
            "core": self._core,
            "addon": self._addon,
            "ai": self._ai,
            "finalize": self._finalize,
        }
        try:
            for index, step in enumerate(STEPS):
                self._check()
                self.state["steps"][index]["state"] = "active"
                self._report(index, 0.0)
                warnings_before = len(self.warnings)
                handlers[step.key](lambda f, i=index: self._report(i, f))
                self.state["steps"][index]["state"] = "warning" if len(self.warnings) > warnings_before else "done"
                self._report(index, 1.0)
            self.state["status"] = "done"
        except Cancelled:
            self._rollback()
            self.state["status"] = "cancelled"
        except Exception as exc:  # shown on the screen, then rolled back
            log.exception("Installation failed")
            for step_state in self.state["steps"]:
                if step_state["state"] == "active":
                    step_state["state"] = "failed"
            self.state["error"] = str(exc) if isinstance(exc, InstallerError) else f"{type(exc).__name__}: {exc}"
            self._rollback()
            self.state["status"] = "failed"
        finally:
            shutil.rmtree(self.work, ignore_errors=True)
        self.on_progress(self.state)
        return self.state

    # --- steps --------------------------------------------------------------

    def _download(self, progress: Callable[[float], None]) -> None:
        wanted: list[tuple[str, str]] = []
        if self.options.install_addon and self.options.targets:
            wanted.append(("tapir", f"{sysmod.TAPIR_RELEASES}/{self.system.tapir_installer_asset()}"))
        if self.options.install_addon and not self.system.has_uv():
            wanted.append(("uv", f"{sysmod.UV_RELEASES}/{self.system.uv_asset()}"))
        for number, (name, url) in enumerate(wanted):
            destination = self.work / url.rsplit("/", 1)[1]
            share = 1 / len(wanted)
            try:
                self.downloader(url, destination, lambda f, n=number, s=share: progress((n + f) * s), self._cancel.is_set)
                self.downloads[name] = destination
            except Cancelled:
                raise
            except Exception as exc:
                log.warning("Download of %s failed: %s", url, exc)
                what = "the Tapir add-on" if name == "tapir" else "uv (used to run Archicad scripts)"
                self.warnings.append(f"Could not download {what}. Check the internet connection and run setup again.")
        progress(1.0)

    def _core(self, progress: Callable[[float], None]) -> None:
        system = self.system
        system.stop_running_app()
        progress(0.1)
        if self.payload is not None:
            target = system.install_dir
            if target.exists():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            self.actions.append({"type": "dir", "path": str(target)})
            if system.is_mac:
                # ditto keeps the app bundle's symlinks, permissions and signature.
                if subprocess.run(["ditto", "-x", "-k", str(self.payload), str(target.parent)]).returncode != 0:
                    raise InstallerError("Could not copy the app into your Applications folder.")
            else:
                with zipfile.ZipFile(self.payload) as archive:
                    members = archive.infolist()
                    for number, member in enumerate(members):
                        self._check()
                        extracted = Path(archive.extract(member, target))
                        mode = (member.external_attr >> 16) & 0o777
                        if mode & 0o111:  # zipfile does not restore the executable bit itself
                            extracted.chmod(mode)
                        if number % 50 == 0:
                            progress(0.1 + 0.8 * number / max(1, len(members)))
        progress(0.9)
        if system.is_windows:
            exe = system.app_command()[0]
            sysmod.windows_set_registry(sysmod.UNINSTALL_KEY, {
                "DisplayName": APP_NAME,
                "DisplayVersion": __version__,
                "Publisher": sysmod.PUBLISHER,
                "DisplayIcon": exe,
                "InstallLocation": str(system.install_dir),
                "UninstallString": subprocess.list2cmdline(system.app_command("uninstall")),
                "QuietUninstallString": subprocess.list2cmdline(system.app_command("uninstall", "--quiet")),
                "NoModify": 1,
                "NoRepair": 1,
            })
            self.actions.append({"type": "registry", "key": sysmod.UNINSTALL_KEY})
            command = system.app_command("panel")
            sysmod.windows_create_shortcut(system.start_menu_shortcut, command[0], subprocess.list2cmdline(command[1:]),
                                           exe, "Chat with Claude to change your Archicad model")
            self.actions.append({"type": "file", "path": str(system.start_menu_shortcut)})
        progress(1.0)

    def _addon(self, progress: Callable[[float], None]) -> None:
        if not self.options.install_addon:
            progress(1.0)
            return
        system = self.system
        # 1. The palette launcher: "Archicad MCP" in the Tapir palette's script list.
        script = system.tapir_scripts_dir / f"{APP_NAME}.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(LAUNCHER_SCRIPT.format(command=system.app_command("panel")), encoding="utf8")
        self.actions.append({"type": "file", "path": str(script)})
        progress(0.1)

        # 2. uv, which the Tapir palette uses to run Python scripts (kept on uninstall, it is a shared tool).
        if "uv" in self.downloads:
            try:
                self._install_uv(self.downloads["uv"])
            except Exception as exc:
                log.warning("uv install failed: %s", exc)
                self.warnings.append("Could not install uv. Tapir will offer to install it the first time you run a script.")
        progress(0.25)

        # 3. The Tapir add-on itself.
        if not self.options.targets:
            self.warnings.append("No Archicad version was selected, so the Tapir add-on was not installed.")
            progress(1.0)
            return
        if "tapir" not in self.downloads:
            progress(1.0)
            return
        installer = self._tapir_executable(self.downloads["tapir"])
        detected = [t for t in self.options.targets if t.detected]
        custom = [t for t in self.options.targets if not t.detected]
        calls: list[list[str]] = []
        if detected:
            calls.append([installer, "--console", "--versions", ",".join(str(t.version) for t in detected)])
        for target in custom:
            calls.append([installer, "--console", "--versions", str(target.version),
                          "--addOnsFolder", str(system.addons_folder(target))])
        for number, command in enumerate(calls):
            self._check()
            try:
                code = system.run(command) if not system.is_windows else system.run_elevated(command)
                if code != 0 and system.is_mac:
                    code = system.run_elevated(command)
            except InstallerError as exc:
                self.warnings.append(str(exc))
                continue
            if code != 0:
                self.warnings.append(f"The Tapir add-on installer stopped with code {code}. "
                                     "You can install the add-on later from github.com/ENZYME-APD/tapir-archicad-automation.")
            progress(0.25 + 0.75 * (number + 1) / len(calls))
        progress(1.0)

    def _ai(self, progress: Callable[[float], None]) -> None:
        system = self.system
        self._configure_claude_desktop()
        progress(0.4)
        settings_file = system.data_dir / "config.json"
        if not settings_file.exists():
            system.data_dir.mkdir(parents=True, exist_ok=True)
            settings_file.write_text(json.dumps({"auto_open_panel": self.options.desktop_app}, indent=2), encoding="utf8")
        progress(0.6)
        if self.options.desktop_app:
            command = system.app_command("panel", "--background")
            if system.is_windows:
                sysmod.windows_set_registry(sysmod.RUN_KEY, {APP_NAME: subprocess.list2cmdline(command)})
                self.actions.append({"type": "registry", "key": sysmod.RUN_KEY, "value": APP_NAME})
            elif system.is_mac:
                plist = system.launch_agent_path
                plist.parent.mkdir(parents=True, exist_ok=True)
                arguments = "".join(f"<string>{_xml(part)}</string>" for part in command)
                plist.write_text(LAUNCH_AGENT.format(label=sysmod.LAUNCH_AGENT_LABEL, arguments=arguments), encoding="utf8")
                self.actions.append({"type": "launch_agent", "path": str(plist)})
        progress(1.0)

    def _finalize(self, progress: Callable[[float], None]) -> None:
        system = self.system
        if self.payload is not None and not Path(system.app_command()[0]).exists():
            raise InstallerError("The Archicad MCP app is missing after the copy. Run setup again.")
        progress(0.3)
        self._write_manifest()
        progress(0.6)
        if self.options.desktop_app:
            sysmod.spawn_detached(system.app_command("panel", "--background"))
        time.sleep(0.2)
        progress(1.0)

    # --- helpers ------------------------------------------------------------

    def _install_uv(self, archive: Path) -> None:
        target = self.system.uv_dir
        target.mkdir(parents=True, exist_ok=True)
        names = {"uv", "uvx", "uv.exe", "uvx.exe"}
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                for member in zf.infolist():
                    name = Path(member.filename).name
                    if name in names:
                        (target / name).write_bytes(zf.read(member))
        else:
            with tarfile.open(archive) as tf:
                for member in tf.getmembers():
                    name = Path(member.name).name
                    if name in names and member.isfile():
                        (target / name).write_bytes(tf.extractfile(member).read())
                        (target / name).chmod(0o755)

    def _tapir_executable(self, download_path: Path) -> str:
        if download_path.suffix != ".zip":
            return str(download_path)
        folder = self.work / "tapir"
        folder.mkdir(parents=True, exist_ok=True)
        if self.system.is_mac:
            # ditto keeps the signed app bundle intact; zipfile would drop its symlinks.
            if subprocess.run(["ditto", "-x", "-k", str(download_path), str(folder)]).returncode != 0:
                raise InstallerError("Could not unpack the Tapir installer.")
        else:
            with zipfile.ZipFile(download_path) as zf:
                zf.extractall(folder)
        apps = list(folder.rglob("*.app"))
        if apps:
            executables = [p for p in (apps[0] / "Contents" / "MacOS").iterdir() if p.is_file()]
            if executables:
                return str(executables[0])
        files = [p for p in folder.rglob("*") if p.is_file()]
        if not files:
            raise InstallerError("The Tapir installer download was empty.")
        return str(files[0])

    def _configure_claude_desktop(self) -> None:
        path = self.system.claude_config_path
        if not path.parent.exists():
            return  # Claude Desktop is not installed
        try:
            config = json.loads(path.read_text(encoding="utf8")) if path.exists() else {}
            if not isinstance(config, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            self.warnings.append("Claude Desktop's settings file could not be read, so Archicad MCP was not added to it.")
            return
        servers = config.setdefault("mcpServers", {})
        previous = servers.get("archicad")
        command = self.system.server_command()
        servers["archicad"] = {"command": command[0], "args": command[1:]}
        path.write_text(json.dumps(config, indent=2), encoding="utf8")
        self.actions.append({"type": "claude_config", "path": str(path), "previous": previous})

    def _write_manifest(self) -> None:
        manifest = {
            "version": __version__,
            "installed": time.time(),
            "installDir": str(self.system.install_dir),
            "targets": [t.to_dict() for t in self.options.targets],
            "options": {"installAddon": self.options.install_addon, "desktopApp": self.options.desktop_app},
            "actions": self.actions,
        }
        self.system.data_dir.mkdir(parents=True, exist_ok=True)
        self.system.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf8")

    def _rollback(self) -> None:
        undo_actions(self.system, self.actions)
        self.actions.clear()


def _xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def undo_actions(system: System, actions: list[dict[str, Any]], skip_dirs: bool = False) -> None:
    for action in reversed(actions):
        try:
            kind = action["type"]
            if kind == "dir" and not skip_dirs:
                shutil.rmtree(action["path"], ignore_errors=True)
            elif kind == "file":
                Path(action["path"]).unlink(missing_ok=True)
            elif kind == "registry" and system.is_windows:
                sysmod.windows_delete_registry(action["key"], action.get("value"))
            elif kind == "launch_agent":
                Path(action["path"]).unlink(missing_ok=True)
            elif kind == "claude_config":
                path = Path(action["path"])
                config = json.loads(path.read_text(encoding="utf8"))
                servers = config.get("mcpServers", {})
                if action.get("previous") is None:
                    servers.pop("archicad", None)
                else:
                    servers["archicad"] = action["previous"]
                path.write_text(json.dumps(config, indent=2), encoding="utf8")
        except Exception:
            log.exception("Could not undo %s", action)


def read_manifest(system: System) -> dict[str, Any] | None:
    try:
        return json.loads(system.manifest_path.read_text(encoding="utf8"))
    except (OSError, ValueError):
        return None


def uninstall(system: System) -> None:
    """Remove what the installer added. Settings and conversations stay in ~/.archicad-mcp."""
    manifest = read_manifest(system)
    system.stop_running_app()
    actions = manifest["actions"] if manifest else []
    running_from_install_dir = system.frozen and Path(sys.executable).resolve().is_relative_to(system.install_dir.resolve())
    undo_actions(system, actions, skip_dirs=running_from_install_dir)
    system.manifest_path.unlink(missing_ok=True)
    if running_from_install_dir:
        _remove_after_exit(system, system.install_dir)


def _remove_after_exit(system: System, folder: Path) -> None:
    """The running app cannot delete its own folder, so a helper does it once we have exited."""
    if system.is_windows:
        command = f'ping 127.0.0.1 -n 4 > nul & rmdir /s /q "{folder}"'
        sysmod.spawn_detached(["cmd", "/c", command])
    else:
        sysmod.spawn_detached(["/bin/sh", "-c", f"sleep 3; rm -rf {sysmod._sh_quote(str(folder))}"])
