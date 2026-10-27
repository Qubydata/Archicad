"""What a Claude request costs, at Anthropic list prices, in micro-dollars."""

from __future__ import annotations

from typing import Any

# USD per million tokens: input, output, cache read. Cache writes cost 1.25 x input.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    # Possible server-side refusal fallback models.
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
}
UNKNOWN_MODEL = (5.00, 25.00, 0.50)  # charge unknown models like the most expensive known one


def cost_micros(model: str, usage: dict[str, Any]) -> int:
    input_price, output_price, cache_read_price = PRICES.get(model, UNKNOWN_MODEL)
    tokens_in = usage.get("input_tokens") or 0
    tokens_out = usage.get("output_tokens") or 0
    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_write = usage.get("cache_creation_input_tokens") or 0
    dollars_per_million = (
        tokens_in * input_price
        + tokens_out * output_price
        + cache_read * cache_read_price
        + cache_write * input_price * 1.25
    )
    return int(round(dollars_per_million))  # tokens x USD/Mtok = micro-dollars
