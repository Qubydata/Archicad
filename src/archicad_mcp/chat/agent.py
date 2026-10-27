"""The chat agent: Claude with the Archicad MCP tools, run in-process.

The agent lists the tools of the MCP server, hands them to Claude, and runs the
tool calls Claude makes through the same server, so the chat window and an
external MCP client (Claude Desktop, Claude Code, Cursor...) behave the same.
Each turn is reported as a stream of small event dicts for the user interface.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from ..server import INSTRUCTIONS
from .config import Settings
from .history import Conversation, ConversationStore, title_for

SYSTEM_PROMPT = (
    "You are Archicad MCP, an AI assistant built into Graphisoft Archicad. The user is an architect "
    "who types what they want in a chat panel next to their model, and you carry it out with the tools.\n\n"
    + INSTRUCTIONS
    + "\nChat style:\n"
    "- Keep replies short and plain. Say what you changed, with numbers (sizes, counts), not a tool log.\n"
    "- If a request is ambiguous in a way that changes the result (which story, which walls), ask one short question.\n"
    "- After creating or moving geometry, take a get_view_image picture when it helps confirm the result.\n"
)

# Models that accept the server-side refusal fallback.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"}
MAX_TOOL_ROUNDS = 40
MAX_TOOL_TEXT = 60_000
MAX_TRANSCRIPT_IMAGES = 6


def _tool_result_content(result: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Convert an MCP CallToolResult into Anthropic tool_result content and UI image events."""
    content: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    for item in result.content:
        if item.type == "text":
            text = item.text
            if len(text) > MAX_TOOL_TEXT:
                text = text[:MAX_TOOL_TEXT] + "\n[truncated: ask for fewer elements or fields]"
            content.append({"type": "text", "text": text})
        elif item.type == "image":
            content.append(
                {"type": "image", "source": {"type": "base64", "media_type": item.mime_type, "data": item.data}}
            )
            images.append({"type": "image", "mime": item.mime_type, "data": item.data})
    if not content:
        content.append({"type": "text", "text": "(no output)"})
    return content, images


def _blocks(content: list[Any]) -> list[Any]:
    """Assistant content as plain dicts (sent back unchanged, and saved with the conversation)."""
    return [block.to_dict(mode="json") if hasattr(block, "to_dict") else block for block in content]


def _summary(content: list[dict[str, Any]], limit: int = 240) -> str:
    text = " ".join(c["text"] for c in content if c["type"] == "text")
    try:
        text = json.dumps(json.loads(text), separators=(",", ":"))
    except ValueError:
        pass
    return text if len(text) <= limit else text[:limit] + "..."


class ChatAgent:
    def __init__(self, server: MCPServer, settings: Settings, store: ConversationStore | None = None) -> None:
        self.server = server
        self.settings = settings
        self.store = store
        self.conversation = Conversation()
        self.stop_requested = False
        self.busy = False
        self._tools: list[dict[str, Any]] | None = None

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self.conversation.messages

    @property
    def transcript(self) -> list[dict[str, Any]]:
        return self.conversation.transcript

    def reset(self) -> None:
        """Start a new conversation; the previous one stays under Recent."""
        self.conversation = Conversation()

    def open(self, conversation_id: str) -> None:
        if self.store is None:
            raise KeyError(conversation_id)
        self.conversation = self.store.load(conversation_id)

    def _record(self, event: dict[str, Any]) -> None:
        """Keep what the panel shows, so a reopened panel can redraw the conversation."""
        kind = event["type"]
        last = self.transcript[-1] if self.transcript else None
        if kind == "text":
            if last and last["type"] == "assistant":
                last["text"] += event["text"]
            else:
                self.transcript.append({"type": "assistant", "text": event["text"]})
        elif kind == "tool_start":
            self.transcript.append({"type": "tool", "id": event["id"], "name": event["name"], "input": event["input"]})
        elif kind == "tool_end":
            for item in reversed(self.transcript):
                if item["type"] == "tool" and item["id"] == event["id"]:
                    item.update(ok=event["ok"], summary=event["summary"])
                    break
        elif kind == "image":
            self.transcript.append(dict(event))
            images = [i for i, item in enumerate(self.transcript) if item["type"] == "image"]
            for index in reversed(images[:-MAX_TRANSCRIPT_IMAGES]):
                del self.transcript[index]
        elif kind in ("error", "stopped"):
            self.transcript.append({"type": "error", "text": event.get("message", "Stopped.")})

    async def send(self, text: str) -> AsyncIterator[dict[str, Any]]:
        """Run one user turn, yielding UI events until Claude has answered."""
        conversation = self.conversation
        if not conversation.title:
            conversation.title = title_for(text)
        self.transcript.append({"type": "user", "text": text})
        try:
            async for event in self._turn(text):
                self._record(event)
                yield event
        finally:
            conversation.updated = time.time()
            if self.store is not None:
                self.store.save(conversation)

    async def tools(self) -> list[dict[str, Any]]:
        if self._tools is None:
            self._tools = [
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.input_schema,
                    "eager_input_streaming": True,
                }
                for tool in await self.server.list_tools()
            ]
        return self._tools

    def _client(self) -> anthropic.AsyncAnthropic:
        key = self.settings.resolved_api_key
        return anthropic.AsyncAnthropic(api_key=key) if key else anthropic.AsyncAnthropic()

    def _request(self, tools: list[dict[str, Any]]) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": 64000,
            "system": SYSTEM_PROMPT,
            "tools": tools,
            "messages": self.messages,
            "output_config": {"effort": self.settings.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if self.settings.model in FALLBACK_MODELS:
            request["betas"] = ["server-side-fallback-2026-07-01"]
            request["fallbacks"] = "default"
        return request

    async def _turn(self, text: str) -> AsyncIterator[dict[str, Any]]:
        self.stop_requested = False
        self.messages.append({"role": "user", "content": text})
        client = self._client()
        tools = await self.tools()
        json_retries = 0

        for _ in range(MAX_TOOL_ROUNDS):
            try:
                async with client.beta.messages.stream(**self._request(tools)) as stream:
                    async for event in stream:
                        if self.stop_requested:
                            break
                        if event.type == "text":
                            yield {"type": "text", "text": event.text}
                    if self.stop_requested:
                        yield {"type": "stopped"}
                        return
                    response = await stream.get_final_message()
                json_retries = 0
            except ValueError:
                # A tool input the SDK could not parse at all; ask for the turn again.
                json_retries += 1
                if json_retries > 2:
                    yield {"type": "error", "message": "Claude sent a tool call that could not be read. Please try again."}
                    return
                continue

            if response.stop_reason == "pause_turn":
                self.messages.append({"role": "assistant", "content": _blocks(response.content)})
                continue
            if response.stop_reason == "refusal":
                yield {"type": "error", "message": "Claude declined this request. Try rephrasing it."}
                return

            tool_uses = [block for block in response.content if block.type == "tool_use"]
            if response.stop_reason == "max_tokens" and tool_uses:
                yield {"type": "error", "message": "The answer got too long. Try asking for a smaller step."}
                return
            self.messages.append({"role": "assistant", "content": _blocks(response.content)})
            if not tool_uses:
                return

            results = []
            for block in tool_uses:
                if self.stop_requested:
                    results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                         "content": "Stopped by the user before this ran."}
                    )
                    continue
                yield {"type": "tool_start", "id": block.id, "name": block.name, "input": block.input}
                if not isinstance(block.input, dict):
                    content = [{"type": "text", "text": "Invalid tool input: expected a JSON object."}]
                    is_error, images = True, []
                else:
                    try:
                        result = await self.server.call_tool(block.name, block.input)
                        content, images = _tool_result_content(result)
                        is_error = bool(result.is_error)
                    except ToolError as exc:
                        content = [{"type": "text", "text": str(exc)}]
                        is_error, images = True, []
                for image in images:
                    yield image
                yield {"type": "tool_end", "id": block.id, "ok": not is_error, "summary": _summary(content)}
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error})
            self.messages.append({"role": "user", "content": results})
            if self.stop_requested:
                yield {"type": "stopped"}
                return

        yield {"type": "error", "message": "Stopped after many steps. Say 'continue' to keep going."}
