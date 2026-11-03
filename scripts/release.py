"""Make a release: set the version everywhere, note the changes, commit and tag.

    python scripts/release.py 0.2.0 "Sun Analysis: faster tiles" "AI Assistant: new room tool"
    git push origin main --follow-tags

    python scripts/release.py notes v0.2.0   (used by CI: checks the tag and prints its notes)

Pushing the tag (v0.2.0) makes the Build installers workflow build the Windows and
macOS installers and publish them as a release, which installed copies of
Tevavid Tools then offer as an update. The notes become the release's
"What's new" (also kept in CHANGELOG.md).
"""

from __future__ import annotations

import argparse
import datetime
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
INIT = ROOT / "src" / "archicad_mcp" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"
LOCK = ROOT / "uv.lock"


def parse(version: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"{version!r} is not a version like 1.2.3")
    return tuple(int(part) for part in version.split("."))


def current() -> str:
    return re.search(r'__version__ = "([^"]+)"', INIT.read_text(encoding="utf8")).group(1)


def notes(tag: str) -> str:
    """The CHANGELOG.md section of a tag; fails when the tag and the code's version differ."""
    version = tag.removeprefix("refs/tags/").lstrip("v")
    if version != current():
        raise SystemExit(f"Tag {tag} does not match the version in the code ({current()}). Use scripts/release.py to release.")
    text = CHANGELOG.read_text(encoding="utf8") if CHANGELOG.exists() else ""
    match = re.search(rf"(?ms)^## {re.escape(version)}\b[^\n]*\n(.*?)(?=^## |\Z)", text)
    return match.group(1).strip() if match else f"Tevavid Tools {version}"


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "notes":
        print(notes(sys.argv[2]))
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("notes", nargs="+", help="what changed, one item per argument")
    parser.add_argument("--no-commit", action="store_true", help="only edit the files")
    args = parser.parse_args()

    old = current()
    if parse(args.version) <= parse(old):
        raise SystemExit(f"{args.version} must be newer than the current {old}")
    if not args.no_commit and subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True,
                                             cwd=ROOT).stdout.strip():
        raise SystemExit("Commit or stash your changes first, so the release commit holds only the version change.")

    INIT.write_text(INIT.read_text(encoding="utf8").replace(f'__version__ = "{old}"', f'__version__ = "{args.version}"'),
                    encoding="utf8")
    PYPROJECT.write_text(re.sub(r'(?m)^version = "[^"]+"', f'version = "{args.version}"',
                                PYPROJECT.read_text(encoding="utf8"), count=1), encoding="utf8")
    if LOCK.exists():  # uv.lock records the project's own version too
        LOCK.write_text(LOCK.read_text(encoding="utf8").replace(
            f'name = "tevavid-tools"\nversion = "{old}"', f'name = "tevavid-tools"\nversion = "{args.version}"'), encoding="utf8")
    entry = f"## {args.version} ({datetime.date.today().isoformat()})\n\n" + "".join(f"- {n}\n" for n in args.notes) + "\n"
    history = CHANGELOG.read_text(encoding="utf8") if CHANGELOG.exists() else "# Changelog\n\n"
    head, _, rest = history.partition("\n\n")
    CHANGELOG.write_text(f"{head}\n\n{entry}{rest}", encoding="utf8")
    print(f"Version {old} -> {args.version}")
    if args.no_commit:
        return
    subprocess.run(["git", "add", str(INIT), str(PYPROJECT), str(CHANGELOG), str(LOCK)], check=True, cwd=ROOT)
    subprocess.run(["git", "commit", "-m", f"Release {args.version}"], check=True, cwd=ROOT)
    subprocess.run(["git", "tag", "-a", f"v{args.version}", "-m", "\n".join(args.notes)], check=True, cwd=ROOT)
    print(f"Tagged v{args.version}. Publish with: git push origin main --follow-tags")


if __name__ == "__main__":
    sys.exit(main())
