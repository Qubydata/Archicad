"""The GDL library part, as the XML that Archicad's LP_XMLConverter turns into a .gsm file.

The format is the one ``LP_XMLConverter libpart2xml`` writes (a ``<Symbol>``
with its ancestry, scripts, parameters and preview picture); see
https://gdl.graphisoft.com/tips-and-tricks/how-to-use-the-lp_xmlconverter-tool/
and the example parts Graphisoft publishes with its GDL tools.

What the part does:

* It is a Model Element (an ordinary object) with the standard size
  parameters A, B and ZZYZX. The model is stored at its imported size and
  scaled to them, so it can be stretched; ``keepProportions`` (on by default)
  scales the other two sizes along when one is changed in the settings.
* The 3D script has one body per part (VERT, EDGE, PGON, BODY). Edges
  between coplanar triangles are hidden, edges between gently bent triangles
  are marked as edges of a curved surface (smooth, hidden in views), the
  others stay visible.
* Each imported material is defined in the script with its colour. Every one
  also has a Material parameter: picking a surface there replaces the
  model's colour (``useModelColours`` switches back).
* The 2D script draws the outline of the model seen from above and of each
  part, plus hotspots; ``showProjection`` switches to a projection of the 3D
  model (slower).
"""

from __future__ import annotations

import re
import uuid
from xml.sax.saxutils import quoteattr

import numpy as np

FORMAT_VERSION = "38"  # the library part format libpart2xml writes for Archicad 22; newer converters read it
PARAM_SECTION_VERSION = "26"
OWNER, SIGNATURE = "1196638531", "1196644685"  # "GSAC", "GSOM": what Graphisoft's own parts carry
GENERAL_GDL_OBJECT = "F938E33A-329D-4A36-BE3E-85E126820996"
MODEL_ELEMENT = "103E8D2C-8230-42E1-9597-46F84CCE28C0"
GUID_NAMESPACE = uuid.UUID("6f1f3a52-4b55-4d0c-9a07-0d8e4b7c2a11")

COPLANAR_DEGREES = 1.0  # edges between faces closer than this are hidden
SMOOTH_DEGREES = 35.0  # edges between faces closer than this are smooth (curved surface)
MAX_PART_OUTLINES = 300
MIN_AREA = 1e-12
DECIMALS = 5  # 0.01 mm in metres


def library_part_guid(key: str) -> str:
    """A stable GUID for a library part, so saving the same object again replaces it instead of duplicating it."""
    return str(uuid.uuid5(GUID_NAMESPACE, key.lower())).upper()


def gdl_text(text: str, limit: int = 120) -> str:
    """Text safe inside a GDL string and an XML CDATA section."""
    text = "".join(ch for ch in str(text) if ch.isprintable()).replace('"', "'").replace("]]>", "]] >")
    return text[:limit].strip()


def _num(value: float) -> str:
    text = f"{float(value):.{DECIMALS}f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "", "-") else text


def _cdata(text: str) -> str:
    return "<![CDATA[" + text.replace("]]>", "]] >") + "]]>"


# --- 3D ------------------------------------------------------------------------

def _clean_faces(vertices: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Faces with an area, the vertices they use, and their unit normals."""
    faces = np.asarray(faces, dtype=np.int64).reshape(-1, 3)
    if not len(faces):
        return vertices[:0], faces, np.zeros((0, 3))
    tri = vertices[faces]
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area = np.linalg.norm(normals, axis=1)
    keep = area > MIN_AREA
    faces, normals, area = faces[keep], normals[keep], area[keep]
    used, inverse = np.unique(faces, return_inverse=True)
    return vertices[used], inverse.reshape(-1, 3), normals / area[:, None]


def body_lines(vertices: np.ndarray, faces: np.ndarray) -> tuple[list[str], bool]:
    """VERT, EDGE, PGON and BODY lines for one mesh, and whether it is closed (watertight)."""
    vertices, faces, normals = _clean_faces(np.asarray(vertices, dtype=np.float64), faces)
    if not len(faces):
        return [], False
    corners = np.stack([faces, np.roll(faces, -1, axis=1)], axis=2).reshape(-1, 2)  # (a,b), (b,c), (c,a) per face
    keys = np.sort(corners, axis=1)
    edges, inverse, counts = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
    inverse = inverse.reshape(-1)
    signs = np.where(corners[:, 0] == keys[:, 0], 1, -1)
    face_of = np.repeat(np.arange(len(faces)), 3)

    status = np.zeros(len(edges), dtype=np.int64)
    shared = counts[inverse] == 2
    order = np.argsort(inverse[shared], kind="stable")
    pairs = face_of[shared][order].reshape(-1, 2)  # the two faces of each edge shared by exactly two
    pair_edges = inverse[shared][order][::2]
    cosine = np.einsum("ij,ij->i", normals[pairs[:, 0]], normals[pairs[:, 1]])
    status[pair_edges[cosine >= np.cos(np.radians(COPLANAR_DEGREES))]] = 1  # invisible
    bent = (cosine < np.cos(np.radians(COPLANAR_DEGREES))) & (cosine >= np.cos(np.radians(SMOOTH_DEGREES)))
    status[pair_edges[bent]] = 2  # edge of a curved surface
    curved = np.zeros(len(faces), dtype=bool)
    curved[pairs[bent].reshape(-1)] = True

    lines = ["BASE"]
    lines += [f"VERT {_num(x)}, {_num(y)}, {_num(z)}" for x, y, z in vertices]
    lines += [f"EDGE {a + 1}, {b + 1}, -1, -1, {s}" for (a, b), s in zip(edges, status)]
    signed = ((inverse + 1) * signs).reshape(-1, 3)
    lines += [f"PGON 3, 0, {2 if c else 0}, {e1}, {e2}, {e3}" for (e1, e2, e3), c in zip(signed, curved)]
    closed = bool((counts == 2).all())
    lines.append("BODY -1" if closed else "BODY 4")  # 4: a surface model, nothing is filled where it is cut
    return lines, closed


def script_3d(meshes, material_names: dict[str, str], material_params: dict[str, str], title: str) -> str:
    lines = [
        f"! {title}: imported with Tevavid Tools Model Import.",
        "! The model is stored at its imported size and scaled to A, B and ZZYZX.",
        "_sx = 1 : _sy = 1 : _sz = 1",
        "IF _a0 > 0.00001 THEN _sx = A / _a0",
        "IF _b0 > 0.00001 THEN _sy = B / _b0",
        "IF _h0 > 0.00001 THEN _sz = ZZYZX / _h0",
        "",
    ]
    for hx in ("-A / 2", "A / 2"):
        for hy in ("-B / 2", "B / 2"):
            for hz in ("0", "ZZYZX"):
                lines.append(f"HOTSPOT {hx}, {hy}, {hz}")
    lines.append("HOTSPOT 0, 0, 0")
    lines += ["", "MUL _sx, _sy, _sz", ""]
    for mesh in meshes:
        body, _ = body_lines(mesh.vertices, mesh.faces)
        if not body:
            continue
        lines += [
            f"! Part: {gdl_text(mesh.name)}",
            "IF useModelColours THEN",
            f'\tMATERIAL "{material_names[mesh.material]}"',
            "ELSE",
            f"\tMATERIAL {material_params[mesh.material]}",
            "ENDIF",
        ]
        lines += body
        lines.append("")
    lines.append("DEL 1")
    return "\n".join(lines) + "\n"



def material_definitions(materials, material_names: dict[str, str]) -> list[str]:
    """DEFINE MATERIAL lines (general type 0: surface colour, then the reflection and transparency values)."""
    lines = []
    for material in materials:
        r, g, b = (_num(c) for c in material.color)
        transparency = _num(max(0.0, min(1.0, 1.0 - material.opacity)))
        lines.append(f'DEFINE MATERIAL "{material_names[material.id]}" 0, {r}, {g}, {b}, '
                     f"0.6, 0.8, 0.2, {transparency}, 20, 2, 1, 1, 1, 0, 0, 0, 0")
    return lines


# --- 2D ------------------------------------------------------------------------

def convex_hull(points: np.ndarray) -> np.ndarray:
    """The 2D convex hull of points, counterclockwise (Andrew's monotone chain)."""
    points = np.unique(np.round(np.asarray(points, dtype=np.float64)[:, :2], 6), axis=0)
    if len(points) < 3:
        return points

    def half(sequence):
        chain: list = []
        for p in sequence:
            while len(chain) >= 2:
                (ox, oy), (ax, ay) = chain[-2], chain[-1]
                if (ax - ox) * (p[1] - oy) - (ay - oy) * (p[0] - ox) > 1e-12:
                    break
                chain.pop()
            chain.append((p[0], p[1]))
        return chain

    rows = [tuple(p) for p in points]
    lower, upper = half(rows), half(reversed(rows))
    return np.asarray(lower[:-1] + upper[:-1])


def _poly2(hull: np.ndarray) -> str:
    coordinates = ", ".join(f"{_num(x)}, {_num(y)}" for x, y in hull)
    return f"POLY2 {len(hull)}, 5, {coordinates}"  # 1 contour + 4 closed


def _area(hull: np.ndarray) -> float:
    if len(hull) < 3:
        return 0.0
    x, y = hull[:, 0], hull[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2)


def script_2d(meshes, title: str) -> str:
    everything = np.concatenate([m.vertices for m in meshes]) if meshes else np.zeros((0, 3))
    outline = convex_hull(everything) if len(everything) else np.zeros((0, 2))
    total = _area(outline) or 1.0
    parts = []
    for mesh in meshes:
        hull = convex_hull(mesh.vertices)
        area = _area(hull)
        if area > total * 1e-4 and area < total * 0.98:
            parts.append((area, hull))
    parts = [hull for _, hull in sorted(parts, key=lambda p: -p[0])[:MAX_PART_OUTLINES]]
    lines = [
        f"! {title}: plan symbol.",
        "PEN gs_cont_pen",
        "IF showProjection THEN",
        "\tPROJECT2 3, 270, 2",
        "ELSE",
        "\t_sx = 1 : _sy = 1",
        "\tIF _a0 > 0.00001 THEN _sx = A / _a0",
        "\tIF _b0 > 0.00001 THEN _sy = B / _b0",
        "\tMUL2 _sx, _sy",
    ]
    if len(outline) >= 3:
        lines.append("\t" + _poly2(outline))
    for hull in parts:
        lines.append("\t" + _poly2(hull))
    lines += ["\tDEL 1", "ENDIF", ""]
    for x in ("-A / 2", "0", "A / 2"):
        for y in ("-B / 2", "0", "B / 2"):
            lines.append(f"HOTSPOT2 {x}, {y}")
    return "\n".join(lines) + "\n"


# --- master and parameter scripts ----------------------------------------------

def script_master(size) -> str:
    a0, b0, h0 = size
    return ("! The size the model was imported at, in metres.\n"
            f"_a0 = {_num(a0)}\n_b0 = {_num(b0)}\n_h0 = {_num(h0)}\n")


def script_parameters(size) -> str:
    a0, b0, h0 = size
    lines = [
        "! Picking a surface for one of the materials switches from the model's own colours to the surfaces.",
        'IF STRSTR(GLOB_MODPAR_NAME, "mat_") = 1 THEN PARAMETERS useModelColours = 0',
        "",
        "! Keep the proportions: changing one size scales the other two along.",
        "IF keepProportions THEN",
    ]
    sizes = {"A": a0, "B": b0, "ZZYZX": h0}
    origin = {"A": "_a0", "B": "_b0", "ZZYZX": "_h0"}
    for changed, value in sizes.items():
        if value <= 1e-5:
            continue
        others = [f"{name} = {changed} * {origin[name]} / {origin[changed]}" for name in sizes if name != changed]
        lines.append(f'\tIF GLOB_MODPAR_NAME = "{changed}" THEN PARAMETERS {", ".join(others)}')
    lines.append("ENDIF")
    return "\n".join(lines) + "\n"


# --- the XML -------------------------------------------------------------------

def _param(kind: str, name: str, description: str, value: str, *, fixed: bool = False, hidden: bool = False,
           child: bool = False) -> str:
    flags = [flag for flag, on in (("ParFlg_Child", child), ("ParFlg_Hidden", hidden)) if on]
    quoted = '"' + gdl_text(description) + '"'
    text = [f'\t\t<{kind} Name="{name}">', f"\t\t\t<Description>{_cdata(quoted)}</Description>"]
    if fixed:
        text.append("\t\t\t<Fix/>")
    if flags:
        text += ["\t\t\t<Flags>"] + [f"\t\t\t\t<{flag}/>" for flag in flags] + ["\t\t\t</Flags>"]
    text += [f"\t\t\t<Value>{value}</Value>", f"\t\t</{kind}>"]
    return "\n".join(text)


def _script(tag: str, body: str) -> str:
    return f'<{tag} SectVersion="20" SectionFlags="0" SubIdent="0">\n{_cdata(body)}\n</{tag}>\n'


def library_part_xml(name: str, meshes, materials: dict, *, guid: str | None = None, preview: str | None = None,
                     source: str = "", comment: str = "", keywords=(), default_surface: int = 1) -> str:
    """The library part as LP_XMLConverter XML.

    ``meshes`` are :class:`~.cleanup.ExportMesh` objects (metres, Z up, origin
    at the bottom centre), ``materials`` maps their material ids to
    :class:`~.reader.Material`. ``preview`` is the preview PNG's path relative
    to the image folder given to the converter with ``-img``.
    """
    meshes = [m for m in meshes if len(m.faces)]
    if not meshes:
        raise ValueError("A library part needs at least one mesh with faces.")
    title = gdl_text(name, 80) or "Imported model"
    guid = guid or library_part_guid(title)
    everything = np.concatenate([m.vertices for m in meshes])
    low, high = everything.min(axis=0), everything.max(axis=0)
    size = tuple(float(v) for v in high - low)

    used: list[str] = []
    for mesh in meshes:
        if mesh.material not in used:
            used.append(mesh.material)
    tag = guid.split("-")[0].lower()
    material_names = {mid: f"tvd_{tag}_{i + 1}" for i, mid in enumerate(used)}
    material_params = {mid: f"mat_{i + 1}" for i, mid in enumerate(used)}

    master = script_master(size)
    three_d = "\n".join(material_definitions([materials[m] for m in used], material_names)) + "\n\n"
    three_d += script_3d(meshes, material_names, material_params, title)

    parameters = [
        _param("Length", "A", "Width", _num(size[0])),
        _param("Length", "B", "Depth", _num(size[1])),
        _param("Length", "ZZYZX", "Height", _num(size[2])),
        _param("Boolean", "AC_show2DHotspotsIn3D", "Show 2D Hotspots in 3D", "0", fixed=True, hidden=True),
        _param("Length", "ac_bottomlevel", "Bottom Level", "1", fixed=True, hidden=True),
        _param("Length", "ac_toplevel", "Top Level", "0", fixed=True, hidden=True),
        _param("Boolean", "keepProportions", "Keep Proportions", "1"),
        '\t\t<Title Name="surfaces_title">\n\t\t\t<Description><![CDATA["Surfaces"]]></Description>\n\t\t</Title>',
        _param("Boolean", "useModelColours", "Use the Model's Own Colours", "1"),
    ]
    for mid in used:
        parameters.append(_param("Material", material_params[mid], materials[mid].name, str(int(default_surface)),
                                 child=True))
    parameters += [
        '\t\t<Title Name="plan_title">\n\t\t\t<Description><![CDATA["Floor Plan"]]></Description>\n\t\t</Title>',
        _param("PenColor", "gs_cont_pen", "Contour Pen", "1", child=True),
        _param("Boolean", "showProjection", "Plan Symbol from the 3D Model (slower)", "0", child=True),
    ]

    triangles = sum(len(m.faces) for m in meshes)
    about = comment or (f"{title}, imported with Tevavid Tools Model Import"
                        + (f" from {gdl_text(source, 200)}" if source else "")
                        + f". {triangles} polygons, {len(meshes)} parts.")
    words = "; ".join(gdl_text(k, 60) for k in ("Tevavid", "imported", *keywords) if gdl_text(k, 60))
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<Symbol IsArchivable="false" IsPlaceable="true" MainGUID="{guid}" MigrationValue="Normal" '
        f'Owner="{OWNER}" Signature="{SIGNATURE}" Version="{FORMAT_VERSION}">',
        '<Ancestry SectVersion="1" SectionFlags="0" SubIdent="0" Template="false">',
        f"\t<MainGUID>{GENERAL_GDL_OBJECT}</MainGUID>",
        f"\t<MainGUID>{MODEL_ELEMENT}</MainGUID>",
        "</Ancestry>",
        "",
        '<CalledMacros SectVersion="2" SectionFlags="0" SubIdent="0">',
        "</CalledMacros>",
        "",
        _script("Script_1D", master),
        _script("Script_2D", script_2d(meshes, title)),
        _script("Script_3D", three_d),
        _script("Script_PR", ""),
        _script("Script_UI", ""),
        _script("Script_VL", script_parameters(size)),
        f'<ParamSection SectVersion="{PARAM_SECTION_VERSION}" SectionFlags="0" SubIdent="0">',
        "\t<ParamSectHeader>",
        f"\t\t<Version>{PARAM_SECTION_VERSION}</Version>",
        "\t\t<AutoHotspots>false</AutoHotspots>",
        "\t\t<StatBits>",
        "\t\t\t<STBit_FixSize/>",
        "\t\t</StatBits>",
        "\t\t<WDLeftFrame>0</WDLeftFrame>",
        "\t\t<WDRightFrame>0</WDRightFrame>",
        "\t\t<WDTopFrame>0</WDTopFrame>",
        "\t\t<WDBotFrame>0</WDBotFrame>",
        "\t\t<LayFlags>65535</LayFlags>",
        "\t\t<WDMirrorThickness>0</WDMirrorThickness>",
        "\t\t<WDWallInset>0</WDWallInset>",
        "\t\t<SymbolColor>0</SymbolColor>",
        "\t</ParamSectHeader>",
        "\t<Parameters>",
        *parameters,
        "\t</Parameters>",
        "</ParamSection>",
        "",
    ]
    if preview:
        out += ['<Picture MIME="image/png" SectVersion="19" SectionFlags="0" SubIdent="0" '
                f"path={quoteattr(preview)}/>", ""]
    out += [
        '<Keywords SectVersion="1" SectionFlags="0" SubIdent="0">',
        _cdata(words),
        "</Keywords>",
        "",
        '<Comment SectVersion="20" SectionFlags="0" SubIdent="0">',
        _cdata(gdl_text(about, 2000)),
        "</Comment>",
        "",
        "</Symbol>",
        "",
    ]
    return "\n".join(out)


def safe_file_name(name: str) -> str:
    """A library part file name (without extension) that works on Windows and macOS."""
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(name))
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:80] or "Imported model"
