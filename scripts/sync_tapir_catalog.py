"""Download the Tapir command reference and store it as the bundled catalog.

Tapir publishes its command reference as two JavaScript files that each assign
one JSON literal to a variable. This script fetches them, parses the JSON
(nothing is executed) and writes src/archicad_mcp/data/tapir_catalog.json.

Usage:
    python scripts/sync_tapir_catalog.py [--ref main]
"""

import argparse
import json
import pathlib
import urllib.request

REPO = "ENZYME-APD/tapir-archicad-automation"
DOCS_DIR = "docs/archicad-addon"
OUTPUT = pathlib.Path(__file__).resolve().parent.parent / "src" / "archicad_mcp" / "data" / "tapir_catalog.json"


def fetch(ref: str, path: str) -> str:
    url = f"https://raw.githubusercontent.com/{REPO}/{ref}/{path}"
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read().decode("utf8")


def parse_assignment(text: str, variable: str):
    """Return the JSON literal assigned to `var <variable> = ...;`."""
    start = text.index(variable)
    start = text.index("=", start) + 1
    end = text.find("var ", start)
    literal = text[start:end if end != -1 else len(text)].strip().rstrip(";").strip()
    return json.loads(literal)


def resolve_commit(ref: str) -> str:
    url = f"https://api.github.com/repos/{REPO}/commits/{ref}"
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.loads(response.read())["sha"]
    except Exception:
        return ref


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref", default="main", help="Tapir branch, tag or commit")
    args = parser.parse_args()

    groups = parse_assignment(fetch(args.ref, f"{DOCS_DIR}/command_definitions.js"), "gCommands")
    definitions = parse_assignment(fetch(args.ref, f"{DOCS_DIR}/common_schema_definitions.js"), "gSchemaDefinitions")
    version = json.loads(fetch(args.ref, "tools/package_info.json"))["version"]

    commands = []
    for group in groups:
        for command in group["commands"]:
            commands.append({
                "name": command["name"],
                "group": group["name"],
                "version": command.get("version"),
                "description": command.get("description", ""),
                "inputScheme": command.get("inputScheme"),
                "outputScheme": command.get("outputScheme"),
            })

    catalog = {
        "source": f"https://github.com/{REPO}",
        "ref": args.ref,
        "commit": resolve_commit(args.ref),
        "tapirVersion": version,
        "commands": commands,
        "definitions": definitions,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(catalog, separators=(",", ":")), encoding="utf8")
    print(f"Wrote {len(commands)} commands and {len(definitions)} definitions (Tapir {version}) to {OUTPUT}")


if __name__ == "__main__":
    main()
