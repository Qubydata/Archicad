"""Start an installed archicad-mcp-server the way Claude Desktop does and check it answers.

    python packaging/smoke_test.py <path to archicad-mcp-server>
"""

import sys

import anyio
from mcp import Client, StdioServerParameters


async def main(command: str) -> None:
    async with Client(StdioServerParameters(command=command, args=[])) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert {"create_walls", "run_tapir_command", "get_view_image"} <= tools, tools
        result = await client.call_tool("search_tapir_commands", {"query": "create wall", "limit": 3})
        names = [c["name"] for c in result.structured_content["commands"]]
        assert names[0] == "CreateWalls", names
        print(f"{client.server_info.name}: {len(tools)} tools, catalog search works")


anyio.run(main, sys.argv[1])
