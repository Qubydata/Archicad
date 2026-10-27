# Archicad MCP

An AI assistant for Graphisoft Archicad. You chat with it in a panel inside Archicad
("build a 6 x 4 m room with 3 m walls", "add a window to the selected wall", "list the
doors with their sizes") and it changes the open model for you. It is also an
[MCP](https://modelcontextprotocol.io) server, so Claude Desktop, Claude Code, Cursor and other
MCP clients can operate Archicad, the same way Blender MCP and the Revit MCP servers work
for those programs.

## For users: install

1. Download the installer from the [releases page](../../releases):
   `ArchicadMCPSetup.exe` (Windows) or `ArchicadMCPSetup-macOS.zip` (macOS).
2. Run it and click **Next**, pick your Archicad versions, then **Install**.
   The setup installs everything Archicad MCP needs:
   - the Archicad MCP app,
   - the free [Tapir add-on](https://github.com/ENZYME-APD/tapir-archicad-automation), which lets
     the assistant create and edit elements (Windows asks for administrator permission for this step),
   - the **MCP** panel inside Archicad,
   - the `archicad` server in Claude Desktop, if Claude Desktop is installed,
   - optionally the background app, which opens the panel whenever Archicad starts.
3. Start Archicad and open a project. The **MCP** panel appears next to the model.
4. Archicad MCP is free and works with your own Claude, in either of two ways:
   - **With a Claude plan (Pro, Max, Team):** use it from Claude Desktop, at no extra cost. The
     setup connects it (restart Claude Desktop once); the panel's Settings tab can also connect it.
   - **In the panel inside Archicad:** paste an Anthropic API key from
     [console.anthropic.com](https://console.anthropic.com/settings/keys) in the Settings tab.
     Anthropic bills the usage, pay as you go. (Claude plans can only be used from Anthropic's own
     apps, which is why the panel needs an API key.)

Every change is a normal Archicad change, so Undo works. Uninstall from Windows Settings >
Apps, or run `ArchicadMCP uninstall` on macOS. Settings and conversations are kept in
`~/.archicad-mcp`. The Tapir add-on stays installed, since other tools use it.

For IT deployment: `ArchicadMCPSetup.exe --silent [--no-addon] [--no-desktop-app]`.

## How it works

```
 Archicad                                   Archicad MCP
 +--------------------------+               +-----------------------------------------+
 | JSON API (port 19723)    | <-- HTTP ---> | MCP server: 31 tools                     |
 | Tapir add-on (~430 cmds) |               |   curated: create_walls, get_elements... |
 | MCP palette (Tapir UI)   | <-- local --> |   generic: run_tapir_command + catalog   |
 +--------------------------+     web       | Chat service: Claude + the same tools    |
                                            +-----------------------------------------+
                                               ^ stdio (MCP)            ^ Anthropic API
                                       Claude Desktop, Cursor...     the panel's chat
```

- Archicad 25 and later runs a JSON API on `localhost:19723` while it is open. Read access is
  built in. The Tapir add-on adds about 430 more commands, including element creation, editing,
  layers, attributes, dimensions, layouts, IFC and MCP, under `API.ExecuteAddOnCommand`.
- `archicad_mcp.server` exposes that as MCP tools. The everyday tasks have small typed tools
  (`create_walls`, `create_slabs`, `create_windows`, `move_elements`, `get_element_properties`,
  `get_view_image`, ...). Everything else goes through `search_tapir_commands`,
  `get_tapir_command_schema` and `run_tapir_command`, backed by a bundled copy of Tapir's command
  schemas, so the model can use any Tapir command with the right parameters. This plays the role
  of `execute_blender_code` in Blender MCP.
- `archicad_mcp.chat` is the panel: a local web app (127.0.0.1 only, with a per-session token)
  shown inside Archicad through Tapir's `ShowScriptUI` palette. It runs Claude with the same MCP
  tools in-process and keeps conversations under **Recent**.
- `archicad_mcp.installer` is the setup wizard (pywebview) and its install engine. Each change it
  makes is recorded in a manifest, so cancel, failure and uninstall undo exactly what was done.

## For developers

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --extra installer
uv run pytest                      # 38 tests; a fake Archicad stands in for the real one
uv run archicad-mcp                # the MCP server on stdio
uv run archicad-mcp-panel --browser   # the chat panel, in a browser
uv run python -m archicad_mcp.launcher setup   # the setup wizard
```

Use the server from Claude Desktop without the installer (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "archicad": { "command": "uv", "args": ["--directory", "/path/to/Archicad", "run", "archicad-mcp"] }
  }
}
```

Or from Claude Code: `claude mcp add archicad -- uv --directory /path/to/Archicad run archicad-mcp`.

Environment variables: `ARCHICAD_PORT` (skip discovery), `ARCHICAD_HOST`, `ARCHICAD_TIMEOUT`
(seconds, default 120), `ANTHROPIC_API_KEY` (used by the panel when no key is saved),
`ARCHICAD_MCP_HOME` (settings folder, default `~/.archicad-mcp`).

### Building the installers

`uv run python packaging/build.py` builds the installer for the platform it runs on. The
[Build installers](.github/workflows/build-installers.yml) workflow builds the Windows and macOS
installers on GitHub's runners, installs each one silently, starts the installed MCP server and
uninstalls again. Pushing a `v*` tag publishes them as a GitHub release.

Before a public release:
- Sign the Windows installer (Authenticode) so SmartScreen does not warn, and sign and notarize
  the macOS apps with an Apple Developer ID.
- Consider your own product name. "Archicad" is a Graphisoft trademark.

### Updating the Tapir command catalog

`python scripts/sync_tapir_catalog.py [--ref <tag or branch>]` refreshes
`src/archicad_mcp/data/tapir_catalog.json` from the Tapir repository.

## Repository layout

```
src/archicad_mcp/
  server.py, connection.py, catalog.py   MCP server, Archicad JSON API client, Tapir catalog
  chat/                                  panel: agent, web service, conversations, static/index.html
  installer/                             setup wizard (ui/*.html), install engine, OS integration
  launcher.py                            entry point of the packaged apps
packaging/                               PyInstaller specs, build and smoke test scripts, icons
tests/                                   tests and the fake Archicad
```

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for the licenses of bundled material.
