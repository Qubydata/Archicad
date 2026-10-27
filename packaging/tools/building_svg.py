"""Generate the isometric line-art building used on the setup welcome screen."""
import math, sys

S = 15.0
OX, OY = 44, 420
def P(x, y, z):
    sx = OX + (x - y) * math.cos(math.radians(30)) * S
    sy = OY - (x + y) * math.sin(math.radians(30)) * S - z * S
    return f"{sx:.1f},{sy:.1f}"

out = []
def poly(points, fill="none", stroke="#8fa3b9", width=0.9, extra=""):
    out.append(f'<polygon points="{" ".join(P(*p) for p in points)}" fill="{fill}" stroke="{stroke}" stroke-width="{width}" stroke-linejoin="round" {extra}/>')
def line(a, b, stroke="#8fa3b9", width=0.8, extra=""):
    out.append(f'<polyline points="{P(*a)} {P(*b)}" fill="none" stroke="{stroke}" stroke-width="{width}" stroke-linecap="round" {extra}/>')

W, D, H = 11.0, 8.0, 9.6
# Ground plane and paving lines.
poly([(-8, -9, 0), (20, -9, 0), (20, 16, 0), (-8, 16, 0)], fill="none", stroke="none")
for i in range(-3, 20, 3):
    line((i, -10, 0), (i, -1.2, 0), stroke="#c9d6e3", width=0.6)
for j in (-7.5, -4.5, -1.2):
    line((-4, j, 0), (19, j, 0), stroke="#c9d6e3", width=0.6)
line((-1.2, -1.2, 0), (-1.2, 12, 0), stroke="#c9d6e3", width=0.6)
line((-4, -1.2, 0), (-4, 12, 0), stroke="#c9d6e3", width=0.6)
# Low garden wall and bench.
poly([(2, -5, 0), (8, -5, 0), (8, -5, 0.6), (2, -5, 0.6)], fill="#f4f8fb")
poly([(2, -5, 0.6), (8, -5, 0.6), (8, -4.4, 0.6), (2, -4.4, 0.6)], fill="#ffffff")

# Main volume faces: side (x = W), front (y = 0), roof.
poly([(0, 0, 0), (0, D, 0), (0, D, H), (0, 0, H)], fill="#e7eef6")
poly([(0, 0, 0), (W, 0, 0), (W, 0, H), (0, 0, H)], fill="#f7fafc")
poly([(0, 0, H), (W, 0, H), (W, D, H), (0, D, H)], fill="#ffffff")

# Upper floors: deep white frame around glazing.
fx0, fx1, fz0, fz1 = 0.7, W - 0.7, 3.4, H - 0.7
poly([(fx0, 0, fz0), (fx1, 0, fz0), (fx1, 0, fz1), (fx0, 0, fz1)], fill="#dfe9f3", stroke="#7f95ad", width=1.0)
inner = 0.35
poly([(fx0 + inner, -0.01, fz0 + inner), (fx1 - inner, -0.01, fz0 + inner), (fx1 - inner, -0.01, fz1 - inner), (fx0 + inner, -0.01, fz1 - inner)], fill="#d6e3f0", stroke="#8fa3b9", width=0.7)
mid = (fz0 + fz1) / 2
line((fx0 + inner, 0, mid), (fx1 - inner, 0, mid), stroke="#7f95ad", width=1.1)
line((fx0 + inner, 0, mid - 0.25), (fx1 - inner, 0, mid - 0.25), stroke="#9db0c4", width=0.6)
x = fx0 + inner
step = (fx1 - fx0 - 2 * inner) / 6
for k in range(1, 6):
    line((x + k * step, 0, fz0 + inner), (x + k * step, 0, fz1 - inner), stroke="#93a7bc", width=0.7)
# Balcony rail on the middle slab.


# Ground floor: recessed glazing with a door.
poly([(0.6, 0, 0.2), (7.6, 0, 0.2), (7.6, 0, 2.8), (0.6, 0, 2.8)], fill="#dbe6f1", stroke="#8fa3b9", width=0.8)
for k in range(1, 6):
    gx = 0.6 + k * 7.0 / 6
    line((gx, 0, 0.2), (gx, 0, 2.8), stroke="#9db0c4", width=0.6)
poly([(8.4, 0, 0), (10.2, 0, 0), (10.2, 0, 2.6), (8.4, 0, 2.6)], fill="#e9f0f7", stroke="#8fa3b9", width=0.8)
line((9.3, 0, 0), (9.3, 0, 2.6), stroke="#9db0c4", width=0.6)
# Slab edge above the ground floor.
line((0, 0, 3.05), (W, 0, 3.05), stroke="#7f95ad", width=1.0)

# Side windows.
for z0 in (0.8, 4.0, 6.9):
    for y0 in (1.2, 4.6):
        poly([(0, y0, z0), (0, y0 + 2.2, z0), (0, y0 + 2.2, z0 + 1.7), (0, y0, z0 + 1.7)], fill="#d9e5f0", stroke="#8fa3b9", width=0.7)

# Roof parapet and pergola frame.
poly([(0.25, 0.25, H), (W - 0.25, 0.25, H), (W - 0.25, D - 0.25, H), (0.25, D - 0.25, H)], fill="none", stroke="#b2c2d2", width=0.6)
ph = H + 2.4
for (px, py) in ((1.2, 1.5), (6.5, 1.5), (1.2, 6.5), (6.5, 6.5)):
    line((px, py, H), (px, py, ph), stroke="#9db0c4", width=0.7)
poly([(1.2, 1.5, ph), (6.5, 1.5, ph), (6.5, 6.5, ph), (1.2, 6.5, ph)], fill="none", stroke="#9db0c4", width=0.7)
for k in range(1, 5):
    line((1.2 + k * 1.06, 1.5, ph), (1.2 + k * 1.06, 6.5, ph), stroke="#b9c8d7", width=0.5)
# Roof box (stair core).
poly([(7.6, 4.2, H), (10.2, 4.2, H), (10.2, 4.2, H + 1.6), (7.6, 4.2, H + 1.6)], fill="#f3f7fb")
poly([(7.6, 4.2, H), (7.6, 7.4, H), (7.6, 7.4, H + 1.6), (7.6, 4.2, H + 1.6)], fill="#e4ecf5")
poly([(7.6, 4.2, H + 1.6), (10.2, 4.2, H + 1.6), (10.2, 7.4, H + 1.6), (7.6, 7.4, H + 1.6)], fill="#ffffff")

# Tree on the right.
tx, ty = 10.4, -3.2
line((tx, ty, 0), (tx, ty, 3.6), stroke="#8fa3b9", width=1.0)
for (dx, dz, ex, ez) in ((0, 2.2, -1.1, 3.4), (0, 2.8, 1.0, 4.0), (0, 3.4, -0.6, 4.6), (0, 3.0, 0.4, 5.2)):
    line((tx + dx, ty, dz), (tx + ex, ty, ez), stroke="#9db0c4", width=0.6)
cx, cy = [float(v) for v in P(tx, ty, 4.6).split(",")]
out.append(f'<ellipse cx="{cx:.1f}" cy="{cy:.1f}" rx="22" ry="30" fill="#eef3f8" fill-opacity=".7" stroke="#9db0c4" stroke-width="0.8"/>')
out.append(f'<ellipse cx="{cx-6:.1f}" cy="{cy+6:.1f}" rx="13" ry="17" fill="none" stroke="#b9c8d7" stroke-width="0.6"/>')
gx, gy = [float(v) for v in P(tx, ty, 0).split(",")]
out.append(f'<ellipse cx="{gx:.1f}" cy="{gy:.1f}" rx="12" ry="4" fill="none" stroke="#c3d1df" stroke-width="0.6"/>')

svg = ('<svg class="art" viewBox="0 0 240 520" preserveAspectRatio="xMidYMid slice" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
       '<defs><linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#eef3f9"/><stop offset="1" stop-color="#f8fafc"/></linearGradient></defs>'
       '<rect width="240" height="520" fill="url(#sky)"/>' + "".join(out) + "</svg>")
open(sys.argv[1], "w").write(svg)
print(len(svg))
