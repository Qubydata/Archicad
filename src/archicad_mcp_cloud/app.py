"""The Archicad MCP cloud service.

    POST /v1/auth/start          {email}               e-mail a 6-digit sign-in code
    POST /v1/auth/verify         {email, code, device} sign in, returns a device token
    POST /v1/auth/logout                                sign this device out
    GET  /v1/me                                         account, subscription and usage
    POST /v1/billing/checkout                           Stripe Checkout link to subscribe
    POST /v1/billing/portal                             Stripe Customer Portal link
    POST /v1/stripe/webhook                             subscription updates from Stripe
    POST /anthropic/v1/messages                         Claude, metered against the allowance

The desktop app sends its device token as the API key of the Anthropic SDK and
uses <service>/anthropic as the SDK's base URL, so requests and streamed
answers pass through unchanged. Each request's cost (at Anthropic list prices)
is recorded, and requests are refused once the period's allowance is used.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import mailer, pricing
from .config import CloudConfig
from .db import Database, User
from .stripe_api import Stripe, StripeError, subscription_period, verify_webhook

log = logging.getLogger("archicad_mcp_cloud")

ACTIVE_STATUSES = {"active", "trialing", "comped"}
RENEWAL_GRACE = 2 * 86400  # tolerate a late renewal webhook
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FORWARD_HEADERS = ("anthropic-version", "anthropic-beta")


@dataclass
class Entitlement:
    active: bool
    allowed: bool
    message: str
    used_micros: int
    limit_micros: int


def _error(status: int, kind: str, message: str) -> JSONResponse:
    """An error in the Anthropic API's shape, so the SDK in the app reports it cleanly."""
    return JSONResponse({"type": "error", "error": {"type": kind, "message": message}}, status_code=status)


class RateLimiter:
    def __init__(self, limit: int, window: float) -> None:
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = self.hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


def create_app(
    config: CloudConfig | None = None,
    anthropic_transport: httpx.AsyncBaseTransport | None = None,
    stripe_transport: httpx.AsyncBaseTransport | None = None,
    mail_transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    config = config or CloudConfig()
    db = Database(config.database_path, config.secret_key)
    stripe = Stripe(config.stripe_secret_key, config.stripe_api_base, stripe_transport)
    login_limiter = RateLimiter(10, 600)
    for problem in config.problems():
        log.warning("Configuration: %s", problem)

    def entitlement(user: User) -> Entitlement:
        now = time.time()
        limit_usd = config.trial_allowance_usd if user.status == "trialing" else config.monthly_allowance_usd
        limit = int(limit_usd * 1_000_000)
        since = user.period_start or now - 30 * 86400
        used = db.used_micros(user.id, since)
        if user.status not in ACTIVE_STATUSES:
            messages = {
                "past_due": "The last payment did not go through. Update your card under Manage subscription.",
                "unpaid": "The subscription is unpaid. Update your card under Manage subscription.",
                "canceled": "Your subscription has ended. Subscribe again to keep using Archicad MCP.",
            }
            message = messages.get(user.status, "Start your subscription to use Archicad MCP.")
            return Entitlement(False, False, message, used, limit)
        grace = 0 if user.status == "comped" else RENEWAL_GRACE
        if user.period_end and user.period_end + grace < now:
            return Entitlement(False, False, "Your subscription period has ended. Check Manage subscription.", used, limit)
        if used >= limit:
            when = time.strftime("%B %d", time.gmtime(user.period_end)) if user.period_end else "next month"
            return Entitlement(True, False, f"This period's AI allowance is used up. It resets on {when}.", used, limit)
        return Entitlement(True, True, "", used, limit)

    def current_user(request: Request) -> User | None:
        token = request.headers.get("x-api-key", "")
        auth = request.headers.get("authorization", "")
        if not token and auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        return db.user_for_token(token)

    def account(user: User) -> dict[str, Any]:
        e = entitlement(user)
        return {
            "email": user.email,
            "status": user.status,
            "active": e.active,
            "allowed": e.allowed,
            "message": e.message,
            "periodEnd": user.period_end,
            "cancelAtPeriodEnd": bool(user.cancel_at_period_end),
            "usage": {"usedUsd": round(e.used_micros / 1e6, 2), "limitUsd": round(e.limit_micros / 1e6, 2)},
            "billing": bool(config.stripe_secret_key and config.stripe_price_id),
            "hasCustomer": bool(user.stripe_customer_id),
            "trialDays": config.trial_days,
        }

    # --- sign-in ------------------------------------------------------------

    async def auth_start(request: Request) -> Response:
        email = str((await request.json()).get("email", "")).strip().lower()
        if not EMAIL_RE.match(email) or len(email) > 254:
            return JSONResponse({"error": "Enter a valid e-mail address."}, status_code=400)
        client_ip = request.client.host if request.client else "?"
        if not login_limiter.allow(client_ip):
            return JSONResponse({"error": "Too many attempts. Try again in a few minutes."}, status_code=429)
        code = db.create_login_code(email)
        if code is None:
            return JSONResponse({"ok": True, "message": "A code was just sent. Check your inbox."})
        try:
            await mailer.send_login_code(config, email, code, mail_transport)
        except Exception:
            log.exception("Could not send the sign-in e-mail")
            return JSONResponse({"error": "The sign-in e-mail could not be sent. Try again later."}, status_code=502)
        return JSONResponse({"ok": True})

    async def auth_verify(request: Request) -> Response:
        data = await request.json()
        email = str(data.get("email", "")).strip().lower()
        code = str(data.get("code", "")).strip()
        if not db.check_login_code(email, code):
            return JSONResponse({"error": "That code is not right or has expired."}, status_code=400)
        user = db.get_or_create_user(email)
        token = db.create_device(user.id, str(data.get("device", "Archicad MCP")))
        return JSONResponse({"token": token, "account": account(user)})

    async def auth_logout(request: Request) -> Response:
        token = request.headers.get("x-api-key", "")
        db.delete_device(token)
        return JSONResponse({"ok": True})

    async def me(request: Request) -> Response:
        user = current_user(request)
        if not user:
            return _error(401, "authentication_error", "Sign in again.")
        return JSONResponse(account(user))

    # --- billing ------------------------------------------------------------

    async def checkout(request: Request) -> Response:
        user = current_user(request)
        if not user:
            return _error(401, "authentication_error", "Sign in again.")
        try:
            if not user.stripe_customer_id:
                customer = await stripe.create_customer(user.email, user.id)
                db.set_customer(user.id, customer)
                user = db.user_by_id(user.id)
            trial = config.trial_days if user.status == "none" else 0  # one trial per account
            url = await stripe.create_checkout(
                user.stripe_customer_id, config.stripe_price_id, user.id,
                f"{config.public_url}/billing/success", f"{config.public_url}/billing/cancel", trial,
            )
        except StripeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"url": url})

    async def portal(request: Request) -> Response:
        user = current_user(request)
        if not user:
            return _error(401, "authentication_error", "Sign in again.")
        if not user.stripe_customer_id:
            return JSONResponse({"error": "There is no subscription to manage yet."}, status_code=400)
        try:
            url = await stripe.create_portal(user.stripe_customer_id, f"{config.public_url}/billing/return")
        except StripeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"url": url})

    async def stripe_webhook(request: Request) -> Response:
        payload = await request.body()
        try:
            event = verify_webhook(payload, request.headers.get("stripe-signature", ""), config.stripe_webhook_secret)
        except StripeError as exc:
            log.warning("Rejected a webhook: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=400)
        if not db.first_time_event(event.get("id", "")):
            return JSONResponse({"ok": True, "duplicate": True})
        kind = event.get("type", "")
        obj = event.get("data", {}).get("object", {})
        if kind == "checkout.session.completed":
            user = db.user_by_customer(obj.get("customer") or "")
            if user is None and obj.get("client_reference_id"):
                user = db.user_by_id(int(obj["client_reference_id"]))
                if user and obj.get("customer"):
                    db.set_customer(user.id, obj["customer"])
        elif kind.startswith("customer.subscription."):
            user = db.user_by_customer(obj.get("customer") or "")
            if user is None and (obj.get("metadata") or {}).get("user_id"):
                user = db.user_by_id(int(obj["metadata"]["user_id"]))
                if user:
                    db.set_customer(user.id, obj.get("customer"))
            if user is None:
                log.warning("Subscription event %s for an unknown customer %s", event.get("id"), obj.get("customer"))
            else:
                status = "canceled" if kind.endswith(".deleted") else obj.get("status", "none")
                start, end = subscription_period(obj)
                db.set_subscription(user.id, obj.get("id"), status, start, end, bool(obj.get("cancel_at_period_end")))
                log.info("User %s subscription is now %s", user.email, status)
        return JSONResponse({"ok": True})

    # --- Claude, metered ----------------------------------------------------

    async def messages(request: Request) -> Response:
        user = current_user(request)
        if not user:
            return _error(401, "authentication_error", "Sign in to Archicad MCP again (Settings).")
        e = entitlement(user)
        if not e.allowed:
            return _error(402, "billing_error", e.message)
        if not config.anthropic_api_key:
            return _error(503, "api_error", "The AI service is not configured yet.")
        try:
            body = await request.json()
            max_tokens = int(body.get("max_tokens") or 0)
        except (ValueError, TypeError, AttributeError):
            return _error(400, "invalid_request_error", "Body must be a JSON object with a numeric max_tokens.")
        model = body.get("model")
        if model not in config.allowed_models:
            return _error(400, "invalid_request_error", f"Model {model} is not part of the subscription.")
        if max_tokens > config.max_tokens_cap:
            body["max_tokens"] = config.max_tokens_cap
        if any(isinstance(t, dict) and t.get("type") not in (None, "custom") for t in body.get("tools") or []):
            return _error(400, "invalid_request_error", "Server tools are not part of the subscription.")
        for unsupported in ("mcp_servers", "container"):
            body.pop(unsupported, None)

        headers = {"x-api-key": config.anthropic_api_key, "content-type": "application/json"}
        for name in FORWARD_HEADERS:
            if request.headers.get(name):
                headers[name] = request.headers[name]
        headers.setdefault("anthropic-version", "2023-06-01")
        url = f"{config.anthropic_base_url}/v1/messages"
        if request.url.query:
            url += f"?{request.url.query}"

        client = httpx.AsyncClient(transport=anthropic_transport, timeout=httpx.Timeout(900, connect=15))
        try:
            upstream = await client.send(client.build_request("POST", url, json=body, headers=headers), stream=True)
        except httpx.HTTPError as exc:
            await client.aclose()
            log.warning("Anthropic API unreachable: %s", exc)
            return _error(502, "api_error", "The AI service could not be reached. Try again in a moment.")
        content_type = upstream.headers.get("content-type", "application/json")

        if upstream.status_code >= 400 or "text/event-stream" not in content_type:
            data = await upstream.aread()
            await upstream.aclose()
            await client.aclose()
            if upstream.status_code < 400:
                try:
                    reply = json.loads(data)
                    _record(user, reply.get("model", model), reply.get("usage") or {})
                except ValueError:
                    pass
            return Response(data, status_code=upstream.status_code, media_type=content_type)

        async def relay():
            seen = {"model": model, "usage": {}}
            buffer = b""
            try:
                async for chunk in upstream.aiter_bytes():  # decoded, since content-encoding is not passed on
                    yield chunk
                    buffer += chunk
                    *lines, buffer = buffer.split(b"\n")
                    for line in lines:
                        _watch_usage(line, seen)
            finally:
                await upstream.aclose()
                await client.aclose()
                _record(user, seen["model"], seen["usage"])

        return StreamingResponse(relay(), status_code=upstream.status_code, media_type="text/event-stream",
                                 headers={"cache-control": "no-cache"})

    def _record(user: User, model: str, usage: dict[str, Any]) -> None:
        if usage:
            db.record_usage(user.id, model, usage, pricing.cost_micros(model, usage))

    # --- pages --------------------------------------------------------------

    def page(title: str, text: str) -> HTMLResponse:
        return HTMLResponse(
            '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f"<title>{title}</title></head><body style=\"font-family:Segoe UI,-apple-system,sans-serif;"
            "display:grid;place-items:center;min-height:90vh;margin:0;padding:16px;color:#1b1b1f;background:#f6f8f7\">"
            '<div style="max-width:420px;text-align:center;background:#fff;border:1px solid #e5e7eb;border-radius:14px;'
            f'padding:28px 24px"><h1 style="font-size:22px;margin:0 0 10px">{title}</h1>'
            f'<p style="color:#4b5563;margin:0">{text}</p></div></body></html>'
        )

    async def billing_success(request: Request) -> Response:
        return page("You are subscribed", "Thank you. Go back to Archicad: the MCP panel is ready to use.")

    async def billing_cancel(request: Request) -> Response:
        return page("Checkout cancelled", "Nothing was charged. You can subscribe any time from the panel's Settings tab.")

    async def billing_return(request: Request) -> Response:
        return page("All set", "Your changes are saved. Go back to Archicad.")

    async def health(request: Request) -> Response:
        return JSONResponse({"ok": True, "service": "archicad-mcp-cloud"})

    app = Starlette(routes=[
        Route("/", health),
        Route("/health", health),
        Route("/v1/auth/start", auth_start, methods=["POST"]),
        Route("/v1/auth/verify", auth_verify, methods=["POST"]),
        Route("/v1/auth/logout", auth_logout, methods=["POST"]),
        Route("/v1/me", me),
        Route("/v1/billing/checkout", checkout, methods=["POST"]),
        Route("/v1/billing/portal", portal, methods=["POST"]),
        Route("/v1/stripe/webhook", stripe_webhook, methods=["POST"]),
        Route("/anthropic/v1/messages", messages, methods=["POST"]),
        Route("/billing/success", billing_success),
        Route("/billing/cancel", billing_cancel),
        Route("/billing/return", billing_return),
    ])
    app.state.db = db
    app.state.config = config
    return app


def _watch_usage(line: bytes, seen: dict[str, Any]) -> None:
    """Pick the model and token usage out of the streamed events."""
    if not line.startswith(b"data:"):
        return
    try:
        event = json.loads(line[5:].strip())
    except ValueError:
        return
    if event.get("type") == "message_start":
        message = event.get("message") or {}
        seen["model"] = message.get("model") or seen["model"]
        seen["usage"] = dict(message.get("usage") or {})
    elif event.get("type") == "message_delta":
        seen["usage"].update({k: v for k, v in (event.get("usage") or {}).items() if isinstance(v, (int, float))})
