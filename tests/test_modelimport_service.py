"""Model Import with a (fake) Archicad: saving library objects, the embedded library, placing, the Morph fallback."""

import json
import os
import sys
import time

import jsonschema
import pytest
from fake_archicad import FakeArchicad
from mcp.server.mcpserver.exceptions import ToolError
from model_files import fake_converter, table_meshes, write_table

from archicad_mcp import catalog
from archicad_mcp.connection import ArchicadConnection
from archicad_mcp.installer.system import System
from archicad_mcp.modelimport import ModelImportError, library
from archicad_mcp.modelimport.service import MORPH_FACES, ModelImport

TRIANGLES = sum(len(mesh.faces) for _, mesh, _ in table_meshes())


def check_against_catalog(fake: FakeArchicad) -> set[str]:
    """Every Tapir command the tool sent exists in the bundled catalog and matches its input schema."""
    sent = set()
    for request in fake.requests:
        if request["command"] != "API.ExecuteAddOnCommand":
            continue
        name = request["parameters"]["addOnCommandId"]["commandName"]
        parameters = request["parameters"]["addOnCommandParameters"]
        command = catalog.get_command(name)
        assert command is not None, f"{name} is not a Tapir command"
        sent.add(name)
        if command["inputScheme"] is None:
            assert not parameters, name
            continue
        definitions = catalog.schema_with_definitions(command)["definitions"]
        schema = json.loads(json.dumps({**command["inputScheme"], "definitions": definitions})
                            .replace('"$ref": "#/', '"$ref": "#/definitions/'))
        jsonschema.Draft7Validator(schema).validate(parameters)
    return sent


@pytest.fixture
def archicad(tmp_path):
    with FakeArchicad() as fake:
        fake.embedded_library_path = str(tmp_path / "Embedded")
        (tmp_path / "Embedded").mkdir()
        fake.archicad_location = str(tmp_path / "nowhere" / "Archicad.exe")
        yield fake


@pytest.fixture
def table(tmp_path):
    return write_table(tmp_path / "downloads" / "Dining Table.glb")


def make_tool(archicad, tmp_path, converter="fake", connected=True) -> ModelImport:
    if converter == "fake":
        converter = fake_converter(tmp_path)
    return ModelImport(ArchicadConnection(port=archicad.port) if connected else None,
                       library_root=tmp_path / "Tevavid Library", work_dir=tmp_path / "work",
                       system=System(home=tmp_path / "home"), converter=converter)


def test_open_clean_up_and_save_to_the_shared_library(archicad, table, tmp_path):
    tool = make_tool(archicad, tmp_path)
    steps = []
    state = tool.open(table, progress=lambda f, message: steps.append((f, message)))
    assert steps[0][0] == 0.0 and steps[-1] == (1.0, "Done")
    assert state["hasModel"] and state["name"] == "Dining Table" and state["triangles"] == TRIANGLES
    assert "D5 Works" in state["licenseReminder"] and "Furniture/Tables" in state["suggestedFolders"]

    screws = sorted(p["id"] for p in state["parts"] if p["name"].startswith("Screw"))
    assert sorted(tool.small_parts(0.01)) == screws
    state = tool.remove_small_parts(0.01)
    assert sorted(state["deleted"]) == screws and state["canUndo"]
    state = tool.set_level(2)
    assert state["level"] == 2 and state["triangles"] == state["levels"][2]["triangles"] < TRIANGLES - 36

    saved = tool.save("Oak Table", folder="Furniture > Tables", place=True, position=(1.0, 2.0))
    gsm = tmp_path / "Tevavid Library" / "Furniture" / "Tables" / "Oak Table.gsm"
    assert saved["mode"] == "library" and saved["file"] == str(gsm) and gsm.read_bytes().startswith(b"GSM ")
    assert saved["folder"] == "Furniture/Tables" and saved["triangles"] == state["triangles"]
    assert saved["budget"]["colour"] == "green" and "license" in saved["licenseReminder"]
    assert (tmp_path / "work" / "xml" / "Oak Table.xml").is_file()
    assert (tmp_path / "work" / "xml" / "images" / "Oak Table.png").read_bytes().startswith(b"\x89PNG")
    assert saved["libraryLoaded"] and str(tmp_path / "Tevavid Library") in [lib["path"] for lib in archicad.libraries]
    assert any("Added" in note for note in saved["notes"])

    (placed,) = archicad.tapir_calls("CreateObjects")
    data = placed["objectsData"][0]
    assert data["libraryPartName"] == "Oak Table" and data["coordinates"] == {"x": 1.0, "y": 2.0, "z": 0.0}
    assert data["dimensions"] == pytest.approx({"x": 1.2, "y": 0.8, "z": 0.91})
    assert archicad.elements[saved["placed"]]["type"] == "Object"

    again = tool.save("Oak Table", folder="Furniture/Tables")
    assert again["guid"] == saved["guid"] and archicad.reloads == 1 and again["placed"] is None
    assert len(archicad.tapir_calls("AddLibraries")) == 1
    assert {"GetLibraries", "AddLibraries", "ReloadLibraries", "CreateObjects"} <= check_against_catalog(archicad)


def test_place_at_a_clicked_point(archicad, table, tmp_path):
    tool = make_tool(archicad, tmp_path)
    tool.open(table)
    saved = tool.save(place=True, pick_point=True)
    assert saved["name"] == "Dining Table"
    (prompt,) = archicad.tapir_calls("GetPointFromUser")
    assert prompt["prompt"] == "Click where to place Dining Table"
    assert archicad.tapir_calls("CreateObjects")[0]["objectsData"][0]["coordinates"] == {"x": 3.0, "y": 4.0, "z": 0.0}
    archicad.clicked_point = None
    with pytest.raises(ModelImportError, match="No point was picked"):
        tool.save(place=True, pick_point=True)
    check_against_catalog(archicad)


def test_save_into_the_embedded_library(archicad, table, tmp_path):
    tool = make_tool(archicad, tmp_path)
    tool.open(table)
    saved = tool.save("Oak Table", folder="Furniture", target="embedded", place=True)
    assert saved["mode"] == "library" and saved["file"] == "Furniture/Oak Table.gsm"
    assert archicad.embedded_folders == ["Furniture"]
    (added,) = archicad.embedded_files
    assert added["outputPath"] == "Furniture/Oak Table.gsm" and added["type"] == "Object"
    assert open(added["inputPath"], "rb").read().startswith(b"GSM ")
    assert archicad.tapir_calls("AddFilesToEmbeddedLibrary")[0]["overwriteExisting"] is True
    assert not archicad.tapir_calls("AddLibraries")
    assert not (tmp_path / "Tevavid Library" / "Furniture" / "Oak Table.gsm").exists()

    assert tool.create_folder("Lighting > Lamps", target="embedded") == {"folder": "Lighting/Lamps",
                                                                         "target": "embedded"}
    (tmp_path / "Embedded" / "Furniture").mkdir()
    (tmp_path / "Embedded" / "Furniture" / "Chair.gsm").write_bytes(b"gsm")
    tree = tool.library_tree("embedded")
    assert tree["target"] == "embedded" and tree["folders"][0]["objects"] == ["Chair"]
    assert {"GetSpecialFolders", "CreateFolderInEmbeddedLibrary", "AddFilesToEmbeddedLibrary"} \
        <= check_against_catalog(archicad)


def test_morphs_when_lp_xmlconverter_is_missing(archicad, table, tmp_path, monkeypatch):
    monkeypatch.delenv(library.CONVERTER_ENV, raising=False)
    tool = make_tool(archicad, tmp_path, converter=None)
    status = tool.converter_status()
    assert not status["found"] and "Morph" in status["message"]
    tool.open(table)
    saved = tool.save("Oak Table", place=True)
    assert saved["mode"] == "morphs" and "LP_XMLConverter was not found" in saved["notes"][0]
    assert (tmp_path / "work" / "xml" / "Oak Table.xml").is_file()  # kept for a later conversion

    (surfaces,) = archicad.tapir_calls("CreateSurfaces")
    names = [s["name"] for s in surfaces["surfaceDataArray"]]
    assert sorted(names) == ["Oak Table - Metal", "Oak Table - Wood"] and surfaces["overwriteExisting"] is True
    wood = next(s for s in surfaces["surfaceDataArray"] if s["name"].endswith("Wood"))
    assert wood["surfaceColor"]["red"] == pytest.approx(150 / 255) and wood["transparency"] == 0

    morphs = [m for call in archicad.tapir_calls("CreateMorphs") for m in call["morphsData"]]
    assert len(morphs) == len(saved["morphs"]) == 10  # 8 small parts and the vase in two pieces
    assert sum(len(m["body"]["polygons"]) for m in morphs) == TRIANGLES
    known = set(archicad.surfaces)
    for morph in morphs:
        body = morph["body"]
        assert len(body["polygons"]) <= MORPH_FACES and body["edgeDefault"] == "HardHidden"
        assert all(0 <= i < len(body["vertices"]) for p in body["polygons"] for i in p["vertexIds"])
        assert morph["surfaceId"]["guid"] in known and morph["basePoint"] == {"x": 0.0, "y": 0.0, "z": 0.0}
    assert {m["body"]["bodyType"] for m in morphs} == {"Solid", "Surface"}  # boxes are closed, vase halves not
    assert {"CreateSurfaces", "CreateMorphs", "GetArchicadLocation"} <= check_against_catalog(archicad)


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in converter is an executable script")
def test_converter_of_the_running_archicad(archicad, table, tmp_path, monkeypatch):
    monkeypatch.delenv(library.CONVERTER_ENV, raising=False)
    folder = tmp_path / "Archicad 29"
    folder.mkdir()
    converter = folder / "LP_XMLConverter"
    script = fake_converter(tmp_path)[1]
    converter.write_text(f"#!{sys.executable}\n" + open(script).read())
    converter.chmod(0o755)
    archicad.archicad_location = str(folder / "Archicad")
    tool = make_tool(archicad, tmp_path, converter=None)
    assert tool.converter_status() == {"found": True, "path": str(converter),
                                       "message": "Models are saved as Archicad library objects."}
    tool.open(table)
    assert tool.save()["mode"] == "library"


def test_without_archicad(table, tmp_path, monkeypatch):
    monkeypatch.delenv(library.CONVERTER_ENV, raising=False)
    tool = ModelImport(None, library_root=tmp_path / "lib", work_dir=tmp_path / "work",
                       system=System(home=tmp_path / "home"), converter=fake_converter(tmp_path))
    tool.open(table)
    saved = tool.save("Table", folder="Furniture")
    assert saved["libraryLoaded"] is False and (tmp_path / "lib" / "Furniture" / "Table.gsm").is_file()
    with pytest.raises(ModelImportError, match="not connected"):
        tool.save(target="embedded")
    tool.converter_override = None
    with pytest.raises(ModelImportError, match="The GDL XML is in"):
        tool.save("Table")


def test_conversion_errors_are_reported(archicad, table, tmp_path):
    tool = make_tool(archicad, tmp_path, converter=fake_converter(tmp_path, fail=True))
    tool.open(table)
    with pytest.raises(library.ConversionError, match="syntax error at line 3") as error:
        tool.save()
    assert isinstance(error.value, ToolError)


def test_edits_preview_budget_and_errors(archicad, table, tmp_path):
    tool = make_tool(archicad, tmp_path)
    assert tool.state()["hasModel"] is False
    with pytest.raises(ModelImportError, match="Open a model first"):
        tool.delete_parts(["p0"])
    with pytest.raises(ModelImportError, match="Interoperability > Merge"):
        (tmp_path / "Sofa.skp").write_bytes(b"skp")
        tool.open(tmp_path / "Sofa.skp")

    state = tool.open(table, unit="cm")
    assert state["unit"] == "cm" and state["size"]["x"] == pytest.approx(0.012)
    state = tool.set_unit("m")
    assert state["size"]["x"] == pytest.approx(1.2)
    state = tool.set_up_axis("z")
    assert state["upAxis"] == "Z" and state["size"]["z"] == pytest.approx(0.8)
    state = tool.set_turn(90)
    assert state["turn"] == 90
    for _ in range(3):
        state = tool.undo()
    assert state["upAxis"] == "Y" and state["unit"] == "cm" and state["undone"] is True
    top = next(p["id"] for p in state["parts"] if p["name"] == "TableTop")
    assert tool.delete_parts([top])["deleted"] == [top]
    assert tool.restore_parts([top])["restored"] == [top]

    assert tool.preview(64).startswith(b"\x89PNG")
    assert tool.budget(placements=20)["colour"] == "red"
    with pytest.raises(ModelImportError, match="Unknown library"):
        tool.save(target="cloud")
    with pytest.raises(ModelImportError, match="cannot be a folder name"):
        tool.save(folder="../outside")
    assert tool.close()["hasModel"] is False


def test_shared_library_tree_and_downloads(archicad, table, tmp_path):
    tool = make_tool(archicad, tmp_path)
    assert tool.create_folder("Furniture > Sofas")["folder"] == "Furniture/Sofas"
    tree = tool.library_tree()
    assert tree["target"] == "shared" and tree["folders"][0]["folders"][0]["name"] == "Sofas"
    os.utime(table, (time.time() - 60, time.time() - 60))  # downloaded a minute ago
    found = tool.new_downloads(time.time() - 3600, table.parent)
    assert [f["name"] for f in found] == ["Dining Table.glb"]


def test_command_line(tmp_path, table, capsys, monkeypatch):
    from archicad_mcp.modelimport.__main__ import main

    assert main(["--demo", "--xml-only", str(tmp_path / "out"), "--remove-small", "0.01"]) == 0
    printed = capsys.readouterr().out
    assert "Removed 2 small parts" in printed and "Demo Table: 6 parts" in printed
    assert (tmp_path / "out" / "Demo Table.xml").is_file()
    assert (tmp_path / "out" / "images" / "Demo Table.png").is_file()

    monkeypatch.setattr(library, "find_converter", lambda *args: None)
    code = main([str(table), "--library", str(tmp_path / "lib"), "--max-polygons", "2000"])
    assert code == 1 and "LP_XMLConverter was not found" in capsys.readouterr().err
