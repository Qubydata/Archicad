"""The error the Model Import engine raises for anything the user should be told."""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError


class ModelImportError(ToolError):
    """A model could not be read, cleaned up or saved; the message says why and what to do.

    A ToolError, so the MCP server and the panel show the message as it is.
    """
