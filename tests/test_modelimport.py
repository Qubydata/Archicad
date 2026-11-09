"""Model Import engine: reading, clean-up, polygon reduction, preview, GDL XML, library folders, Downloads."""

import os
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import numpy as np
import pytest
import trimesh
from model_files import (
    coloured_ply,
    fake_converter,
    table_meshes,
    textured_box_zip,
    write_table,
)
from PIL import Image

from archicad_mcp.installer.system import System
from archicad_mcp.modelimport import ModelImportError, gdl, library, reader
from archicad_mcp.modelimport.cleanup import (
    ROTATIONS,
    ExportMesh,
    ImportSession,
    guess_unit,
)
from archicad_mcp.modelimport.downloads import new_downloads
from archicad_mcp.modelimport.preview import render_preview
from archicad_mcp.modelimport.simplify import (
    Levels,
    budget,
    level_targets,
    simplify_mesh,
)

TABLE_TRIANGLES = sum(len(mesh.faces) for _, mesh, _ in table_meshes())
TOP_Z_PLUS_VASE = 0.75 + 0.16  # the vase stands on the top


@pytest.fixture
def table_glb(tmp_path):
    return write_table(tmp_path / "glb" / "Dining Table.glb")


@pytest.fixture
def session(table_glb):
    return ImportSession(reader.read_model(table_glb))


# --- reading -------------------------------------------------------------------

def test_glb_parts_materials_units_and_axis(table_glb):
    model = reader.read_model(table_glb)
    assert model.format == "glTF" and model.reader == "trimesh" and model.name == "Dining Table"
    assert model.unit == "m" and model.up_axis == "Y"
    names = [p.name for p in model.parts]
    assert set(names) == {"TableTop", "Leg 1", "Leg 2", "Leg 3", "Leg 4", "Screw 1", "Screw 2", "Screw 3", "Vase"}
    assert model.triangles == TABLE_TRIANGLES
    materials = {m.name: m for m in model.materials.values()}
    assert set(materials) == {"Wood", "Metal"}
    assert materials["Wood"].to_dict()["color"] == "#966432" and materials["Metal"].opacity == 1.0
    top = next(p for p in model.parts if p.name == "TableTop")
    assert model.materials[top.material].name == "Wood"
    assert len(top.vertices) == 8 and top.triangles == 12  # welded: a box has 8 corners


def test_obj_with_mtl_in_centimetres_is_guessed(tmp_path):
    path = write_table(tmp_path / "obj" / "table.obj", scale=100.0)
    model = reader.read_model(path)
    assert model.format == "OBJ" and model.unit is None and model.up_axis == "Y"
    assert {m.name for m in model.materials.values()} == {"Wood", "Metal"}
    assert "TableTop mesh" in {p.name for p in model.parts}  # OBJ objects keep their names
    assert guess_unit(model) == ("cm", "size")
    session = ImportSession(model)
    assert session.size() == pytest.approx((1.2, 0.8, TOP_Z_PLUS_VASE), abs=1e-3)





def test_stl_is_one_part_z_up_and_millimetres_are_guessed(tmp_path):
    path = write_table(tmp_path / "chair.stl", scale=1000.0, y_up=False)
    model = reader.read_model(path)
    assert [p.name for p in model.parts] == ["chair"] and model.up_axis == "Z"
    assert [m.name for m in model.materials.values()] == ["Default"]
    session = ImportSession(model)
    assert session.edits.unit == "mm" and session.unit_source == "size"
    assert session.size() == pytest.approx((1.2, 0.8, TOP_Z_PLUS_VASE), abs=1e-3)


def test_ply_face_colours_become_a_material(tmp_path):
    model = reader.read_model(coloured_ply(tmp_path / "block.ply"))
    (material,) = model.materials.values()
    assert material.to_dict()["color"] == "#0080ff" and material.name == "Colour #0080ff"


def test_zip_with_textured_obj(tmp_path):
    model = reader.read_model(textured_box_zip(tmp_path / "Cube_D5.zip"))
    assert model.archive == tmp_path / "Cube_D5.zip" and model.name == "Cube_D5"
    assert model.workdir is not None and model.workdir.is_dir()
    (material,) = model.materials.values()
    assert material.name == "RedPaint" and material.texture
    r, g, b = material.color
    assert r > 0.9 and g < 0.1 and b < 0.1  # the texture's average colour
    workdir = model.workdir
    model.close()
    assert not workdir.exists()


def test_zip_without_a_model(tmp_path):
    with zipfile.ZipFile(tmp_path / "docs.zip", "w") as archive:
        archive.writestr("readme.txt", "nothing")
    with pytest.raises(ModelImportError, match="no 3D model"):
        reader.read_model(tmp_path / "docs.zip")
    with zipfile.ZipFile(tmp_path / "sketchup.zip", "w") as archive:
        archive.writestr("Sofa.skp", b"skp")
    with pytest.raises(ModelImportError, match="Interoperability > Merge"):
        reader.read_model(tmp_path / "sketchup.zip")


def test_zip_member_paths_are_checked():
    assert reader._safe_member("Model/table.obj")
    for bad in ("../evil.obj", "/etc/passwd", "a/../../b.obj", "C:/x.obj", "\\\\server\\x.obj"):
        assert not reader._safe_member(bad), bad


def test_unreadable_formats_say_what_to_do(tmp_path):
    for name, text in (("Sofa.skp", "File > Interoperability > Merge"), ("chair.blend", "Export it from Blender"),
                       ("lamp.xyz", "unknown 3D format")):
        (tmp_path / name).write_bytes(b"data")
        with pytest.raises(ModelImportError, match=text.replace(">", ".")):
            reader.read_model(tmp_path / name)
    with pytest.raises(ModelImportError, match="does not exist"):
        reader.read_model(tmp_path / "missing.glb")
    (tmp_path / "broken.glb").write_bytes(b"not a glb")
    with pytest.raises(ModelImportError, match="Could not read broken.glb"):
        reader.read_model(tmp_path / "broken.glb")


def test_fbx_without_assimp_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(reader, "assimp_available", lambda: False)
    (tmp_path / "sofa.fbx").write_bytes(b"Kaydara FBX Binary")
    with pytest.raises(ModelImportError, match="assimp-py"):
        reader.read_model(tmp_path / "sofa.fbx")
    (tmp_path / "chair.3ds").write_bytes(b"MM")
    with pytest.raises(ModelImportError, match="3DS files need the assimp-py package"):
        reader.read_model(tmp_path / "chair.3ds")


def test_assimp_reader_matches_trimesh(tmp_path):
    pytest.importorskip("assimp_py")
    for path in (write_table(tmp_path / "a" / "table.glb"), write_table(tmp_path / "b" / "table.obj")):
        by_trimesh = reader._read_with_trimesh(path, reader.FORMATS[path.suffix])
        by_assimp = reader._read_with_assimp(path, reader.FORMATS[path.suffix])
        assert by_assimp.reader == "assimp" and by_assimp.triangles == by_trimesh.triangles
        assert {m.name for m in by_assimp.materials.values()} == {"Wood", "Metal"}
        low = np.concatenate([p.vertices for p in by_assimp.parts]).min(axis=0)
        assert low == pytest.approx(np.concatenate([p.vertices for p in by_trimesh.parts]).min(axis=0), abs=1e-6)


def test_assimp_applies_node_transforms(tmp_path):
    pytest.importorskip("assimp_py")
    scene = trimesh.Scene()
    moved = np.eye(4)
    moved[:3, 3] = (10, 20, 30)
    scene.add_geometry(trimesh.creation.box((1, 2, 3)), node_name="Moved", transform=moved)
    scene.export(str(tmp_path / "moved.glb"))
    model = reader._read_with_assimp(tmp_path / "moved.glb", "glTF")
    assert model.parts[0].name == "Moved"
    assert model.parts[0].vertices.min(axis=0) == pytest.approx((9.5, 19, 28.5))


def test_collada_up_axis_and_unit(tmp_path):
    path = tmp_path / "lamp.dae"
    path.write_bytes(trimesh.exchange.dae.export_collada([trimesh.creation.box((10, 20, 30))]))
    text = path.read_text()
    text = text.replace("<up_axis>Y_UP</up_axis>", "<up_axis>Z_UP</up_axis>")
    if "<up_axis>" not in text:
        text = text.replace("</asset>", "<up_axis>Z_UP</up_axis></asset>")
    text = text.replace("<unit ", '<unit name="centimeter" meter="0.01" ', 1) if "<unit " in text else \
        text.replace("</asset>", '<unit name="centimeter" meter="0.01"/></asset>')
    path.write_text(text)
    model = reader.read_model(path)
    assert model.format == "Collada" and model.up_axis == "Z" and model.unit == "cm"
    assert ImportSession(model).size() == pytest.approx((0.1, 0.2, 0.3))


def test_weld_merges_seams_and_drops_degenerate_faces():
    vertices = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0], [5, 5, 5]], dtype=float)
    faces = np.array([[0, 1, 2], [3, 5, 4], [0, 0, 1]])
    welded, welded_faces = reader._weld(vertices, faces)
    assert len(welded) == 4 and welded_faces.shape == (2, 3)


# --- clean-up --------------------------------------------------------------------

def test_y_up_model_stands_on_the_ground_at_the_bottom_centre(session):
    assert session.edits.up_axis == "Y" and session.edits.unit == "m" and session.unit_source == "file"
    assert session.size() == pytest.approx((1.2, 0.8, TOP_Z_PLUS_VASE), abs=1e-6)
    points = np.concatenate([m.vertices for m in session.meshes(0)])
    assert points.min(axis=0)[2] == pytest.approx(0.0)
    centre = (points.min(axis=0) + points.max(axis=0)) / 2
    assert centre[:2] == pytest.approx((0.0, 0.0), abs=1e-9)
    vase = next(m for m in session.meshes(0) if m.name == "Vase")
    # The vase was at +x, +y (behind the front edge) in the original: it must still be there.
    assert vase.vertices.mean(axis=0)[:2] == pytest.approx((0.2, 0.1), abs=1e-3)


def test_up_axis_unit_turn_and_undo(session):
    session.set_up_axis("Z")
    assert session.size() == pytest.approx((1.2, TOP_Z_PLUS_VASE, 0.8), abs=1e-6)
    session.set_unit("cm")
    assert session.size() == pytest.approx((0.012, TOP_Z_PLUS_VASE / 100, 0.008), abs=1e-6)
    assert session.undo() and session.edits.unit == "m"
    assert session.undo() and session.edits.up_axis == "Y"
    assert not session.undo()
    session.set_turn(90)
    assert session.size() == pytest.approx((0.8, 1.2, TOP_Z_PLUS_VASE), abs=1e-6)
    with pytest.raises(ModelImportError, match="Unknown unit"):
        session.set_unit("furlong")
    with pytest.raises(ModelImportError, match="Unknown up axis"):
        session.set_up_axis("W")


def test_rotations_are_proper_rotations():
    for axis, rows in ROTATIONS.items():
        matrix = np.asarray(rows, dtype=float)
        assert np.allclose(matrix @ matrix.T, np.eye(3)) and np.linalg.det(matrix) == pytest.approx(1.0), axis
        up = {"X": 0, "Y": 1, "Z": 2}[axis[-1]]
        direction = np.zeros(3)
        direction[up] = -1.0 if axis.startswith("-") else 1.0
        assert matrix @ direction == pytest.approx((0, 0, 1)), axis


def test_delete_restore_and_small_parts(session):
    parts = {p["name"]: p for p in session.parts()}
    assert parts["TableTop"]["size"] == {"x": 1.2, "y": 0.8, "z": 0.04}
    assert parts["Leg 1"]["materialName"] == "Metal" and parts["Vase"]["triangles"] == 5120
    screws = sorted(parts[f"Screw {i}"]["id"] for i in (1, 2, 3))
    assert sorted(session.small_parts(0.01)) == screws
    assert sorted(session.remove_small_parts(0.01)) == screws
    assert all(p["deleted"] for p in session.parts() if p["name"].startswith("Screw"))
    assert session.triangles(0) == TABLE_TRIANGLES - 36

    session.delete_parts([parts["TableTop"]["id"], parts["Vase"]["id"]])
    assert session.size() == pytest.approx((1.15, 0.75, 0.71), abs=1e-6)  # only the legs are left
    with pytest.raises(ModelImportError, match="Unknown part ids: nope"):
        session.delete_parts(["nope"])
    legs = [parts[f"Leg {i}"]["id"] for i in (1, 2, 3, 4)]
    with pytest.raises(ModelImportError, match="every part"):
        session.delete_parts(legs)
    with pytest.raises(ModelImportError, match="Every part is below"):
        session.remove_small_parts(10.0)
    assert session.undo()
    assert session.size() == pytest.approx((1.2, 0.8, TOP_Z_PLUS_VASE), abs=1e-6)
    session.restore_parts()
    assert not [p for p in session.parts() if p["deleted"]]
    assert session.undo() and len([p for p in session.parts() if p["deleted"]]) == 3


def test_history_is_capped(session):
    for i in range(150):
        session.set_turn(i + 1)
    assert len(session.history) == 100


def test_summary_for_the_panel(session):
    summary = session.summary(placements=1)
    assert summary["name"] == "Dining Table" and summary["format"] == "glTF" and summary["unitSource"] == "file"
    assert summary["size"] == {"x": 1.2, "y": 0.8, "z": 0.91}
    assert {u["id"] for u in summary["units"]} == {"mm", "cm", "m", "inch", "ft"}
    assert summary["triangles"] == summary["fullTriangles"] == TABLE_TRIANGLES
    assert len(summary["parts"]) == 9 and {m["name"] for m in summary["materials"]} == {"Wood", "Metal"}
    levels = summary["levels"]
    assert levels[0]["triangles"] == TABLE_TRIANGLES and len(levels) > 1
    assert all(a["triangles"] >= b["triangles"] for a, b in zip(levels, levels[1:]))
    assert summary["budget"]["colour"] == "green" and summary["canUndo"] is False


def test_levels_reduce_the_vase_but_keep_boxes(session):
    last = session.set_level(99)
    assert last == len(session.levels) - 1
    meshes = {m.name: m for m in session.meshes()}
    assert len(meshes["TableTop"].faces) == 12 and len(meshes["Leg 1"].faces) == 12
    assert len(meshes["Vase"].faces) < 1000
    # Reduced, the vase still has its size and place.
    full = {m.name: m for m in session.meshes(0)}
    assert np.ptp(meshes["Vase"].vertices, axis=0) == pytest.approx(np.ptp(full["Vase"].vertices, axis=0), rel=0.1)
    assert session.triangles() < session.triangles(0)


def test_guess_unit_from_size():
    def model(extent):
        part = reader.Part("p0", "x", "m0", np.array([[0, 0, 0], [extent, extent / 2, extent / 3]], float),
                           np.array([[0, 1, 1]]))
        return reader.Model(Path("x.obj"), "OBJ", [part], {})

    assert guess_unit(model(1.2))[0] == "m"
    assert guess_unit(model(2.4))[0] == "m"
    assert guess_unit(model(80))[0] == "cm"
    assert guess_unit(model(180))[0] == "cm"
    assert guess_unit(model(900))[0] == "mm"
    assert guess_unit(model(2000))[0] == "mm"


# --- polygon reduction and budget ---------------------------------------------------

def test_level_targets():
    assert level_targets(1200) == [1200]
    targets = level_targets(500_000)
    assert len(targets) == 8 and targets[0] == 500_000 and targets[-1] == 1000
    ratios = [a / b for a, b in zip(targets, targets[1:])]
    assert max(ratios) / min(ratios) < 1.01  # log-spaced
    assert level_targets(5_000_000)[-1] == 10_000  # 0.2 % of a huge model


def test_simplify_mesh_reaches_the_target_and_keeps_an_open_border():
    sphere = trimesh.creation.icosphere(subdivisions=5)
    vertices, faces = simplify_mesh(sphere.vertices, sphere.faces, 2000)
    assert 1500 <= len(faces) <= 2200 and faces.max() < len(vertices)
    # An open, gently curved sheet (like a cushion cover): its border must stay where it is.
    n = 30
    x, y = np.meshgrid(np.linspace(0, 1, n), np.linspace(0, 1, n))
    sheet = np.column_stack([x.ravel(), y.ravel(), 0.05 * np.sin(3 * x.ravel()) * np.sin(3 * y.ravel())])
    quads = [(r * n + c, r * n + c + 1, (r + 1) * n + c + 1, (r + 1) * n + c)
             for r in range(n - 1) for c in range(n - 1)]
    faces = np.array([t for a, b, c, d in quads for t in ((a, b, c), (a, c, d))])
    v, f = simplify_mesh(sheet, faces, len(faces) // 4)
    assert len(f) < len(faces) // 2
    for corner in ((0, 0), (1, 0), (1, 1), (0, 1)):
        assert np.isclose(v[:, :2], corner).all(axis=1).any(), corner
    on_edge = np.isclose(v[:, 0], 0) | np.isclose(v[:, 0], 1) | np.isclose(v[:, 1], 0) | np.isclose(v[:, 1], 1)
    assert on_edge.sum() >= 4 * (n - 1) * 0.9  # the border vertices are locked
    small = trimesh.creation.box()
    assert simplify_mesh(small.vertices, small.faces, 4)[1].shape == (12, 3)


def test_levels_are_computed_once_with_progress(session):
    levels = Levels(session.model.parts)
    calls = []
    levels.compute(lambda f, message: calls.append(f))
    assert levels.computed and calls[-1] == pytest.approx(1.0)
    counts = levels.counts(session.model.parts[i].id for i in range(len(session.model.parts)))
    assert counts[0] == TABLE_TRIANGLES and counts == sorted(counts, reverse=True)


def test_budget_colours():
    assert budget(20_000)["colour"] == "green"
    assert budget(20_001)["colour"] == "amber" and "Medium" in budget(20_001)["message"]
    assert budget(100_001)["colour"] == "red" and "Heavy" in budget(100_001)["message"]
    assert budget(6_000, placements=10)["colour"] == "amber" and budget(5_000, placements=10)["colour"] == "green"
    assert budget(1_200, placements=20)["colour"] == "amber" and budget(1_000, placements=25)["green"] == 1_000
    assert budget(30_000, placements=20)["colour"] == "red" and "20 copies" in budget(30_000, placements=20)["message"]
    assert 0 < budget(50_000)["fraction"] < 1 and budget(10**7)["fraction"] == 1.0


# --- preview -------------------------------------------------------------------------

def test_preview_png(session):
    data = render_preview(session.meshes(), session.model.materials, size=128)
    assert data.startswith(b"\x89PNG")
    image = Image.open(__import__("io").BytesIO(data)).convert("RGBA")
    assert image.size == (128, 128)
    pixels = np.asarray(image).reshape(-1, 4)
    drawn = pixels[pixels[:, 3] > 200]
    assert 0.1 < len(drawn) / len(pixels) < 0.9
    assert (drawn[:, 0] > drawn[:, 2] + 30).any()  # brownish wood
    assert pixels[0, 3] == 0  # transparent corners
    assert render_preview([], {}, size=32).startswith(b"\x89PNG")


# --- GDL -------------------------------------------------------------------------------

def parse_bodies(script: str) -> list[dict]:
    bodies, current = [], None
    for line in script.splitlines():
        line = line.strip()
        if line == "BASE":
            current = {"vert": [], "edge": [], "pgon": []}
        elif line.startswith(("VERT ", "EDGE ", "PGON ")):
            values = [float(v) for v in line[5:].split(",")]
            current[line[:4].lower()].append(values)
        elif line.startswith("BODY "):
            current["body"] = int(line[5:])
            bodies.append(current)
    return bodies


def check_body(body: dict) -> None:
    vertices, edges = len(body["vert"]), body["edge"]
    for a, b, p1, p2, status in edges:
        assert 1 <= a <= vertices and 1 <= b <= vertices and a != b and (p1, p2) == (-1, -1)
        assert status in (0, 1, 2)
    for n, vect, status, *loop in body["pgon"]:
        assert n == 3 == len(loop) and vect == 0 and status in (0, 2)
        ends = []
        for e in loop:
            a, b = edges[abs(int(e)) - 1][:2]
            ends.append((a, b) if e > 0 else (b, a))
        assert all(ends[i][1] == ends[(i + 1) % 3][0] for i in range(3)), loop


def make_xml(session, **kwargs) -> tuple[str, ET.Element]:
    text = gdl.library_part_xml("Dining Table", session.meshes(), session.model.materials, preview="Dining Table.png",
                                source="Dining Table.glb", **kwargs)
    return text, ET.fromstring(text.encode("utf8"))


def test_library_part_xml_structure(session):
    text, root = make_xml(session)
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert root.tag == "Symbol" and root.get("IsPlaceable") == "true" and root.get("Version") == gdl.FORMAT_VERSION
    assert root.get("MainGUID") == gdl.library_part_guid("Dining Table")
    assert [g.text for g in root.find("Ancestry")] == [gdl.GENERAL_GDL_OBJECT, gdl.MODEL_ELEMENT]
    for tag in ("Script_1D", "Script_2D", "Script_3D", "Script_PR", "Script_UI", "Script_VL"):
        assert root.find(tag) is not None and root.find(tag).get("SectVersion") == "20", tag
    params = {p.get("Name"): p for p in root.find("ParamSection/Parameters")}
    assert list(params)[:3] == ["A", "B", "ZZYZX"]
    assert float(params["A"].find("Value").text) == pytest.approx(1.2)
    assert float(params["B"].find("Value").text) == pytest.approx(0.8)
    assert float(params["ZZYZX"].find("Value").text) == pytest.approx(0.91)
    materials = [p for p in params.values() if p.tag == "Material"]
    assert [m.get("Name") for m in materials] == ["mat_1", "mat_2"]
    assert {m.find("Description").text for m in materials} == {'"Wood"', '"Metal"'}
    assert params["useModelColours"].find("Value").text == "1" and params["keepProportions"].tag == "Boolean"
    assert params["AC_show2DHotspotsIn3D"].find("Flags/ParFlg_Hidden") is not None
    picture = root.find("Picture")
    assert picture.get("path") == "Dining Table.png" and picture.get("MIME") == "image/png"
    assert picture.get("SubIdent") == "0"
    assert "Dining Table.glb" in root.find("Comment").text
    assert "_a0 = 1.2" in root.find("Script_1D").text and "_h0 = 0.91" in root.find("Script_1D").text


def test_3d_script_bodies(session):
    _, root = make_xml(session)
    script = root.find("Script_3D").text
    bodies = parse_bodies(script)
    assert len(bodies) == 9 and sum(len(b["pgon"]) for b in bodies) == TABLE_TRIANGLES
    for body in bodies:
        check_body(body)
        assert body["body"] == -1  # every test part is closed
    top = bodies[[m.name for m in session.meshes()].index("TableTop")]
    statuses = sorted(int(e[4]) for e in top["edge"])
    assert statuses == [0] * 12 + [1] * 6  # 12 box edges stay, the 6 face diagonals are hidden
    vase = bodies[[m.name for m in session.meshes()].index("Vase")]
    assert {int(e[4]) for e in vase["edge"]} == {2} and {int(p[2]) for p in vase["pgon"]} == {2}  # smooth
    assert script.count('\tMATERIAL "tvd_') == 9 and script.count("DEFINE MATERIAL") == 2
    assert "MUL _sx, _sy, _sz" in script and "IF _h0 > 0.00001 THEN _sz = ZZYZX / _h0" in script
    assert script.count("HOTSPOT ") == 9
    coords = np.array([v for b in bodies for v in b["vert"]])
    assert coords.min(axis=0) == pytest.approx((-0.6, -0.4, 0.0), abs=1e-5)


def test_open_mesh_is_a_surface_body_and_degenerate_faces_are_dropped():
    vertices = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [2, 0, 0]], dtype=float)
    faces = np.array([[0, 1, 2], [0, 2, 3], [0, 1, 4]])  # the last one has no area
    lines, closed = gdl.body_lines(vertices, faces)
    (body,) = parse_bodies("\n".join(lines))
    assert not closed and body["body"] == 4 and len(body["pgon"]) == 2 and len(body["vert"]) == 4
    check_body(body)
    assert sorted(int(e[4]) for e in body["edge"]) == [0, 0, 0, 0, 1]
    assert gdl.body_lines(vertices, np.array([[0, 1, 4]])) == ([], False)


def test_2d_script_outline_and_hotspots(session):
    _, root = make_xml(session)
    script = root.find("Script_2D").text
    assert "PROJECT2 3, 270, 2" in script and "MUL2 _sx, _sy" in script
    polys = [line.strip() for line in script.splitlines() if line.strip().startswith("POLY2 ")]
    outline = [float(v) for v in polys[0][6:].split(",")]
    assert outline[0] == 4 and outline[1] == 5  # the table top's rectangle, closed contour
    points = np.array(outline[2:]).reshape(-1, 2)
    assert points.min(axis=0) == pytest.approx((-0.6, -0.4)) and points.max(axis=0) == pytest.approx((0.6, 0.4))
    assert len(polys) >= 5  # the legs and the vase too
    assert script.count("HOTSPOT2 ") == 9 and "HOTSPOT2 A / 2, B / 2" in script


def test_parameter_script(session):
    _, root = make_xml(session)
    script = root.find("Script_VL").text
    assert 'STRSTR(GLOB_MODPAR_NAME, "mat_") = 1 THEN PARAMETERS useModelColours = 0' in script
    assert 'IF GLOB_MODPAR_NAME = "A" THEN PARAMETERS B = A * _b0 / _a0, ZZYZX = A * _h0 / _a0' in script
    flat = [ExportMesh("p", "Rug", "m0", np.array([[0, 0, 0], [2, 0, 0], [2, 3, 0], [0, 3, 0]], float),
                       np.array([[0, 1, 2], [0, 2, 3]]))]
    rug = ET.fromstring(gdl.library_part_xml("Rug", flat, {"m0": reader.Material("m0", "Wool")}))
    assert '"ZZYZX" THEN' not in rug.find("Script_VL").text  # a flat model never divides by its zero height
    assert rug.find("Picture") is None


def test_names_and_text_are_made_safe():
    weird = 'Sofa "Deluxe" ]]> v2'
    text = gdl.library_part_xml(weird, [ExportMesh("p", 'Leg "1"]]>', "m0", np.eye(3), np.array([[0, 1, 2]]))],
                                {"m0": reader.Material("m0", 'Oak "dark"]]>')}, source="a]]>b.glb")
    root = ET.fromstring(text)
    assert root.find("Script_3D").text.count('"') % 2 == 0
    assert gdl.gdl_text('a "b" ]]> c') == "a 'b' ]] > c"
    assert gdl.safe_file_name('Table: "big" / small?') == "Table big small"
    assert gdl.safe_file_name("...") == "Imported model"
    assert gdl.library_part_guid("Furniture/Table") == gdl.library_part_guid("furniture/table")
    assert gdl.library_part_guid("a") != gdl.library_part_guid("b")
    with pytest.raises(ValueError):
        gdl.library_part_xml("Empty", [], {})


def test_convex_hull():
    rng = np.random.default_rng(3)
    square = np.array([[0, 0], [2, 0], [2, 2], [0, 2]], dtype=float)
    points = np.vstack([square, rng.random((200, 2)) * 1.8 + 0.1])
    hull = gdl.convex_hull(points)
    assert sorted(map(tuple, hull)) == sorted(map(tuple, square))
    x, y = hull[:, 0], hull[:, 1]
    assert (np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) > 0  # counterclockwise


# --- library folders and the converter ------------------------------------------------------

def test_library_folders(tmp_path):
    root = tmp_path / "Tevavid Library"
    assert library.clean_folder("Furniture > Tables") == "Furniture/Tables"
    assert library.clean_folder(" Furniture\\ Office  Chairs ") == "Furniture/Office Chairs"
    assert library.clean_folder("") == ""
    for bad in ("../x", "/abs", "C:\\x", "a/./b", "a?b"):
        with pytest.raises(ModelImportError):
            library.clean_folder(bad)
    assert library.create_folder(root, "Furniture > Tables")["folder"] == "Furniture/Tables"
    library.create_folder(root, "Lighting")
    (root / "Furniture" / "Tables" / "Oak Table.gsm").write_bytes(b"gsm")
    (root / ".hidden").mkdir()
    tree = library.folder_tree(root)
    assert tree["path"] == str(root) and [f["name"] for f in tree["folders"]] == ["Furniture", "Lighting"]
    tables = tree["folders"][0]["folders"][0]
    assert tables["folder"] == "Furniture/Tables" and tables["objects"] == ["Oak Table"]
    assert library.object_path(root, "Furniture/Tables", "Oak") == root / "Furniture" / "Tables" / "Oak.gsm"
    assert library.default_library_root(System(home=tmp_path)) == tmp_path / "Documents" / "Tevavid Library"


def test_converter_locations():
    win = library.converter_candidates(r"C:\Program Files\Graphisoft\Archicad 28\Archicad.exe", "win32")
    assert str(win[0]).replace("\\", "/").endswith("Archicad 28/LP_XMLConverter.exe")
    mac = library.converter_candidates("/Applications/GRAPHISOFT/Archicad 28/Archicad 28.app/Contents/MacOS/Archicad",
                                       "darwin")
    assert str(mac[0]) == ("/Applications/GRAPHISOFT/Archicad 28/Archicad 28.app/Contents/MacOS/"
                           "LP_XMLConverter.app/Contents/MacOS/LP_XMLConverter")
    assert library.converter_candidates(None) == []



def test_convert_with_a_fake_converter(session, tmp_path):
    converter = fake_converter(tmp_path)
    text, _ = make_xml(session)
    xml = tmp_path / "Dining Table.xml"
    xml.write_text(text, encoding="utf8")
    images = tmp_path / "images"
    images.mkdir()
    (images / "Dining Table.png").write_bytes(render_preview(session.meshes(), session.model.materials, 64))
    gsm = tmp_path / "lib" / "Furniture" / "Dining Table.gsm"
    done = library.convert(converter, xml, gsm, images)
    assert gsm.read_bytes().startswith(b"GSM ") and "Conversion finished" in done["output"]
    call = (tmp_path / "calls.json").read_text().splitlines()[0]
    assert '"xml2libpart", "-img"' in call and call.endswith('Dining Table.gsm"]')
    assert not list(gsm.parent.glob(".*"))  # no staging files left behind

    failing = fake_converter(tmp_path / "lib", fail=True)
    with pytest.raises(library.ConversionError, match="syntax error at line 3"):
        library.convert(failing, xml, gsm, images)
    assert gsm.read_bytes().startswith(b"GSM ")  # the previous version is kept
    with pytest.raises(library.ConversionError, match="Could not run"):
        library.convert(tmp_path / "missing-converter", xml, gsm)


def test_convert_timeout(tmp_path):
    slow = tmp_path / "slow.py"
    slow.write_text("import time\ntime.sleep(5)\n")
    with pytest.raises(library.ConversionError, match="did not finish"):
        library.convert([sys.executable, str(slow)], tmp_path / "a.xml", tmp_path / "a.gsm", timeout=0.5)


@pytest.mark.skipif(sys.platform == "win32", reason="runs the fake converter as an executable script")
def test_find_converter_in_an_installed_archicad(tmp_path, monkeypatch):
    monkeypatch.delenv(library.CONVERTER_ENV, raising=False)
    system = System(home=tmp_path)
    for version in (27, 28):
        folder = tmp_path / "GRAPHISOFT" / f"Archicad {version}"
        folder.mkdir(parents=True)
        (folder / "Archicad").write_text("")
        converter = folder / "LP_XMLConverter"
        converter.write_text(f"#!{sys.executable}\n" + Path(fake_converter(tmp_path)[1]).read_text())
        converter.chmod(0o755)
    assert library.find_converter(None, None, system) == tmp_path / "GRAPHISOFT" / "Archicad 28" / "LP_XMLConverter"
    assert library.find_converter(None, 27, system) == tmp_path / "GRAPHISOFT" / "Archicad 27" / "LP_XMLConverter"
    running = tmp_path / "GRAPHISOFT" / "Archicad 27" / "Archicad"
    assert library.find_converter(running, None, system).parent.name == "Archicad 27"
    monkeypatch.setenv(library.CONVERTER_ENV, str(tmp_path / "GRAPHISOFT" / "Archicad 28" / "LP_XMLConverter"))
    assert library.find_converter(running, 27, system).parent.name == "Archicad 28"
    monkeypatch.delenv(library.CONVERTER_ENV)
    assert library.find_converter(None, None, System(home=tmp_path / "nobody")) is None

    # The real thing is an executable: run the shell-script stand-in directly.
    xml = tmp_path / "t.xml"
    xml.write_text('<?xml version="1.0"?><Symbol MainGUID="X"/>')
    library.convert(library.find_converter(None, None, system), xml, tmp_path / "t.gsm")
    assert (tmp_path / "t.gsm").read_bytes() == b"GSM X"


# --- Downloads ----------------------------------------------------------------------------------

def test_new_downloads(tmp_path):
    now = time.time()

    def file(name, age, data=b"x"):
        path = tmp_path / name
        path.write_bytes(data)
        os.utime(path, (now - age, now - age))
        return path

    file("old.glb", 3600)
    file("table.glb", 60)
    file("sofa.fbx", 30)
    file("photo.jpg", 30)
    file("chair.obj.crdownload", 30)
    file("growing.obj", 0.5)
    file("Couch.skp", 20)
    with zipfile.ZipFile(tmp_path / "Lamp_D5.zip", "w") as archive:
        archive.writestr("Lamp/lamp.fbx", b"fbx")
        archive.writestr("Lamp/textures/a.jpg", b"jpg")
    os.utime(tmp_path / "Lamp_D5.zip", (now - 10, now - 10))
    with zipfile.ZipFile(tmp_path / "invoice.zip", "w") as archive:
        archive.writestr("invoice.pdf", b"pdf")
    os.utime(tmp_path / "invoice.zip", (now - 10, now - 10))

    found = new_downloads(now - 600, tmp_path, now=now)
    assert [f["name"] for f in found] == ["Lamp_D5.zip", "Couch.skp", "sofa.fbx", "table.glb"]
    assert found[0]["contains"] == ["lamp.fbx"] and found[0]["readable"] and found[0]["format"] == "ZIP"
    assert found[1]["readable"] is False and found[1]["format"] == "SketchUp"
    assert found[3]["format"] == "glTF" and found[3]["size"] == 1
    assert new_downloads(now - 600, tmp_path / "missing", now=now) == []
    assert new_downloads(now - 600, tmp_path, now=now + 10)[0]["name"] == "growing.obj"
