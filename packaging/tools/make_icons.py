"""Draw the app icons and logo images from the Tevavid mark.

    uv run --with pillow python packaging/tools/make_icons.py

Reads the mark's two shapes from src/archicad_mcp/assets/tevavid-mark.svg and
writes the black mark on a white rounded tile as packaging/icons/tevavid-tools
.png/.ico/.icns, the setup splash picture packaging/icons/splash.png, and
src/archicad_mcp/assets/logo-128.png and logo-256.png.
"""

from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
MARK = ROOT / "src" / "archicad_mcp" / "assets" / "tevavid-mark.svg"


def shapes() -> list[list[tuple[float, float]]]:
    """The mark's paths (M, L, Q and Z only) as point lists, curves sampled finely."""
    out = []
    for d in re.findall(r'<path d="([^"]+)"', MARK.read_text(encoding="utf8")):
        tokens = re.findall(r"[MLQZ]|-?\d+(?:\.\d+)?", d)
        points: list[tuple[float, float]] = []
        i = 0
        while i < len(tokens):
            cmd = tokens[i]
            if cmd in "ML":
                points.append((float(tokens[i + 1]), float(tokens[i + 2])))
                i += 3
            elif cmd == "Q":
                (x0, y0) = points[-1]
                cx, cy, x1, y1 = map(float, tokens[i + 1:i + 5])
                for step in range(1, 25):
                    t = step / 24
                    points.append(((1 - t) ** 2 * x0 + 2 * (1 - t) * t * cx + t * t * x1,
                                   (1 - t) ** 2 * y0 + 2 * (1 - t) * t * cy + t * t * y1))
                i += 5
            else:
                i += 1
        out.append(points)
    return out


def tile(size: int) -> Image.Image:
    scale = 4  # draw large, then shrink, for smooth edges
    big = size * scale
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = round(big * 0.02)
    draw.rounded_rectangle((margin, margin, big - margin, big - margin), radius=round(big * 0.22),
                           fill=(255, 255, 255, 255), outline=(226, 228, 232, 255), width=max(1, big // 128))
    polys = shapes()
    xs = [x for p in polys for x, _ in p]
    ys = [y for p in polys for _, y in p]
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    fit = big * 0.70 / max(width, height)
    ox = (big - width * fit) / 2 - min(xs) * fit
    oy = (big - height * fit) / 2 - min(ys) * fit
    for p in polys:
        draw.polygon([(ox + x * fit, oy + y * fit) for x, y in p], fill=(17, 17, 17, 255))
    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    icons = ROOT / "packaging" / "icons"
    master = tile(1024)
    master.save(icons / "tevavid-tools.png")
    master.save(icons / "tevavid-tools.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    master.save(icons / "tevavid-tools.icns")
    assets = ROOT / "src" / "archicad_mcp" / "assets"
    for size in (128, 256):
        tile(size).save(assets / f"logo-{size}.png")
    # Shown by the Windows setup while it unpacks itself (PyInstaller splash screen).
    splash = Image.new("RGB", (420, 240), (255, 255, 255))
    mark = tile(150)
    splash.paste(mark, ((420 - 150) // 2, 26), mark)
    draw = ImageDraw.Draw(splash)
    draw.rectangle((0, 0, 419, 239), outline=(226, 228, 232))
    text = "Starting Tevavid Tools Setup..."
    font = ImageFont.load_default(size=15)
    width = draw.textlength(text, font=font)
    draw.text(((420 - width) / 2, 194), text, fill=(97, 101, 109), font=font)
    splash.save(icons / "splash.png")
    print("icons written")


if __name__ == "__main__":
    main()
