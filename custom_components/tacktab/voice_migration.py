"""Read-only Voice Satellite migration discovery and guarded application.

The old integration persists most configuration in its HA entities.  This
module deliberately reads only entity/configuration state; references are
reported so the user can change them after Tacktab is switched.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

OLD_DOMAIN = "voice_satellite"
OLD_TIMER_EVENT = "voice_satellite_timer"
NEW_TIMER_EVENT = "tacktab_voice_timer"


def _value(value: Any) -> Any:
    """Make state/config values safe to return through websocket JSON."""
    return value if isinstance(value, (str, int, float, bool)) or value is None else None


def _contains_reference(value: Any, satellite_id: str) -> bool:
    """Find literal old entity/event references in a parsed YAML value."""
    if isinstance(value, str):
        return satellite_id in value or OLD_TIMER_EVENT in value
    if isinstance(value, Mapping):
        return any(_contains_reference(key, satellite_id) or _contains_reference(item, satellite_id) for key, item in value.items())
    if isinstance(value, Iterable):
        return any(_contains_reference(item, satellite_id) for item in value)
    return False


def scan_references(documents: Mapping[str, Any], satellite_id: str) -> list[dict[str, str]]:
    """Return, never mutate, configured automations and scripts using old APIs."""
    matches: list[dict[str, str]] = []
    for kind in ("automation", "script"):
        source = documents.get(kind, {})
        items = source if isinstance(source, list) else source.values() if isinstance(source, Mapping) else ()
        for index, item in enumerate(items):
            if not isinstance(item, Mapping) or not _contains_reference(item, satellite_id):
                continue
            identifier = str(item.get("id") or item.get("alias") or item.get("name") or index)
            matches.append({"kind": kind, "id": identifier, "name": str(item.get("alias") or item.get("name") or identifier)})
    return matches


def _yaml_documents(config_dir: str) -> dict[str, Any]:
    """Best-effort YAML reads; malformed user YAML must not break voice setup."""
    result: dict[str, Any] = {}
    for kind, filename in (("automation", "automations.yaml"), ("script", "scripts.yaml")):
        try:
            with Path(config_dir, filename).open(encoding="utf-8") as handle:
                result[kind] = yaml.safe_load(handle) or {}
        except (OSError, yaml.YAMLError):
            result[kind] = {}
    return result


def build_plan(states: Mapping[str, Any], entries: Iterable[Any], documents: Mapping[str, Any]) -> dict[str, Any]:
    """Build JSON-ready candidates from supplied HA state and registry entries."""
    old_entries = {entry.entry_id for entry in entries if getattr(entry, "domain", None) == OLD_DOMAIN}
    candidates: list[dict[str, Any]] = []
    for entity_id, state in states.items():
        if not entity_id.startswith("assist_satellite."):
            continue
        entry_id = getattr(state, "config_entry_id", None)
        attrs = getattr(state, "attributes", {})
        if entry_id not in old_entries and attrs.get("integration") != OLD_DOMAIN:
            continue
        settings = {
            "assistant": _value(attrs.get("pipeline")),
            "pipeline_2": _value(attrs.get("pipeline_2")),
            "wake_word": _value(attrs.get("wake_word_model")),
            "wake_word_2": _value(attrs.get("wake_word_model_2")),
            "wake_detection": _value(attrs.get("wake_word_detection")),
            "finished_speaking": _value(attrs.get("vad_sensitivity")),
            "conversation": _value(attrs.get("session_duration")),
            "timer_muted": _value(attrs.get("mute_timers")),
            "timer_visible": _value(attrs.get("hide_on_screen_countdown")),
            "timer_name_visible": _value(attrs.get("hide_timer_name_on_alert")),
        }
        candidates.append({
            "entity_id": entity_id,
            "name": str(attrs.get("friendly_name") or entity_id.removeprefix("assist_satellite.").replace("_", " ").title()),
            "state": str(getattr(state, "state", "unknown")),
            "settings": settings,
            "references": scan_references(documents, entity_id),
            # No skin is portable until an explicit native mapping exists.
            "appearance_mapping": None,
        })
    return {"candidates": candidates, "timer_event_mapping": {"from": OLD_TIMER_EVENT, "to": NEW_TIMER_EVENT}}


async def async_build_plan(hass: HomeAssistant) -> dict[str, Any]:
    """Read old integration state and YAML without reloading or writing either."""
    registry = er.async_get(hass)
    states: dict[str, Any] = {}
    children: dict[str, dict[str, Any]] = {}
    registry_states: list[tuple[Any, Any]] = []
    for entry in registry.entities.values():
        state = hass.states.get(entry.entity_id)
        if state is not None:
            children.setdefault(entry.config_entry_id, {})[entry.unique_id] = state
            registry_states.append((entry, state))
    for entry, state in registry_states:
        if state is not None:
            if entry.entity_id.startswith("assist_satellite."):
                attrs = dict(state.attributes)
                for unique_id, child in children.get(entry.config_entry_id, {}).items():
                    suffixes = {
                        "_wake_word_model": "wake_word", "_wake_word_model_2": "wake_word_2",
                        "_wake_word_detection": "wake_detection", "_session_duration": "conversation",
                        "_wake_word_sensitivity": "wake_sensitivity", "_pipeline_2": "pipeline_2",
                        "_mute_timers": "timer_muted",
                    }
                    for suffix, key in suffixes.items():
                        if unique_id.endswith(suffix):
                            attrs[key] = child.state
                    if "vad" in unique_id or "finished_speaking" in unique_id:
                        attrs["vad_sensitivity"] = child.state
                states[entry.entity_id] = type("MigrationState", (), {"state": state.state, "attributes": attrs, "config_entry_id": entry.config_entry_id})()
    return build_plan(states, hass.config_entries.async_entries(), _yaml_documents(hass.config.config_dir))
