"""Read 3D model files into parts: one triangle mesh per named node (object) and material.

glTF/GLB, OBJ (+MTL), STL, PLY, OFF and Collada (DAE) are read with trimesh.
FBX, 3DS and 3MF are read with assimp (the ``assimp-py`` package, which has
wheels for Python 3.12 and newer only); without it they fail with a clear
message. A ZIP archive (how many sites deliver a model with its textures) is
unpacked and its model file read. SketchUp files cannot be read: their format
is closed, and Archicad imports them itself.

The parts keep the file's own coordinates and units; :mod:`.cleanup` turns
them into metres, Z up, standing on the ground.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .errors import ModelImportError

UNITS = {"mm": 0.001, "cm": 0.01, "m": 1.0, "inch": 0.0254, "ft": 0.3048}
UP_AXES = ("Z", "Y", "X", "-Z", "-Y", "-X")

TRIMESH_FORMATS = {".glb": "glTF", ".gltf": "glTF", ".obj": "OBJ", ".stl": "STL", ".ply": "PLY",
                   ".off": "OFF", ".dae": "Collada"}
ASSIMP_FORMATS = {".fbx": "FBX", ".3ds": "3DS", ".3mf": "3MF"}
FORMATS = {**TRIMESH_FORMATS, **ASSIMP_FORMATS}
ARCHIVES = (".zip",)
SUPPORTED_EXTENSIONS = tuple(FORMATS) + ARCHIVES
# The order in which an archive's files are tried: the richest formats first.
ARCHIVE_PREFERENCE = (".fbx", ".glb", ".gltf", ".obj", ".dae", ".3ds", ".3mf", ".ply", ".stl", ".off")

# Most programs write these axes; the user can change them in the import.
DEFAULT_UP = {"glTF": "Y", "OBJ": "Y", "FBX": "Y", "Collada": "Y", "STL": "Z", "PLY": "Z", "OFF": "Z",
              "3DS": "Z", "3MF": "Z"}

SKETCHUP_MESSAGE = ("SketchUp (.skp) files cannot be read here: the format is closed. "
                    "Use Archicad's own File > Interoperability > Merge for SketchUp files, "
                    "or export the model from SketchUp as Collada (.dae), FBX or OBJ.")
OTHER_APPS = {".blend": "Blender", ".max": "3ds Max", ".c4d": "Cinema 4D", ".ma": "Maya", ".mb": "Maya",
              ".rvt": "Revit", ".3dm": "Rhino", ".usdz": "a USD app", ".usd": "a USD app"}
ASSIMP_MISSING = ("{fmt} files need the assimp-py package, which comes with the Tevavid Tools app "
                  "(Python 3.12 or newer). Export the model as glTF/GLB or OBJ instead, "
                  "or update Tevavid Tools.")
MAX_ARCHIVE_BYTES = 4 * 1024 ** 3
DEFAULT_COLOR = (0.75, 0.75, 0.75)


@dataclass
class Material:
    id: str
    name: str
    color: tuple[float, float, float] = DEFAULT_COLOR  # 0..1, with the texture's average colour mixed in
    opacity: float = 1.0
    texture: str | None = None  # the texture file's name, for information

    def to_dict(self) -> dict:
        r, g, b = (int(round(c * 255)) for c in self.color)
        return {"id": self.id, "name": self.name, "color": f"#{r:02x}{g:02x}{b:02x}",
                "opacity": round(self.opacity, 3), "texture": self.texture}


@dataclass
class Part:
    id: str
    name: str
    material: str  # Material.id
    vertices: np.ndarray  # (n, 3) float64, in the file's units and axes
    faces: np.ndarray  # (m, 3) int64 indices into vertices

    @property
    def triangles(self) -> int:
        return int(len(self.faces))


@dataclass
class Model:
    path: Path  # the file that was read (inside the unpacked archive for a ZIP)
    format: str
    parts: list[Part]
    materials: dict[str, Material]
    up_axis: str = "Z"  # the file's up axis, as far as it is known
    unit: str | None = None  # the unit the file states, if it states one
    unit_scale: float | None = None  # metres per file unit, when the file states it but it is not in UNITS
    reader: str = "trimesh"
    notes: list[str] = field(default_factory=list)
    archive: Path | None = None  # the ZIP the file came from
    workdir: Path | None = None  # where the archive was unpacked; removed by close()

    @property
    def triangles(self) -> int:
        return sum(p.triangles for p in self.parts)

    @property
    def name(self) -> str:
        """A name for the library part: the archive's or the file's name."""
        return (self.archive or self.path).stem

    def close(self) -> None:
        if self.workdir is not None:
            shutil.rmtree(self.workdir, ignore_errors=True)
            self.workdir = None


def assimp_available() -> bool:
    try:
        import assimp_py  # noqa: F401
    except ImportError:
        return False
    return True


def read_model(path: str | Path) -> Model:
    """Read a model file (or a ZIP with one) into parts and materials."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".skp":
        raise ModelImportError(SKETCHUP_MESSAGE)
    if ext in OTHER_APPS:
        raise ModelImportError(f"{path.name} is a {OTHER_APPS[ext]} file. Export it from {OTHER_APPS[ext]} "
                               "as FBX, OBJ or glTF/GLB and import that.")
    if not path.is_file():
        raise ModelImportError(f"{path} does not exist.")
    if ext in ARCHIVES:
        return _read_archive(path)
    if ext not in FORMATS:
        raise ModelImportError(f"{path.name}: unknown 3D format. Supported: "
                               + ", ".join(sorted({*FORMATS.values()})) + ", and ZIP archives with one of them.")
    fmt = FORMATS[ext]
    if ext in ASSIMP_FORMATS:
        if assimp_available():
            model = _read_with_assimp(path, fmt)
        elif ext == ".3mf":
            model = _read_with_trimesh(path, fmt)
        else:
            raise ModelImportError(ASSIMP_MISSING.format(fmt=fmt))
    else:
        try:
            model = _read_with_trimesh(path, fmt)
        except ModelImportError:
            if ext == ".dae" and assimp_available():
                model = _read_with_assimp(path, fmt)
            else:
                raise
    if not model.parts:
        raise ModelImportError(f"{path.name} has no triangle geometry (only points, lines or empty nodes).")
    return model


# --- archives -----------------------------------------------------------------

def _read_archive(path: Path) -> Model:
    try:
        archive = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ModelImportError(f"{path.name} is not a readable ZIP archive: {exc}") from exc
    with archive:
        members = [m for m in archive.infolist() if not m.is_dir() and _safe_member(m.filename)]
        names = [m.filename for m in members]
        models = [n for n in names if Path(n).suffix.lower() in FORMATS and not Path(n).name.startswith("._")]
        if not models:
            if any(n.lower().endswith(".skp") for n in names):
                raise ModelImportError(SKETCHUP_MESSAGE)
            raise ModelImportError(f"{path.name} contains no 3D model file this tool can read.")
        if sum(m.file_size for m in members) > MAX_ARCHIVE_BYTES:
            raise ModelImportError(f"{path.name} unpacks to more than 4 GB; "
                                   "unpack it yourself and import the model file.")
        models.sort(key=lambda n: (ARCHIVE_PREFERENCE.index(Path(n).suffix.lower()), n.count("/"), len(n)))
        workdir = Path(tempfile.mkdtemp(prefix="tevavid-import-"))
        try:
            for member in members:
                archive.extract(member, workdir)
            model = read_model(workdir / models[0])
        except BaseException:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
    model.archive = path
    model.workdir = workdir
    if len(models) > 1:
        model.notes.append(f"The archive has {len(models)} model files; opened {Path(models[0]).name}.")
    return model


def _safe_member(name: str) -> bool:
    parts = Path(name.replace("\\", "/")).parts
    return bool(parts) and not name.startswith(("/", "\\")) and ".." not in parts and ":" not in parts[0]


# --- shared helpers -----------------------------------------------------------

class _Materials:
    """Collects materials, one entry per distinct name and look."""

    def __init__(self) -> None:
        self.by_key: dict[tuple, Material] = {}

    def add(self, name: str | None, color, opacity: float = 1.0, texture: str | None = None) -> str:
        color = tuple(float(np.clip(c, 0.0, 1.0)) for c in (color if color is not None else DEFAULT_COLOR)[:3])
        if re.fullmatch(r"material_\d+", str(name or "")):
            name = None  # trimesh's placeholder for an unnamed material
        name = _clean_name(name) or ("Default" if texture is None and color == DEFAULT_COLOR else "Material")
        key = (name, tuple(round(c, 3) for c in color), round(float(opacity), 2), texture)
        if key not in self.by_key:
            self.by_key[key] = Material(f"m{len(self.by_key)}", name, color, float(np.clip(opacity, 0.0, 1.0)), texture)
        return self.by_key[key].id

    def result(self, parts: list[Part]) -> dict[str, Material]:
        """The materials the parts use, with distinct names."""
        used = {part.material for part in parts}
        materials = {m.id: m for m in self.by_key.values() if m.id in used}
        names: dict[str, int] = {}
        for material in materials.values():  # the UI and the GDL parameters need distinct names
            names[material.name] = names.get(material.name, 0) + 1
            if names[material.name] > 1:
                material.name = f"{material.name} {names[material.name]}"
        return materials


def _clean_name(name: str | None) -> str:
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    return "".join(ch for ch in text if ch.isprintable())[:80]


def _weld(vertices: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Merge vertices at the same position (files split them at UV and normal seams) and drop degenerate faces."""
    vertices = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    faces = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
    if not len(faces):
        return vertices[:0], faces
    finite = np.isfinite(vertices).all(axis=1)
    faces = faces[finite[faces].all(axis=1)]
    span = float(np.ptp(vertices[finite], axis=0).max()) if finite.any() else 0.0
    tolerance = max(span * 1e-6, 1e-12)
    keys = np.round(np.where(finite[:, None], vertices, 0.0) / tolerance).astype(np.int64)
    _, first, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    faces = inverse.reshape(-1)[faces]
    faces = faces[(faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])]
    used, compact = np.unique(faces, return_inverse=True)
    return vertices[first[used]], compact.reshape(-1, 3).astype(np.int64)


def _image_color(image) -> tuple[float, float, float] | None:
    """The average colour of a texture image (a PIL image or a file path), weighted by its alpha."""
    try:
        from PIL import Image

        if not isinstance(image, Image.Image):
            image = Image.open(image)
        small = image.convert("RGBA")
        small.thumbnail((32, 32))
        pixels = np.asarray(small, dtype=np.float64).reshape(-1, 4) / 255.0
        weight = pixels[:, 3]
        if weight.sum() <= 0:
            return None
        return tuple(float(c) for c in (pixels[:, :3] * weight[:, None]).sum(axis=0) / weight.sum())
    except Exception:
        return None


def _mix(color, texture_color):
    if texture_color is None:
        return color
    return tuple(c * t for c, t in zip(color, texture_color))


def _parts_from_meshes(meshes: list[tuple[str, np.ndarray, np.ndarray, str]]) -> list[Part]:
    """Build parts from (name, vertices, faces, material id); names are made distinct."""
    parts: list[Part] = []
    counts: dict[str, int] = {}
    for name, vertices, faces, material in meshes:
        vertices, faces = _weld(vertices, faces)
        if not len(faces):
            continue
        name = _clean_name(name) or f"Part {len(parts) + 1}"
        counts[name] = counts.get(name, 0) + 1
        parts.append(Part(f"p{len(parts)}", name if counts[name] == 1 else f"{name} ({counts[name]})", material,
                          vertices, faces))
    return parts


# --- trimesh ------------------------------------------------------------------

def _read_with_trimesh(path: Path, fmt: str) -> Model:
    import trimesh

    kwargs = {"split_objects": True, "group_material": False} if fmt == "OBJ" else {}
    try:
        scene = trimesh.load(str(path), force="scene", **kwargs)
    except ModuleNotFoundError as exc:
        raise ModelImportError(f"Reading {fmt} files needs the Python package {exc.name}, which is missing.") from exc
    except Exception as exc:
        raise ModelImportError(f"Could not read {path.name} as {fmt}: {exc}") from exc
    materials = _Materials()
    meshes = []
    skipped = 0
    order = {name: index for index, name in enumerate(scene.geometry)}  # the file's order
    nodes = sorted(scene.graph.nodes_geometry, key=lambda n: (order.get(scene.graph[n][1], len(order)), str(n)))
    for node in nodes:
        transform, geometry_name = scene.graph[node]
        mesh = scene.geometry.get(geometry_name)
        if not isinstance(mesh, trimesh.Trimesh):
            skipped += 1
            continue
        if not len(mesh.faces):
            continue
        vertices = trimesh.transformations.transform_points(np.asarray(mesh.vertices, dtype=np.float64), transform)
        name = path.stem if node == path.name else node
        if fmt == "OBJ" and mesh.metadata.get("object"):
            name = mesh.metadata["object"]
        meshes.append((name, vertices, mesh.faces, _trimesh_material(mesh, materials)))
    parts = _parts_from_meshes(meshes)
    model = Model(path, fmt, parts, materials.result(parts), up_axis=DEFAULT_UP[fmt])
    if skipped:
        model.notes.append(f"{skipped} point or line objects were left out.")
    if fmt == "glTF":
        model.unit = "m"  # glTF is in metres by definition
    elif fmt == "Collada":
        _collada_axes(path, model)
    return model


def _trimesh_material(mesh, materials: _Materials) -> str:
    from trimesh.visual.material import PBRMaterial, SimpleMaterial

    visual = mesh.visual
    material = getattr(visual, "material", None)
    if isinstance(material, PBRMaterial):
        factor = material.baseColorFactor
        color = DEFAULT_COLOR if factor is None else tuple(np.asarray(factor[:3], dtype=np.float64) / 255.0)
        opacity = 1.0 if factor is None else float(factor[3]) / 255.0
        texture = material.baseColorTexture
        return materials.add(material.name, _mix(color, _image_color(texture) if texture is not None else None),
                             opacity, _texture_name(texture))
    if isinstance(material, SimpleMaterial):
        diffuse = np.asarray(material.diffuse, dtype=np.float64) / 255.0
        image = getattr(material, "image", None)
        texture_color = _image_color(image) if image is not None else None
        return materials.add(material.name, _mix(tuple(diffuse[:3]), texture_color),
                             float(diffuse[3]) if len(diffuse) > 3 else 1.0, _texture_name(image))
    if getattr(visual, "kind", None) in ("face", "vertex"):
        colors = visual.face_colors if visual.kind == "face" else visual.vertex_colors
        mean = np.asarray(colors, dtype=np.float64).mean(axis=0) / 255.0
        hex_color = "#" + "".join(f"{int(round(c * 255)):02x}" for c in mean[:3])
        return materials.add(f"Colour {hex_color}", tuple(mean[:3]), float(mean[3]))
    return materials.add(None, DEFAULT_COLOR)


def _texture_name(image) -> str | None:
    if image is None:
        return None
    if isinstance(image, (str, Path)):
        return Path(image).name
    name = getattr(image, "filename", None) or (getattr(image, "info", None) or {}).get("name")
    return Path(str(name)).name if name else "embedded"


def _collada_axes(path: Path, model: Model) -> None:
    """Collada states its up axis and unit in the header; trimesh does not apply them."""
    try:
        with open(path, "rb") as file:
            head = file.read(256 * 1024).decode("utf8", "replace")
    except OSError:
        return
    axis = re.search(r"<up_axis>\s*([XYZ])_UP\s*</up_axis>", head)
    if axis:
        model.up_axis = axis.group(1)
    unit = re.search(r"<unit\b[^>]*\bmeter\s*=\s*\"([0-9.eE+-]+)\"", head)
    if unit:
        _set_declared_unit(model, float(unit.group(1)))


def _set_declared_unit(model: Model, metres: float) -> None:
    for name, scale in UNITS.items():
        if abs(metres - scale) <= scale * 1e-3:
            model.unit = name
            return
    if metres > 0:
        model.unit_scale = metres


# --- assimp -------------------------------------------------------------------

def _read_with_assimp(path: Path, fmt: str) -> Model:
    import assimp_py

    flags = (assimp_py.Process_Triangulate | assimp_py.Process_JoinIdenticalVertices
             | assimp_py.Process_SortByPType | assimp_py.Process_FindDegenerates)
    try:
        scene = assimp_py.import_file(str(path), flags)
    except Exception as exc:
        raise ModelImportError(f"Could not read {path.name} as {fmt}: {exc}") from exc
    if scene is None:
        raise ModelImportError(f"Could not read {path.name} as {fmt}.")
    materials = _Materials()
    material_ids = [_assimp_material(m, path.parent, materials) for m in scene.materials]
    meshes = []

    def walk(node, parent: np.ndarray) -> None:
        world = parent @ np.asarray(node.transformation, dtype=np.float64).reshape(4, 4)
        for index in node.mesh_indices:
            mesh = scene.meshes[index]
            indices = np.asarray(mesh.indices, dtype=np.int64)
            if mesh.num_faces * 3 != len(indices) or not len(indices):
                continue  # points or lines
            vertices = np.asarray(mesh.vertices, dtype=np.float64).reshape(-1, 3)
            vertices = vertices @ world[:3, :3].T + world[:3, 3]
            name = node.name if len(node.mesh_indices) == 1 or not mesh.name else f"{node.name} {mesh.name}"
            if mesh.material_index < len(material_ids):
                material = material_ids[mesh.material_index]
            else:
                material = materials.add(None, DEFAULT_COLOR)
            meshes.append((name or mesh.name, vertices, indices.reshape(-1, 3), material))
        for child in node.children:
            walk(child, world)

    walk(scene.root_node, np.eye(4))
    parts = _parts_from_meshes(meshes)
    return Model(path, fmt, parts, materials.result(parts), up_axis=DEFAULT_UP[fmt], reader="assimp")


def _assimp_material(material: dict, folder: Path, materials: _Materials) -> str:
    color = material.get("COLOR_BASE") or material.get("COLOR_DIFFUSE") or DEFAULT_COLOR
    opacity = material.get("OPACITY", 1.0)
    if len(color) > 3 and opacity == 1.0:
        opacity = color[3]
    textures = material.get("TEXTURES") or {}
    # 1 is the diffuse texture, 12 the PBR base colour
    paths = textures.get(1) or textures.get(12) or next((v for v in textures.values() if v), None)
    texture = paths[0] if isinstance(paths, (list, tuple)) and paths else None
    found = _find_texture(texture, folder) if texture else None
    return materials.add(material.get("NAME"), _mix(tuple(color[:3]), _image_color(found) if found else None),
                         float(opacity if opacity is not None else 1.0),
                         Path(str(texture).replace("\\", "/")).name if texture else None)


def _find_texture(texture: str, folder: Path) -> Path | None:
    if texture.startswith("*"):
        return None  # embedded in the file; assimp-py does not hand those out
    relative = Path(texture.replace("\\", "/"))
    for candidate in (folder / relative, folder / relative.name):
        if candidate.is_file():
            return candidate
    for count, candidate in enumerate(folder.rglob(relative.name)):
        if candidate.is_file():
            return candidate
        if count > 200:
            break
    return None
