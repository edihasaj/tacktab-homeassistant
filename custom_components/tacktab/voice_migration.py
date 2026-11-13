"""Read-only Voice Satellite migration discovery.

Upstream keeps its configuration in HA entities: the satellite itself plus
select and switch children whose unique ids start with the config entry id.
This module reads only that state and returns one canonical, typed `settings`
shape; every value is `None` when the source did not say, so callers leave
their own preference untouched instead of guessing.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .voice_conversation import memory_option_for
from .voice_migration_refs import OLD_TIMER_EVENT, ReferenceDiscovery, async_discover_references, scan_references

__all__ = ["OLD_DOMAIN", "OLD_TIMER_EVENT", "NEW_TIMER_EVENT", "async_build_plan", "build_plan", "scan_references"]

OLD_DOMAIN = "voice_satellite"
NEW_TIMER_EVENT = "tacktab_voice_timer"
_UNKNOWN = {"unknown", "unavailable", "none", ""}

# (canonical source key, platform, unique id suffix). The pipeline and VAD
# selects come from HA core's assist_pipeline and use "-" instead of "_".
_CHILDREN = (
    ("assistant", "select", "-pipeline"), ("assistant_2", "select", "_pipeline_2"),
    ("wake_word", "select", "_wake_word_model"), ("wake_word_2", "select", "_wake_word_model_2"),
    ("wake_detection", "select", "_wake_word_detection"), ("wake_sensitivity", "select", "_wake_word_sensitivity"),
    ("finished_speaking", "select", "-vad_sensitivity"), ("conversation", "select", "_session_duration"),
    ("timer_muted", "switch", "_mute_timers"),
)
# The satellite entity mirrors most of those as attributes; used as a fallback.
_ATTRIBUTES = {
    "assistant": "pipeline", "assistant_2": "pipeline_2", "wake_word": "wake_word_model", "wake_word_2": "wake_word_model_2",
    "wake_detection": "wake_word_detection", "wake_sensitivity": "wake_word_sensitivity",
    "conversation": "session_duration", "timer_muted": "mute_timers", "finished_speaking": "vad_sensitivity",
}
# Upstream engine label token -> Tacktab's engine id (the same spelling the fleet voice profile uses).
_ENGINES = {"microwakeword": "micro_wake_word", "openwakeword": "open_wake_word", "vswakeword": "vs_wake_word"}
# Tacktab can run these on a tablet; vsWakeWord has no native counterpart.
SUPPORTED_ENGINES = {"micro_wake_word", "open_wake_word"}
# One rule for every engine: upstream's three labels on Tacktab's 0-1 sensitivity slider (0.5 is "as picked").
# Upstream scales differ per engine (a margin multiplier for microWakeWord, +/-0.10 probability for openWakeWord),
# so they are not copied; fine-tune on the tablet covers the rest.
WAKE_SENSITIVITY = {"slightly sensitive": 0.25, "moderately sensitive": 0.5, "very sensitive": 0.75}
_VAD = {"default", "relaxed", "aggressive"}
# Upstream's built-in microWakeWord ids that Tacktab spells differently.
WAKE_WORD_ALIASES = {"ok_nabu": "okay_nabu"}


@dataclass(slots=True)
class OldSatellite:
    """Raw upstream values for one satellite, keyed by the canonical names."""

    entity_id: str
    name: str
    state: str
    raw: dict[str, Any] = field(default_factory=dict)


def _text(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    return None if text.casefold() in _UNKNOWN else text


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = _text(value)
    return None if text is None else True if text.casefold() in {"on", "true"} else False if text.casefold() in {"off", "false"} else None


def _is_preferred(value: str | None) -> bool | None:
    return None if value is None else value.casefold() == "preferred"


def _wake_id(value: Any) -> str | None:
    """Upstream's model id as Tacktab spells it (`ok_nabu` -> `okay_nabu`)."""
    text = _text(value)
    return None if text is None else WAKE_WORD_ALIASES.get(text, text)


def normalize_wake_detection(value: Any) -> dict[str, Any]:
    """Map upstream's detection labels to mode (local/server/disabled) and engine."""
    label = _text(value)
    if label is None:
        return {"wake_detection": None, "wake_engine": None, "wake_engine_supported": None, "wake_detection_label": None}
    key = re.sub(r"[^a-z]", "", label.casefold())
    if key == "disabled":
        mode, engine = "disabled", None
    elif key == "homeassistant":
        mode, engine = "server", None
    elif key.startswith("ondevice"):
        engine = next((name for token, name in _ENGINES.items() if token in key), "micro_wake_word" if key == "ondevice" else None)
        mode = "local"
    else:
        mode, engine = None, None
    supported = None if mode is None else engine is None or engine in SUPPORTED_ENGINES
    return {"wake_detection": mode, "wake_engine": engine, "wake_engine_supported": supported, "wake_detection_label": label}


def normalize_wake_sensitivity(value: Any) -> dict[str, Any]:
    """`wake_sensitivity` is Tacktab's 0-1 value (None when unknown); the label is kept for display."""
    label = _text(value)
    level = WAKE_SENSITIVITY.get(label.casefold()) if label else None
    return {"wake_sensitivity": level, "wake_sensitivity_label": label if level is not None else None}


def normalize_conversation(value: Any) -> dict[str, Any]:
    """`persistent`, `isolated` or `<n>_minutes|hour(s)`, in any case/spacing.

    Also names the Tacktab "Conversation memory" option that keeps context for at
    least as long (`conversation_memory`) and whether that rounded the duration up.
    """
    text = _text(value)
    mode: str | None = None
    seconds: int | None = None
    if text is not None:
        key = re.sub(r"[\s-]+", "_", text.casefold())
        match = re.fullmatch(r"(\d+)_(minute|hour)s?", key)
        if key in {"persistent", "isolated"}:
            mode = key
        elif match:
            mode, seconds = "timed", int(match[1]) * (60 if match[2] == "minute" else 3600)
    memory, rounded = memory_option_for(mode, seconds)
    return {"conversation": mode, "conversation_seconds": seconds, "conversation_memory": memory, "conversation_memory_rounded": rounded}


def normalize_settings(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The one typed shape shared by `build_plan`, the apply command and both apps."""
    assistant = _text(raw.get("assistant"))
    vad = _text(raw.get("finished_speaking"))
    vad = vad.casefold() if vad else None
    settings: dict[str, Any] = {
        "assistant": assistant, "assistant_preferred": _is_preferred(assistant), "assistant_pipeline_id": None,
        "assistant_2": _text(raw.get("assistant_2")),
        "wake_word": _wake_id(raw.get("wake_word")), "wake_word_source": _text(raw.get("wake_word")), "wake_word_2": _text(raw.get("wake_word_2")),
        "finished_speaking": vad if vad in _VAD else None,
        "timer_muted": _flag(raw.get("timer_muted")),
    }
    return settings | normalize_wake_sensitivity(raw.get("wake_sensitivity")) | normalize_wake_detection(raw.get("wake_detection")) | normalize_conversation(raw.get("conversation"))


def plan_warnings(settings: Mapping[str, Any]) -> list[dict[str, str]]:
    """Things Tacktab cannot carry; shown in review instead of silently dropped."""
    warnings: list[dict[str, str]] = []
    if settings.get("wake_engine_supported") is False:
        warnings.append({"code": "unsupported_wake_engine", "detail": str(settings.get("wake_detection_label"))})
    if settings.get("wake_word_2") or settings.get("assistant_2"):
        warnings.append({"code": "second_assistant_not_carried", "detail": "Second wake word and assistant are not migrated"})
    if settings.get("conversation_memory_rounded"):
        warnings.append({"code": "conversation_memory_rounded", "detail": str(settings.get("conversation_memory"))})
    warnings.append({"code": "appearance_not_mapped", "detail": "Voice Satellite skins have no native mapping"})
    return warnings


def build_plan(satellites: list[OldSatellite], discovery: ReferenceDiscovery) -> dict[str, Any]:
    """Build JSON-ready candidates from already-read upstream values."""
    candidates = []
    for satellite in satellites:
        settings = normalize_settings(satellite.raw)
        candidates.append({
            "entity_id": satellite.entity_id, "name": satellite.name, "state": satellite.state, "settings": settings,
            "references": discovery.for_satellite(satellite.entity_id), "warnings": plan_warnings(settings),
            # No skin is portable until an explicit native mapping exists.
            "appearance_mapping": None,
        })
    return {
        "candidates": candidates,
        "references_complete": discovery.complete, "reference_warnings": list(discovery.warnings),
        "timer_event_mapping": {"from": OLD_TIMER_EVENT, "to": NEW_TIMER_EVENT},
    }


def read_satellites(hass: HomeAssistant) -> list[OldSatellite]:
    """Read upstream entities by their documented unique ids; writes nothing."""
    registry = er.async_get(hass)
    result: list[OldSatellite] = []
    for entry in registry.entities.values():
        if entry.platform != OLD_DOMAIN or entry.domain != "assist_satellite":
            continue
        state = hass.states.get(entry.entity_id)
        if state is None:
            continue
        attrs = state.attributes
        raw: dict[str, Any] = {key: attrs.get(name) for key, name in _ATTRIBUTES.items()}
        prefix = entry.unique_id
        for key, platform, suffix in _CHILDREN:
            child_id = registry.async_get_entity_id(platform, OLD_DOMAIN, f"{prefix}{suffix}")
            child = hass.states.get(child_id) if child_id else None
            if child is not None and _text(child.state) is not None:
                raw[key] = child.state
        name = attrs.get("friendly_name") or entry.entity_id.removeprefix("assist_satellite.").replace("_", " ").title()
        result.append(OldSatellite(entry.entity_id, str(name), str(state.state), raw))
    return result


async def async_build_plan(hass: HomeAssistant) -> dict[str, Any]:
    """Read old integration state and automation config without writing either."""
    from homeassistant.components.assist_pipeline import async_get_pipelines

    plan = build_plan(read_satellites(hass), await async_discover_references(hass))
    pipelines = list(async_get_pipelines(hass))
    for candidate in plan["candidates"]:
        settings = candidate["settings"]
        wanted = settings["assistant"]
        if wanted and not settings["assistant_preferred"]:
            match = next((p for p in pipelines if p.name == wanted), None) or next((p for p in pipelines if p.name.casefold() == wanted.casefold()), None)
            settings["assistant_pipeline_id"] = None if match is None else match.id
            if match is None:
                candidate["warnings"].append({"code": "assistant_not_found", "detail": wanted})
    return plan
