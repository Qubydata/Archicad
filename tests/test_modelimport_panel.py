import base64
import json

import pytest

from archicad_mcp import server
from archicad_mcp.connection import ArchicadConnection
from archicad_mcp.installer.system import System
from archicad_mcp.modelimport.service import ModelImport
from fake_archicad import FakeArchicad
from model_files import fake_converter, write_table


@pytest.fixture
def panel(monkeypatch, tmp_path):
    from starlette.testclient import TestClient

    from archicad_mcp.chat import config, web

    with FakeArchicad() as fake:
        connection = ArchicadConnection(port=fake.port)
        tool = ModelImport(connection, library_root=tmp_path / "library", work_dir=tmp_path / "work",
                           system=System(home=tmp_path / "home"), converter=fake_converter(tmp_path))
        monkeypatch.setattr(server, "model_import", tool)
        monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config" / "config.json")
        client = TestClient(web.create_app(8765, token="secret"), base_url="http://127.0.0.1:8765")
        yield fake, client, {"x-tevavid-token": "secret"}, tmp_path


def events(response):
    return [json.loads(line) for line in response.text.splitlines()]


def test_model_import_in_the_panel(panel):
    fake, client, headers, tmp_path = panel
    state = client.get("/api/import", headers=headers).json()
    assert state["hasModel"] is False and "license" in state["licenseReminder"] and state["converter"]["found"]

    model = write_table(tmp_path / "Dining Table.glb")
    opened = events(client.post("/api/import/open", headers=headers, json={
        "name": model.name, "content": base64.b64encode(model.read_bytes()).decode()}))
    assert opened[0]["type"] == "progress" and opened[-1]["type"] == "result"
    state = opened[-1]["state"]
    assert state["hasModel"] and state["name"] == "Dining Table" and len(state["levels"]) > 1

    leg = next(p for p in state["parts"] if p["name"].startswith("Leg"))
    edited = client.post("/api/import/edit", headers=headers, json={"action": "delete", "ids": [leg["id"]]}).json()
    assert next(p for p in edited["parts"] if p["id"] == leg["id"])["deleted"] and edited["canUndo"]
    assert client.post("/api/import/edit", headers=headers, json={"action": "undo"}).json()["canUndo"] is False
    level = client.post("/api/import/edit", headers=headers, json={"action": "level", "value": 3}).json()
    assert level["level"] == 3 and level["triangles"] < state["triangles"]
    copies = client.post("/api/import/edit", headers=headers, json={"action": "placements", "placements": 20}).json()
    assert copies["budget"]["placements"] == 20
    assert client.post("/api/import/edit", headers=headers, json={"action": "bogus"}).status_code == 400

    preview = client.get("/api/import/preview", headers=headers)
    assert preview.status_code == 200 and preview.content.startswith(b"\x89PNG")
    small = client.get("/api/import/small?fraction=0.01", headers=headers).json()["ids"]
    assert small

    assert client.post("/api/import/folder", headers=headers, json={"folder": "Furniture > Tables"}).status_code == 200
    tree = client.get("/api/import/library", headers=headers).json()
    assert [f["name"] for f in tree["folders"]] == ["Furniture"]

    saved = events(client.post("/api/import/save", headers=headers, json={
        "name": "Dining Table", "folder": "Furniture/Tables", "place": False}))
    assert saved[-1]["type"] == "result", saved[-1]
    gsm = tmp_path / "library" / "Furniture" / "Tables" / "Dining Table.gsm"
    assert gsm.read_bytes().startswith(b"GSM ")


def test_only_downloads_can_be_opened_by_path(panel):
    fake, client, headers, tmp_path = panel
    answer = client.post("/api/import/open", headers=headers, json={"path": str(tmp_path / "secret.obj")})
    assert answer.status_code == 400
    bad = events(client.post("/api/import/open", headers=headers, json={
        "name": "house.skp", "content": base64.b64encode(b"x").decode()}))
    assert bad[-1]["type"] == "error" and "Merge" in bad[-1]["message"]


def test_model_import_for_the_assistant(panel):
    import anyio
    from mcp import Client

    fake, client, headers, tmp_path = panel
    model = write_table(tmp_path / "chair.glb")

    async def run():
        async with Client(server.mcp) as mcp_client:
            opened = await mcp_client.call_tool("model_import_open", {"file_path": str(model)})
            first = opened.structured_content["parts"][0]["id"]
            edited = await mcp_client.call_tool("model_import_edit", {"delete_parts": [first], "level": 2})
            saved = await mcp_client.call_tool("model_import_save", {"folder": "Furniture > Chairs", "place": False})
            return opened, edited, saved

    opened, edited, saved = anyio.run(run)
    assert not opened.is_error and not edited.is_error and not saved.is_error, (edited.content, saved.content)
    assert edited.structured_content["level"] == 2
    assert (tmp_path / "library" / "Furniture" / "Chairs" / "chair.gsm").exists()
