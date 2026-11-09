"""Small 3D model files for the Model Import tests, made with trimesh.

The test table, in Archicad's terms (metres, Z up): a 1.2 x 0.8 m wooden top
from 0.71 to 0.75 m, four metal legs, three 6 mm metal screws and a 5120
triangle wooden vase on the top, so there is something to reduce.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

WOOD = (150, 100, 50, 255)
METAL = (200, 200, 210, 255)
TOP_Z = 0.75
Y_UP = np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], dtype=float)  # Z-up to Y-up


def box(size, centre) -> trimesh.Trimesh:
    mesh = trimesh.creation.box(size)
    mesh.apply_translation(centre)
    return mesh


def table_meshes() -> list[tuple[str, trimesh.Trimesh, tuple]]:
    """(name, mesh, colour) of every part, Z up, in metres."""
    parts = [("TableTop", box((1.2, 0.8, 0.04), (0, 0, 0.73)), WOOD)]
    for i, (x, y) in enumerate([(-0.55, -0.35), (0.55, -0.35), (-0.55, 0.35), (0.55, 0.35)]):
        parts.append((f"Leg {i + 1}", box((0.05, 0.05, 0.71), (x, y, 0.355)), METAL))
    for i, x in enumerate((-0.3, 0.0, 0.3)):
        parts.append((f"Screw {i + 1}", box((0.006, 0.006, 0.006), (x, 0.395, 0.705)), METAL))
    vase = trimesh.creation.icosphere(subdivisions=4, radius=0.08)
    vase.apply_translation((0.2, 0.1, TOP_Z + 0.08))
    parts.append(("Vase", vase, WOOD))
    return parts


def table_scene(scale: float = 1.0, y_up: bool = True) -> trimesh.Scene:
    materials = {WOOD: trimesh.visual.material.PBRMaterial(name="Wood", baseColorFactor=list(WOOD)),
                 METAL: trimesh.visual.material.PBRMaterial(name="Metal", baseColorFactor=list(METAL))}
    scene = trimesh.Scene()
    for name, mesh, colour in table_meshes():
        mesh = mesh.copy()
        if y_up:
            mesh.apply_transform(Y_UP)
        mesh.apply_scale(scale)
        mesh.visual = trimesh.visual.TextureVisuals(material=materials[colour])
        scene.add_geometry(mesh, node_name=name, geom_name=f"{name} mesh")
    return scene


def write_table(path: Path, scale: float = 1.0, y_up: bool = True) -> Path:
    """The table as glTF/GLB, OBJ (+MTL), STL or PLY, depending on the extension."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    table_scene(scale, y_up).export(str(path))
    return path


def textured_box_zip(path: Path) -> Path:
    """A ZIP like the ones download sites deliver: an OBJ, its MTL and a red texture in a sub-folder."""
    folder = path.parent / "zip-src"
    folder.mkdir(parents=True, exist_ok=True)
    mesh = trimesh.creation.box((0.5, 0.5, 0.5))
    uv = np.random.default_rng(1).random((len(mesh.vertices), 2))
    image = Image.new("RGB", (8, 8), (255, 0, 0))
    material = trimesh.visual.material.SimpleMaterial(name="RedPaint", image=image, diffuse=(255, 255, 255, 255))
    mesh.visual = trimesh.visual.TextureVisuals(uv=uv, material=material)
    scene = trimesh.Scene()
    scene.add_geometry(mesh, node_name="Cube", geom_name="Cube")
    scene.export(str(folder / "cube.obj"))
    with zipfile.ZipFile(path, "w") as archive:
        for file in folder.iterdir():
            archive.write(file, f"Cube_model/{file.name}")
        archive.writestr("Cube_model/readme.txt", "A cube.")
    return path


def coloured_ply(path: Path) -> Path:
    mesh = trimesh.creation.box((2.0, 1.0, 0.5))
    mesh.visual = trimesh.visual.ColorVisuals(mesh, face_colors=np.tile([0, 128, 255, 255], (len(mesh.faces), 1)))
    mesh.export(str(path))
    return path


def fake_converter(folder: Path, fail: bool = False) -> list[str]:
    """A stand-in for LP_XMLConverter: checks the XML and the arguments, writes a .gsm, logs its call."""
    script = folder / "fake_lp_xmlconverter.py"
    script.write_text(f'''
import sys, json, xml.etree.ElementTree as ET
from pathlib import Path
args = sys.argv[1:]
Path({str(folder / "calls.json")!r}).open("a").write(json.dumps(args) + "\\n")
assert args[0] == "xml2libpart", args
if {fail!r}:
    print("Error: Script_3D: syntax error at line 3")
    sys.exit(3)
xml, gsm = args[-2], args[-1]
root = ET.parse(xml).getroot()
assert root.tag == "Symbol"
if "-img" in args:
    picture = root.find("Picture")
    assert picture is None or (Path(args[args.index("-img") + 1]) / picture.get("path")).is_file()
Path(gsm).write_bytes(b"GSM " + root.get("MainGUID").encode())
print("Conversion finished")
''')
    return [sys.executable, str(script)]

