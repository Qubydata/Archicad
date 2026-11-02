import math
from datetime import date, datetime

import numpy as np
import pytest

from archicad_mcp.sun.geometry import build_scene, surface_transmission
from archicad_mcp.sun.raycast import transmission_along, transmission_matrix
from archicad_mcp.sun.solar import Location, clear_sky, day_samples, direction_in_model, sun_position

LONDON = Location(51.5, 0.0, utc_offset=0)


def box(x0, y0, z0, x1, y1, z1, surface=None):
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]]
    polygons = [{"vertexIds": f, **({"surfaceId": {"guid": surface}} if surface else {})} for f in faces]
    return {"vertices": [{"x": a, "y": b, "z": c} for a, b, c in v], "polygons": polygons}


# --- sun position ------------------------------------------------------------

def test_summer_solstice_noon_in_london():
    altitude, azimuth = sun_position(LONDON, datetime(2026, 6, 21, 12, 2))
    assert math.degrees(altitude) == pytest.approx(90 - 51.5 + 23.44, abs=0.5)
    assert math.degrees(azimuth) == pytest.approx(180, abs=2)


def test_equinox_sun_rises_in_the_east_and_sets_in_the_west():
    morning = sun_position(LONDON, datetime(2026, 3, 20, 6, 30))
    evening = sun_position(LONDON, datetime(2026, 3, 20, 17, 45))
    assert 80 < math.degrees(morning[1]) < 100 and morning[0] > -0.05
    assert 260 < math.degrees(evening[1]) < 280


def test_southern_hemisphere_noon_sun_is_in_the_north():
    sydney = Location(-33.87, 151.2, utc_offset=10)
    altitude, azimuth = sun_position(sydney, datetime(2026, 12, 21, 12, 0))
    assert math.degrees(altitude) > 75
    assert math.degrees(azimuth) < 30 or math.degrees(azimuth) > 330


def test_project_north_turns_the_sun():
    default = direction_in_model(LONDON, 0.0, math.pi)  # sun due south, north is +Y
    assert default == pytest.approx((0.0, -1.0, 0.0), abs=1e-9)
    rotated = Location(51.5, 0.0, north=0.0)  # north along +X
    assert direction_in_model(rotated, 0.0, math.pi) == pytest.approx((-1.0, 0.0, 0.0), abs=1e-9)


def test_clear_sky_and_day_samples():
    assert clear_sky(6, -0.1) == (0.0, 0.0)
    dni, dhi = clear_sky(6, math.radians(60))
    assert 800 < dni < 1000 and 100 < dhi < 150
    samples = day_samples(LONDON, date(2026, 6, 21), step_minutes=30)
    assert 30 <= len(samples) <= 35  # about 16.5 hours of daylight
    winter = day_samples(LONDON, date(2026, 12, 21), step_minutes=30)
    assert len(winter) < len(samples) / 2
    assert all(s.altitude > 0 and abs(np.linalg.norm(s.direction) - 1) < 1e-9 for s in samples)


# --- materials ---------------------------------------------------------------

def test_surface_transmission():
    assert surface_transmission({"name": "Glass - Clear", "transparency": 85}) == pytest.approx(0.85)
    assert surface_transmission({"name": "Glass - Blue"}) == pytest.approx(0.7)
    assert surface_transmission({"name": "Concrete - Smooth", "transparency": 0}) == 0
    assert surface_transmission({"name": "Polycarbonate panel"}) == pytest.approx(0.7)


# --- ray casting -------------------------------------------------------------

def test_roof_blocks_high_sun_only():
    scene = build_scene([{"type": "Slab", "bodies": [box(-1, -1, 3, 1, 1, 3.2)]}])
    points = np.array([[0.0, 0.0, 0.0], [5.0, 0.0, 0.0]])
    assert transmission_along(points, np.array([0, 0, 1.0]), scene) == pytest.approx([0, 1])
    low = np.array([1, 0, 0.1]) / np.linalg.norm([1, 0, 0.1])
    assert transmission_along(points, low, scene) == pytest.approx([1, 1])


def test_glass_counts_once_per_pane_and_panes_multiply():
    scene = build_scene([{"type": "Window", "bodies": [box(-1, 2, -1, 1, 2.01, 1, "G")]}], {"G": 0.7})
    point = np.array([[0.0, 0.0, 0.0]])
    assert transmission_along(point, np.array([0, 1.0, 0]), scene) == pytest.approx([0.7])
    two = build_scene([{"type": "Window", "bodies": [box(-1, 2, -1, 1, 2.01, 1, "G"), box(-1, 3, -1, 1, 3.01, 1, "G")]}], {"G": 0.7})
    assert transmission_along(point, np.array([0, 1.0, 0]), two) == pytest.approx([0.49])
    assert two.glass[0]["area"] == pytest.approx(4.04, rel=0.02)


def test_faces_with_holes_let_light_through_the_hole():
    # A 4 x 4 m wall at y = 2 with a 1 x 1 m hole in the middle, as Archicad reports wall openings.
    vertices = [(-2, 2, -2), (2, 2, -2), (2, 2, 2), (-2, 2, 2), (-0.5, 2, -0.5), (0.5, 2, -0.5), (0.5, 2, 0.5), (-0.5, 2, 0.5)]
    body = {"vertices": [{"x": a, "y": b, "z": c} for a, b, c in vertices],
            "polygons": [{"vertexIds": [0, 1, 2, 3], "holes": [{"vertexIds": [7, 6, 5, 4]}]}]}
    scene = build_scene([{"type": "Wall", "bodies": [body]}])
    points = np.array([[0.0, 0.0, 0.0], [1.5, 0.0, 1.5]])
    assert transmission_along(points, np.array([0, 1.0, 0]), scene) == pytest.approx([1, 0])


def _brute_force(point, direction, scene):
    for a, b, c in scene.triangles:
        e1, e2 = b - a, c - a
        h = np.cross(direction, e2)
        det = e1 @ h
        if abs(det) < 1e-12:
            continue
        f, s = 1 / det, point - a
        u = f * (s @ h)
        if u < 0 or u > 1:
            continue
        q = np.cross(s, e1)
        v = f * (direction @ q)
        if v < 0 or u + v > 1:
            continue
        if f * (e2 @ q) > 1e-4:
            return 0.0
    return 1.0


def test_matches_brute_force_on_a_random_city():
    rng = np.random.default_rng(7)
    boxes = []
    for _ in range(120):
        x, y = rng.uniform(-15, 15, 2)
        w, d, h = rng.uniform(0.3, 3, 3)
        boxes.append(box(x, y, 0, x + w, y + d, h))
    scene = build_scene([{"type": "Wall", "bodies": boxes}])
    points = np.column_stack([rng.uniform(-12, 12, 300), rng.uniform(-12, 12, 300), np.full(300, 0.05)])
    directions = rng.normal(size=(12, 3))
    directions[:, 2] = np.abs(directions[:, 2]) + 0.15
    directions /= np.linalg.norm(directions, axis=1, keepdims=True)
    fast = transmission_matrix(points, directions, scene)
    for i in range(0, 300, 7):
        for k in range(12):
            assert fast[i, k] == _brute_force(points[i], directions[k], scene), (i, k)
