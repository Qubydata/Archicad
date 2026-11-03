"""Automatic updates: find a newer release, download its setup and run it silently.

The feed is https://tevavid.com/downloads/latest.json, which every release
makes (scripts/make_feed.py) next to its installers:

    {"version": "0.2.0", "notes": "...", "url": "https://tevavid.com/...",
     "assets": {"win32": {"url": "0.2.0/TevavidToolsSetup.exe", "sha256": "...", "size": 123}}}

Asset URLs may be relative to the feed. The TEVAVID_UPDATE_FEED environment
variable points copies elsewhere, for testing; a GitHub "latest release" API
URL works too.

The background service checks once a day, downloads a newer setup by itself
and installs it while no Archicad is running. The panel shows the same state
with an "Update now" button. Setup keeps the installed tools and options, and
a failed update leaves the old version in place.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import __version__

log = logging.getLogger("archicad_mcp.updater")

# Where releases are published: the installers and latest.json, publicly downloadable.
DOWNLOADS_URL = "https://tevavid.com/downloads"
FEED_ENV = "TEVAVID_UPDATE_FEED"
CHECK_EVERY = 24 * 3600
ASSETS = {"win32": "TevavidToolsSetup.exe", "darwin": "TevavidToolsSetup-macOS.zip", "linux": "TevavidToolsSetup-linux"}


class UpdateError(Exception):
    """Shown to the user as is."""


def feed_url() -> str:
    return os.environ.get(FEED_ENV) or f"{DOWNLOADS_URL}/latest.json"


def parse_version(text: str) -> tuple[int, ...]:
    parts = []
    for part in text.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
        if not part.isdigit():
            break  # 1.2.0-beta: stop at the suffix
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


@dataclass
class Release:
    version: str
    notes: str
    page: str
    url: str  # the setup for this platform
    size: int = 0
    sha256: str = ""
    published: str = ""


def _get_json(url: str, timeout: float = 20) -> Any:
    headers = {"User-Agent": f"TevavidTools/{__version__}", "Accept": "application/vnd.github+json"}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
        return json.loads(response.read())


def parse_feed(data: dict[str, Any], platform: str, base: str = "") -> Release | None:
    """A GitHub release or a plain JSON feed, as the Release for this platform (None when it has no setup for it)."""
    name = ASSETS.get(platform)
    if "tag_name" in data:  # GitHub
        if data.get("draft") or data.get("prerelease"):
            return None
        asset = next((a for a in data.get("assets", []) if a.get("name") == name), None)
        if asset is None:
            return None
        digest = asset.get("digest") or ""
        return Release(
            version=str(data["tag_name"]).lstrip("vV"), notes=data.get("body") or "", page=data.get("html_url") or "",
            url=asset["browser_download_url"], size=int(asset.get("size") or 0),
            sha256=digest.split(":", 1)[1] if digest.startswith("sha256:") else "", published=data.get("published_at") or "",
        )
    asset = (data.get("assets") or {}).get(platform)
    if not asset or "version" not in data:
        return None
    return Release(
        version=str(data["version"]).lstrip("vV"), notes=data.get("notes") or "", page=data.get("url") or "",
        url=urllib.parse.urljoin(base, asset["url"]), size=int(asset.get("size") or 0), sha256=asset.get("sha256") or "",
        published=data.get("published") or "",
    )


def fetch_latest(platform: str = sys.platform) -> Release | None:
    try:
        url = feed_url()
        return parse_feed(_get_json(url), platform, base=url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("No release has been published yet.") from exc
        raise UpdateError(f"The update server answered {exc.code}.") from exc
    except (OSError, ValueError) as exc:
        raise UpdateError("Could not reach the update server. Check the internet connection.") from exc


def download_file(url: str, destination: Path, progress: Callable[[float], None]) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": f"TevavidTools/{__version__}"})
    with urllib.request.urlopen(request, timeout=60) as response, open(destination, "wb") as file:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while chunk := response.read(256 * 1024):
            file.write(chunk)
            done += len(chunk)
            if total:
                progress(done / total)
    progress(1.0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _spawn(command: list[str]) -> None:
    from .installer.system import spawn_detached

    spawn_detached(command)


class Updater:
    def __init__(
        self,
        data_dir: Path,
        current: str = __version__,
        platform: str = sys.platform,
        fetch: Callable[[str], Release | None] = fetch_latest,
        downloader: Callable[[str, Path, Callable[[float], None]], None] = download_file,
        spawn: Callable[[list[str]], None] = _spawn,
        can_install: bool | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.current = current
        self.platform = platform
        self.fetch = fetch
        self.downloader = downloader
        self.spawn = spawn
        # Only the packaged app installs updates; a copy run from source is updated with git.
        self.can_install = bool(getattr(sys, "frozen", False)) if can_install is None else can_install
        self.state_file = data_dir / "update.json"
        self.folder = data_dir / "updates"
        self.progress: float | None = None  # while downloading

    # --- state --------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=2), encoding="utf8")

    def _latest(self, state: dict[str, Any]) -> Release | None:
        try:
            return Release(**state["latest"]) if state.get("latest") else None
        except TypeError:
            return None

    def _ready_file(self, state: dict[str, Any], latest: Release | None) -> Path | None:
        if not latest or state.get("downloadedVersion") != latest.version:
            return None
        path = Path(state.get("downloaded", ""))
        return path if path.is_file() else None

    def status(self) -> dict[str, Any]:
        state = self._load()
        latest = self._latest(state)
        available = bool(latest and is_newer(latest.version, self.current))
        return {
            "current": self.current,
            "latest": latest.version if latest else None,
            "available": available,
            "notes": latest.notes if available else "",
            "page": latest.page if latest else "",
            "size": latest.size if latest else 0,
            "checked": state.get("checked"),
            "error": state.get("error"),
            "ready": available and self._ready_file(state, latest) is not None,
            "downloading": self.progress is not None,
            "progress": self.progress,
            "canInstall": self.can_install,
            "failedVersion": state.get("failedVersion"),
        }

    # --- actions ------------------------------------------------------------

    def check(self, force: bool = False) -> dict[str, Any]:
        """Ask the feed for the latest release (at most once a day unless forced)."""
        state = self._load()
        if not force and time.time() - float(state.get("checked") or 0) < CHECK_EVERY:
            return self.status()
        state["checked"] = time.time()
        try:
            latest = self.fetch(self.platform)
            state["latest"] = asdict(latest) if latest else None
            state["error"] = None
        except UpdateError as exc:
            state["error"] = str(exc)
        self._save(state)
        self._clean_old_downloads(state)
        return self.status()

    def download(self) -> Path:
        state = self._load()
        latest = self._latest(state)
        if not latest or not is_newer(latest.version, self.current):
            raise UpdateError("Tevavid Tools is up to date.")
        ready = self._ready_file(state, latest)
        if ready:
            return ready
        self.folder.mkdir(parents=True, exist_ok=True)
        # The setup keeps its own file name: the app tells setup from panel by it.
        destination = self.folder / latest.version / ASSETS.get(self.platform, "TevavidToolsSetup")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        self.progress = 0.0
        try:
            self.downloader(latest.url, partial, lambda f: setattr(self, "progress", max(0.0, min(1.0, f))))
            if latest.sha256 and _sha256(partial) != latest.sha256.lower():
                raise UpdateError("The downloaded update was damaged. It will be downloaded again.")
            partial.replace(destination)
        except UpdateError:
            partial.unlink(missing_ok=True)
            raise
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise UpdateError("The update could not be downloaded. Check the internet connection.") from exc
        finally:
            self.progress = None
        state.update(downloaded=str(destination), downloadedVersion=latest.version)
        self._save(state)
        return destination

    def install(self, relaunch: bool = True) -> None:
        """Start the downloaded setup to update silently. Setup stops this app, so call it last."""
        if not self.can_install:
            raise UpdateError("Updates install from the packaged app. Download the new setup from the release page.")
        setup = self.download()
        args = ["--silent", "--update"] + (["--relaunch"] if relaunch else [])
        state = self._load()
        state["installing"] = {"version": state.get("downloadedVersion"), "started": time.time()}
        self._save(state)
        self.spawn([str(self._setup_executable(setup)), *args])

    def _setup_executable(self, setup: Path) -> Path:
        if self.platform == "darwin":
            # The macOS setup is a zipped app; ditto unpacks it with its signature intact.
            folder = Path(tempfile.mkdtemp(prefix="unpacked-", dir=setup.parent))
            if subprocess.run(["ditto", "-x", "-k", str(setup), str(folder)]).returncode != 0:
                raise UpdateError("The update could not be unpacked.")
            apps = list(folder.glob("*.app"))
            if not apps:
                raise UpdateError("The update download has no setup inside.")
            return apps[0] / "Contents" / "MacOS" / "TevavidToolsSetup"
        if self.platform != "win32":
            setup.chmod(0o755)
        return setup

    def after_start(self) -> None:
        """Called when the app starts: notice whether an update that was started went through."""
        state = self._load()
        attempt = state.pop("installing", None)
        if not attempt:
            return
        if attempt.get("version") and is_newer(attempt["version"], self.current):
            state["failedVersion"] = attempt["version"]  # still the old version: setup failed and rolled back
            log.warning("The update to %s did not install; see setup.log", attempt["version"])
        else:
            state.pop("failedVersion", None)
        self._save(state)

    def tick(self, archicad_running: bool, automatic: bool) -> str | None:
        """One round of the background service: check, download, and install while Archicad is closed."""
        status = self.check()
        if not (automatic and status["available"] and self.can_install):
            return None
        if status["failedVersion"] == status["latest"]:
            return None  # this version failed once; the panel offers it, but it is not retried by itself
        try:
            self.download()
        except UpdateError as exc:
            log.info("Update download failed: %s", exc)
            return None
        if archicad_running:
            return "ready"
        self.install(relaunch=False)
        return "installing"

    def _clean_old_downloads(self, state: dict[str, Any]) -> None:
        keep = str(Path(state["downloaded"]).parent) if state.get("downloaded") else None
        if not self.folder.is_dir():
            return
        for item in self.folder.iterdir():
            if str(item) != keep:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
