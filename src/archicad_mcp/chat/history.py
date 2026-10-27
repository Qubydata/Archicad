"""Saved conversations, listed under Recent in the panel."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ICON_KEYWORDS = [
    ("section", ("section", "elevation", "cut")),
    ("sun", ("daylight", "sun", "light", "shadow", "solar")),
    ("table", ("schedule", "table", "quantit", "list", "count", "area", "volume")),
    ("cube", ("option", "compare", "3d", "view", "massing", "model")),
    ("wall", ("wall", "slab", "column", "beam", "roof", "room", "zone", "door", "window", "build", "create")),
]


def icon_for(title: str) -> str:
    lowered = title.lower()
    for icon, words in ICON_KEYWORDS:
        if any(word in lowered for word in words):
            return icon
    return "chat"


def title_for(text: str) -> str:
    title = re.sub(r"\s+", " ", text).strip().rstrip("?.!")
    title = title[:1].upper() + title[1:]
    return title if len(title) <= 52 else title[:50].rstrip() + "..."


@dataclass
class Conversation:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    messages: list[dict[str, Any]] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "updated": self.updated, "icon": icon_for(self.title)}


def _plain(value: Any) -> Any:
    """Make API objects JSON-friendly (the SDK's content blocks and test doubles alike)."""
    if hasattr(value, "to_dict"):
        return value.to_dict(mode="json")
    return vars(value)


class ConversationStore:
    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def _path(self, conversation_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{12}", conversation_id):
            raise ValueError("bad conversation id")
        return self.folder / f"{conversation_id}.json"

    def save(self, conversation: Conversation) -> None:
        if not conversation.messages:
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        data = {
            "id": conversation.id,
            "title": conversation.title,
            "created": conversation.created,
            "updated": conversation.updated,
            "messages": conversation.messages,
            "transcript": conversation.transcript,
        }
        path = self._path(conversation.id)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, default=_plain), encoding="utf8")
        temp.replace(path)

    def load(self, conversation_id: str) -> Conversation:
        data = json.loads(self._path(conversation_id).read_text(encoding="utf8"))
        return Conversation(**data)

    def delete(self, conversation_id: str) -> None:
        self._path(conversation_id).unlink(missing_ok=True)

    def recent(self, limit: int | None = None) -> list[dict[str, Any]]:
        items = []
        for path in self.folder.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf8"))
            except (OSError, ValueError):
                continue
            items.append(Conversation(id=data["id"], title=data.get("title", ""), updated=data.get("updated", 0)).summary())
        items.sort(key=lambda item: -item["updated"])
        return items[:limit] if limit else items
