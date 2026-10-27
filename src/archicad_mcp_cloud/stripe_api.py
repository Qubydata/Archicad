"""The few Stripe calls the service needs, over Stripe's REST API, and webhook verification.

Stripe's API takes form-encoded bodies with bracketed keys for nested values
(line_items[0][price]=...). Webhooks are signed with HMAC-SHA256 over
"<timestamp>.<raw body>" and sent in the Stripe-Signature header.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import httpx

WEBHOOK_TOLERANCE = 300


class StripeError(Exception):
    pass


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        pairs = []
        for key, item in value.items():
            pairs += _flatten(item, f"{prefix}[{key}]" if prefix else str(key))
        return pairs
    if isinstance(value, (list, tuple)):
        pairs = []
        for index, item in enumerate(value):
            pairs += _flatten(item, f"{prefix}[{index}]")
        return pairs
    if isinstance(value, bool):
        return [(prefix, "true" if value else "false")]
    return [(prefix, str(value))]


class Stripe:
    def __init__(self, secret_key: str, api_base: str = "https://api.stripe.com",
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.secret_key = secret_key
        self.api_base = api_base
        self.transport = transport

    async def _post(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.secret_key:
            raise StripeError("Billing is not configured on this server (STRIPE_SECRET_KEY).")
        async with httpx.AsyncClient(transport=self.transport, timeout=30) as client:
            response = await client.post(
                f"{self.api_base}{path}", content=urlencode(_flatten(params)),
                headers={"Authorization": f"Bearer {self.secret_key}",
                         "Content-Type": "application/x-www-form-urlencoded"},
            )
        data = response.json()
        if response.status_code >= 400:
            raise StripeError(data.get("error", {}).get("message", f"Stripe error {response.status_code}"))
        return data

    async def create_customer(self, email: str, user_id: int) -> str:
        customer = await self._post("/v1/customers", {"email": email, "metadata": {"user_id": user_id}})
        return customer["id"]

    async def create_checkout(self, customer_id: str, price_id: str, user_id: int, success_url: str,
                              cancel_url: str, trial_days: int) -> str:
        params: dict[str, Any] = {
            "mode": "subscription",
            "customer": customer_id,
            "client_reference_id": user_id,
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "allow_promotion_codes": True,
            "subscription_data": {"metadata": {"user_id": user_id}},
        }
        if trial_days > 0:
            params["subscription_data"]["trial_period_days"] = trial_days
        session = await self._post("/v1/checkout/sessions", params)
        return session["url"]

    async def create_portal(self, customer_id: str, return_url: str) -> str:
        session = await self._post("/v1/billing_portal/sessions", {"customer": customer_id, "return_url": return_url})
        return session["url"]


def verify_webhook(payload: bytes, signature_header: str, secret: str, now: float | None = None) -> dict[str, Any]:
    """Return the event if the Stripe-Signature header is valid, else raise StripeError."""
    if not secret:
        raise StripeError("STRIPE_WEBHOOK_SECRET is not set")
    parts = [item.split("=", 1) for item in signature_header.split(",") if "=" in item]
    timestamp = next((v for k, v in parts if k == "t"), None)
    signatures = [v for k, v in parts if k == "v1"]
    if not timestamp or not signatures:
        raise StripeError("bad signature header")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, s) for s in signatures):
        raise StripeError("signature mismatch")
    if abs((now or time.time()) - int(timestamp)) > WEBHOOK_TOLERANCE:
        raise StripeError("timestamp outside the tolerance")
    return json.loads(payload)


def sign_webhook(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Build a Stripe-Signature header (for tests and local webhook replays)."""
    timestamp = timestamp or int(time.time())
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def subscription_period(subscription: dict[str, Any]) -> tuple[float | None, float | None]:
    """The current period of a subscription; newer API versions keep it on the subscription items."""
    start = subscription.get("current_period_start")
    end = subscription.get("current_period_end")
    if start is None or end is None:
        items = (subscription.get("items") or {}).get("data") or []
        if items:
            start = items[0].get("current_period_start", start)
            end = items[0].get("current_period_end", end)
    return start, end
