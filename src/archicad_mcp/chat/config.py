"""Settings of the chat app, kept in a small JSON file in the user's home folder."""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import asdict, dataclass
from importlib import resources

CONFIG_DIR = pathlib.Path(os.environ.get("ARCHICAD_MCP_HOME", pathlib.Path.home() / ".archicad-mcp"))
CONFIG_FILE = CONFIG_DIR / "config.json"



def _bundled_cloud_url() -> str:
    """The cloud service URL written into the app at build time (packaging/build.py), if any."""
    try:
        return resources.files("archicad_mcp.chat").joinpath("cloud_url.txt").read_text(encoding="utf8").strip()
    except (OSError, ValueError):
        return ""


# The Archicad MCP cloud service (sign-in and subscriptions); ARCHICAD_MCP_CLOUD_URL overrides it.
# Empty: there is no sign-in and users bring their own Anthropic API key.
DEFAULT_CLOUD_URL = _bundled_cloud_url()


def cloud_url() -> str:
    return os.environ.get("ARCHICAD_MCP_CLOUD_URL", DEFAULT_CLOUD_URL).strip().rstrip("/")


MODELS = {
    "claude-opus-5-5": "Claude Opus 5.5 (best results)",
    "claude-sonnet-5-5": "Claude Sonnet 5.5 (faster, cheaper)",
    "claude-haiku-5-5": "Claude Haiku 5.5 (fastest, cheapest)",
}
EFFORTS = ["low", "medium", "high", "xhigh", "max"]


@dataclass
class Settings:
    api_key: str = ""
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    auto_open_panel: bool = True
    account_email: str = ""
    account_token: str = ""

    @property
    def resolved_api_key(self) -> str:
        return self.api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    @property
    def uses_subscription(self) -> bool:
        """Signed in to the cloud service: Claude requests go through it, billed to the subscription."""
        return bool(self.account_token and cloud_url())

    @property
    def ready(self) -> bool:
        return self.uses_subscription or bool(self.resolved_api_key)


def load() -> Settings:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf8"))
    except (OSError, ValueError):
        return Settings()
    settings = Settings(**{k: v for k, v in data.items() if k in Settings.__dataclass_fields__})
    if settings.model not in MODELS:
        settings.model = Settings.model
    if settings.effort not in EFFORTS:
        settings.effort = Settings.effort
    return settings


def save(settings: Settings) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(asdict(settings), indent=2), encoding="utf8")
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass
