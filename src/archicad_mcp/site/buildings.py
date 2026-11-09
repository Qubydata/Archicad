"""Neighbouring buildings from OpenStreetMap, as simple blocks.

Footprints come from the Overpass API. Heights come from the building's
"height" tag, else its number of floors ("building:levels") times 3 m, else
6 m, which is marked as estimated. OpenStreetMap data is (c) OpenStreetMap
contributors, under the ODbL: the attribution must stay visible with it.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from .. import __version__
from .geo import Frame

log = logging.getLogger("archicad_mcp.site")

OVERPASS = ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter")
STOREY = 3.0
DEFAULT_HEIGHT = 6.0
MAX_BUILDINGS = 600
ATTRIBUTION = "Buildings: (c) OpenStreetMap contributors (ODbL)."

Post = Callable[[str, str], dict]


@dataclass
class Building:
    outline: list[tuple[float, float]]  # model x, y
    height: float
    estimated: bool
    name: str = ""


def http_post(url: str, query: str, timeout: float = 60) -> dict:
    data = urllib.parse.urlencode({"data": query}).encode()
    request = urllib.request.Request(url, data=data, headers={"User-Agent": f"TevavidTools/{__version__}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def query(south: float, west: float, north: float, east: float) -> str:
    box = f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"
    return (f'[out:json][timeout:50];(way["building"]({box});'
            f'relation["building"]["type"="multipolygon"]({box}););out geom tags;')


def parse_length(text: str | None) -> float | None:
    """'12', '12 m', '12.5m', "40'" or '40 ft' as metres."""
    if not text:
        return None
    match = re.match(r"\s*([0-9]+(?:[.,][0-9]+)?)\s*(m|ft|')?", str(text))
    if not match:
        return None
    value = float(match.group(1).replace(",", "."))
    return value * 0.3048 if match.group(2) in ("ft", "'") else value


def height_of(tags: dict) -> tuple[float, bool]:
    height = parse_length(tags.get("height"))
    if height and height > 0:
        return height, False
    levels = parse_length(tags.get("building:levels"))
    if levels and levels > 0:
        roof = parse_length(tags.get("roof:levels")) or 0
        return (levels + roof) * STOREY, False
    return DEFAULT_HEIGHT, True


def parse(data: dict, frame: Frame) -> list[Building]:
    """Buildings from an Overpass 'out geom' answer, in model coordinates (outer rings only)."""
    found: list[Building] = []
    for element in data.get("elements", []):
        tags = element.get("tags", {})
        rings = []
        if element.get("type") == "way" and element.get("geometry"):
            rings.append(element["geometry"])
        elif element.get("type") == "relation":
            rings += [m["geometry"] for m in element.get("members", []) if m.get("role") == "outer" and m.get("geometry")]
        height, estimated = height_of(tags)
        for ring in rings:
            lats = [p["lat"] for p in ring]
            lons = [p["lon"] for p in ring]
            xs, ys = frame.to_model(lats, lons)
            found.append(Building(list(zip(map(float, xs), map(float, ys))), height, estimated, tags.get("name", "")))
    return found


def fetch(frame: Frame, half_size: float, post: Post = http_post) -> list[Building]:
    """Buildings within a square of half_size metres around the origin, nearest first."""
    corners_x = [-half_size, half_size, half_size, -half_size]
    corners_y = [-half_size, -half_size, half_size, half_size]
    lats, lons = frame.to_geo(corners_x, corners_y)
    text = query(float(min(lats)), float(min(lons)), float(max(lats)), float(max(lons)))
    last_error: Exception | None = None
    for url in OVERPASS:
        try:
            data = post(url, text)
            break
        except Exception as exc:  # try the next mirror
            log.info("Overpass %s failed: %s", url, exc)
            last_error = exc
    else:
        raise ConnectionError(f"OpenStreetMap could not be reached ({last_error}).")
    buildings = [b for b in parse(data, frame)
                 if any(abs(x) <= half_size and abs(y) <= half_size for x, y in b.outline)]
    buildings.sort(key=lambda b: min(x * x + y * y for x, y in b.outline))
    return buildings[:MAX_BUILDINGS]
