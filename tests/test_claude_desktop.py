import json
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from archicad_mcp import claude_desktop, server
from archicad_mcp.chat import config, web
from fake_archicad import FakeArchicad


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    fake_system = SimpleNamespace(claude_config_path=path, server_command=lambda: ["/apps/tevavid-mcp-server"])
    monkeypatch.setattr(claude_desktop, "_system", lambda: fake_system)
    return path


def test_not_installed(desktop):
    assert claude_desktop.status() == {"installed": False, "connected": False}
    with pytest.raises(claude_desktop.ClaudeDesktopError):
        claude_desktop.connect()


def test_connect_keeps_other_servers(desktop):
    desktop.parent.mkdir()
    desktop.write_text(json.dumps({"mcpServers": {"blender": {"command": "uvx", "args": ["blender-mcp"]}}, "theme": "dark"}))
    assert claude_desktop.status() == {"installed": True, "connected": False}
    assert claude_desktop.connect() is None
    saved = json.loads(desktop.read_text())
    assert saved["theme"] == "dark" and "blender" in saved["mcpServers"]
    assert saved["mcpServers"]["archicad"] == {"command": "/apps/tevavid-mcp-server", "args": []}
    assert claude_desktop.status()["connected"] is True
    assert claude_desktop.connect(["/new/server", "mcp"]) == {"command": "/apps/tevavid-mcp-server", "args": []}


def test_unreadable_settings_are_left_alone(desktop):
    desktop.parent.mkdir()
    desktop.write_text("{not json")
    assert claude_desktop.is_connected() is False
    with pytest.raises(claude_desktop.ClaudeDesktopError):
        claude_desktop.connect()
    assert desktop.read_text() == "{not json"


def test_panel_shows_and_connects_claude_desktop(desktop, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "home")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "home" / "config.json")
    desktop.parent.mkdir()
    headers = {"x-tevavid-token": "secret"}
    with FakeArchicad() as fake:
        monkeypatch.setattr(server.archicad, "_port", fake.port)
        client = TestClient(web.create_app(8765, token="secret"), base_url="http://127.0.0.1:8765")
        assert client.get("/api/status", headers=headers).json()["claudeDesktop"] == {"installed": True, "connected": False}
        assert client.post("/api/claude-desktop/connect", json={}, headers=headers).json()["connected"] is True
        assert client.get("/api/status", headers=headers).json()["claudeDesktop"]["connected"] is True


# --- Claude Code (Claude in VS Code, JetBrains and the terminal) ---------------

from archicad_mcp import claude_code  # noqa: E402


@pytest.fixture
def code(tmp_path, monkeypatch):
    path = tmp_path / "home" / ".claude.json"
    path.parent.mkdir()
    fake_system = SimpleNamespace(claude_code_config_path=path, server_command=lambda: ["/apps/tevavid-mcp-server"])
    monkeypatch.setattr(claude_code, "_system", lambda: fake_system)
    return path


def test_claude_code_not_installed(code):
    assert claude_code.status() == {"installed": False, "connected": False}
    with pytest.raises(claude_code.ClaudeCodeError):
        claude_code.connect()
    assert not code.exists()


def test_claude_code_counts_as_installed_once_it_has_run(code):
    (code.parent / ".claude").mkdir()  # the folder exists before ~/.claude.json does
    assert claude_code.status() == {"installed": True, "connected": False}
    claude_code.connect()
    assert json.loads(code.read_text())["mcpServers"]["archicad"]["command"] == "/apps/tevavid-mcp-server"


def test_claude_code_keeps_everything_else(code):
    settings = {"numStartups": 12, "projects": {"/work": {"mcpServers": {"x": {}}}},
                "mcpServers": {"blender": {"type": "stdio", "command": "uvx", "args": ["blender-mcp"]}}}
    code.write_text(json.dumps(settings))
    assert claude_code.connect() is None
    saved = json.loads(code.read_text())
    assert saved["numStartups"] == 12 and saved["projects"] == settings["projects"]
    assert saved["mcpServers"]["blender"] == settings["mcpServers"]["blender"]
    assert saved["mcpServers"]["archicad"] == {"type": "stdio", "command": "/apps/tevavid-mcp-server", "args": [], "env": {}}
    assert claude_code.status() == {"installed": True, "connected": True}
    assert [p.name for p in code.parent.iterdir()] == [".claude.json"]  # no temporary file left behind


def test_claude_code_unreadable_settings_are_left_alone(code):
    code.write_text("{half written")
    with pytest.raises(claude_code.ClaudeCodeError):
        claude_code.connect()
    assert code.read_text() == "{half written"


def test_panel_connects_claude_code(code, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "data" / "config.json")
    code.write_text("{}")
    headers = {"x-tevavid-token": "secret"}
    with FakeArchicad() as fake:
        monkeypatch.setattr(server.archicad, "_port", fake.port)
        client = TestClient(web.create_app(8765, token="secret"), base_url="http://127.0.0.1:8765")
        assert client.get("/api/status", headers=headers).json()["claudeCode"] == {"installed": True, "connected": False}
        assert client.post("/api/claude-code/connect", json={}, headers=headers).json()["connected"] is True
