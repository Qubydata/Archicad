"""The install steps, their progress, cancel with rollback, and uninstall.

Every change the installer makes is written to a manifest
(~/.tevavid-tools/install.json). Rollback and uninstall undo the manifest's
actions in reverse order. The Tapir add-on is not removed on uninstall, since
other tools may use it.

Running setup again over an installed copy updates it: the new app is unpacked
next to the old one and swapped in at the end, so a failed or cancelled update
leaves the old version working. Components that were dropped (a tool that was
unticked, the background service) are removed, and the Tapir add-on is only
installed for Archicad versions that did not have it yet.
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

from .. import __version__, claude_desktop, suite
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


# The tools that existed before the manifest recorded "offered", so an update knows which tools are new.
FIRST_RELEASE_TOOLS = ["assistant", "sun"]


@dataclass
class Options:
    targets: list[ArchicadInstall] = field(default_factory=list)
    install_addon: bool = True  # the Tapir add-on and the palette launcher, for the tools that need them
    desktop_app: bool = True
    tools: list[str] = field(default_factory=suite.default_tools)
    relaunch: bool = False  # open the panel again when done (an update started from the panel)

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any], platform: str | None = None) -> Options:
        """The choices made at the last install, for an update that asks nothing."""
        targets = [ArchicadInstall(int(t["version"]), str(t["path"]), bool(t.get("detected"))) for t in manifest.get("targets", [])]
        tools = suite.clean(manifest.get("tools", suite.default_tools(platform)), platform)
        chosen = manifest.get("options", {})
        return cls(targets, bool(chosen.get("installAddon", suite.needs_tapir(tools))), bool(chosen.get("desktopApp", True)), tools)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Options:
        targets = [ArchicadInstall(int(t["version"]), str(t["path"]), bool(t.get("detected"))) for t in data.get("targets", [])]
        tools = suite.clean(data["tools"]) if "tools" in data else suite.default_tools()
        addon = bool(data.get("installAddon", suite.needs_tapir(tools)))
        return cls(targets, addon, bool(data.get("desktopApp", True)), tools)


class Cancelled(Exception):
    pass


Downloader = Callable[[str, Path, Callable[[float], None], Callable[[], bool]], None]


def download(url: str, destination: Path, progress: Callable[[float], None], cancelled: Callable[[], bool]) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": f"TevavidToolsSetup/{__version__}"})
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


# What Tapir writes to Documents/Tapir/config.json when it starts without one.
TAPIR_CONFIG_DEFAULTS = {
    "repositories": [{"displayName": "Tapir", "repoOwner": "ENZYME-APD", "repoName": "tapir-archicad-automation",
                      "relativeLoc": "builtin-scripts", "token": "", "excludeFromDownloadPattern": "",
                      "includePattern": "", "excludePattern": ""}],
    "localFolders": [],
    "askUpdatingAddOnBeforeEachExecution": False,
    "uvLocation": "",
    "pythonLocation": "",
}

LAUNCHER_SCRIPT = '''"""Opens the Tevavid Tools panel. Installed by the Tevavid Tools setup; run it from the Tapir palette."""

import subprocess
import sys

COMMAND = {command!r}

# Tapir passes --port <Archicad port>; the panel uses it to talk to this Archicad.
args = COMMAND + ["--exit-with-archicad"] + sys.argv[1:]
if sys.platform == "win32":
    subprocess.Popen(args, creationflags=0x00000008 | 0x00000200 | 0x08000000, close_fds=True)
else:
    subprocess.Popen(args, start_new_session=True, close_fds=True)
print("Opening the Tevavid Tools panel...")
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
        self.work = Path(tempfile.mkdtemp(prefix="tevavid-setup-"))
        self.downloads: dict[str, Path] = {}
        self.previous = read_manifest(system)  # set when this run updates an installed copy
        self._previous_keys = {_key(a) for a in (self.previous or {}).get("actions", [])}
        self._staging: Path | None = None
        self._old_dir: Path | None = None  # the replaced app, kept until the update has succeeded
        self._settings_backup: str | None = None
        self.tapir_done: set[int] = set()
        self._cancel = threading.Event()
        self.state: dict[str, Any] = {
            "status": "running",
            "percent": 0,
            "steps": [{"key": s.key, "label": s.label, "state": "pending"} for s in STEPS],
            "warnings": self.warnings,
            "error": None,
            "update": self.updating,
            "previousVersion": (self.previous or {}).get("version"),
            "version": __version__,
        }

    @property
    def updating(self) -> bool:
        return self.previous is not None

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
            self._tell(self.state)

    def _tell(self, state: dict[str, Any]) -> None:
        """Report progress; a display problem must not stop or undo the installation."""
        try:
            self.on_progress(state)
        except Exception:
            log.warning("Progress display failed", exc_info=True)

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
            if self._staging is not None:
                shutil.rmtree(self._staging, ignore_errors=True)
        self._tell(self.state)
        return self.state

    # --- steps --------------------------------------------------------------

    def _download(self, progress: Callable[[float], None]) -> None:
        wanted: list[tuple[str, str]] = []
        if self.options.install_addon and self._tapir_targets():
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
            # Unpack next to the installed app, then swap: an update that fails keeps the old version.
            staging = target.parent / f".{target.name}.staging"
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            self._staging = staging
            unpacked = staging / target.name
            if system.is_mac:
                # ditto keeps the app bundle's symlinks, permissions and signature.
                if subprocess.run(["ditto", "-x", "-k", str(self.payload), str(staging)]).returncode != 0:
                    raise InstallerError("Could not copy the app into your Applications folder.")
            else:
                with zipfile.ZipFile(self.payload) as archive:
                    members = archive.infolist()
                    for number, member in enumerate(members):
                        self._check()
                        extracted = Path(archive.extract(member, unpacked))
                        mode = (member.external_attr >> 16) & 0o777
                        if mode & 0o111:  # zipfile does not restore the executable bit itself
                            extracted.chmod(mode)
                        if number % 50 == 0:
                            progress(0.1 + 0.75 * number / max(1, len(members)))
            if not unpacked.exists():
                raise InstallerError("The setup file is damaged. Download it again.")
            self._check()
            self._swap_in(unpacked, target)
            self.actions.append({"type": "dir", "path": str(target)})
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
        # 1. The palette launcher: "Tevavid Tools" in the Tapir palette's script list.
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
        self._point_tapir_at_uv()
        progress(0.25)

        # 3. The Tapir add-on itself, for the Archicad versions that do not have it from an earlier install.
        if not self.options.targets:
            self.warnings.append("No Archicad version was selected, so the Tapir add-on was not installed.")
            progress(1.0)
            return
        if "tapir" not in self.downloads:
            progress(1.0)
            return
        installer = self._tapir_executable(self.downloads["tapir"])
        wanted = self._tapir_targets()
        detected = [t for t in wanted if t.detected]
        custom = [t for t in wanted if not t.detected]
        calls: list[tuple[list[str], list[int]]] = []
        if detected:
            calls.append(([installer, "--console", "--versions", ",".join(str(t.version) for t in detected)],
                          [t.version for t in detected]))
        for target in custom:
            calls.append(([installer, "--console", "--versions", str(target.version),
                           "--addOnsFolder", str(system.addons_folder(target))], [target.version]))
        for number, (command, versions) in enumerate(calls):
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
            else:
                self.tapir_done.update(versions)
            progress(0.25 + 0.75 * (number + 1) / len(calls))
        progress(1.0)

    def _ai(self, progress: Callable[[float], None]) -> None:
        system = self.system
        if "assistant" in self.options.tools:
            self._configure_claude_desktop()
        progress(0.4)
        # The panel shows a tab for each installed tool; existing settings (like an API key) are kept.
        settings_file = system.data_dir / "config.json"
        try:
            self._settings_backup = settings_file.read_text(encoding="utf8")
            settings = json.loads(self._settings_backup)
        except (OSError, ValueError):
            settings = {"auto_open_panel": self.options.desktop_app}
        settings["tools"] = self.options.tools
        system.data_dir.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(settings, indent=2), encoding="utf8")
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
            raise InstallerError("The Tevavid Tools app is missing after the copy. Run setup again.")
        progress(0.3)
        manifest = self._write_manifest()
        if self.previous:
            # What the last install added but this one did not (an unticked tool or option) is removed.
            kept = {_key(a) for a in manifest["actions"]}
            undo_actions(system, [a for a in self.previous.get("actions", []) if _key(a) not in kept])
        if self._old_dir is not None:
            shutil.rmtree(self._old_dir, ignore_errors=True)
            self._old_dir = None
        progress(0.6)
        try:
            if self.options.desktop_app:
                command = system.app_command("panel", "--background")
                sysmod.spawn_detached(command + ["--open"] if self.options.relaunch else command)
            elif self.options.relaunch:
                sysmod.spawn_detached(system.app_command("panel", "--exit-with-archicad"))
        except OSError as exc:
            log.warning("Could not start Tevavid Tools: %s", exc)
        time.sleep(0.2)
        progress(1.0)

    # --- helpers ------------------------------------------------------------

    def _tapir_installed(self) -> set[int]:
        """Archicad versions an earlier install already put the Tapir add-on into."""
        if not self.previous or not self.previous.get("options", {}).get("installAddon"):
            return set()
        if "tapirInstalled" in self.previous:
            return {int(v) for v in self.previous["tapirInstalled"]}
        return {int(t["version"]) for t in self.previous.get("targets", [])}  # manifests of 0.1.0

    def _tapir_targets(self) -> list[ArchicadInstall]:
        installed = self._tapir_installed()
        return [t for t in self.options.targets if t.version not in installed]

    def _swap_in(self, unpacked: Path, target: Path) -> None:
        old = target.parent / f".{target.name}.old"
        shutil.rmtree(old, ignore_errors=True)
        if target.exists():
            for attempt in range(6):
                try:
                    target.rename(old)
                    break
                except OSError:
                    if attempt == 5:
                        raise InstallerError("Tevavid Tools is still in use. Close Claude Desktop and run setup again.")
                    self.system.stop_running_app()  # Windows keeps running programs' files locked
                    time.sleep(1)
            self._old_dir = old
        unpacked.rename(target)

    def _point_tapir_at_uv(self) -> None:
        """Tell Tapir where uv is (Documents/Tapir/config.json, "uvLocation").

        On Windows Tapir only looks for uv on the PATH that Archicad started with, and the uv we
        install is not on it, so without this Tapir reports that uv is missing.
        """
        system = self.system
        uv = system.uv_dir / ("uv.exe" if system.is_windows else "uv")
        if not uv.exists():
            found = shutil.which("uv")
            if not found:
                return  # nothing to point at: the uv download failed, which was already reported
            uv = Path(found)
        path = system.tapir_scripts_dir.parent / "config.json"
        try:
            config = json.loads(path.read_text(encoding="utf8")) if path.exists() else dict(TAPIR_CONFIG_DEFAULTS)
            if not isinstance(config, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            log.warning("Tapir's config.json could not be read; writing a fresh one")
            config = dict(TAPIR_CONFIG_DEFAULTS)
        previous = config.get("uvLocation", "")  # an update carries the first install's value over
        config["uvLocation"] = str(uv)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=4), encoding="utf8")
        self.actions.append({"type": "tapir_uv", "path": str(path), "previous": previous})

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
        if not claude_desktop.is_installed(path):
            return  # Claude Desktop is not installed
        try:
            previous = claude_desktop.connect(self.system.server_command(), path)
        except claude_desktop.ClaudeDesktopError:
            self.warnings.append("Claude Desktop's settings file could not be read, so Tevavid Tools was not added to it.")
            return
        self.actions.append({"type": "claude_config", "path": str(path), "previous": previous})

    def _merged_actions(self) -> list[dict[str, Any]]:
        """This run's actions, in the order the first install made them, so uninstall can undo them all."""
        new = {_key(a): a for a in self.actions}
        merged = []
        for old in (self.previous or {}).get("actions", []):
            action = new.pop(_key(old), None)
            if action is None:
                continue
            if action["type"] in ("claude_config", "tapir_uv"):
                # Uninstall restores what was there before Tevavid Tools, not our own older entry.
                action = {**action, "previous": old.get("previous")}
            merged.append(action)
        return merged + list(new.values())

    def _write_manifest(self) -> dict[str, Any]:
        previous = self.previous or {}
        tapir = sorted(self._tapir_installed() | self.tapir_done) if self.options.install_addon else []
        manifest = {
            "version": __version__,
            "installed": previous.get("installed", time.time()),
            "updated": time.time(),
            "installDir": str(self.system.install_dir),
            "targets": [t.to_dict() for t in self.options.targets],
            "options": {"installAddon": self.options.install_addon, "desktopApp": self.options.desktop_app},
            "tools": self.options.tools,
            "offered": [t.id for t in suite.TOOLS if t.available and t.supported(self.system.platform)],
            "tapirInstalled": tapir,
            "actions": self._merged_actions(),
        }
        self.system.data_dir.mkdir(parents=True, exist_ok=True)
        self.system.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf8")
        return manifest

    def _rollback(self) -> None:
        # Undo only what this run added; what an earlier install made stays, with the old app put back.
        undo_actions(self.system, [a for a in self.actions if _key(a) not in self._previous_keys])
        if self._old_dir is not None and self._old_dir.exists():
            target = self.system.install_dir
            shutil.rmtree(target, ignore_errors=True)
            self._old_dir.rename(target)
            self._old_dir = None
        if self.previous and self._settings_backup is not None:
            (self.system.data_dir / "config.json").write_text(self._settings_backup, encoding="utf8")
        self.actions.clear()


def _key(action: dict[str, Any]) -> tuple:
    """What an action changed, to match the same change across installs."""
    return action["type"], action.get("path") or action.get("key"), action.get("value")


def offered_tools(manifest: dict[str, Any]) -> list[str]:
    """The tools the installed copy offered, so setup can mark the newer ones as new."""
    return manifest.get("offered", FIRST_RELEASE_TOOLS)


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
            elif kind == "tapir_uv":
                path = Path(action["path"])
                config = json.loads(path.read_text(encoding="utf8"))
                config["uvLocation"] = action.get("previous") or ""
                path.write_text(json.dumps(config, indent=4), encoding="utf8")
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
    """Remove what the installer added. Settings and conversations stay in ~/.tevavid-tools."""
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
        # cmd /s /c "...": cmd drops the outer quotes and keeps the ones around the path,
        # which has a space ("Tevavid Tools"). A list would get list2cmdline's \" quoting, which cmd ignores.
        sysmod.spawn_detached(f'cmd.exe /s /c "ping 127.0.0.1 -n 4 > nul & rmdir /s /q "{folder}""')
    else:
        sysmod.spawn_detached(["/bin/sh", "-c", f"sleep 3; rm -rf {sysmod._sh_quote(str(folder))}"])
