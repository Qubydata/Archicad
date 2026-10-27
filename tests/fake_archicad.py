"""A tiny stand-in for Archicad's JSON API, with just enough Tapir to test the server."""

from __future__ import annotations

import base64
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PNG_BYTES = b"\x89PNG\r\n\x1a\nfake"

PROPERTIES = [
    {"propertyId": {"guid": "P-ID"}, "propertyGroupName": "General Parameters", "propertyName": "Element ID",
     "propertyValueType": "String", "propertyIsEditable": True},
    {"propertyId": {"guid": "P-HEIGHT"}, "propertyGroupName": "General Parameters", "propertyName": "Height",
     "propertyValueType": "Real", "propertyIsEditable": False},
    {"propertyId": {"guid": "P-NAME-A"}, "propertyGroupName": "Zone", "propertyName": "Name",
     "propertyValueType": "String", "propertyIsEditable": True},
    {"propertyId": {"guid": "P-NAME-B"}, "propertyGroupName": "Custom", "propertyName": "Name",
     "propertyValueType": "String", "propertyIsEditable": True},
]

CREATE_COMMANDS = {
    "CreateWalls": ("wallsData", "Wall"),
    "CreateSlabs": ("slabsData", "Slab"),
    "CreateColumns": ("columnsData", "Column"),
    "CreateBeams": ("beamsData", "Beam"),
    "CreateRoofs": ("roofsData", "Roof"),
    "CreateWindows": ("windowsData", "Window"),
    "CreateDoors": ("doorsData", "Door"),
    "CreateZones": ("zonesData", "Zone"),
    "CreateObjects": ("objectsData", "Object"),
}


class FakeArchicad:
    def __init__(self, tapir: bool = True, view_image: bool = True) -> None:
        self.tapir = tapir
        self.view_image = view_image
        self.elements: dict[str, dict[str, Any]] = {}
        self.selection: list[str] = []
        self.properties: dict[tuple[str, str], str] = {}
        self.requests: list[dict[str, Any]] = []
        self.palettes: list[dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> FakeArchicad:
        self._thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()

    def tapir_calls(self, name: str) -> list[dict[str, Any]]:
        return [
            r["parameters"]["addOnCommandParameters"]
            for r in self.requests
            if r["command"] == "API.ExecuteAddOnCommand"
            and r["parameters"]["addOnCommandId"]["commandName"] == name
        ]

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                answer = fake.handle(body["command"], body.get("parameters", {}))
                data = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        return Handler

    # --- built-in commands ----------------------------------------------------

    def handle(self, command: str, parameters: dict[str, Any]) -> dict[str, Any]:
        ok = lambda result: {"succeeded": True, "result": result}  # noqa: E731
        if command == "API.IsAlive":
            return ok({"isAlive": True})
        if command == "API.GetProductInfo":
            return ok({"version": 29, "buildNumber": 3000, "languageCode": "INT"})
        if command == "API.IsAddOnCommandAvailable":
            return ok({"available": self.tapir})
        if command == "API.GetAllElements":
            return ok({"elements": [{"elementId": {"guid": g}} for g in self.elements]})
        if command == "API.GetElementsByType":
            wanted = parameters["elementType"]
            return ok({"elements": [{"elementId": {"guid": g}} for g, e in self.elements.items() if e["type"] == wanted]})
        if command == "API.GetSelectedElements":
            return ok({"elements": [{"elementId": {"guid": g}} for g in self.selection]})
        if command == "API.ExecuteAddOnCommand" and self.tapir:
            name = parameters["addOnCommandId"]["commandName"]
            return ok({"addOnCommandResponse": self.handle_tapir(name, parameters["addOnCommandParameters"])})
        return {"succeeded": False, "error": {"code": 4001, "message": f"Unknown command {command}"}}

    # --- Tapir commands -------------------------------------------------------

    def handle_tapir(self, name: str, p: dict[str, Any]) -> dict[str, Any]:
        guids = lambda items: [i["elementId"]["guid"] for i in items]  # noqa: E731
        if name == "GetAddOnVersion":
            return {"version": "1.7.4"}
        if name == "GetProjectInfo":
            return {"isUntitled": False, "isTeamwork": False, "projectName": "Test House"}
        if name == "GetStories":
            return {"firstStory": 0, "lastStory": 1, "actStory": 0, "stories": [
                {"index": 0, "name": "Ground Floor", "level": 0.0, "height": 3.0},
                {"index": 1, "name": "First Floor", "level": 3.0, "height": 3.0},
            ]}
        if name in CREATE_COMMANDS:
            key, element_type = CREATE_COMMANDS[name]
            created = []
            for item in p[key]:
                if element_type in ("Window", "Door") and item["ownerWallId"]["guid"] not in self.elements:
                    created.append({"error": {"code": -2130312312, "message": "Invalid owner wall"}})
                    continue
                guid = str(uuid.uuid4()).upper()
                self.elements[guid] = {"type": element_type, "data": item}
                created.append({"elementId": {"guid": guid}})
            return {"elements": created}
        if name == "GetDetailsOfElements":
            return {"detailsOfElements": [
                {"type": self.elements[g]["type"], "id": g[:4], "floorIndex": 0, "layerIndex": 1}
                if g in self.elements else {"error": {"code": 1, "message": "Not found"}}
                for g in guids(p["elements"])
            ]}
        if name == "MoveElements":
            results = []
            for item in p["elementsWithMoveVectors"]:
                guid = item["elementId"]["guid"]
                if guid in self.elements:
                    self.elements[guid]["moved"] = item["moveVector"]
                    results.append({"success": True})
                else:
                    results.append({"success": False, "error": {"code": 1, "message": "Not found"}})
            return {"executionResults": results}
        if name == "RotateElements":
            return {"executionResults": [{"success": True} for _ in p["elementsWithRotations"]]}
        if name == "DeleteElements":
            results = []
            for guid in guids(p["elements"]):
                results.append({"success": self.elements.pop(guid, None) is not None})
            return {"executionResults": results}
        if name == "GetAllProperties":
            return {"properties": PROPERTIES}
        if name == "GetPropertyValuesOfElements":
            prop_ids = [x["propertyId"]["guid"] for x in p["properties"]]
            out = []
            for guid in guids(p["elements"]):
                if guid not in self.elements:
                    out.append({"error": {"code": 1, "message": "Not found"}})
                    continue
                out.append({"propertyValues": [
                    {"propertyValue": {"value": self.properties.get((guid, pid), "")}} for pid in prop_ids
                ]})
            return {"propertyValuesForElements": out}
        if name == "SetPropertyValuesOfElements":
            for v in p["elementPropertyValues"]:
                self.properties[(v["elementId"]["guid"], v["propertyId"]["guid"])] = v["propertyValue"]["value"]
            return {"executionResults": [{"success": True} for _ in p["elementPropertyValues"]]}
        if name == "GetQuantitiesOfElements":
            return {"quantitiesOfElements": [{"type": "Wall", "quantities": {"length": 5.0}} for _ in p["elements"]]}
        if name == "ChangeSelectionOfElements":
            for guid in guids(p.get("removeElementsFromSelection", [])):
                if guid in self.selection:
                    self.selection.remove(guid)
            for guid in guids(p.get("addElementsToSelection", [])):
                if guid not in self.selection:
                    self.selection.append(guid)
            return {"executionResultsOfAddToSelection": [], "executionResultsOfRemoveFromSelection": []}
        if name == "HighlightElements":
            return {"success": True}
        if name == "GetAvailableLibraryParts":
            return {"skippedCount": 0, "libraryParts": [
                {"guid": "L1", "index": 1, "documentName": "Chair 01", "typeId": "Object"},
                {"guid": "L2", "index": 2, "documentName": "Table Round", "typeId": "Object"},
                {"guid": "L3", "index": 3, "documentName": "Door Single", "typeId": "Door"},
                {"guid": "L4", "index": 4, "documentName": "Window Casement", "typeId": "Window"},
                {"guid": "L5", "index": 5, "documentName": "Zone Stamp", "typeId": "Room"},
            ]}
        if name == "ShowScriptUI":
            self.palettes.append(p)
            return {"success": True}
        if name == "SaveProject":
            return {"success": True}
        if name == "GetCurrentViewImage" and self.view_image:
            return {"image": base64.b64encode(PNG_BYTES).decode(), "width": 1, "height": 1}
        if name == "SaveCurrentWindowAsImage":
            with open(p["imageFilePath"], "wb") as file:
                file.write(PNG_BYTES)
            return {"imageFilePath": p["imageFilePath"]}
        return {"error": {"code": 404, "message": f"Unknown Tapir command {name}"}}
