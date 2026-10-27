"""The panel's sign-in and subscription flow against the real cloud service, run in-process."""

import json
import re

import anthropic
import httpx
import httpx2
import pytest
from starlette.testclient import TestClient

from archicad_mcp import server
from archicad_mcp.chat import cloud as cloud_client
from archicad_mcp.chat import config, web
from fake_archicad import FakeArchicad
from test_cloud import cloud, send_event  # noqa: F401  (the cloud fixture)

HEADERS = {"x-archicad-mcp-token": "secret"}


@pytest.fixture
def panel(cloud, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "home")
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "home" / "config.json")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ARCHICAD_MCP_CLOUD_URL", "https://cloud.test")
    monkeypatch.setattr(cloud_client, "TRANSPORT", httpx.ASGITransport(app=cloud.app_))
    opened = []
    monkeypatch.setattr(web.webbrowser, "open", opened.append)
    with FakeArchicad() as fake:
        monkeypatch.setattr(server.archicad, "_port", fake.port)
        app = web.create_app(8765, token="secret")
        app.state.agent.http_client = anthropic.DefaultAsyncHttpxClient(transport=httpx2.ASGITransport(app=cloud.app_))
        client = TestClient(app, base_url="http://127.0.0.1:8765")
        client.opened = opened
        client.app_ = app
        yield client


def chat(panel, text):
    response = panel.post("/api/chat", json={"message": text}, headers=HEADERS)
    return [json.loads(line) for line in response.text.splitlines()]


def test_sign_in_subscribe_and_chat_through_the_service(panel, cloud):  # noqa: F811
    status = panel.get("/api/status", headers=HEADERS).json()["settings"]
    assert status["cloudAvailable"] is True and status["ready"] is False
    assert panel.get("/api/account", headers=HEADERS).json() == {"available": True, "signedIn": False}

    assert panel.post("/api/account/start", json={"email": "anna@studio.com"}, headers=HEADERS).json() == {"ok": True}
    code = re.search(r"\b(\d{6})\b", cloud.fakes.mails[-1]["text"]).group(1)
    bad = panel.post("/api/account/verify", json={"email": "anna@studio.com", "code": "x"}, headers=HEADERS)
    assert bad.status_code == 400 and "not right" in bad.json()["error"]
    signed = panel.post("/api/account/verify", json={"email": "anna@studio.com", "code": code}, headers=HEADERS).json()
    assert signed["ok"] is True
    saved = config.load()
    assert saved.account_email == "anna@studio.com" and saved.account_token.startswith("amc_")

    status = panel.get("/api/status", headers=HEADERS).json()["settings"]
    assert status["ready"] is True and status["subscription"] is True

    # Signed in, not subscribed yet: the chat explains what to do.
    events = chat(panel, "hello")
    assert events[0]["type"] == "error" and "subscription" in events[0]["message"] and "Settings" in events[0]["message"]

    # Subscribe: checkout opens in the browser; Stripe then reports the subscription.
    assert panel.post("/api/account/subscribe", json={}, headers=HEADERS).json()["ok"] is True
    assert panel.opened == ["https://checkout.stripe.com/c/pay/cs_1"]
    send_event(cloud, "customer.subscription.created", {
        "id": "sub_1", "customer": "cus_123", "status": "trialing", "cancel_at_period_end": False,
        "current_period_start": 1, "current_period_end": 9_999_999_999,
    })
    account = panel.get("/api/account", headers=HEADERS).json()
    assert account["signedIn"] and account["status"] == "trialing" and account["allowed"] is True

    # Chatting now streams Claude's answer through the service, which meters it.
    events = chat(panel, "hello again")
    assert {"type": "text", "text": "Hello"} in events and events[-1] == {"type": "done"}
    upstream, sent = cloud.fakes.anthropic_calls[-1]
    assert upstream.headers["x-api-key"] == "sk-ant-server"
    assert sent["fallbacks"] == "default" and sent["tools"]  # the panel's request arrives intact
    assert panel.get("/api/account", headers=HEADERS).json()["usage"]["usedUsd"] > 0

    assert panel.post("/api/account/manage", json={}, headers=HEADERS).json()["ok"] is True
    assert panel.opened[-1].startswith("https://billing.stripe.com/")

    panel.post("/api/account/signout", json={}, headers=HEADERS)
    assert config.load().account_token == ""
    assert panel.get("/api/account", headers=HEADERS).json()["signedIn"] is False


def test_revoked_device_is_signed_out(panel, cloud):  # noqa: F811
    settings = panel.app_.state.settings
    settings.account_token, settings.account_email = "amc_revoked", "x@y.z"
    answer = panel.get("/api/account", headers=HEADERS).json()
    assert answer["signedIn"] is False and "signed out" in answer["notice"]
    assert settings.account_token == ""


def test_without_a_service_only_api_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.delenv("ARCHICAD_MCP_CLOUD_URL", raising=False)
    client = TestClient(web.create_app(8765, token="secret"), base_url="http://127.0.0.1:8765")
    assert client.get("/api/account", headers=HEADERS).json() == {"available": False}
