"""Bounded, in-memory realtime conversation retention."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

MAX_MEMORY_ITEMS = 64
MAX_MEMORY_BYTES = 512 * 1024


@dataclass(slots=True)
class RetainedContext:
    """Context that may seed a new provider socket for one identity and slot."""

    expires_at: float
    items: list[dict[str, Any]]
    resume: dict[str, Any] | None


def item_size(item: dict[str, Any]) -> int:
    """Return a stable, conservative retained-size estimate."""
    return len(json.dumps(item, separators=(",", ":"), ensure_ascii=False).encode())


def append_bounded(
    items: list[dict[str, Any]], item: dict[str, Any], current_bytes: int
) -> int:
    """Append one completed item and evict oldest entries to the retention caps."""
    items.append(item)
    current_bytes += item_size(item)
    while items and (len(items) > MAX_MEMORY_ITEMS or current_bytes > MAX_MEMORY_BYTES):
        current_bytes -= item_size(items.pop(0))
    return current_bytes
