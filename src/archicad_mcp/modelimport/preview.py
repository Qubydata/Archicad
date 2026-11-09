"""A small picture of the model, for the library part's preview and the panel.

A flat-shaded axonometric view from the front left. Triangles are drawn with
a depth buffer in numpy: each one is sampled densely enough to cover its
pixels, and every pixel keeps its nearest sample. Drawn at twice the size and
scaled down, so the edges are smooth. Needs no graphics card.
"""

from __future__ import annotations

import io

import numpy as np

VIEW_FROM = np.array([-0.75, -1.0, 0.8])  # the camera's direction from the model: front left, above
LIGHT_FROM = np.array([-0.3, -0.9, 1.0])
SUPERSAMPLE = 2
CHUNK_SAMPLES = 1_000_000  # samples handled at once, to keep memory small


def render_preview(meshes, materials: dict, size: int = 256, background=(0, 0, 0, 0)) -> bytes:
    """A square PNG of the meshes (objects with ``vertices``, ``faces`` and ``material``)."""
    from PIL import Image

    triangles, colours = [], []
    for mesh in meshes:
        if not len(mesh.faces):
            continue
        triangles.append(np.asarray(mesh.vertices, dtype=np.float64)[mesh.faces])
        material = materials.get(mesh.material)
        colour = np.asarray(material.color if material else (0.75, 0.75, 0.75), dtype=np.float64)
        colours.append(np.repeat(colour[None, :], len(mesh.faces), axis=0))
    big = size * SUPERSAMPLE
    pixels = np.zeros((big * big, 4), dtype=np.uint8)
    pixels[:] = background
    if triangles:
        tris = np.concatenate(triangles)
        forward = -VIEW_FROM / np.linalg.norm(VIEW_FROM)
        right = np.cross(forward, [0.0, 0.0, 1.0])
        right /= np.linalg.norm(right)
        up = np.cross(right, forward)
        x, y, depth = tris @ right, tris @ up, tris @ forward
        span = max(float(np.ptp(x)), float(np.ptp(y)), 1e-9)
        scale = big * 0.9 / span
        screen = np.stack([(x - (x.min() + x.max()) / 2) * scale + big / 2,
                           big / 2 - (y - (y.min() + y.max()) / 2) * scale, depth], axis=2)
        normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
        lengths = np.linalg.norm(normals, axis=1)
        normals = normals / np.where(lengths > 0, lengths, 1.0)[:, None]
        light = LIGHT_FROM / np.linalg.norm(LIGHT_FROM)
        shade = 0.38 + 0.62 * np.abs(normals @ light)
        rgb = np.clip(np.concatenate(colours) * shade[:, None] * 255.0, 0, 255).astype(np.uint8)
        _rasterize(screen, rgb, big, pixels)
    image = Image.fromarray(pixels.reshape(big, big, 4), "RGBA").resize((size, size), Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _rasterize(screen: np.ndarray, rgb: np.ndarray, big: int, pixels: np.ndarray) -> None:
    """Depth-buffered drawing of screen-space triangles (n, 3, [x, y, depth]) into ``pixels``."""
    # Sample each triangle in rows parallel to its longest edge (the base), from the base up to the opposite
    # corner, about 1.5 samples per pixel each way: as many samples as the triangle has pixels, slivers too.
    lengths = np.stack([np.hypot(*(screen[:, (i + 1) % 3, :2] - screen[:, i, :2]).T) for i in range(3)], axis=1)
    longest = np.argmax(lengths, axis=1)
    rows_of = np.arange(len(screen))
    base0, base1 = screen[rows_of, longest], screen[rows_of, (longest + 1) % 3]
    apex = screen[rows_of, (longest + 2) % 3]
    base = lengths[rows_of, longest]
    edge, toward = (base1 - base0)[:, :2], (apex - base0)[:, :2]
    height = np.abs(edge[:, 0] * toward[:, 1] - edge[:, 1] * toward[:, 0]) / np.maximum(base, 1e-12)
    columns = np.clip(np.ceil(base * 1.5).astype(np.int64) + 1, 2, 4 * big)
    rows = np.clip(np.ceil(height * 1.5).astype(np.int64) + 1, 2, 4 * big)
    counts = columns * rows
    depth_buffer = np.full(big * big, np.inf)
    start = 0
    while start < len(screen):
        end = start + max(1, int(np.searchsorted(np.cumsum(counts[start:]), CHUNK_SAMPLES)))
        index = np.repeat(np.arange(start, end), counts[start:end])
        offsets = np.repeat(np.cumsum(counts[start:end]) - counts[start:end], counts[start:end])
        local = np.arange(len(index)) - offsets
        k = columns[index]
        u = ((local % k) / (k - 1))[:, None]
        w = ((local // k) / (rows[index] - 1))[:, None]
        points = (1.0 - w) * (base0[index] + u * (base1[index] - base0[index])) + w * apex[index]
        px, py = np.floor(points[:, 0]).astype(np.int64), np.floor(points[:, 1]).astype(np.int64)
        inside = (px >= 0) & (px < big) & (py >= 0) & (py < big)
        key, depth, owner = py[inside] * big + px[inside], points[inside, 2], index[inside]
        order = np.lexsort((depth, key))
        key, depth, owner = key[order], depth[order], owner[order]
        first = np.concatenate([[True], key[1:] != key[:-1]])  # the nearest sample of each pixel
        key, depth, owner = key[first], depth[first], owner[first]
        nearer = depth < depth_buffer[key]
        depth_buffer[key[nearer]] = depth[nearer]
        pixels[key[nearer], :3] = rgb[owner[nearer]]
        pixels[key[nearer], 3] = 255
        start = end
