"""Ground heights from free terrain tiles.

The tiles are the Terrain Tiles on Amazon (Mapzen / Tilezen "terrarium" PNGs),
free and without a key. They combine the best open elevation data per area:
SRTM (about 30 m) for most of the world, and finer national data (for example
3DEP in the United States, about 3 m to 10 m) where it exists. Heights are
metres above sea level, coded in the colours: R * 256 + G + B / 256 - 32768.
"""

from __future__ import annotations

import io
import logging
import math
import urllib.request
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .. import __version__
from .geo import TILE, Frame, global_pixel, ground_resolution

log = logging.getLogger("archicad_mcp.site")

TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
MAX_ZOOM = 15
MAX_TILES = 64
SOURCE = ("Terrain Tiles (Mapzen/Tilezen) on AWS: SRTM, about 30 m, in most countries; "
          "finer national survey data where available.")
ATTRIBUTION = "Elevation: Terrain Tiles by Mapzen/Tilezen (SRTM, NASA; 3DEP, USGS; other open sources)."

Fetch = Callable[[str], bytes]


def http_get(url: str, timeout: float = 30) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": f"TevavidTools/{__version__}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def decode(png: bytes) -> np.ndarray:
    """Heights (metres) of one terrarium tile, as a 256 x 256 array."""
    from PIL import Image

    rgb = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"), dtype=np.float64)
    return rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0


def encode(heights: np.ndarray) -> bytes:
    """The reverse of decode: a terrarium PNG of a height array (used by tests and checks)."""
    from PIL import Image

    value = np.asarray(heights, dtype=np.float64) + 32768.0
    r = np.floor(value / 256.0)
    g = np.floor(value - r * 256.0)
    b = np.round((value - r * 256.0 - g) * 256.0)
    rgb = np.stack([r, g, np.minimum(b, 255)], axis=-1).astype(np.uint8)
    out = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(out, "PNG")
    return out.getvalue()


class TileCache:
    """Tiles kept on disk, so a site can be rebuilt without downloading again."""

    def __init__(self, folder: Path | None, fetch: Fetch = http_get) -> None:
        self.folder = folder
        self.fetch = fetch

    def tile(self, z: int, x: int, y: int) -> np.ndarray:
        path = self.folder / str(z) / str(x) / f"{y}.png" if self.folder else None
        if path and path.exists():
            try:
                return decode(path.read_bytes())
            except Exception:
                path.unlink(missing_ok=True)
        data = self.fetch(TILE_URL.format(z=z, x=x, y=y))
        heights = decode(data)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return heights


def choose_zoom(latitude: float, half_size: float, max_tiles: int = MAX_TILES) -> int:
    """The finest zoom whose tiles over the site stay within max_tiles."""
    for zoom in range(MAX_ZOOM, 0, -1):
        tiles_across = 2 * half_size / (ground_resolution(latitude, zoom) * TILE) + 2
        if tiles_across ** 2 <= max_tiles:
            return zoom
    return 1


def sample(frame: Frame, xs: np.ndarray, ys: np.ndarray, cache: TileCache, zoom: int,
           progress: Callable[[float], None] = lambda f: None) -> np.ndarray:
    """Heights above sea level at the model points (xs[i], ys[j]); result[j, i]. Bilinear between pixels."""
    gx, gy = np.meshgrid(xs, ys)
    lat, lon = frame.to_geo(gx, gy)
    px, py = global_pixel(lat, lon, zoom)
    px, py = px - 0.5, py - 0.5  # pixel centres
    x0, y0 = int(math.floor(px.min())) - 1, int(math.floor(py.min())) - 1
    x1, y1 = int(math.floor(px.max())) + 2, int(math.floor(py.max())) + 2
    tx0, ty0, tx1, ty1 = x0 // TILE, y0 // TILE, x1 // TILE, y1 // TILE
    tiles = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
    mosaic = np.zeros(((ty1 - ty0 + 1) * TILE, (tx1 - tx0 + 1) * TILE))
    for number, (tx, ty) in enumerate(tiles):
        n = 2 ** zoom
        mosaic[(ty - ty0) * TILE:(ty - ty0 + 1) * TILE, (tx - tx0) * TILE:(tx - tx0 + 1) * TILE] = \
            cache.tile(zoom, tx % n, min(max(ty, 0), n - 1))
        progress((number + 1) / len(tiles))
    mx, my = px - tx0 * TILE, py - ty0 * TILE
    ix, iy = np.floor(mx).astype(int), np.floor(my).astype(int)
    fx, fy = mx - ix, my - iy
    h00, h10 = mosaic[iy, ix], mosaic[iy, ix + 1]
    h01, h11 = mosaic[iy + 1, ix], mosaic[iy + 1, ix + 1]
    return (h00 * (1 - fx) + h10 * fx) * (1 - fy) + (h01 * (1 - fx) + h11 * fx) * fy
