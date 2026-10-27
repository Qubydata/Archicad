"""Settings of the cloud service, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class CloudConfig:
    # Where the service is reachable, used in Stripe return links.
    public_url: str = field(default_factory=lambda: _env("PUBLIC_URL", "http://127.0.0.1:8080").rstrip("/"))
    database_path: str = field(default_factory=lambda: _env("DATABASE_PATH", "archicad-mcp-cloud.db"))
    # Used to hash login codes; any long random string. Changing it invalidates pending codes only.
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY"))

    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    anthropic_base_url: str = field(default_factory=lambda: _env("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/"))
    allowed_models: tuple[str, ...] = field(default_factory=lambda: tuple(
        m.strip() for m in _env("ALLOWED_MODELS", "claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5").split(",") if m.strip()
    ))
    max_tokens_cap: int = field(default_factory=lambda: int(_env("MAX_TOKENS_CAP", "64000")))

    stripe_secret_key: str = field(default_factory=lambda: _env("STRIPE_SECRET_KEY"))
    stripe_webhook_secret: str = field(default_factory=lambda: _env("STRIPE_WEBHOOK_SECRET"))
    stripe_price_id: str = field(default_factory=lambda: _env("STRIPE_PRICE_ID"))
    stripe_api_base: str = field(default_factory=lambda: _env("STRIPE_API_BASE", "https://api.stripe.com").rstrip("/"))
    trial_days: int = field(default_factory=lambda: int(_env("TRIAL_DAYS", "7")))

    # The AI cost (at Anthropic list prices) a subscriber may use per billing period.
    monthly_allowance_usd: float = field(default_factory=lambda: float(_env("MONTHLY_ALLOWANCE_USD", "20")))
    trial_allowance_usd: float = field(default_factory=lambda: float(_env("TRIAL_ALLOWANCE_USD", "3")))

    # Login code e-mails: "resend", "smtp" or "console" (prints codes to the log, for development).
    email_provider: str = field(default_factory=lambda: _env("EMAIL_PROVIDER", "console"))
    email_from: str = field(default_factory=lambda: _env("EMAIL_FROM", "Archicad MCP <login@example.com>"))
    resend_api_key: str = field(default_factory=lambda: _env("RESEND_API_KEY"))
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: int(_env("SMTP_PORT", "587")))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD"))

    def problems(self) -> list[str]:
        """Settings that are missing for a production deployment."""
        missing = []
        for name, value in (
            ("SECRET_KEY", self.secret_key),
            ("ANTHROPIC_API_KEY", self.anthropic_api_key),
            ("STRIPE_SECRET_KEY", self.stripe_secret_key),
            ("STRIPE_WEBHOOK_SECRET", self.stripe_webhook_secret),
            ("STRIPE_PRICE_ID", self.stripe_price_id),
        ):
            if not value:
                missing.append(f"{name} is not set")
        if self.email_provider == "console":
            missing.append("EMAIL_PROVIDER is 'console': login codes are only written to the log")
        return missing
