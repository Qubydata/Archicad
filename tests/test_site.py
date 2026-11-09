import json
import math
import re

import ezdxf
import numpy as np
import pytest

from archicad_mcp.site import buildings as osm
from archicad_mcp.site import elevation, survey
from archicad_mcp.site.geo import Frame, global_pixel
from archicad_mcp.site.service import BUILDINGS_LAYER, TERRAIN_LAYER, SiteService
from archicad_mcp.connection import ArchicadConnection, ArchicadError
from archicad_mcp.site.terrain import Grid, extrusion_body, grid_axis, grid_mesh, survey_mesh
from fake_archicad import FakeArchicad


def tilted_tiles(base=100.0, per_pixel=0.5, near=(51.5, -0.12)):
    """Fake terrarium tiles: height = base + per_pixel * (global pixel column - the column of `near`)."""
    calls = []

    def fetch(url):
        z, x, y = map(int, re.search(r"/(\d+)/(\d+)/(\d+)\.png$", url).groups())
        calls.append((z, x, y))
        reference = np.floor(global_pixel(near[0], near[1], z)[0])
        columns = x * 256 + np.arange(256) - reference
        return elevation.encode(np.tile(base + per_pixel * columns, (256, 1)))

    fetch.calls = calls
    return fetch


def test_frame_round_trip_and_north():
    frame = Frame(6.45, 3.39)  # Lagos, north is +y
    x, y = frame.to_model(6.45 + 0.001, 3.39)
    assert abs(x) < 1e-6 and y == pytest.approx(110.6, rel=1e-2)
    lat, lon = frame.to_geo(x, y)
    assert lat == pytest.approx(6.451) and lon == pytest.approx(3.39)
    turned = Frame(6.45, 3.39, north=0.0)  # north along +x
    x, y = turned.to_model(6.451, 3.39)
    assert x == pytest.approx(110.6, rel=1e-2) and abs(y) < 1e-6
    east_x, east_y = turned.to_model(6.45, 3.391)
    assert east_y < 0 and abs(east_x) < 1e-6  # east is clockwise from north


def test_terrarium_round_trip_and_sampling(tmp_path):
    heights = np.array([[0.0, 12.25, -3.5, 2500.0]] * 4)
    assert np.allclose(elevation.decode(elevation.encode(heights)), heights, atol=1 / 256)
    fetch = tilted_tiles()
    cache = elevation.TileCache(tmp_path / "cache", fetch)
    frame = Frame(51.5, -0.12)
    axis = np.array([-50.0, 0.0, 50.0])
    sampled = elevation.sample(frame, axis, axis, cache, 15)
    lat, lon = frame.to_geo(*np.meshgrid(axis, axis))
    px, _ = global_pixel(lat, lon, 15)
    reference = np.floor(global_pixel(51.5, -0.12, 15)[0])
    assert np.allclose(sampled, 100.0 + 0.5 * (px - 0.5 - reference), atol=0.01)
    calls = len(fetch.calls)
    elevation.sample(frame, axis, axis, cache, 15)
    assert len(fetch.calls) == calls  # the second time from the disk cache
    assert elevation.choose_zoom(51.5, 150) == 15 and elevation.choose_zoom(51.5, 15000) < 15


def test_grid_mesh_and_buildings_geometry():
    axis = grid_axis(10, 5)
    assert list(axis) == [-10, -5, 0, 5, 10]
    grid = Grid(axis, axis, np.add.outer(axis, axis) * 0.1)  # z = 0.1 (x + y)
    mesh = grid_mesh(grid)
    assert len(mesh["polygonCoordinates"]) == 16 and len(mesh["sublines"]) == 3
    assert all(len(s["coordinates"]) == 3 for s in mesh["sublines"])
    assert all(p["z"] == pytest.approx(0.1 * (p["x"] + p["y"])) for p in mesh["polygonCoordinates"])
    ring = [(p["x"], p["y"]) for p in mesh["polygonCoordinates"]]
    assert sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:] + ring[:1])) > 0  # counterclockwise
    assert grid.height_at(2.5, -2.5) == pytest.approx(0.0)

    body = extrusion_body([(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)], 9)  # clockwise, closed
    assert len(body["vertices"]) == 8 and len(body["polygons"]) == 6
    roof = [body["vertices"][k] for k in body["polygons"][0]["vertexIds"]]
    assert all(v["z"] == 9 for v in roof)
    assert sum(a["x"] * b["y"] - b["x"] * a["y"] for a, b in zip(roof, roof[1:] + roof[:1])) > 0
    assert extrusion_body([(0, 0), (0.5, 0), (0, 0.5)], 3) is None  # too small to matter


def test_reading_point_lists():
    csv_text = "Point,Easting,Northing,Elevation,Code\n1,10.0,20.0,5.5,TOP\n2,30,20,6.0,TOP\n3,20,40,7.25,BANK\n"
    data = survey.read("levels.csv", csv_text.encode())
    assert data.points.tolist() == [[10, 20, 5.5], [30, 20, 6.0], [20, 40, 7.25]]
    pnez = "1 200.0 100.0 3.0\n2 210.0 100.0 3.5\n3 205.0 120.0 4.0\nbad line\n"
    data = survey.read("points.txt", pnez.encode(), order="pnez")
    assert data.points[0].tolist() == [100.0, 200.0, 3.0] and any("skipped" in n for n in data.notes)
    semicolons = "x;y;z\n1,5;2,0;3,25\n4,0;5,0;6,0\n7;8;9\n"
    assert survey.read("p.csv", semicolons.encode()).points[0].tolist() == [1.5, 2.0, 3.25]
    with pytest.raises(ValueError, match="DXF"):
        survey.read("plan.dwg", b"...")


def test_reading_a_dxf(tmp_path):
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4  # millimetres
    space = doc.modelspace()
    space.add_lwpolyline([(0, 0), (10000, 0), (10000, 10000)], dxfattribs={"elevation": 2000})
    space.add_point((5000, 5000, 2500))
    space.add_line((0, 0, 0), (20000, 0, 0))  # a flat boundary line
    space.add_text("2750", dxfattribs={"insert": (2000, 8000)})
    path = tmp_path / "survey.dxf"
    doc.saveas(path)
    data = survey.read("survey.dxf", path.read_bytes())
    assert data.units == "mm" and len(data.lines) == 1 and len(data.points) == 2
    assert any("left out" in n for n in data.notes)
    factor, note = survey.unit_factor(data)
    assert factor == 0.001 and "mm" in note
    metres = data.scaled(factor)
    assert metres.lines[0][1].tolist() == [10.0, 0.0, 2.0] and sorted(metres.points[:, 2].tolist()) == [2.5, 2.75]


def test_map_coordinates_use_the_survey_point():
    data = survey.Survey(np.array([[500010.0, 700020.0, 105.0], [500030.0, 700020.0, 106.0], [500020.0, 700040.0, 107.0]]))
    point = {"position": {"eastings": 500000.0, "northings": 700000.0, "elevation": 100.0},
             "positionInProject": {"x": 0.0, "y": 0.0, "z": 0.0},
             "geoReferencingParameters": {"xAxisAbscissa": 1.0, "xAxisOrdinate": 0.0, "scale": 1.0}}
    moved, notes = survey.to_project(data, point)
    assert moved.points.tolist() == [[10.0, 20.0, 5.0], [30.0, 20.0, 6.0], [20.0, 40.0, 7.0]] and notes
    centred, notes = survey.to_project(data, {"position": {"eastings": 0, "northings": 0, "elevation": 0}})
    box = centred.points[:, :2]
    assert np.allclose((box.min(axis=0) + box.max(axis=0)) / 2, [0.0, 0.0]) and "not set" in notes[0]
    local = survey.Survey(np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 1.0, 2.0]]))
    assert survey.to_project(local, None)[0] is local


def test_survey_mesh():
    points = np.array([[0, 0, 1], [10, 0, 2], [10, 10, 3], [0, 10, 4], [5, 5, 9]], dtype=float)
    contour = np.array([[2, 2, 5], [8, 2, 5], [8, 8, 5]], dtype=float)
    mesh, notes = survey_mesh(points, [contour])
    assert len(mesh["polygonCoordinates"]) == 4  # the corners
    lengths = sorted(len(s["coordinates"]) for s in mesh["sublines"])
    assert lengths == [2, 3]  # the inner point as a level point, and the contour
    with pytest.raises(ValueError):
        survey_mesh(np.array([[0, 0, 1], [1, 1, 1], [2, 2, 1]], dtype=float), [])


OVERPASS_ANSWER = {"elements": [
    {"type": "way", "tags": {"building": "yes", "height": "12 m", "name": "Shop"},
     "geometry": [{"lat": 51.5001, "lon": -0.1201}, {"lat": 51.5001, "lon": -0.1199}, {"lat": 51.5002, "lon": -0.1199},
                  {"lat": 51.5002, "lon": -0.1201}, {"lat": 51.5001, "lon": -0.1201}]},
    {"type": "way", "tags": {"building": "house", "building:levels": "2"},
     "geometry": [{"lat": 51.4999, "lon": -0.1201}, {"lat": 51.4999, "lon": -0.1199}, {"lat": 51.49985, "lon": -0.1199},
                  {"lat": 51.49985, "lon": -0.1201}]},
    {"type": "relation", "tags": {"building": "yes", "type": "multipolygon"},
     "members": [{"role": "outer", "geometry": [{"lat": 51.5003, "lon": -0.1203}, {"lat": 51.5003, "lon": -0.1200},
                                                {"lat": 51.5004, "lon": -0.1200}, {"lat": 51.5004, "lon": -0.1203}]}]},
]}


def test_buildings_from_openstreetmap():
    frame = Frame(51.5, -0.12)
    found = osm.parse(OVERPASS_ANSWER, frame)
    assert [(b.height, b.estimated) for b in found] == [(12.0, False), (6.0, False), (6.0, True)]
    assert found[0].name == "Shop" and all(abs(x) < 60 and abs(y) < 60 for x, y in found[0].outline)
    assert osm.parse_length("40'") == pytest.approx(12.192)
    queries = []

    def post(url, text):
        queries.append((url, text))
        if "overpass-api.de" in url:
            raise OSError("busy")
        return OVERPASS_ANSWER

    nearby = osm.fetch(frame, 100, post)
    assert len(queries) == 2 and 'way["building"]' in queries[0][1] and len(nearby) == 3


@pytest.fixture
def site(monkeypatch, tmp_path):
    with FakeArchicad() as fake:
        connection = ArchicadConnection(port=fake.port)
        posts = []

        def post(url, text):
            posts.append(text)
            return OVERPASS_ANSWER

        fake.location = {"longitude": -0.12, "latitude": 51.5, "altitude": 30.0, "north": math.pi / 2}
        service = SiteService(connection, tmp_path, fetch_tile=tilted_tiles(base=40.0, per_pixel=0.02), post=post)
        yield fake, service, posts


def test_site_from_map(site):
    fake, service, posts = site
    progress = []
    state = service.from_map(size=200, include_buildings=True, progress=lambda f, m: progress.append(f))
    meshes = [e for e in fake.elements.values() if e["type"] == "Mesh"]
    assert len(meshes) == 1 and state["terrain"] == 1 and state["buildings"] == 3
    mesh = meshes[0]["data"]
    heights = [p["z"] for p in mesh["polygonCoordinates"]] + [p["z"] for s in mesh["sublines"] for p in s["coordinates"]]
    assert min(heights) < 0 < max(heights)  # the ground slopes, with the origin at 0
    side = len({p["x"] for p in mesh["polygonCoordinates"]})
    assert len(mesh["polygonCoordinates"]) == 4 * side - 4 and len(mesh["sublines"]) == side - 2
    layers = {layer["name"]: layer["index"] for layer in fake.layers}
    terrain = next(g for g, e in fake.elements.items() if e["type"] == "Mesh")
    assert fake.elements[terrain]["layer"] == layers[TERRAIN_LAYER]
    morphs = [g for g, e in fake.elements.items() if e["type"] == "Morph"]
    assert len(morphs) == 3 and all(fake.elements[g]["layer"] == layers[BUILDINGS_LAYER] for g in morphs)
    last = state["last"]
    assert last["source"] == "map" and last["elevation"]["min"] < last["elevation"]["origin"] < last["elevation"]["max"]
    assert any("OpenStreetMap" in n for n in last["notes"]) and any("estimated" in n or "6 m" in n for n in last["notes"])
    assert progress == sorted(progress) and progress[-1] == 1.0

    again = service.from_map(size=200, include_buildings=False, ground="altitude")
    assert [e["type"] for e in fake.elements.values()].count("Mesh") == 1  # replaced
    assert again["buildings"] == 3  # kept, since buildings were not rebuilt
    mesh = next(e for e in fake.elements.values() if e["type"] == "Mesh")["data"]
    centre = again["last"]["elevation"]["origin"]
    assert min(p["z"] for p in mesh["polygonCoordinates"]) == pytest.approx(again["last"]["elevation"]["min"] - 30.0, abs=0.05)
    assert centre > 30.0

    restarted = SiteService(service.connection, service.data_dir)
    assert restarted.clear()["removed"] == {"terrain": 1, "buildings": 3}
    assert not [e for e in fake.elements.values() if e["type"] in ("Mesh", "Morph")]


def test_site_needs_a_location(site):
    fake, service, _ = site
    fake.location = {"longitude": 0.0, "latitude": 0.0, "altitude": 0.0, "north": math.pi / 2}
    with pytest.raises(ArchicadError, match="Project Location"):
        service.from_map()


def test_site_from_survey(site):
    fake, service, _ = site
    text = "x,y,z\n0,0,1\n20,0,2\n20,20,3\n0,20,2.5\n10,10,4\n"
    state = service.from_survey("levels.csv", text.encode())
    mesh = next(e for e in fake.elements.values() if e["type"] == "Mesh")["data"]
    assert len(mesh["polygonCoordinates"]) == 4 and len(mesh["sublines"]) == 1
    assert state["last"]["source"] == "survey" and state["last"]["elevation"] == {"min": 1.0, "max": 4.0}
    with pytest.raises(ArchicadError, match="DXF"):
        service.from_survey("plan.dwg", b"x")


def test_site_in_the_panel_and_for_the_assistant(site, monkeypatch, tmp_path):
    import base64

    import anyio
    from mcp import Client
    from starlette.testclient import TestClient

    from archicad_mcp import server
    from archicad_mcp.chat import config, web

    fake, service, _ = site
    monkeypatch.setattr(server, "site", service)
    monkeypatch.setattr(server.archicad, "_port", fake.port)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "home")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "home" / "config.json")
    client = TestClient(web.create_app(8765, token="secret"), base_url="http://127.0.0.1:8765")
    headers = {"x-tevavid-token": "secret"}

    state = client.get("/api/site", headers=headers).json()
    assert state["terrain"] == 0 and state["projectLocation"]["latitude"] == 51.5 and "surveyPoint" not in state["projectLocation"]
    events = [json.loads(line) for line in client.post("/api/site/map", json={"size": 200, "buildings": True},
                                                       headers=headers).text.splitlines()]
    assert events[0]["type"] == "progress" and events[-1]["type"] == "result"
    assert events[-1]["state"]["terrain"] == 1 and events[-1]["state"]["buildings"] == 3

    csv_text = "x,y,z\n0,0,1\n20,0,2\n20,20,3\n0,20,2.5\n"
    events = [json.loads(line) for line in client.post("/api/site/survey", headers=headers, json={
        "name": "levels.csv", "content": base64.b64encode(csv_text.encode()).decode()}).text.splitlines()]
    assert events[-1]["state"]["last"]["source"] == "survey"
    bad = [json.loads(line) for line in client.post("/api/site/survey", headers=headers, json={
        "name": "plan.dwg", "content": base64.b64encode(b"x").decode()}).text.splitlines()]
    assert bad[-1]["type"] == "error" and "DXF" in bad[-1]["message"]
    assert client.post("/api/site/survey", json={"name": "a.csv"}, headers=headers).status_code == 400

    assert client.post("/api/site/clear", json={"what": "buildings"}, headers=headers).json()["removed"] == {"buildings": 3}

    async def call():
        async with Client(server.mcp) as mcp_client:
            path = tmp_path / "points.csv"
            path.write_text(csv_text)
            return await mcp_client.call_tool("site_from_survey", {"file_path": str(path)})

    answer = anyio.run(call)
    assert not answer.is_error and answer.structured_content["last"]["points"] == 4
