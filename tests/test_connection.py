from archicad_mcp.connection import ArchicadConnection
from fake_archicad import FakeArchicad


def namespaces_used(fake):
    return {r["parameters"]["addOnCommandId"]["commandNamespace"] for r in fake.requests
            if r["command"] == "API.ExecuteAddOnCommand"}


def test_prefers_the_tevavid_add_on_and_falls_back_to_tapir():
    with FakeArchicad() as fake:
        fake.namespaces = {"TevavidCommand", "TapirCommand"}
        connection = ArchicadConnection(port=fake.port)
        assert connection.tapir("GetAddOnVersion")["version"]
        assert namespaces_used(fake) == {"TevavidCommand"} and connection.is_tapir_available()

    with FakeArchicad() as fake:  # only a plain Tapir
        connection = ArchicadConnection(port=fake.port)
        assert connection.tapir("GetAddOnVersion")["version"]
        assert namespaces_used(fake) == {"TapirCommand"}

    with FakeArchicad(tapir=False) as fake:  # neither: not remembered, so a later add-on is found
        connection = ArchicadConnection(port=fake.port)
        assert not connection.is_tapir_available()
        fake.tapir, fake.namespaces = True, {"TevavidCommand"}
        assert connection.is_tapir_available()
