"""Model Import from the command line, for trying the engine on a real Archicad (see SPIKE.md).

    python -m archicad_mcp.modelimport --demo --xml-only out
    python -m archicad_mcp.modelimport chair.glb --name "Spike Chair" --folder Spike
    python -m archicad_mcp.modelimport chair.glb --archicad --place

Without ``--archicad`` nothing talks to Archicad: the object is converted with
LP_XMLConverter (found in the installed Archicad, or given with
``--converter``) into the library folder. With it, the running Archicad also
loads the library and, with ``--place``, gets the object placed at the origin.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from mcp.server.mcpserver.exceptions import ToolError

from .reader import Material, Model, Part


def demo_model() -> Model:
    """A small table (Z up, metres): a wooden top, four metal legs, two screws and a smooth wooden vase."""
    import trimesh

    materials = {"m0": Material("m0", "Oak", (0.59, 0.39, 0.2)), "m1": Material("m1", "Steel", (0.78, 0.78, 0.82))}
    meshes = [("Top", trimesh.creation.box((1.2, 0.8, 0.04)), (0, 0, 0.73), "m0")]
    for i, (x, y) in enumerate([(-0.55, -0.35), (0.55, -0.35), (-0.55, 0.35), (0.55, 0.35)]):
        meshes.append((f"Leg {i + 1}", trimesh.creation.cylinder(0.025, 0.71, sections=24), (x, y, 0.355), "m1"))
    for i, x in enumerate((-0.3, 0.3)):
        meshes.append((f"Screw {i + 1}", trimesh.creation.box((0.006, 0.006, 0.006)), (x, 0.395, 0.705), "m1"))
    vase = trimesh.creation.icosphere(subdivisions=4, radius=0.08)
    vase.vertices[:, 2] *= 1.5  # an egg-shaped vase, smooth enough to show the polygon reduction
    meshes.append(("Vase", vase, (0.25, 0.1, 0.75 + 0.12), "m0"))
    parts = []
    for i, (name, mesh, offset, material) in enumerate(meshes):
        parts.append(Part(f"p{i}", name, material, np.asarray(mesh.vertices) + offset, np.asarray(mesh.faces)))
    return Model(Path("Demo Table.demo"), "demo", parts, materials, up_axis="Z", unit="m", reader="built-in")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m archicad_mcp.modelimport", description=__doc__.split("\n")[0])
    parser.add_argument("model", nargs="?", help="a model file (glTF/GLB, OBJ, FBX, ...) or a ZIP with one")
    parser.add_argument("--demo", action="store_true", help="use a built-in test table instead of a file")
    parser.add_argument("--name", help="library part name (default: the file's name)")
    parser.add_argument("--folder", default="", help="library sub-folder, like 'Furniture/Tables'")
    parser.add_argument("--library", help="library folder (default: Documents/Tevavid Library)")
    parser.add_argument("--target", choices=("shared", "embedded"), default="shared")
    parser.add_argument("--unit", help="mm, cm, m, inch or ft (default: from the file, or guessed from the size)")
    parser.add_argument("--up", help="the file's up axis: Z, Y, X, -Z, -Y or -X")
    parser.add_argument("--remove-small", type=float, metavar="FRACTION",
                        help="delete parts smaller than this fraction of the model, like 0.01")
    parser.add_argument("--max-polygons", type=int, help="use the most detailed level at or below this")
    parser.add_argument("--xml-only", metavar="FOLDER", help="only write the XML and preview into FOLDER")
    parser.add_argument("--converter", help="path of LP_XMLConverter (default: found in the installed Archicad)")
    parser.add_argument("--archicad", action="store_true", help="also use the running Archicad (Tapir add-on)")
    parser.add_argument("--place", action="store_true", help="place the object at the origin (needs --archicad)")
    parser.add_argument("--morphs", action="store_true",
                        help="act as if LP_XMLConverter were missing: place Morph elements (needs --archicad)")
    args = parser.parse_args(argv)
    if not args.model and not args.demo:
        parser.error("give a model file or --demo")

    from .cleanup import ImportSession
    from .service import ModelImport

    connection = None
    if args.archicad:
        from ..connection import ArchicadConnection

        connection = ArchicadConnection()
    tool = ModelImport(connection, library_root=args.library, converter=args.converter)
    if args.morphs:
        tool.find_converter = lambda: None
    try:
        if args.demo:
            tool.session = ImportSession(demo_model())
            tool.session.levels.compute()
        else:
            tool.open(args.model, unit=args.unit, up_axis=args.up)
        session = tool.session
        if args.remove_small:
            print(f"Removed {len(session.remove_small_parts(args.remove_small))} small parts")
        if args.max_polygons:
            levels = session.level_list()
            session.set_level(next((lv["index"] for lv in levels if lv["triangles"] <= args.max_polygons),
                                   len(levels) - 1))
        summary = tool.state()
        kept = sum(not p["deleted"] for p in summary["parts"])
        print(f"{summary['name']}: {kept} parts, {summary['triangles']:,} polygons "
              f"({summary['budget']['colour']}), {summary['size']['x']:.3f} x {summary['size']['y']:.3f} x "
              f"{summary['size']['z']:.3f} m, unit {summary['unit']} ({summary['unitSource']}), up {summary['upAxis']}")
        if args.xml_only:
            result = tool.export_xml(args.name, args.folder, args.target, out_dir=args.xml_only)
        else:
            result = tool.save(args.name, args.folder, args.target, place=args.place)
    except ToolError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
