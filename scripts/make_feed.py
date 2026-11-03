"""Make the folder to upload to tevavid.com/downloads for a release.

    python scripts/make_feed.py v0.2.0 release/ site/

Copies the installers found in release/ to site/0.2.0/ and writes
site/latest.json, the file installed copies check for updates. Upload the
contents of site/ to https://tevavid.com/downloads/ (the version folder first,
latest.json last, so no copy sees the new version before its files are there).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from archicad_mcp.updater import ASSETS  # noqa: E402
from release import notes  # noqa: E402

PAGE = "https://tevavid.com"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def make(tag: str, release: Path, site: Path) -> dict:
    text = notes(tag)  # also checks the tag matches the code's version
    version = tag.removeprefix("refs/tags/").lstrip("v")
    folder = site / version
    folder.mkdir(parents=True, exist_ok=True)
    assets = {}
    for platform, name in ASSETS.items():
        source = release / name
        if not source.is_file():
            continue
        shutil.copy2(source, folder / name)
        assets[platform] = {"url": f"{version}/{name}", "sha256": sha256(source), "size": source.stat().st_size}
    if not assets:
        raise SystemExit(f"No installers in {release}")
    feed = {"version": version, "notes": text, "url": PAGE,
            "published": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "assets": assets}
    (site / "latest.json").write_text(json.dumps(feed, indent=2), encoding="utf8")
    return feed


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    feed = make(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
    print(f"{feed['version']}: {', '.join(feed['assets'])}")
