import json

import anyio
import pytest

from mcp import Client

from archicad_mcp import server
from archicad_mcp.sun.service import LAYER_NAME, SunAnalysis
from fake_archicad import FakeArchicad
from test_sun import box, wall_with_opening


def call(tool, arguments=None):
    async def run():
        async with Client(server.mcp) as client:
            return await client.call_tool(tool, arguments or {})

    answer = anyio.run(run)
    return answer


def result(tool, arguments=None):
    answer = call(tool, arguments)
    assert not answer.is_error, answer.content
    return answer.structured_content


@pytest.fixture
def model(monkeypatch, tmp_path):
    """A 4 x 6 m room with a glazed 2 x 1.5 m window in its south wall, in London."""
    with FakeArchicad() as fake:
        monkeypatch.setattr(server.archicad, "_port", fake.port)
        monkeypatch.setattr(server, "sun", SunAnalysis(server.archicad, tmp_path / "sun_tiles.json"))
        t, h = 0.2, 3.0
        for body in (box(-t, 6, 0, 4 + t, 6 + t, h), box(-t, -t, 0, 0, 6 + t, h), box(4, -t, 0, 4 + t, 6 + t, h),
                     wall_with_opening(0, 4, -t, 0, h, 1, 3, 0.9, 2.4)):
            fake.add("Wall", bodies=[body])
        fake.add("Slab", bodies=[box(-t, -t, -0.3, 4 + t, 6 + t, 0)])
        fake.add("Roof", bodies=[box(-t, -t, h, 4 + t, 6 + t, h + 0.3)])
        fake.surfaces["GLASS"] = {"attributeId": {"guid": "GLASS"}, "index": 9, "name": "Glass - Clear", "transparency": 70}
        fake.add("Window", bodies=[box(1, -0.11, 0.9, 3, -0.09, 2.4, "GLASS")])
        fake.add("Zone", details={"name": "Living", "numberStr": "001", "zCoordinate": 0.0, "holes": [],
                                  "polygonOutline": [{"x": 0, "y": 0}, {"x": 4, "y": 0}, {"x": 4, "y": 6}, {"x": 0, "y": 6}]})
        yield fake


def test_analysis_places_coloured_tiles_and_scrubs_through_the_day(model):
    summary = result("sun_analysis", {"date": "2026-03-20", "grid": 0.5})
    assert summary["points"] == 96 and summary["tiles"] == 96
    assert summary["location"]["latitude"] == 51.5 and summary["rooms"][0]["name"] == "001 Living"
    tiles = [g for g, e in model.elements.items() if e["type"] == "Morph"]
    assert len(tiles) == 96
    layer = next(layer for layer in model.layers if layer["name"] == LAYER_NAME)
    assert all(model.elements[g]["layer"] == layer["index"] for g in tiles)
    first = model.tapir_calls("CreateMorphs")[0]["morphsData"][0]
    assert first["castShadow"] is False and first["size"]["z"] == pytest.approx(0.01)

    noon = model.highlights[-1]
    assert len(noon["elements"]) == 96 and len(noon["highlightedColors"]) == 96
    stats = summary["shown"]["stats"]
    assert stats["unit"] == "lux" and stats["max"] > 10_000  # the sun patch through the window
    assert summary["times"][summary["timeIndex"]] == "12:00"

    evening = result("sun_analysis_show", {"time": "17:30"})
    assert evening["times"][evening["timeIndex"]] == "17:30"
    assert evening["shown"]["stats"]["mean"] < stats["mean"]
    assert model.highlights[-1]["highlightedColors"] != noon["highlightedColors"]

    df = result("sun_analysis_show", {"metric": "daylight_factor"})["shown"]["stats"]
    assert df["unit"] == "%" and 1.0 < df["mean"] < 5.0
    hours = result("sun_analysis_show", {"metric": "sun_hours"})["shown"]
    assert hours["stats"]["unit"] == "h" and 2.0 < hours["stats"]["max"] < 6.0 and hours["high"] >= hours["stats"]["max"]

    # Running again replaces the tiles and does not see the old tiles as obstacles.
    again = result("sun_analysis", {"date": "2026-03-20", "metric": "daylight_factor"})
    assert again["shown"]["stats"]["mean"] == pytest.approx(df["mean"])
    assert len([e for e in model.elements.values() if e["type"] == "Morph"]) == 96

    assert result("sun_analysis_clear")["removed"] == 96
    assert not [e for e in model.elements.values() if e["type"] == "Morph"]
    assert model.highlights[-1]["elements"] == []


def test_location_matters(model):
    london = result("sun_analysis", {"date": "2026-12-21", "metric": "sun_hours"})["shown"]["stats"]["max"]
    model.location = {"longitude": 18.4, "latitude": -33.9, "altitude": 0, "north": 1.5707963267948966}  # Cape Town
    cape_town = result("sun_analysis", {"date": "2026-12-21", "metric": "sun_hours"})["shown"]["stats"]["max"]
    # In the southern summer the noon sun stands high in the north: hardly any sun through a south window.
    assert london > 1.0 and cape_town < london
    rome = result("sun_analysis", {"date": "2026-12-21", "metric": "sun_hours", "latitude": 41.9, "longitude": 12.5})
    assert rome["location"]["latitude"] == 41.9 and rome["shown"]["stats"]["max"] > 1.0


def test_tiles_survive_a_restart_for_clearing(model, tmp_path):
    result("sun_analysis", {"date": "2026-06-21"})
    saved = json.loads((tmp_path / "sun_tiles.json").read_text())["tiles"]
    assert len(saved) == 96
    restarted = SunAnalysis(server.archicad, tmp_path / "sun_tiles.json")
    assert restarted.clear()["removed"] == 96
    assert not [e for e in model.elements.values() if e["type"] == "Morph"]


def test_helpful_errors(model):
    for guid in [g for g, e in model.elements.items() if e["type"] == "Zone"]:
        del model.elements[guid]
    answer = call("sun_analysis")
    assert answer.is_error and "No rooms (zones) on the current story" in answer.content[0].text
    show = call("sun_analysis_show", {"time": "10:00"})
    assert show.is_error and "Run a sun analysis first" in show.content[0].text
