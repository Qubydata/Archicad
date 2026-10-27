import base64
import json
from typing import Any

import anyio
import pytest

from mcp import Client

from archicad_mcp import catalog, connection, server
from fake_archicad import PNG_BYTES, FakeArchicad


def call(tool: str, arguments: dict[str, Any] | None = None):
    async def run():
        async with Client(server.mcp) as client:
            return await client.call_tool(tool, arguments or {})

    return anyio.run(run)


def result(tool: str, arguments: dict[str, Any] | None = None) -> Any:
    answer = call(tool, arguments)
    assert not answer.is_error, answer.content
    content = answer.structured_content
    if content is None:
        return json.loads(answer.content[0].text)
    return content.get("result", content) if isinstance(content, dict) and list(content) == ["result"] else content


@pytest.fixture
def fake(monkeypatch):
    with FakeArchicad() as fake:
        monkeypatch.setattr(server.archicad, "host", "127.0.0.1")
        monkeypatch.setattr(server.archicad, "_port", fake.port)
        yield fake


def wall(x1, y1, x2, y2, **extra):
    return {"begCoordinate": {"x": x1, "y": y1}, "endCoordinate": {"x": x2, "y": y2}, **extra}


def test_status_reports_product_and_tapir(fake):
    status = result("archicad_status")
    assert status["port"] == fake.port
    assert status["product"]["version"] == 29
    assert status["tapirInstalled"] is True
    assert status["tapirVersion"] == "1.7.4"
    assert status["project"]["projectName"] == "Test House"


def test_status_without_tapir_gives_install_hint(monkeypatch):
    with FakeArchicad(tapir=False) as fake:
        monkeypatch.setattr(server.archicad, "_port", fake.port)
        status = result("archicad_status")
    assert status["tapirInstalled"] is False
    assert "tapir-archicad-automation" in status["hint"]


def test_discovery_scans_port_range(monkeypatch):
    with FakeArchicad() as fake:
        monkeypatch.setattr(connection, "FIRST_PORT", fake.port - 1)
        monkeypatch.setattr(connection, "LAST_PORT", fake.port + 1)
        conn = connection.ArchicadConnection(host="127.0.0.1")
        monkeypatch.delenv("ARCHICAD_PORT", raising=False)
        conn._port = None
        assert conn.port == fake.port
        assert [i.port for i in conn.discover()] == [fake.port]


def test_no_archicad_is_a_readable_tool_error(monkeypatch):
    monkeypatch.setattr(server.archicad, "_port", None)
    monkeypatch.setattr(connection, "FIRST_PORT", 1)
    monkeypatch.setattr(connection, "LAST_PORT", 1)
    answer = call("get_stories")
    assert answer.is_error
    assert "No running Archicad found" in answer.content[0].text


def test_create_walls_sends_tapir_shape_and_returns_guids(fake):
    created = result("create_walls", {"walls": [
        wall(0, 0, 5, 0, height=3.0, thickness=0.3, structureType="Basic"),
        wall(5, 0, 5, 4, logHeight=1.0),
    ]})
    assert len(created["created"]) == 2
    sent = fake.tapir_calls("CreateWalls")[0]["wallsData"]
    assert sent[0] == {"begCoordinate": {"x": 0.0, "y": 0.0}, "endCoordinate": {"x": 5.0, "y": 0.0},
                       "height": 3.0, "thickness": 0.3, "structureType": "Basic"}
    assert sent[1]["logHeight"] == 1.0  # extra Tapir fields pass through


def test_invalid_wall_input_is_rejected_before_archicad(fake):
    answer = call("create_walls", {"walls": [wall(0, 0, 1, 0, height=-1)]})
    assert answer.is_error
    assert fake.tapir_calls("CreateWalls") == []


def test_windows_convert_owner_and_report_item_errors(fake):
    wall_guid = result("create_walls", {"walls": [wall(0, 0, 5, 0)]})["created"][0]
    created = result("create_windows", {"windows": [
        {"ownerWallId": wall_guid, "centerOffset": 1.5, "width": 1.0},
        {"ownerWallId": "00000000-0000-0000-0000-000000000000", "centerOffset": 1.0},
    ]})
    assert len(created["created"]) == 1
    assert created["errors"][0]["index"] == 1
    assert fake.tapir_calls("CreateWindows")[0]["windowsData"][0]["ownerWallId"] == {"guid": wall_guid}


def test_zones_build_geometry(fake):
    result("create_zones", {"zones": [
        {"name": "Living", "numberStr": "001", "polygonCoordinates": [{"x": 0, "y": 0}, {"x": 4, "y": 0}, {"x": 4, "y": 4}]},
        {"name": "Kitchen", "numberStr": "002", "referencePosition": {"x": 1, "y": 1}},
    ]})
    sent = fake.tapir_calls("CreateZones")[0]["zonesData"]
    assert "polygonCoordinates" in sent[0]["geometry"] and "polygonCoordinates" not in sent[0]
    assert sent[1]["geometry"] == {"referencePosition": {"x": 1.0, "y": 1.0}}
    answer = call("create_zones", {"zones": [{"name": "Nowhere", "numberStr": "003"}]})
    assert answer.is_error and "polygonCoordinates or referencePosition" in answer.content[0].text


def test_get_elements_by_type_with_details(fake):
    result("create_walls", {"walls": [wall(0, 0, 1, 0), wall(1, 0, 1, 1)]})
    result("create_slabs", {"slabs": [{"polygonCoordinates": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]}]})
    walls = result("get_elements", {"element_type": "Wall", "with_details": True})
    assert walls["count"] == 2
    assert {d["type"] for d in walls["details"].values()} == {"Wall"}
    everything = result("get_elements", {"limit": 1})
    assert everything["count"] == 3 and everything["truncated"] is True and len(everything["guids"]) == 1


def test_move_rotate_delete(fake):
    guid = result("create_columns", {"columns": [{"coordinates": {"x": 1, "y": 2}}]})["created"][0]
    moved = result("move_elements", {"guids": [guid, "missing"], "dx": 2.0})
    assert moved["succeeded"] == 1 and moved["failed"][0]["guid"] == "missing"
    assert fake.elements[guid]["moved"] == {"x": 2.0, "y": 0.0, "z": 0.0}

    result("rotate_elements", {"guids": [guid], "angle_degrees": 90, "origin_x": 1, "origin_y": 1})
    rotation = fake.tapir_calls("RotateElements")[0]["elementsWithRotations"][0]["rotation"]
    assert rotation["beginPoint"] == {"x": 2.0, "y": 1.0}
    assert rotation["endPoint"]["x"] == pytest.approx(1.0) and rotation["endPoint"]["y"] == pytest.approx(2.0)

    assert result("delete_elements", {"guids": [guid]})["succeeded"] == 1
    assert guid not in fake.elements


def test_properties_by_name_group_and_ambiguity(fake):
    guid = result("create_walls", {"walls": [wall(0, 0, 1, 0)]})["created"][0]
    assert result("find_properties", {"search": "element id"})[0]["guid"] == "P-ID"
    result("set_element_properties", {"assignments": [{"guid": guid, "property": "Element ID", "value": "W-01"}]})
    values = result("get_element_properties", {"guids": [guid], "properties": ["Element ID", "Custom/Name"]})
    assert values[guid] == {"Element ID": "W-01", "Custom/Name": ""}

    ambiguous = call("get_element_properties", {"guids": [guid], "properties": ["Name"]})
    assert ambiguous.is_error and "ambiguous" in ambiguous.content[0].text
    unknown = call("get_element_properties", {"guids": [guid], "properties": ["Nope"]})
    assert unknown.is_error and "find_properties" in unknown.content[0].text


def test_select_replace_keeps_requested_elements(fake):
    a, b, c = result("create_walls", {"walls": [wall(0, 0, 1, 0), wall(1, 0, 2, 0), wall(2, 0, 3, 0)]})["created"]
    fake.selection = [a, b]
    result("select_elements", {"guids": [b, c]})
    assert sorted(fake.selection) == sorted([b, c])
    result("select_elements", {"guids": [b], "mode": "remove"})
    assert fake.selection == [c]
    selected = result("get_selected_elements")
    assert selected["guids"] == [c] and selected["details"][c]["type"] == "Wall"


def test_view_image_direct_and_fallback(monkeypatch, tmp_path):
    for view_image in (True, False):
        with FakeArchicad(view_image=view_image) as fake:
            monkeypatch.setattr(server.archicad, "_port", fake.port)
            monkeypatch.setattr(server.tempfile, "gettempdir", lambda: str(tmp_path))
            answer = call("get_view_image")
            assert not answer.is_error
            image = answer.content[0]
            assert image.type == "image" and base64.b64decode(image.data) == PNG_BYTES
            assert bool(fake.tapir_calls("SaveCurrentWindowAsImage")) is (not view_image)


def test_generic_commands_and_errors(fake):
    assert result("run_tapir_command", {"command_name": "getstories"})["lastStory"] == 1
    assert result("run_archicad_command", {"command": "IsAlive"})["isAlive"] is True

    tapir_error = call("run_tapir_command", {"command_name": "NoSuchCommand"})
    assert tapir_error.is_error and "Unknown Tapir command NoSuchCommand" in tapir_error.content[0].text
    api_error = call("run_archicad_command", {"command": "API.Nope"})
    assert api_error.is_error and "code 4001" in api_error.content[0].text


def test_catalog_search_and_schema():
    names = [c["name"] for c in result("search_tapir_commands", {"query": "create wall"})["commands"]]
    assert names[0] == "CreateWalls"
    schema = result("get_tapir_command_schema", {"command_name": "CreateWalls"})
    assert "wallsData" in schema["inputScheme"]["properties"]
    assert {"Coordinate2D", "ElementIdsOrErrors", "Guid"} <= set(schema["definitions"])
    assert call("get_tapir_command_schema", {"command_name": "CreateWal"}).is_error
    assert len(catalog.load_catalog()["commands"]) > 300
