import hashlib
import json
import time

import pytest

from archicad_mcp import updater
from archicad_mcp.updater import Release, UpdateError, Updater

SETUP = b"new setup program"


def github_release(version="0.2.0", digest=True, **extra):
    return {
        "tag_name": f"v{version}", "body": "- Faster sun tiles\n- New tool", "html_url": "https://example.test/r",
        "draft": False, "prerelease": False, "published_at": "2026-11-01T10:00:00Z",
        "assets": [
            {"name": "TevavidToolsSetup.exe", "browser_download_url": "https://example.test/win.exe", "size": 10,
             "digest": "sha256:" + hashlib.sha256(SETUP).hexdigest() if digest else None},
            {"name": "TevavidToolsSetup-macOS.zip", "browser_download_url": "https://example.test/mac.zip", "size": 11},
        ],
        **extra,
    }


def test_versions():
    assert updater.parse_version("v1.10.0") == (1, 10, 0)
    assert updater.is_newer("1.10.0", "1.9.3") and not updater.is_newer("0.1.0", "0.1.0")
    assert updater.parse_version("1.2.0-beta") == (1, 2, 0)


def test_feeds():
    release = updater.parse_feed(github_release(), "win32")
    assert release.version == "0.2.0" and release.url.endswith("win.exe") and len(release.sha256) == 64
    assert updater.parse_feed(github_release(), "darwin").sha256 == ""
    assert updater.parse_feed(github_release(), "linux") is None  # no setup for it
    assert updater.parse_feed(github_release(prerelease=True), "win32") is None
    plain = updater.parse_feed({"version": "0.3.0", "notes": "n", "assets": {"darwin": {"url": "https://x/mac.zip"}}}, "darwin")
    assert plain.version == "0.3.0" and plain.url == "https://x/mac.zip"


class Fake:
    def __init__(self, tmp_path, release=None, current="0.1.0", setup=SETUP):
        self.release = release if release is not None else updater.parse_feed(github_release(), "win32")
        self.fetches = 0
        self.downloads = 0
        self.spawned = []
        self.setup = setup

        def fetch(platform):
            self.fetches += 1
            if isinstance(self.release, Exception):
                raise self.release
            return self.release

        def download(url, destination, progress):
            self.downloads += 1
            progress(0.5)
            destination.write_bytes(self.setup)
            progress(1.0)

        self.updater = Updater(tmp_path, current=current, platform="win32", fetch=fetch, downloader=download,
                               spawn=self.spawned.append, can_install=True)


def test_check_once_a_day(tmp_path):
    fake = Fake(tmp_path)
    status = fake.updater.check()
    assert status["available"] and status["latest"] == "0.2.0" and "Faster" in status["notes"]
    fake.updater.check()
    assert fake.fetches == 1  # cached
    fake.updater.check(force=True)
    assert fake.fetches == 2

    up_to_date = Fake(tmp_path / "b", current="0.2.0").updater.check()
    assert not up_to_date["available"] and up_to_date["notes"] == ""


def test_errors_are_kept_and_shown(tmp_path):
    fake = Fake(tmp_path, release=UpdateError("Could not reach the update server."))
    status = fake.updater.check()
    assert status["error"] == "Could not reach the update server." and not status["available"]
    with pytest.raises(UpdateError, match="up to date"):
        fake.updater.download()


def test_download_checks_the_digest(tmp_path):
    fake = Fake(tmp_path, setup=b"tampered")
    fake.updater.check()
    with pytest.raises(UpdateError, match="damaged"):
        fake.updater.download()
    assert not list((tmp_path / "updates").rglob("*.exe*"))
    assert not fake.updater.status()["ready"]

    fake.setup = SETUP
    path = fake.updater.download()
    assert path.read_bytes() == SETUP and fake.updater.status()["ready"]
    assert fake.updater.download() == path and fake.downloads == 2  # not downloaded again


def test_install_runs_setup_silently(tmp_path):
    fake = Fake(tmp_path)
    fake.updater.check()
    fake.updater.install(relaunch=True)
    [command] = fake.spawned
    assert command[0].endswith("0.2.0/TevavidToolsSetup.exe") and command[1:] == ["--silent", "--update", "--relaunch"]

    source = Updater(tmp_path, can_install=False, fetch=lambda p: None)
    with pytest.raises(UpdateError, match="packaged app"):
        source.install()


def test_background_installs_only_while_archicad_is_closed(tmp_path):
    fake = Fake(tmp_path)
    assert fake.updater.tick(archicad_running=True, automatic=True) == "ready"
    assert fake.spawned == [] and fake.updater.status()["ready"]
    assert fake.updater.tick(archicad_running=False, automatic=False) is None
    assert fake.updater.tick(archicad_running=False, automatic=True) == "installing"
    assert fake.spawned[0][1:] == ["--silent", "--update"]

    # Setup failed and rolled back: the old version starts again, notices, and does not retry by itself.
    fake.updater.after_start()
    status = fake.updater.status()
    assert status["failedVersion"] == "0.2.0" and status["available"]
    assert fake.updater.tick(archicad_running=False, automatic=True) is None and len(fake.spawned) == 1

    # The new version starts: the failure is forgotten.
    state = json.loads((tmp_path / "update.json").read_text())
    state["installing"] = {"version": "0.2.0", "started": time.time()}
    (tmp_path / "update.json").write_text(json.dumps(state))
    newer = Updater(tmp_path, current="0.2.0", fetch=lambda p: None, can_install=True)
    newer.after_start()
    assert newer.status()["failedVersion"] is None and not newer.status()["available"]


def test_old_downloads_are_removed(tmp_path):
    fake = Fake(tmp_path)
    fake.updater.check()
    fake.updater.download()
    (tmp_path / "updates" / "0.1.5").mkdir()
    (tmp_path / "updates" / "0.1.5" / "TevavidToolsSetup.exe").write_bytes(b"old")
    fake.updater.check(force=True)
    assert [p.name for p in (tmp_path / "updates").iterdir()] == ["0.2.0"]


def test_panel_endpoints(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from archicad_mcp import server
    from archicad_mcp.chat import config, web

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "home")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "home" / "config.json")
    monkeypatch.setattr(server.archicad, "discover", list)
    fake = Fake(tmp_path / "home")
    client = TestClient(web.create_app(8765, token="secret", updater=fake.updater), base_url="http://127.0.0.1:8765")
    headers = {"x-tevavid-token": "secret"}

    assert client.get("/api/update").status_code == 403
    state = client.get("/api/update", headers=headers).json()
    assert state["current"] == "0.1.0" and state["latest"] is None and state["autoUpdate"] is True
    checked = client.post("/api/update/check", json={}, headers=headers).json()
    assert checked["available"] and checked["latest"] == "0.2.0"

    client.post("/api/update/install", json={}, headers=headers)
    deadline = time.time() + 5
    while not fake.spawned and time.time() < deadline:
        time.sleep(0.05)
    assert fake.spawned and fake.spawned[0][1:] == ["--silent", "--update"]  # no Archicad open: no relaunch
    assert client.get("/api/update", headers=headers).json()["job"]["installing"]

    client.post("/api/settings", json={"autoUpdate": False}, headers=headers)
    assert client.get("/api/status", headers=headers).json()["settings"]["autoUpdate"] is False
    assert json.loads((tmp_path / "home" / "config.json").read_text())["auto_update"] is False


def test_release_dataclass_round_trip(tmp_path):
    fake = Fake(tmp_path)
    fake.updater.check()
    saved = json.loads((tmp_path / "update.json").read_text())["latest"]
    assert Release(**saved).version == "0.2.0"


def test_feed_from_tevavid_com(monkeypatch):
    monkeypatch.delenv(updater.FEED_ENV, raising=False)
    assert updater.feed_url() == "https://tevavid.com/downloads/latest.json"
    feed = {"version": "0.2.0", "notes": "n", "assets": {"win32": {"url": "0.2.0/TevavidToolsSetup.exe", "sha256": "ab"}}}
    release = updater.parse_feed(feed, "win32", base=updater.feed_url())
    assert release.url == "https://tevavid.com/downloads/0.2.0/TevavidToolsSetup.exe"


def test_make_feed(tmp_path, monkeypatch):
    import importlib
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parent.parent / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    make_feed = importlib.import_module("make_feed")
    release = tmp_path / "release"
    release.mkdir()
    (release / "TevavidToolsSetup.exe").write_bytes(SETUP)
    (release / "TevavidToolsSetup-macOS.zip").write_bytes(b"mac")
    from archicad_mcp import __version__

    feed = make_feed.make(f"v{__version__}", release, tmp_path / "site")
    saved = json.loads((tmp_path / "site" / "latest.json").read_text())
    assert saved == feed and set(saved["assets"]) == {"win32", "darwin"}
    assert (tmp_path / "site" / __version__ / "TevavidToolsSetup.exe").read_bytes() == SETUP
    parsed = updater.parse_feed(saved, "win32", base="https://tevavid.com/downloads/latest.json")
    assert parsed.sha256 == hashlib.sha256(SETUP).hexdigest() and parsed.url.endswith(f"/{__version__}/TevavidToolsSetup.exe")
    with pytest.raises(SystemExit):
        make_feed.make("v9.9.9", release, tmp_path / "other")  # a tag that is not the code's version
    sys.modules.pop("make_feed", None)
