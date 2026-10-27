import json
from types import SimpleNamespace

import anyio
import pytest
from starlette.testclient import TestClient

from archicad_mcp import server
from archicad_mcp.chat import agent as agent_module
from archicad_mcp.chat import config, web
from archicad_mcp.chat.agent import ChatAgent
from fake_archicad import FakeArchicad


class FakeStream:
    def __init__(self, message):
        self.message = message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def events():
            for block in self.message.content:
                if block.type == "text":
                    yield SimpleNamespace(type="text", text=block.text)
        return events()

    async def get_final_message(self):
        return self.message


class FakeClaude:
    """Plays back scripted assistant turns and records each request."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})
        return FakeStream(self.turns.pop(0))


def text(value):
    return SimpleNamespace(type="text", text=value)


def tool_use(id, name, input):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def turn(stop_reason, *content):
    return SimpleNamespace(stop_reason=stop_reason, content=list(content))


@pytest.fixture
def fake(monkeypatch):
    with FakeArchicad() as fake:
        monkeypatch.setattr(server.archicad, "_port", fake.port)
        yield fake


@pytest.fixture(autouse=True)
def config_home(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def run_turn(chat, message):
    async def collect():
        return [event async for event in chat.send(message)]

    return anyio.run(collect)


def test_agent_runs_tools_and_answers(fake, monkeypatch):
    claude = FakeClaude([
        turn("tool_use", text("Building it."), tool_use("t1", "create_walls", {"walls": [
            {"begCoordinate": {"x": 0, "y": 0}, "endCoordinate": {"x": 6, "y": 0}, "height": 3},
        ]})),
        turn("end_turn", text("Done: one 6 m wall.")),
    ])
    chat = ChatAgent(server.mcp, config.Settings(api_key="k"))
    monkeypatch.setattr(chat, "_client", lambda: claude)

    events = run_turn(chat, "add a wall")
    kinds = [e["type"] for e in events]
    assert kinds == ["text", "tool_start", "tool_end", "text"]
    assert events[2]["ok"] is True and "created" in events[2]["summary"]
    assert len(fake.elements) == 1

    first = claude.requests[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["output_config"] == {"effort": "medium"}
    assert all(t["eager_input_streaming"] for t in first["tools"])
    assert {"create_walls", "run_tapir_command"} <= {t["name"] for t in first["tools"]}

    # The second request carries the tool result for the first tool call.
    result = claude.requests[1]["messages"][-1]["content"][0]
    assert result["tool_use_id"] == "t1" and result["is_error"] is False
    assert [m["role"] for m in chat.messages] == ["user", "assistant", "user", "assistant"]


def test_agent_reports_tool_errors_and_images(fake, monkeypatch):
    claude = FakeClaude([
        turn("tool_use", tool_use("t1", "create_walls", {"walls": "nope"}), tool_use("t2", "get_view_image", {})),
        turn("end_turn", text("Here is the view.")),
    ])
    chat = ChatAgent(server.mcp, config.Settings(api_key="k", model="claude-haiku-5-5"))
    monkeypatch.setattr(chat, "_client", lambda: claude)

    events = run_turn(chat, "show me")
    ends = [e for e in events if e["type"] == "tool_end"]
    assert ends[0]["ok"] is False and ends[1]["ok"] is True
    assert any(e["type"] == "image" for e in events)
    results = claude.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]  # all results in one user message
    assert results[1]["content"][0]["type"] == "image"
    assert "fallbacks" not in claude.requests[0]  # no server-side fallback for Haiku


def test_agent_handles_refusal(monkeypatch):
    chat = ChatAgent(server.mcp, config.Settings(api_key="k"))
    monkeypatch.setattr(chat, "_client", lambda: FakeClaude([turn("refusal")]))
    events = run_turn(chat, "something")
    assert events == [{"type": "error", "message": "Claude declined this request. Try rephrasing it."}]


def test_long_tool_output_is_truncated():
    result = SimpleNamespace(content=[SimpleNamespace(type="text", text="x" * (agent_module.MAX_TOOL_TEXT + 10))])
    content, images = agent_module._tool_result_content(result)
    assert content[0]["text"].endswith("fewer elements or fields]") and images == []


def test_settings_round_trip():
    config.save(config.Settings(api_key="sk-test", model="claude-sonnet-5-5", effort="high"))
    loaded = config.load()
    assert (loaded.api_key, loaded.model, loaded.effort) == ("sk-test", "claude-sonnet-5-5", "high")
    config.CONFIG_FILE.write_text(json.dumps({"model": "gpt-9", "effort": "extreme"}))
    assert (config.load().model, config.load().effort) == ("claude-opus-5-5", "medium")


def make_client(port=8765):
    app = web.create_app(port, token="secret")
    return app, TestClient(app, base_url=f"http://127.0.0.1:{port}")


def test_web_requires_token_and_loopback_host(fake):
    app, client = make_client()
    assert client.get("/").status_code == 200
    assert client.get("/api/status").status_code == 403
    assert client.get("/api/status", headers={"x-archicad-mcp-token": "wrong"}).status_code == 403
    evil = TestClient(app, base_url="http://evil.example:8765")
    assert evil.get("/", headers={"x-archicad-mcp-token": "secret"}).status_code == 403

    status = client.get("/api/status", headers={"x-archicad-mcp-token": "secret"}).json()
    assert status["archicad"]["connected"] is True and status["archicad"]["project"] == "Test House"
    assert status["settings"]["hasKey"] is False


def test_web_settings_and_chat_stream(fake, monkeypatch):
    app, client = make_client()
    headers = {"x-archicad-mcp-token": "secret"}
    client.post("/api/settings", json={"apiKey": "sk-abc", "model": "claude-sonnet-5-5"}, headers=headers)
    assert config.load().api_key == "sk-abc"
    assert client.get("/api/status", headers=headers).json()["settings"]["model"] == "claude-sonnet-5-5"

    claude = FakeClaude([turn("end_turn", text("Hello from Claude"))])
    monkeypatch.setattr(app.state.agent, "_client", lambda: claude)
    response = client.post("/api/chat", json={"message": "hi"}, headers=headers)
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events == [{"type": "text", "text": "Hello from Claude"}, {"type": "done"}]
    assert client.post("/api/reset", json={}, headers=headers).json() == {"ok": True}
    assert app.state.agent.messages == []


def test_web_reports_api_errors(fake, monkeypatch):
    app, client = make_client()

    def broken():
        raise RuntimeError("boom")

    monkeypatch.setattr(app.state.agent, "_client", broken)
    response = client.post("/api/chat", json={"message": "hi"}, headers={"x-archicad-mcp-token": "secret"})
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[0]["type"] == "error" and "boom" in events[0]["message"]
    assert events[-1] == {"type": "done"}


def sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def message_events(stop_reason, blocks):
    events = [{"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}}]
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            events.append({"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}})
            events.append({"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": block["text"]}})
        else:
            events.append({"type": "content_block_start", "index": index, "content_block": {
                "type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}})
            events.append({"type": "content_block_delta", "index": index, "delta": {
                "type": "input_json_delta", "partial_json": json.dumps(block["input"])}})
        events.append({"type": "content_block_stop", "index": index})
    events.append({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                   "usage": {"output_tokens": 5}})
    events.append({"type": "message_stop"})
    return sse(events)


def test_agent_with_real_sdk_against_fake_api(fake, monkeypatch):
    """Runs the real Anthropic SDK streaming code against a scripted HTTP transport."""
    import anthropic
    import httpx2

    bodies = []
    replies = [
        message_events("tool_use", [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "toolu_1", "name": "get_stories", "input": {}},
        ]),
        message_events("end_turn", [{"type": "text", "text": "Two stories."}]),
    ]

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, content=replies.pop(0), headers={"content-type": "text/event-stream"})

    client = anthropic.AsyncAnthropic(
        api_key="test", http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    )
    chat = ChatAgent(server.mcp, config.Settings(api_key="k"))
    monkeypatch.setattr(chat, "_client", lambda: client)

    events = run_turn(chat, "how many stories?")
    assert [e["type"] for e in events] == ["text", "tool_start", "tool_end", "text"]
    first, second = bodies
    assert first["fallbacks"] == "default" and first["stream"] is True
    assert first["cache_control"] == {"type": "ephemeral"}
    # The assistant turn is sent back as the API returned it, followed by the tool result.
    assert second["messages"][1]["content"][1] == {"type": "tool_use", "id": "toolu_1", "name": "get_stories", "input": {}}
    assert second["messages"][2]["content"][0]["tool_use_id"] == "toolu_1"
