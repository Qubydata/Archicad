import io
import json
import tarfile
import zipfile
from pathlib import Path

import pytest

from archicad_mcp import __version__, launcher
from archicad_mcp.installer import gui, install
from archicad_mcp.installer import system as sysmod
from archicad_mcp.installer.install import Installer, Options
from archicad_mcp.installer.system import ArchicadInstall, System


class FakeSystem(System):
    """A Linux layout under a temporary home, recording the commands it would run."""

    def __init__(self, home: Path) -> None:
        super().__init__(home)
        self.platform = "linux"
        self.commands: list[tuple[str, list[str]]] = []
        self.exit_code = 0

    @property
    def data_dir(self) -> Path:
        return self.home / ".tevavid-tools"

    def run(self, command, timeout=900):
        self.commands.append(("run", command))
        return self.exit_code

    def run_elevated(self, command, timeout=900):
        self.commands.append(("elevated", command))
        return self.exit_code

    def stop_running_app(self):
        self.commands.append(("stop", []))

    def has_uv(self):
        return (self.uv_dir / "uv").exists()


def make_archicad(home: Path, version: int) -> None:
    folder = home / "GRAPHISOFT" / f"Archicad {version}"
    folder.mkdir(parents=True)
    (folder / "Archicad").write_text("binary")


def fake_downloader(fail: set[str] = frozenset(), on_call=None):
    def download(url, destination, progress, cancelled):
        if on_call:
            on_call(url)
        name = url.rsplit("/", 1)[1]
        if any(part in name for part in fail):
            raise OSError("network down")
        if name.endswith(".zip"):
            with zipfile.ZipFile(destination, "w") as zf:
                zf.writestr("TapirInstaller/TapirInstaller", "#!/bin/sh\n")
        else:
            data = b"uv binary"
            with tarfile.open(destination, "w:gz") as tf:
                info = tarfile.TarInfo("uv-x86_64-unknown-linux-gnu/uv")
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        progress(1.0)
    return download


@pytest.fixture
def spawned(monkeypatch):
    started = []
    monkeypatch.setattr(sysmod, "spawn_detached", started.append)
    monkeypatch.setattr(install.time, "sleep", lambda s: None)
    return started


@pytest.fixture
def home(tmp_path, spawned):
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture
def payload(tmp_path):
    path = tmp_path / "app.zip"
    with zipfile.ZipFile(path, "w") as zf:
        executable = zipfile.ZipInfo("TevavidTools")
        executable.external_attr = 0o755 << 16
        zf.writestr(executable, "app")
        zf.writestr("_internal/data.txt", "data")
    return path


def claude_config(system: System, servers: dict) -> Path:
    path = system.claude_config_path
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": servers, "other": 1}))
    return path


def test_detects_archicad_and_offers_one_more_version(home):
    make_archicad(home, 28)
    make_archicad(home, 24)  # too old for Tapir
    (home / "GRAPHISOFT" / "Archicad 29").mkdir()  # no executable inside
    system = FakeSystem(home)
    assert [(a.version, a.detected) for a in system.detect_archicad()] == [(28, True)]
    rows = gui.archicad_rows(system)
    assert [(r["version"], r["detected"]) for r in rows] == [(28, True), (30, False)]
    assert rows[0]["path"].endswith("/")
    assert system.version_from_path("D:/Apps/Archicad 27", 30) == 27


def test_full_install(home, payload, spawned):
    make_archicad(home, 28)
    system = FakeSystem(home)
    config = claude_config(system, {"blender": {"command": "uvx"}})
    progress = []
    options = Options(
        [ArchicadInstall(28, str(home / "GRAPHISOFT" / "Archicad 28/"), True),
         ArchicadInstall(27, "/opt/Archicad 27/", False)],
        install_addon=True, desktop_app=True,
    )
    state = Installer(system, options, payload, lambda s: progress.append(s["percent"]), fake_downloader()).run()

    assert state["status"] == "done", state
    assert [s["state"] for s in state["steps"]] == ["done"] * 5
    assert progress == sorted(progress) and progress[-1] == 100

    assert (system.install_dir / "TevavidTools").read_text() == "app"
    assert (system.install_dir / "TevavidTools").stat().st_mode & 0o111  # still executable
    script = system.tapir_scripts_dir / "Tevavid Tools.py"
    assert "archicad_mcp.launcher" in script.read_text() and "--exit-with-archicad" in script.read_text()
    assert (system.uv_dir / "uv").read_bytes() == b"uv binary"

    tapir_runs = [c for kind, c in system.commands if kind == "run"]
    assert tapir_runs[0][1:] == ["--console", "--versions", "28"]
    assert tapir_runs[1][1:] == ["--console", "--versions", "27", "--addOnsFolder", str(Path("/opt/Archicad 27/Add-Ons"))]

    saved = json.loads(config.read_text())
    assert saved["other"] == 1 and "blender" in saved["mcpServers"]
    assert saved["mcpServers"]["archicad"]["args"] == ["-m", "archicad_mcp.launcher", "mcp"]
    assert json.loads((system.data_dir / "config.json").read_text()) == {"auto_open_panel": True, "tools": ["assistant", "sun"]}

    manifest = json.loads(system.manifest_path.read_text())
    assert {a["type"] for a in manifest["actions"]} == {"dir", "file", "claude_config"}
    assert spawned == [system.app_command("panel", "--background")]


def test_uninstall_undoes_everything(home, payload):
    system = FakeSystem(home)
    config = claude_config(system, {"archicad": {"command": "old"}})
    Installer(system, Options([], install_addon=True, desktop_app=False), payload, downloader=fake_downloader()).run()
    assert json.loads(config.read_text())["mcpServers"]["archicad"]["command"] != "old"

    install.uninstall(system)
    assert not system.install_dir.exists()
    assert not (system.tapir_scripts_dir / "Tevavid Tools.py").exists()
    assert json.loads(config.read_text())["mcpServers"]["archicad"] == {"command": "old"}  # previous entry restored
    assert not system.manifest_path.exists()


def test_cancel_rolls_back(home, payload, spawned):
    system = FakeSystem(home)
    config = claude_config(system, {})
    holder = {}
    installer = Installer(system, Options([], install_addon=True, desktop_app=True), payload,
                          downloader=fake_downloader(on_call=lambda url: holder["installer"].cancel()))
    holder["installer"] = installer
    state = installer.run()
    assert state["status"] == "cancelled"
    assert not system.install_dir.exists()
    assert json.loads(config.read_text())["mcpServers"] == {}
    assert not system.manifest_path.exists()
    assert spawned == []


def test_failure_rolls_back_and_reports(home, tmp_path):
    system = FakeSystem(home)
    broken = tmp_path / "broken.zip"
    broken.write_text("not a zip")
    state = Installer(system, Options([], desktop_app=False), broken, downloader=fake_downloader()).run()
    assert state["status"] == "failed"
    assert "BadZipFile" in state["error"]
    assert [s["state"] for s in state["steps"]][:2] == ["done", "failed"]
    assert not system.install_dir.exists()


def test_download_and_addon_problems_are_warnings(home, payload):
    make_archicad(home, 29)
    system = FakeSystem(home)
    system.exit_code = 3
    options = Options(system.detect_archicad(), install_addon=True, desktop_app=False)
    state = Installer(system, options, payload, downloader=fake_downloader(fail={"uv-"})).run()
    assert state["status"] == "done"
    assert [s["state"] for s in state["steps"]] == ["warning", "done", "warning", "done", "done"]
    assert any("uv" in w for w in state["warnings"])
    assert any("code 3" in w for w in state["warnings"])


def test_without_addon_nothing_is_downloaded_or_run(home, payload):
    system = FakeSystem(home)
    calls = []
    state = Installer(system, Options([], install_addon=False, desktop_app=False), payload,
                      downloader=fake_downloader(on_call=calls.append)).run()
    assert state["status"] == "done" and calls == []
    assert not (system.tapir_scripts_dir / "Tevavid Tools.py").exists()
    assert [kind for kind, _ in system.commands] == ["stop"]


def test_options_from_page():
    options = Options.from_dict({"targets": [{"version": 28, "path": "C:/A/", "detected": True}],
                                 "installAddon": False, "desktopApp": True})
    assert options.targets == [ArchicadInstall(28, "C:/A/", True)]
    assert (options.install_addon, options.desktop_app) == (False, True)


def test_pages_inline_their_images():
    html = gui.page("setup.html")
    assert "../../assets/" not in html and 'src="building.svg"' not in html
    assert "data:image/svg+xml;base64," in html and 'class="logo"' in html  # the building art, the vector mark
    for name in ("mini.html", "toast.html"):
        assert "data:image/png;base64," in gui.page(name) and "../../assets/" not in gui.page(name)


def test_launcher_dispatch(capsys, monkeypatch):
    started = []
    monkeypatch.setattr(launcher, "_setup", started.append)
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.sys, "executable", "/home/u/.tevavid-tools/updates/0.3.0/0.3.0-TevavidToolsSetup-linux")
    launcher.main(["--silent", "--update"])
    assert started == [["--silent", "--update"]]  # a dotted file name still runs setup, not the panel
    monkeypatch.undo()
    assert launcher.main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__
    assert launcher.main(["bogus"]) == 2


def test_only_sun_analysis_skips_claude_desktop_and_keeps_settings(home, payload):
    system = FakeSystem(home)
    config = claude_config(system, {"blender": {"command": "uvx"}})
    system.data_dir.mkdir()
    (system.data_dir / "config.json").write_text(json.dumps({"api_key": "sk-keep", "tools": ["assistant"]}))
    options = Options.from_dict({"targets": [], "tools": ["sun", "darkmode", "bogus"], "desktopApp": False})
    assert options.tools == ["sun"] and options.install_addon is True  # Sun Analysis needs Tapir
    state = Installer(system, options, payload, downloader=fake_downloader()).run()
    assert state["status"] == "done"
    assert "archicad" not in json.loads(config.read_text())["mcpServers"]
    saved = json.loads((system.data_dir / "config.json").read_text())
    assert saved == {"api_key": "sk-keep", "tools": ["sun"]}
    assert json.loads(system.manifest_path.read_text())["tools"] == ["sun"]


def test_suite_registry():
    from archicad_mcp import suite

    assert suite.default_tools("win32") == ["assistant", "sun"]
    assert suite.clean(["sun", "assistant", "darkmode"], "win32") == ["assistant", "sun"]  # suite order, no coming-soon
    dark = suite.tool("darkmode")
    assert not dark.available and dark.supported("win32") and not dark.supported("darwin")
    assert suite.needs_tapir(["sun"]) and not suite.needs_tapir([])


def make_payload(path: Path, content: str) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        executable = zipfile.ZipInfo("TevavidTools")
        executable.external_attr = 0o755 << 16
        zf.writestr(executable, content)
    return path


def tapir_runs(system):
    return [c for kind, c in system.commands if kind == "run"]


def test_update_replaces_the_app_and_keeps_the_first_install_state(home, payload, tmp_path, spawned):
    make_archicad(home, 28)
    system = FakeSystem(home)
    config = claude_config(system, {"archicad": {"command": "theirs"}})
    first = Options(system.detect_archicad(), install_addon=True, desktop_app=True)
    assert Installer(system, first, payload, downloader=fake_downloader()).run()["status"] == "done"
    (system.data_dir / "config.json").write_text(json.dumps({"api_key": "sk-keep", "tools": ["assistant", "sun"]}))
    assert len(tapir_runs(system)) == 1

    system.commands.clear()
    downloads = []
    newer = make_payload(tmp_path / "v2.zip", "app v2")
    manifest = install.read_manifest(system)
    state = Installer(system, Options.from_manifest(manifest, "linux"), newer,
                      downloader=fake_downloader(on_call=downloads.append)).run()
    assert state["status"] == "done" and state["update"] and state["previousVersion"] == __version__
    assert (system.install_dir / "TevavidTools").read_text() == "app v2"
    assert not (system.install_dir / "_internal").exists()  # old files do not linger
    assert not list(system.install_dir.parent.glob(".*"))  # no staging or old copy left behind
    assert tapir_runs(system) == [] and downloads == []  # Archicad 28 has the add-on already, uv is there
    assert json.loads((system.data_dir / "config.json").read_text())["api_key"] == "sk-keep"

    updated = install.read_manifest(system)
    assert updated["installed"] == manifest["installed"] and updated["tapirInstalled"] == [28]
    assert sorted(a["type"] for a in updated["actions"]) == sorted(a["type"] for a in manifest["actions"])
    install.uninstall(system)
    assert json.loads(config.read_text())["mcpServers"]["archicad"] == {"command": "theirs"}  # not our older entry


def test_update_adds_the_addon_only_for_new_archicad_versions(home, payload):
    make_archicad(home, 28)
    system = FakeSystem(home)
    Installer(system, Options(system.detect_archicad(), desktop_app=False), payload, downloader=fake_downloader()).run()
    make_archicad(home, 29)
    system.commands.clear()
    state = Installer(system, Options(system.detect_archicad(), desktop_app=False), payload, downloader=fake_downloader()).run()
    assert state["status"] == "done"
    assert [c[1:] for c in tapir_runs(system)] == [["--console", "--versions", "29"]]
    assert install.read_manifest(system)["tapirInstalled"] == [28, 29]


def test_update_removes_what_was_unticked(home, payload):
    system = FakeSystem(home)
    config = claude_config(system, {})
    Installer(system, Options([], install_addon=True, desktop_app=False), payload, downloader=fake_downloader()).run()
    script = system.tapir_scripts_dir / "Tevavid Tools.py"
    assert script.exists() and "archicad" in json.loads(config.read_text())["mcpServers"]

    state = Installer(system, Options.from_dict({"targets": [], "tools": ["assistant"], "desktopApp": False,
                                                 "installAddon": False}),
                      payload, downloader=fake_downloader()).run()
    assert state["status"] == "done"
    assert not script.exists()  # no add-on, so no palette launcher
    assert "archicad" in json.loads(config.read_text())["mcpServers"]

    Installer(system, Options.from_dict({"targets": [], "tools": ["sun"], "desktopApp": False}),
              payload, downloader=fake_downloader()).run()
    assert json.loads(config.read_text())["mcpServers"] == {}  # Claude Desktop is back as it was
    assert script.exists()


def test_failed_update_keeps_the_old_version(home, payload, tmp_path):
    system = FakeSystem(home)
    Installer(system, Options([], desktop_app=False), payload, downloader=fake_downloader()).run()
    (system.data_dir / "config.json").write_text(json.dumps({"tools": ["assistant", "sun"], "api_key": "sk"}))
    before = system.manifest_path.read_text()

    broken = tmp_path / "broken.zip"
    broken.write_text("not a zip")
    state = Installer(system, Options.from_dict({"targets": [], "tools": ["sun"], "desktopApp": False}), broken,
                      downloader=fake_downloader()).run()
    assert state["status"] == "failed"
    assert (system.install_dir / "TevavidTools").read_text() == "app"
    assert system.manifest_path.read_text() == before
    assert (system.tapir_scripts_dir / "Tevavid Tools.py").exists()

    class Exploding(Installer):
        def _finalize(self, progress):
            raise RuntimeError("disk full")

    newer = make_payload(tmp_path / "v2.zip", "app v2")
    state = Exploding(system, Options.from_dict({"targets": [], "tools": ["sun"], "desktopApp": False}), newer,
                      downloader=fake_downloader()).run()
    assert state["status"] == "failed"
    assert (system.install_dir / "TevavidTools").read_text() == "app"  # swapped back
    assert json.loads((system.data_dir / "config.json").read_text())["tools"] == ["assistant", "sun"]
    assert system.manifest_path.read_text() == before


def test_silent_update_keeps_the_choices(home, payload, tmp_path, monkeypatch, spawned):
    system = FakeSystem(home)
    Installer(system, Options.from_dict({"targets": [], "tools": ["sun"], "desktopApp": False}), payload,
              downloader=fake_downloader()).run()
    monkeypatch.setattr(gui, "payload_path", lambda: make_payload(tmp_path / "v2.zip", "app v2"))
    monkeypatch.setattr(install, "download", fake_downloader())
    monkeypatch.setattr(Installer.__init__, "__defaults__", (lambda state: None, fake_downloader()))
    assert gui.run_silent(system, update=True, relaunch=True) == 0
    manifest = install.read_manifest(system)
    assert manifest["tools"] == ["sun"] and manifest["options"]["desktopApp"] is False
    assert (system.install_dir / "TevavidTools").read_text() == "app v2"
    assert spawned == [system.app_command("panel", "--exit-with-archicad")]  # the panel comes back


def test_setup_knows_the_installed_copy(home, payload):
    system = FakeSystem(home)
    assert gui.existing_install(system) is None
    Installer(system, Options([], desktop_app=False), payload, downloader=fake_downloader()).run()
    found = gui.existing_install(system)
    assert found["version"] == __version__ and found["tools"] == ["assistant", "sun"] and not found["newer"]
    assert found["offered"] == ["assistant", "sun"]
    manifest = install.read_manifest(system)
    del manifest["offered"]  # as written by 0.1.0
    manifest["version"] = "9.0.0"
    system.manifest_path.write_text(json.dumps(manifest))
    found = gui.existing_install(system)
    assert found["offered"] == install.FIRST_RELEASE_TOOLS and found["newer"]
