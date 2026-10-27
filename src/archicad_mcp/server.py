"""MCP server that lets an AI assistant operate a running Archicad.

The tools fall in three layers:

* status and discovery of the running Archicad instances,
* curated tools for the everyday modelling and querying tasks, with small
  typed inputs that models fill in reliably,
* generic access to every Tapir command and every built-in Archicad command,
  with a searchable catalog of the Tapir command schemas, so nothing Archicad
  can do over its JSON API is out of reach.
"""

from __future__ import annotations

import base64
import math
import os
import tempfile
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations

from . import catalog
from .connection import ArchicadConnection, ArchicadError

INSTRUCTIONS = """\
You are operating a live Archicad session through its JSON API and the Tapir add-on.

Conventions:
- Lengths are meters, angles are radians unless a tool says degrees. X/Y is the floor plan, Z is up.
- Elements are referred to by their GUID strings. Tools return GUIDs of what they create or find.
- Stories are referred to by floorIndex (see get_stories). Ground floor is usually 0.
- Fields you leave out come from the Archicad tool defaults or from the named favorite.

Workflow:
1. Call archicad_status first to check the connection and the Tapir add-on.
2. Use get_stories and get_elements / get_selected_elements to understand the model before changing it.
3. Use the create_* tools for walls, slabs, columns, beams, roofs, windows, doors, zones and objects.
4. For anything else (dimensions, labels, layers, attributes, layouts, IFC, classifications, MEP,
   design options, publishing...), call search_tapir_commands, then get_tapir_command_schema,
   then run_tapir_command with parameters that match the schema exactly.
5. Call get_view_image to look at the result in the 3D window.
Ask before deleting elements or overwriting files the user did not mention.
"""

mcp = MCPServer(name="archicad", instructions=INSTRUCTIONS)
archicad = ArchicadConnection()

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
MODIFY = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


# --- helpers -----------------------------------------------------------------


def _element_ids(guids: list[str]) -> list[dict[str, Any]]:
    return [{"elementId": {"guid": guid}} for guid in guids]


def _guids(elements: list[dict[str, Any]]) -> list[str]:
    return [item["elementId"]["guid"] for item in elements if "elementId" in item]


def _created(response: dict[str, Any]) -> dict[str, Any]:
    """Split a Tapir creation answer into created GUIDs and per-item errors."""
    created, errors = [], []
    for index, item in enumerate(response.get("elements", [])):
        if "elementId" in item:
            created.append(item["elementId"]["guid"])
        else:
            errors.append({"index": index, "error": item.get("error", item)})
    result: dict[str, Any] = {"created": created}
    if errors:
        result["errors"] = errors
    return result


def _execution_summary(results: list[dict[str, Any]], guids: list[str]) -> dict[str, Any]:
    failed = [
        {"guid": guid, "error": result.get("error")}
        for guid, result in zip(guids, results)
        if not result.get("success", False)
    ]
    return {"succeeded": len(guids) - len(failed), "failed": failed}


class Point2D(BaseModel):
    x: float
    y: float


class Point3D(BaseModel):
    x: float
    y: float
    z: float = 0.0


class TapirItem(BaseModel):
    """Base for creation inputs. Extra Tapir fields are passed through untouched."""

    model_config = ConfigDict(extra="allow")

    def to_tapir(self) -> dict[str, Any]:
        return self.model_dump(exclude_none=True)


class WallSpec(TapirItem):
    begCoordinate: Point2D = Field(description="Start point of the wall reference line.")
    endCoordinate: Point2D = Field(description="End point of the wall reference line.")
    height: float | None = Field(None, gt=0, description="Wall height. Omit to keep the tool default / favorite.")
    thickness: float | None = Field(
        None, gt=0, description="Wall thickness. Applies to Basic walls; send structureType 'Basic' with it."
    )
    floorIndex: int | None = Field(None, description="Story index. With it, zCoordinate is the offset from that story.")
    zCoordinate: float | None = Field(None, description="Absolute bottom Z, or offset from floorIndex when given.")
    structureType: Literal["Basic", "Composite", "Profile"] | None = None
    referenceLineLocation: (
        Literal["Outside", "Center", "Inside", "CoreOutside", "CoreCenter", "CoreInside"] | None
    ) = None
    arcAngle: float | None = Field(None, description="Non-zero makes a curved wall (radians).")
    favoriteName: str | None = Field(None, description="Wall favorite to start from.")


class SlabSpec(TapirItem):
    polygonCoordinates: list[Point2D] = Field(min_length=3, description="Outline of the slab, in order.")
    level: float = Field(0.0, description="Z of the slab reference plane.")
    thickness: float | None = Field(None, gt=0, description="Applies to Basic slabs; send structureType 'Basic' with it.")
    floorIndex: int | None = None
    structureType: Literal["Basic", "Composite"] | None = None
    favoriteName: str | None = None


class ColumnSpec(TapirItem):
    coordinates: Point3D = Field(description="Base point of the column.")
    height: float | None = Field(None, gt=0)
    width: float | None = Field(None, gt=0, description="Cross section width (diameter for round columns).")
    depth: float | None = Field(None, gt=0, description="Cross section depth of rectangular columns.")
    circleBased: bool | None = Field(None, description="True for a round column.")
    floorIndex: int | None = None
    favoriteName: str | None = None


class BeamSpec(TapirItem):
    begCoordinate: Point2D
    endCoordinate: Point2D
    zCoordinate: float = Field(description="Z of the beam reference line.")
    width: float | None = Field(None, gt=0)
    height: float | None = Field(None, gt=0)
    floorIndex: int | None = None
    favoriteName: str | None = None


class RoofSpec(TapirItem):
    polygonCoordinates: list[Point2D] = Field(min_length=3, description="Outline of the roof, in order.")
    level: float = Field(description="Z of the roof pivot / base level.")
    thickness: float | None = Field(None, gt=0)
    floorIndex: int | None = None
    eavesOverhang: float | None = None
    structureType: Literal["Basic", "Composite"] | None = None


class OpeningSpec(TapirItem):
    ownerWallId: str = Field(description="GUID of the wall the window or door goes into.")
    centerOffset: float = Field(ge=0, description="Distance of the opening center from the wall start point.")
    width: float | None = Field(None, gt=0)
    height: float | None = Field(None, gt=0)
    sillHeight: float | None = None
    libraryPartName: str | None = Field(None, description="Library part to use instead of the tool default.")
    favoriteName: str | None = None

    def to_tapir(self) -> dict[str, Any]:
        data = super().to_tapir()
        data["ownerWallId"] = {"guid": self.ownerWallId}
        return data


class ZoneSpec(TapirItem):
    name: str
    numberStr: str = Field(description="Zone number, e.g. '001'.")
    polygonCoordinates: list[Point2D] | None = Field(None, description="Manual outline of the zone.")
    referencePosition: Point2D | None = Field(
        None, description="A point inside a closed room; Archicad finds the outline from the walls around it."
    )
    floorIndex: int | None = None

    def to_tapir(self) -> dict[str, Any]:
        data = super().to_tapir()
        polygon = data.pop("polygonCoordinates", None)
        reference = data.pop("referencePosition", None)
        if polygon:
            data["geometry"] = {"polygonCoordinates": polygon}
        elif reference:
            data["geometry"] = {"referencePosition": reference}
        elif "geometry" not in data:
            raise ArchicadError(f"Zone '{self.name}' needs polygonCoordinates or referencePosition.")
        return data


class ObjectSpec(TapirItem):
    libraryPartName: str = Field(description="Name of a library part loaded in the project, e.g. 'Chair 01'.")
    coordinates: Point3D
    angle: float | None = Field(None, description="Rotation in radians.")
    floorIndex: int | None = None


class PropertyAssignment(BaseModel):
    guid: str = Field(description="Element GUID.")
    property: str = Field(description="Property name, 'Group/Name' or property GUID (see find_properties).")
    value: str = Field(description="New value as a string.")


def _create(command: str, key: str, items: list[TapirItem]) -> dict[str, Any]:
    return _created(archicad.tapir(command, {key: [item.to_tapir() for item in items]}))


def _all_properties() -> list[dict[str, Any]]:
    return archicad.tapir("GetAllProperties").get("properties", [])


def _resolve_properties(names: list[str]) -> dict[str, str]:
    """Map property names ('Name', 'Group/Name' or a GUID) to property GUIDs."""
    properties = _all_properties()
    by_guid = {p["propertyId"]["guid"].lower(): p for p in properties}
    resolved = {}
    for name in names:
        if name.lower() in by_guid:
            resolved[name] = by_guid[name.lower()]["propertyId"]["guid"]
            continue
        group, _, leaf = name.rpartition("/")
        matches = [
            p
            for p in properties
            if p.get("propertyName", "").lower() == leaf.lower()
            and (not group or p.get("propertyGroupName", "").lower() == group.lower())
        ]
        if not matches:
            raise ArchicadError(f"No property named '{name}'. Use find_properties to look it up.")
        if len(matches) > 1 and not group:
            options = ", ".join(f"{m.get('propertyGroupName')}/{m.get('propertyName')}" for m in matches[:10])
            raise ArchicadError(f"Property name '{name}' is ambiguous, use 'Group/Name': {options}")
        resolved[name] = matches[0]["propertyId"]["guid"]
    return resolved


# --- connection --------------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def archicad_status() -> dict[str, Any]:
    """Check the Archicad connection: version, Tapir add-on, open project and stories."""
    status: dict[str, Any] = {"host": archicad.host, "port": archicad.port}
    status["product"] = archicad.command("API.GetProductInfo")
    status["tapirInstalled"] = archicad.is_tapir_available()
    if status["tapirInstalled"]:
        status["tapirVersion"] = archicad.tapir("GetAddOnVersion").get("version")
        status["project"] = archicad.tapir("GetProjectInfo")
    else:
        status["hint"] = (
            "The Tapir add-on is not loaded, so only reading tools work. Install it from "
            "https://github.com/ENZYME-APD/tapir-archicad-automation/releases and restart Archicad."
        )
    return status


@mcp.tool(annotations=READ_ONLY)
def list_archicad_instances() -> list[dict[str, Any]]:
    """List every running Archicad instance this server can reach, with its port."""
    return [
        {"port": instance.port, "product": instance.product_info, "selected": instance.port == archicad._port}
        for instance in archicad.discover()
    ]


@mcp.tool(annotations=MODIFY)
def select_archicad_instance(port: int) -> dict[str, Any]:
    """Send all further commands to the Archicad instance listening on this port."""
    archicad.port = port
    return {"port": port, "product": archicad.command("API.GetProductInfo")}


# --- reading the model -------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def get_stories() -> dict[str, Any]:
    """List the stories (floors) of the project with index, name, level and height."""
    return archicad.tapir("GetStories")


@mcp.tool(annotations=READ_ONLY)
def get_elements(
    element_type: str | None = None,
    filters: list[str] | None = None,
    with_details: bool = False,
    limit: int = 500,
) -> dict[str, Any]:
    """Find elements in the plan.

    element_type: Archicad type such as Wall, Slab, Column, Beam, Window, Door, Object, Zone, Roof,
        Stair, Railing, CurtainWall, Morph, Mesh. Omit for all elements.
    filters: optional Archicad filters, e.g. ["OnActualFloor"], ["IsVisibleByLayer"], ["IsEditable"].
    with_details: also return type, element ID and story of each element (needs Tapir).
    """
    parameters: dict[str, Any] = {}
    if filters:
        parameters["filters"] = filters
    if element_type:
        parameters["elementType"] = element_type
        result = archicad.command("API.GetElementsByType", parameters)
    else:
        result = archicad.command("API.GetAllElements", parameters)
    guids = _guids(result.get("elements", []))
    answer: dict[str, Any] = {"count": len(guids), "guids": guids[:limit]}
    if len(guids) > limit:
        answer["truncated"] = True
    if with_details and guids:
        answer["details"] = _details(guids[:limit], ["type", "id", "floorIndex", "layerIndex"])
    return answer


@mcp.tool(annotations=READ_ONLY)
def get_selected_elements(with_details: bool = True) -> dict[str, Any]:
    """Return the elements the user has selected in Archicad."""
    guids = _guids(archicad.command("API.GetSelectedElements").get("elements", []))
    answer: dict[str, Any] = {"count": len(guids), "guids": guids}
    if with_details and guids:
        answer["details"] = _details(guids, ["type", "id", "floorIndex", "layerIndex", "details"])
    return answer


def _details(guids: list[str], fields: list[str] | None) -> dict[str, Any]:
    parameters: dict[str, Any] = {"elements": _element_ids(guids)}
    if fields:
        parameters["fields"] = fields
    details = archicad.tapir("GetDetailsOfElements", parameters).get("detailsOfElements", [])
    return dict(zip(guids, details))


@mcp.tool(annotations=READ_ONLY)
def get_element_details(guids: list[str], fields: list[str] | None = None) -> dict[str, Any]:
    """Get the geometry and settings of elements, keyed by GUID.

    fields: optional subset of type, id, floorIndex, layerIndex, drawIndex, details,
        floorPlanPolygons, hotlinkId. Asking only for what you need is much faster.
    """
    return _details(guids, fields)


@mcp.tool(annotations=READ_ONLY)
def find_properties(search: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """Search the property definitions of the project (built-in and custom) by name or group."""
    words = search.lower().split()
    found = []
    for prop in _all_properties():
        text = f"{prop.get('propertyGroupName', '')} {prop.get('propertyName', '')}".lower()
        if all(word in text for word in words):
            found.append(
                {
                    "name": f"{prop.get('propertyGroupName')}/{prop.get('propertyName')}",
                    "guid": prop["propertyId"]["guid"],
                    "valueType": prop.get("propertyValueType"),
                    "editable": prop.get("propertyIsEditable"),
                }
            )
            if len(found) >= limit:
                break
    return found


@mcp.tool(annotations=READ_ONLY)
def get_element_properties(guids: list[str], properties: list[str]) -> dict[str, Any]:
    """Read property values of elements.

    properties: property names ('Element ID', 'General Parameters/Height'), or GUIDs from find_properties.
    Returns {guid: {property: value}}; an unreadable value is reported as an error string.
    """
    resolved = _resolve_properties(properties)
    response = archicad.tapir(
        "GetPropertyValuesOfElements",
        {
            "elements": _element_ids(guids),
            "properties": [{"propertyId": {"guid": guid}} for guid in resolved.values()],
        },
    )
    answer: dict[str, Any] = {}
    for guid, entry in zip(guids, response.get("propertyValuesForElements", [])):
        if "error" in entry:
            answer[guid] = {"error": entry["error"].get("message")}
            continue
        values = {}
        for name, item in zip(resolved, entry.get("propertyValues", [])):
            if "propertyValue" in item:
                values[name] = item["propertyValue"].get("value")
            else:
                values[name] = f"error: {item.get('error', {}).get('message', 'not available')}"
        answer[guid] = values
    return answer


@mcp.tool(annotations=READ_ONLY)
def get_element_quantities(guids: list[str]) -> dict[str, Any]:
    """Get calculated quantities (lengths m, areas m2, volumes m3) of walls, slabs, columns, beams, roofs, zones..."""
    response = archicad.tapir("GetQuantitiesOfElements", {"elements": _element_ids(guids)})
    return dict(zip(guids, response.get("quantitiesOfElements", [])))


@mcp.tool(annotations=READ_ONLY)
def get_view_image(source: Literal["3DWindow", "RenderingWindow", "CurrentWindow"] = "3DWindow") -> Image:
    """Take a picture of the 3D window (default), the rendering window, or whatever window is in front.

    Use it to check what you have built. Archicad 30 returns the image directly; older
    versions save the current window to a temporary file, so bring the window you want in front.
    """
    if source != "CurrentWindow":
        try:
            response = archicad.tapir("GetCurrentViewImage", {"source": source, "format": "png"})
            return Image(data=base64.b64decode(response["image"]), format="png")
        except ArchicadError:
            pass
    path = os.path.join(tempfile.gettempdir(), "archicad_mcp_view.png")
    archicad.tapir("SaveCurrentWindowAsImage", {"imageFilePath": path, "format": "png"})
    with open(path, "rb") as file:
        return Image(data=file.read(), format="png")


# --- creating elements -------------------------------------------------------


@mcp.tool(annotations=MODIFY)
def create_walls(walls: list[WallSpec]) -> dict[str, Any]:
    """Create walls. Each wall goes from begCoordinate to endCoordinate. Returns the new GUIDs.

    For a closed room give the corners in order and repeat the first corner as the last end point.
    """
    return _create("CreateWalls", "wallsData", walls)


@mcp.tool(annotations=MODIFY)
def create_slabs(slabs: list[SlabSpec]) -> dict[str, Any]:
    """Create slabs (floors, ceilings) from outline polygons. Returns the new GUIDs."""
    return _create("CreateSlabs", "slabsData", slabs)


@mcp.tool(annotations=MODIFY)
def create_columns(columns: list[ColumnSpec]) -> dict[str, Any]:
    """Create columns at the given base points. Returns the new GUIDs."""
    return _create("CreateColumns", "columnsData", columns)


@mcp.tool(annotations=MODIFY)
def create_beams(beams: list[BeamSpec]) -> dict[str, Any]:
    """Create beams between two points at height zCoordinate. Returns the new GUIDs."""
    return _create("CreateBeams", "beamsData", beams)


@mcp.tool(annotations=MODIFY)
def create_roofs(roofs: list[RoofSpec]) -> dict[str, Any]:
    """Create multi-plane roofs over outline polygons. Returns the new GUIDs.

    For a single-plane roof or custom slopes pass the extra Tapir fields (pivotLine, angle, levels);
    see get_tapir_command_schema('CreateRoofs').
    """
    return _create("CreateRoofs", "roofsData", roofs)


@mcp.tool(annotations=MODIFY)
def create_windows(windows: list[OpeningSpec]) -> dict[str, Any]:
    """Place windows into existing walls. Returns the new GUIDs."""
    return _create("CreateWindows", "windowsData", windows)


@mcp.tool(annotations=MODIFY)
def create_doors(doors: list[OpeningSpec]) -> dict[str, Any]:
    """Place doors into existing walls. Returns the new GUIDs."""
    return _create("CreateDoors", "doorsData", doors)


@mcp.tool(annotations=MODIFY)
def create_zones(zones: list[ZoneSpec]) -> dict[str, Any]:
    """Create zones (rooms) from an outline, or automatically from a point inside closed walls."""
    return _create("CreateZones", "zonesData", zones)


@mcp.tool(annotations=MODIFY)
def create_objects(objects: list[ObjectSpec]) -> dict[str, Any]:
    """Place library part objects (furniture, equipment...). The library part must be loaded in the project."""
    return _create("CreateObjects", "objectsData", objects)


# --- editing -----------------------------------------------------------------


@mcp.tool(annotations=MODIFY)
def move_elements(guids: list[str], dx: float = 0.0, dy: float = 0.0, dz: float = 0.0, copy: bool = False) -> dict[str, Any]:
    """Move elements by a vector in meters. With copy=True the originals stay and copies are moved."""
    vector = {"x": dx, "y": dy, "z": dz}
    response = archicad.tapir(
        "MoveElements",
        {"elementsWithMoveVectors": [{"elementId": {"guid": g}, "moveVector": vector, "copy": copy} for g in guids]},
    )
    return _execution_summary(response.get("executionResults", []), guids)


@mcp.tool(annotations=MODIFY)
def rotate_elements(
    guids: list[str], angle_degrees: float, origin_x: float = 0.0, origin_y: float = 0.0, copy: bool = False
) -> dict[str, Any]:
    """Rotate elements in plan around a point, counterclockwise by angle_degrees."""
    angle = math.radians(angle_degrees)
    rotation = {
        "origin": {"x": origin_x, "y": origin_y},
        "beginPoint": {"x": origin_x + 1.0, "y": origin_y},
        "endPoint": {"x": origin_x + math.cos(angle), "y": origin_y + math.sin(angle)},
    }
    response = archicad.tapir(
        "RotateElements",
        {"elementsWithRotations": [{"elementId": {"guid": g}, "rotation": rotation, "copy": copy} for g in guids]},
    )
    return _execution_summary(response.get("executionResults", []), guids)


@mcp.tool(annotations=DESTRUCTIVE)
def delete_elements(guids: list[str]) -> dict[str, Any]:
    """Delete elements. Can be undone in Archicad with Edit > Undo."""
    response = archicad.tapir("DeleteElements", {"elements": _element_ids(guids)})
    return _execution_summary(response.get("executionResults", []), guids)


@mcp.tool(annotations=MODIFY)
def set_element_properties(assignments: list[PropertyAssignment]) -> dict[str, Any]:
    """Set property values of elements, e.g. renumber IDs or fill custom properties."""
    resolved = _resolve_properties(list(dict.fromkeys(a.property for a in assignments)))
    values = [
        {
            "elementId": {"guid": a.guid},
            "propertyId": {"guid": resolved[a.property]},
            "propertyValue": {"value": a.value},
        }
        for a in assignments
    ]
    response = archicad.tapir("SetPropertyValuesOfElements", {"elementPropertyValues": values})
    return _execution_summary(response.get("executionResults", []), [a.guid for a in assignments])


@mcp.tool(annotations=MODIFY)
def select_elements(guids: list[str], mode: Literal["replace", "add", "remove"] = "replace") -> dict[str, Any]:
    """Change the selection in Archicad so the user can see or keep working with these elements."""
    parameters: dict[str, Any] = {}
    if mode == "replace":
        keep = set(guids)
        current = [
            item
            for item in archicad.command("API.GetSelectedElements").get("elements", [])
            if item.get("elementId", {}).get("guid") not in keep
        ]
        if current:
            parameters["removeElementsFromSelection"] = current
    key = "removeElementsFromSelection" if mode == "remove" else "addElementsToSelection"
    parameters[key] = _element_ids(guids)
    archicad.tapir("ChangeSelectionOfElements", parameters)
    return {"mode": mode, "count": len(guids)}


@mcp.tool(annotations=MODIFY)
def highlight_elements(
    guids: list[str], color: list[int] | None = None, wireframe_others: bool = False
) -> dict[str, Any]:
    """Highlight elements in a color ([r, g, b, a], 0-255, default red). Pass an empty list to clear."""
    rgba = color or [255, 0, 0, 255]
    archicad.tapir(
        "HighlightElements",
        {
            "elements": _element_ids(guids),
            "highlightedColors": [rgba] * len(guids),
            "wireframe3D": wireframe_others,
        },
    )
    return {"highlighted": len(guids)}


@mcp.tool(annotations=MODIFY)
def save_project() -> dict[str, Any]:
    """Save the open project in place."""
    return archicad.tapir("SaveProject")


# --- full API access ---------------------------------------------------------


@mcp.tool(annotations=READ_ONLY)
def search_tapir_commands(query: str = "", group: str | None = None, limit: int = 25) -> dict[str, Any]:
    """Search the several hundred Tapir commands by keywords, e.g. 'dimension', 'layer', 'publish', 'ifc'.

    Leave query empty to list a group. Then read the schema with get_tapir_command_schema.
    """
    return {"groups": catalog.groups(), "commands": catalog.search(query, group, limit)}


@mcp.tool(annotations=READ_ONLY)
def get_tapir_command_schema(command_name: str) -> dict[str, Any]:
    """Return the exact input and output JSON schema of a Tapir command, with the definitions it uses."""
    command = catalog.get_command(command_name)
    if command is None:
        suggestions = [c["name"] for c in catalog.search(command_name, limit=5)]
        raise ArchicadError(f"Unknown Tapir command '{command_name}'. Did you mean: {', '.join(suggestions)}?")
    return catalog.schema_with_definitions(command)


@mcp.tool(annotations=DESTRUCTIVE)
def run_tapir_command(command_name: str, parameters: dict[str, Any] | None = None) -> Any:
    """Run any Tapir command with parameters matching its schema (see get_tapir_command_schema)."""
    command = catalog.get_command(command_name)
    return archicad.tapir(command["name"] if command else command_name, parameters or {})


@mcp.tool(annotations=DESTRUCTIVE)
def run_archicad_command(command: str, parameters: dict[str, Any] | None = None) -> Any:
    """Run a built-in Archicad JSON API command, e.g. 'API.GetAllClassificationSystems'.

    Reference: https://archicadapi.graphisoft.com/JSONInterfaceDocumentation/
    """
    return archicad.command(command, parameters or {})


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
