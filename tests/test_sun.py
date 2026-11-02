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


def wall_with_opening(x0, x1, y0, y1, height, ox0, ox1, oz0, oz1):
    """A wall along x with a rectangular opening, as Archicad reports it: faces with holes, plus reveals."""
    pts = []
    for y in (y0, y1):
        pts += [(x0, y, 0), (x1, y, 0), (x1, y, height), (x0, y, height), (ox0, y, oz0), (ox1, y, oz0), (ox1, y, oz1), (ox0, y, oz1)]
    vertices = [{"x": a, "y": b, "z": c} for a, b, c in pts]
    outer = {"vertexIds": [0, 1, 2, 3], "holes": [{"vertexIds": [7, 6, 5, 4]}]}
    inner = {"vertexIds": [11, 10, 9, 8], "holes": [{"vertexIds": [12, 13, 14, 15]}]}
    caps = [[0, 8, 9, 1], [3, 2, 10, 11], [0, 3, 11, 8], [1, 9, 10, 2]]
    reveals = [[4, 5, 13, 12], [5, 6, 14, 13], [6, 7, 15, 14], [7, 4, 12, 15]]
    return {"vertices": vertices, "polygons": [outer, inner] + [{"vertexIds": f} for f in caps + reveals]}


def test_room_daylight_is_plausible():
    from archicad_mcp.sun.daylight import Room, Settings, analyze

    def scene(window):
        t, h = 0.2, 3.0
        bodies = [box(-t, 6, 0, 4 + t, 6 + t, h), box(-t, -t, 0, 0, 6 + t, h), box(4, -t, 0, 4 + t, 6 + t, h),
                  box(-t, -t, h, 4 + t, 6 + t, h + 0.3), box(-t, -t, -0.3, 4 + t, 6 + t, 0),
                  wall_with_opening(0, 4, -t, 0, h, 1, 3, 0.9, 2.4)]
        elements = [{"type": "Wall", "bodies": bodies}]
        if window == "glass without a material":
            elements.append({"type": "Window", "bodies": [box(1, -0.11, 0.9, 3, -0.09, 2.4)]})
        return build_scene(elements)

    room = Room("r1", "Living", [(0, 0), (4, 0), (4, 6), (0, 6)], floor_z=0.0, height=3.0)
    london = Location(51.5, -0.12, utc_offset=0)
    results = {}
    for window in ("open", "glass without a material"):
        s = scene(window)
        r = analyze(s, [room], london, date(2026, 3, 20), Settings(grid=0.5))
        noon = min(range(len(r.samples)), key=lambda i: abs(r.samples[i].time.hour - 12) * 60 + r.samples[i].time.minute)
        df, lux = r.daylight_factor(), r.illuminance(noon)
        front, back = r.points[:, 1] < 1.5, r.points[:, 1] > 4.5
        results[window] = (df[front].mean(), df[back].mean(), lux[back].mean(), len(s.glass))
        assert df[front].mean() > df[back].mean() > 0.3  # brighter near the window, reflected light at the back
        assert 1.0 < df.mean() < 5.0  # a 2 x 1.5 m window in a 4 x 6 m room
        assert 2.5 < r.sun_hours().max() < 6.0  # equinox sun through a south window
    assert results["open"][3] == 1  # the wall opening, counted once although both wall faces report it
    assert results["glass without a material"][0] < results["open"][0]  # the pane takes its 30%
    assert results["glass without a material"][1] < results["open"][1]  # also for the reflected light at the back
    assert results["glass without a material"][3] == 1  # the pane only: its opening is not counted again


def test_window_without_recognizable_glass_assumes_glazing_and_says_so():
    frame = box(1, -0.25, 0.9, 3, 0.05, 2.4)  # a solid block filling the opening, no thin pane in it
    s = build_scene([{"type": "Wall", "bodies": [wall_with_opening(0, 4, -0.2, 0, 3, 1, 3, 0.9, 2.4)]},
                     {"type": "Window", "bodies": [frame]}])
    assert [(g["transmission"], g["open"]) for g in s.glass] == [(0.7, False)]
    assert "no recognizable glass" in s.notes[0]
    door = build_scene([{"type": "Wall", "bodies": [wall_with_opening(0, 4, -0.2, 0, 3, 1, 2, 0, 2.1)]},
                        {"type": "Door", "bodies": [box(1, -0.15, 0, 2, -0.1, 2.1)]}])
    assert door.glass == []  # a closed door lets no light in
