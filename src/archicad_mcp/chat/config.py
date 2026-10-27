"""Settings of the chat app, kept in a small JSON file in the user's home folder."""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import asdict, dataclass

CONFIG_DIR = pathlib.Path(os.environ.get("ARCHICAD_MCP_HOME", pathlib.Path.home() / ".archicad-mcp"))
CONFIG_FILE = CONFIG_DIR / "config.json"

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

    @property
    def resolved_api_key(self) -> str:
        return self.api_key or os.environ.get("ANTHROPIC_API_KEY", "")


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
