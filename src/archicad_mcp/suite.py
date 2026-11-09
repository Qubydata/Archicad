"""The tools of Tevavid Tools for Archicad.

The installer lists these with a checkbox each, records the ones the user
picked, and the panel shows a tab for each installed tool. To add a tool to
the suite, add it here and give it its panel tab and (if it needs one) its
install step.
"""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass

SUITE_NAME = "Tevavid Tools"
SUITE_FULL_NAME = "Tevavid Tools for Archicad"


@dataclass(frozen=True)
class Tool:
    id: str
    name: str
    summary: str
    needs_tapir: bool = True  # uses the Tapir add-on to talk to Archicad
    platforms: tuple[str, ...] = ()  # empty: every platform
    available: bool = True  # False: listed as coming soon
    note: str = ""

    def supported(self, platform: str | None = None) -> bool:
        return not self.platforms or (platform or sys.platform) in self.platforms

    def to_dict(self, platform: str | None = None) -> dict:
        return {**asdict(self), "supported": self.supported(platform)}


TOOLS: tuple[Tool, ...] = (
    Tool("assistant", "AI Assistant", "Chat with Claude to change your model, here or from Claude Desktop."),
    Tool("sun", "Sun Analysis", "Live sunlight and daylight maps on your rooms in the 3D window."),
    Tool("site", "Site from Map", "The real terrain and neighbouring buildings around your project, or your surveyor's points."),
    Tool("darkmode", "Dark Mode", "A dark interface for Archicad 25 to 29 on Windows.", needs_tapir=False,
         platforms=("win32",), available=False, note="Coming soon"),
)
TOOL_IDS = tuple(tool.id for tool in TOOLS)


def tool(tool_id: str) -> Tool:
    return next(t for t in TOOLS if t.id == tool_id)


def default_tools(platform: str | None = None) -> list[str]:
    """The tools ticked by default: everything available on this platform."""
    return [t.id for t in TOOLS if t.available and t.supported(platform)]


def clean(tool_ids, platform: str | None = None) -> list[str]:
    """Keep only known, available tools for this platform, in suite order."""
    wanted = set(tool_ids or [])
    return [t.id for t in TOOLS if t.id in wanted and t.available and t.supported(platform)]


def needs_tapir(tool_ids) -> bool:
    return any(tool(t).needs_tapir for t in clean(tool_ids))
