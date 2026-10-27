import json
import re
import time
from urllib.parse import parse_qsl

import anyio
import httpx
import pytest
from starlette.testclient import TestClient

from archicad_mcp_cloud import pricing
from archicad_mcp_cloud.app import create_app
from archicad_mcp_cloud.config import CloudConfig
from archicad_mcp_cloud.stripe_api import StripeError, sign_webhook, verify_webhook

WEBHOOK_SECRET = "whsec_test"


def sse_reply(model="claude-opus-5-5", text="Hello", input_tokens=1000, output_tokens=200):
    events = [
        {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": model,
                                              "content": [], "stop_reason": None, "stop_sequence": None,
                                              "usage": {"input_tokens": input_tokens, "output_tokens": 1,
                                                        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": output_tokens}},
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class Fakes:
    def __init__(self):
        self.mails = []
        self.stripe_calls = []
        self.anthropic_calls = []

    def mail(self, request):
        self.mails.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "email_1"})

    def stripe(self, request):
        form = dict(parse_qsl(request.content.decode()))
        self.stripe_calls.append((request.url.path, form, request.headers["authorization"]))
        if request.url.path == "/v1/customers":
            return httpx.Response(200, json={"id": "cus_123"})
        if request.url.path == "/v1/checkout/sessions":
            return httpx.Response(200, json={"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/cs_1"})
        if request.url.path == "/v1/billing_portal/sessions":
            return httpx.Response(200, json={"url": "https://billing.stripe.com/p/session/1"})
        return httpx.Response(404, json={"error": {"message": "unknown"}})

    def anthropic(self, request):
        body = json.loads(request.content)
        self.anthropic_calls.append((request, body))
        if body.get("stream"):
            return httpx.Response(200, content=sse_reply(body["model"]), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={"id": "msg_2", "type": "message", "role": "assistant", "model": body["model"],
                                         "content": [{"type": "text", "text": "Hi"}], "stop_reason": "end_turn",
                                         "usage": {"input_tokens": 100, "output_tokens": 10}})


@pytest.fixture
def cloud(tmp_path):
    fakes = Fakes()
    config = CloudConfig(
        public_url="https://cloud.test", database_path=str(tmp_path / "cloud.db"), secret_key="s3cret",
        anthropic_api_key="sk-ant-server", stripe_secret_key="sk_test_1", stripe_webhook_secret=WEBHOOK_SECRET,
        stripe_price_id="price_pro", email_provider="resend", resend_api_key="re_1",
        monthly_allowance_usd=20, trial_allowance_usd=3, trial_days=7,
    )
    app = create_app(config, httpx.MockTransport(fakes.anthropic), httpx.MockTransport(fakes.stripe),
                     httpx.MockTransport(fakes.mail))
    client = TestClient(app, base_url="https://cloud.test")
    client.fakes = fakes
    client.app_ = app
    return client


def sign_in(cloud, email="anna@studio.com"):
    assert cloud.post("/v1/auth/start", json={"email": email}).json() == {"ok": True}
    code = re.search(r"\b(\d{6})\b", cloud.fakes.mails[-1]["text"]).group(1)
    answer = cloud.post("/v1/auth/verify", json={"email": email, "code": code, "device": "Test PC"}).json()
    return answer["token"], answer["account"]


def send_event(cloud, kind, obj, event_id=None):
    payload = json.dumps({"id": event_id or f"evt_{time.time_ns()}", "type": kind, "data": {"object": obj}}).encode()
    return cloud.post("/v1/stripe/webhook", content=payload, headers={"stripe-signature": sign_webhook(payload, WEBHOOK_SECRET)})


def subscribe(cloud, token, status="active"):
    cloud.post("/v1/billing/checkout", headers={"x-api-key": token})
    now = int(time.time())
    return send_event(cloud, "customer.subscription.created", {
        "id": "sub_1", "customer": "cus_123", "status": status, "cancel_at_period_end": False,
        "items": {"data": [{"current_period_start": now - 60, "current_period_end": now + 30 * 86400}]},
    })


def test_sign_in_with_email_code(cloud):
    assert cloud.post("/v1/auth/start", json={"email": "not-an-email"}).status_code == 400
    assert cloud.post("/v1/auth/start", json={"email": "Anna@Studio.com"}).json() == {"ok": True}
    mail = cloud.fakes.mails[0]
    assert mail["to"] == ["anna@studio.com"] and "sign-in code" in mail["subject"]
    code = re.search(r"\b(\d{6})\b", mail["text"]).group(1)

    # A second request right away does not send another code.
    assert "just sent" in cloud.post("/v1/auth/start", json={"email": "anna@studio.com"}).json()["message"]
    assert len(cloud.fakes.mails) == 1

    wrong = "000000" if code != "000000" else "111111"
    assert cloud.post("/v1/auth/verify", json={"email": "anna@studio.com", "code": wrong}).status_code == 400
    answer = cloud.post("/v1/auth/verify", json={"email": "anna@studio.com", "code": code}).json()
    assert answer["token"].startswith("amc_")
    assert answer["account"]["status"] == "none" and answer["account"]["allowed"] is False
    # A code works once.
    assert cloud.post("/v1/auth/verify", json={"email": "anna@studio.com", "code": code}).status_code == 400

    me = cloud.get("/v1/me", headers={"x-api-key": answer["token"]}).json()
    assert me["email"] == "anna@studio.com" and me["billing"] is True
    cloud.post("/v1/auth/logout", headers={"x-api-key": answer["token"]})
    assert cloud.get("/v1/me", headers={"x-api-key": answer["token"]}).status_code == 401


def test_code_attempts_are_limited(cloud):
    cloud.post("/v1/auth/start", json={"email": "bob@studio.com"})
    code = re.search(r"\b(\d{6})\b", cloud.fakes.mails[-1]["text"]).group(1)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        cloud.post("/v1/auth/verify", json={"email": "bob@studio.com", "code": wrong})
    assert cloud.post("/v1/auth/verify", json={"email": "bob@studio.com", "code": code}).status_code == 400


def test_checkout_portal_and_webhooks(cloud):
    token, _ = sign_in(cloud)
    headers = {"x-api-key": token}
    assert cloud.post("/v1/billing/portal", headers=headers).status_code == 400  # nothing to manage yet

    url = cloud.post("/v1/billing/checkout", headers=headers).json()["url"]
    assert url.startswith("https://checkout.stripe.com/")
    path, form, auth = cloud.fakes.stripe_calls[1]
    assert path == "/v1/checkout/sessions" and auth == "Bearer sk_test_1"
    assert form["mode"] == "subscription" and form["customer"] == "cus_123"
    assert form["line_items[0][price]"] == "price_pro" and form["subscription_data[trial_period_days]"] == "7"
    assert form["success_url"] == "https://cloud.test/billing/success"

    cloud.post("/v1/billing/checkout", headers=headers)
    assert [c[0] for c in cloud.fakes.stripe_calls].count("/v1/customers") == 1  # customer made once

    # Signature checks.
    payload = b'{"id": "evt_x", "type": "customer.subscription.updated", "data": {"object": {}}}'
    assert cloud.post("/v1/stripe/webhook", content=payload, headers={"stripe-signature": "t=1,v1=bad"}).status_code == 400

    now = int(time.time())
    sub = {"id": "sub_1", "customer": "cus_123", "status": "trialing", "cancel_at_period_end": False,
           "items": {"data": [{"current_period_start": now, "current_period_end": now + 7 * 86400}]}}
    assert send_event(cloud, "customer.subscription.created", sub, "evt_1").json() == {"ok": True}
    me = cloud.get("/v1/me", headers=headers).json()
    assert me["status"] == "trialing" and me["allowed"] is True and me["usage"]["limitUsd"] == 3
    assert send_event(cloud, "customer.subscription.created", sub, "evt_1").json()["duplicate"] is True

    send_event(cloud, "customer.subscription.updated", {**sub, "status": "active", "current_period_end": now + 30 * 86400,
                                                        "current_period_start": now})
    me = cloud.get("/v1/me", headers=headers).json()
    assert me["status"] == "active" and me["usage"]["limitUsd"] == 20
    assert cloud.post("/v1/billing/portal", headers=headers).json()["url"].startswith("https://billing.stripe.com/")

    send_event(cloud, "customer.subscription.deleted", {**sub, "status": "active"})
    me = cloud.get("/v1/me", headers=headers).json()
    assert me["status"] == "canceled" and me["allowed"] is False and "ended" in me["message"]

    # Coming back after cancelling: no second trial.
    cloud.post("/v1/billing/checkout", headers=headers)
    assert "subscription_data[trial_period_days]" not in cloud.fakes.stripe_calls[-1][1]


def test_proxy_requires_an_active_subscription(cloud):
    body = {"model": "claude-opus-5-5", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
    assert cloud.post("/anthropic/v1/messages", json=body).status_code == 401
    token, _ = sign_in(cloud)
    answer = cloud.post("/anthropic/v1/messages", json=body, headers={"x-api-key": token})
    assert answer.status_code == 402 and answer.json()["error"]["type"] == "billing_error"
    assert cloud.fakes.anthropic_calls == []


def test_proxy_streams_meters_and_enforces_the_allowance(cloud):
    token, _ = sign_in(cloud)
    subscribe(cloud, token)
    headers = {"x-api-key": token, "anthropic-version": "2023-06-01", "anthropic-beta": "server-side-fallback-2026-07-01"}
    body = {"model": "claude-opus-5-5", "max_tokens": 200000, "stream": True,
            "messages": [{"role": "user", "content": "hi"}], "tools": [{"name": "create_walls", "input_schema": {"type": "object"}}]}

    reply = cloud.post("/anthropic/v1/messages?beta=true", json=body, headers=headers)
    assert reply.status_code == 200 and reply.headers["content-type"].startswith("text/event-stream")
    assert reply.content == sse_reply()  # passed through byte for byte

    upstream, sent = cloud.fakes.anthropic_calls[0]
    assert upstream.headers["x-api-key"] == "sk-ant-server"  # the user's token never reaches Anthropic
    assert upstream.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    assert upstream.url.query == b"beta=true"
    assert sent["max_tokens"] == 64000  # capped

    me = cloud.get("/v1/me", headers={"x-api-key": token}).json()
    expected = pricing.cost_micros("claude-opus-5-5", {"input_tokens": 1000, "output_tokens": 200}) / 1e6
    assert me["usage"]["usedUsd"] == round(expected, 2) == 0.01

    # Non-streaming replies are metered too.
    cloud.post("/anthropic/v1/messages", json={**body, "stream": False}, headers=headers)
    db = cloud.app_.state.db
    user = db.user_by_email("anna@studio.com")
    assert db.used_micros(user.id, 0) == 8000 + int(round(100 * 4 + 10 * 20))

    # Use up the allowance: further requests are refused before reaching Anthropic.
    db.record_usage(user.id, "claude-opus-5-5", {"input_tokens": 0}, 20_000_000)
    refused = cloud.post("/anthropic/v1/messages", json=body, headers=headers)
    assert refused.status_code == 402 and "allowance is used up" in refused.json()["error"]["message"]
    assert len(cloud.fakes.anthropic_calls) == 2


def test_proxy_rejects_other_models_and_server_tools(cloud):
    token, _ = sign_in(cloud)
    subscribe(cloud, token)
    headers = {"x-api-key": token}
    base = {"max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
    assert cloud.post("/anthropic/v1/messages", json={**base, "model": "claude-fable-5-1"}, headers=headers).status_code == 400
    web = {**base, "model": "claude-opus-5-5", "tools": [{"type": "web_search_20260209", "name": "web_search"}]}
    assert cloud.post("/anthropic/v1/messages", json=web, headers=headers).status_code == 400
    assert cloud.post("/anthropic/v1/messages", json=["not", "an", "object"], headers=headers).status_code == 400
    assert cloud.post("/anthropic/v1/messages", json={**base, "model": "claude-opus-5-5", "max_tokens": "lots"},
                      headers=headers).status_code == 400
    assert cloud.fakes.anthropic_calls == []


def test_proxy_reports_an_unreachable_anthropic(cloud):
    def down(request):
        raise httpx.ConnectError("no route")

    app = create_app(cloud.app_.state.config, httpx.MockTransport(down))
    client = TestClient(app, base_url="https://cloud.test")
    token = app.state.db.create_device(app.state.db.get_or_create_user("ops@studio.com").id, "t")
    user = app.state.db.user_by_email("ops@studio.com")
    app.state.db.set_subscription(user.id, "sub", "active", time.time() - 10, time.time() + 86400, False)
    answer = client.post("/anthropic/v1/messages", headers={"x-api-key": token},
                         json={"model": "claude-opus-5-5", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]})
    assert answer.status_code == 502 and answer.json()["error"]["type"] == "api_error"


def test_real_sdk_through_the_proxy(cloud):
    """The desktop app's Anthropic SDK, pointed at the service, streams through it unchanged."""
    import anthropic
    import httpx2

    token, _ = sign_in(cloud)
    subscribe(cloud, token)
    sdk = anthropic.AsyncAnthropic(
        api_key=token, base_url="https://cloud.test/anthropic",
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.ASGITransport(app=cloud.app_)),
    )

    async def run():
        async with sdk.beta.messages.stream(model="claude-opus-5-5", max_tokens=100, betas=["server-side-fallback-2026-07-01"],
                                            fallbacks="default", messages=[{"role": "user", "content": "hi"}]) as stream:
            text = "".join([event.text async for event in stream if event.type == "text"])
            return text, await stream.get_final_message()

    text, message = anyio.run(run)
    assert text == "Hello" and message.usage.output_tokens == 200
    _, sent = cloud.fakes.anthropic_calls[-1]
    assert sent["fallbacks"] == "default"


def test_comped_access_and_admin_cli(cloud, capsys, monkeypatch, tmp_path):
    from archicad_mcp_cloud.__main__ import main

    monkeypatch.setenv("DATABASE_PATH", cloud.app_.state.config.database_path)
    monkeypatch.setenv("SECRET_KEY", "s3cret")
    assert main(["comp", "tester@studio.com", "14"]) == 0
    token, account = sign_in(cloud, "tester@studio.com")
    assert account["status"] == "comped" and account["allowed"] is True
    assert main(["user", "tester@studio.com"]) == 0
    assert "status comped" in capsys.readouterr().out


def test_webhook_signature_helpers():
    payload = b'{"id": "evt_1"}'
    header = sign_webhook(payload, "whsec_x", timestamp=1_700_000_000)
    assert verify_webhook(payload, header, "whsec_x", now=1_700_000_100)["id"] == "evt_1"
    with pytest.raises(StripeError):
        verify_webhook(payload, header, "whsec_x", now=1_700_001_000)  # too old
    with pytest.raises(StripeError):
        verify_webhook(payload + b" ", header, "whsec_x", now=1_700_000_100)  # tampered


def test_pricing():
    assert pricing.cost_micros("claude-opus-5-5", {"input_tokens": 1_000_000, "output_tokens": 1_000_000}) == 24_000_000
    assert pricing.cost_micros("claude-haiku-5-5", {"input_tokens": 1_000_000}) == 100_000
    cached = {"cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}
    assert pricing.cost_micros("claude-sonnet-5-5", cached) == 200_000 + 2_500_000
    assert pricing.cost_micros("mystery-model", {"output_tokens": 1_000_000}) == 25_000_000
