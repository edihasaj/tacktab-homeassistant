"""Persistence and validation for realtime provider settings.

Keys use ordinary config-entry storage.  This is deliberately not claimed to
be encrypted and every public return value contains only ``key_configured``.
"""
from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any

from .voice_realtime_providers import ProviderConfig

PROVIDERS = ("openai", "xai", "gemini")
DEFAULTS = {"openai": ("gpt-realtime", "marin"), "xai": ("grok-voice-3.0", "eve"), "gemini": ("models/gemini-3.8-live", "Kore")}
KEYS = "realtime_provider_keys"
SLOTS = "realtime_slots"
BOOTSTRAP_KEY = "voice_bridge_bootstrap"
TRUSTED_LOCAL_HTTP = "realtime_trusted_local_http"


class RealtimeConfigError(ValueError): pass


@dataclass(frozen=True, slots=True)
class SlotRealtimeConfig:
    mode: str = "assist"
    provider: str = "openai"
    model: str = "gpt-realtime"
    voice: str = "marin"
    instructions: str = ""
    language: str | None = None
    llm_api_ids: tuple[str, ...] = ("assist",)
    follow_up_timeout_ms: int = 15_000
    memory_timeout_ms: int = 1_800_000

    def public(self) -> dict[str, Any]:
        return {"mode": self.mode, "provider": self.provider, "model": self.model, "voice": self.voice, "instructions": self.instructions, "language": self.language, "llm_api_ids": list(self.llm_api_ids), "follow_up_timeout_ms": self.follow_up_timeout_ms, "memory_timeout_ms": self.memory_timeout_ms}


def parse_slot_config(data: dict[str, Any]) -> SlotRealtimeConfig:
    allowed = {"mode", "provider", "model", "voice", "instructions", "language", "llm_api_ids", "follow_up_timeout_ms", "memory_timeout_ms"}
    if not isinstance(data, dict) or set(data) - allowed: raise RealtimeConfigError("Invalid realtime configuration")
    provider = data.get("provider", "openai")
    if provider not in PROVIDERS or data.get("mode", "assist") not in {"assist", "realtime"}: raise RealtimeConfigError("Invalid realtime provider")
    default_model, default_voice = DEFAULTS[provider]
    model, voice = data.get("model", default_model), data.get("voice", default_voice)
    prompt, language, ids = data.get("instructions", ""), data.get("language"), data.get("llm_api_ids", ["assist"])
    followup, memory = data.get("follow_up_timeout_ms", 15_000), data.get("memory_timeout_ms", 1_800_000)
    if not all(isinstance(v, str) and v.strip() and len(v) <= 8_000 for v in (model, voice)) or not isinstance(prompt, str) or len(prompt) > 16_000: raise RealtimeConfigError("Invalid realtime configuration")
    if language is not None and (not isinstance(language, str) or len(language) > 64): raise RealtimeConfigError("Invalid realtime configuration")
    if not isinstance(ids, list) or not ids or len(ids) > 8 or not all(isinstance(v, str) and v for v in ids): raise RealtimeConfigError("Invalid realtime configuration")
    if not isinstance(followup, int) or not 1_000 <= followup <= 60_000 or not isinstance(memory, int) or not 0 <= memory <= 43_200_000: raise RealtimeConfigError("Invalid realtime configuration")
    return SlotRealtimeConfig(data.get("mode", "assist"), provider, model, voice, prompt, language, tuple(ids), followup, memory)


def _data(entry: Any) -> dict[str, Any]: return dict(getattr(entry, "data", {}))
def _options(entry: Any) -> dict[str, Any]: return dict(getattr(entry, "options", {}))


def shared_key_entry(hass: Any, entry: Any) -> Any:
    """Return the single integration bootstrap entry used for provider keys."""
    for candidate in hass.config_entries.async_entries("tacktab"):
        if _data(candidate).get(BOOTSTRAP_KEY) is True:
            return candidate
    raise RealtimeConfigError("Voice provider storage is not ready")


async def async_prepare_provider_store(hass: Any, entry: Any) -> Any:
    """Ensure the canonical bootstrap exists before any key read or write."""
    from .voice_setup import async_ensure_voice_bootstrap_entry
    await async_ensure_voice_bootstrap_entry(hass)
    _migrate_legacy_keys(hass, entry)
    return shared_key_entry(hass, entry)


def _migrate_legacy_keys(hass: Any, entry: Any) -> dict[str, str]:
    """Move old per-tablet keys once into the bootstrap config entry."""
    target = shared_key_entry(hass, entry)
    target_data = _data(target); keys = dict(target_data.get(KEYS, {}))
    changed: list[Any] = []
    for candidate in hass.config_entries.async_entries("tacktab"):
        if candidate is target: continue
        legacy = _data(candidate).get(KEYS, {})
        if not isinstance(legacy, dict): continue
        for provider, value in legacy.items():
            if provider in PROVIDERS and provider not in keys and isinstance(value, str) and value:
                keys[provider] = value
        candidate_data = _data(candidate); candidate_data.pop(KEYS, None)
        hass.config_entries.async_update_entry(candidate, data=candidate_data)
        changed.append(candidate)
    if changed or keys != target_data.get(KEYS, {}):
        target_data[KEYS] = keys
        hass.config_entries.async_update_entry(target, data=target_data)
    return keys


def key_transport_allowed(hass: Any, connection: Any, entry: Any) -> bool:
    """Verify the actual HA websocket request, never a client-provided claim."""
    handler = getattr(getattr(connection, "send_message", None), "__self__", None)
    request = getattr(handler, "_request", None)
    if request is not None and bool(getattr(request, "secure", False)):
        return True
    shared = shared_key_entry(hass, entry)
    if _options(shared).get(TRUSTED_LOCAL_HTTP, _data(shared).get(TRUSTED_LOCAL_HTTP)) is not True or request is None:
        return False
    remote = getattr(request, "remote", None)
    try:
        address = ip_address(remote)
    except ValueError:
        return False
    return address.is_private or address.is_loopback
def key_status(entry: Any, provider: str, hass: Any | None = None) -> dict[str, Any]:
    if provider not in PROVIDERS: raise RealtimeConfigError("Invalid realtime provider")
    keys = _migrate_legacy_keys(hass, entry) if getattr(hass, "config_entries", None) is not None else _data(entry).get(KEYS, {})
    return {"provider": provider, "key_configured": bool(keys.get(provider))}
def api_key(entry: Any, provider: str, hass: Any | None = None) -> str | None:
    if provider not in PROVIDERS: raise RealtimeConfigError("Invalid realtime provider")
    values = _migrate_legacy_keys(hass, entry) if getattr(hass, "config_entries", None) is not None else _data(entry).get(KEYS, {})
    value = values.get(provider)
    return value if isinstance(value, str) and value else None


def update_key(hass: Any, entry: Any, provider: str, action: str, supplied: Any = None) -> dict[str, Any]:
    if provider not in PROVIDERS or action not in {"keep", "replace", "clear"}: raise RealtimeConfigError("Invalid provider key request")
    if action == "replace" and (not isinstance(supplied, str) or not supplied.strip() or len(supplied) > 4_096): raise RealtimeConfigError("Invalid provider key request")
    if action != "replace" and supplied is not None: raise RealtimeConfigError("Invalid provider key request")
    target = shared_key_entry(hass, entry)
    data, keys = _data(target), _migrate_legacy_keys(hass, entry)
    if action == "replace": keys[provider] = supplied.strip()
    elif action == "clear": keys.pop(provider, None)
    data[KEYS] = keys
    hass.config_entries.async_update_entry(target, data=data)
    return key_status(target, provider)


def get_slot(entry: Any, slot_id: str) -> SlotRealtimeConfig:
    raw = _data(entry).get(SLOTS, {}).get(slot_id, {})
    return parse_slot_config(raw)


def update_slot(hass: Any, entry: Any, slot_id: str, config: dict[str, Any]) -> SlotRealtimeConfig:
    parsed, data, slots = parse_slot_config(config), _data(entry), dict(_data(entry).get(SLOTS, {}))
    slots[slot_id] = parsed.public(); data[SLOTS] = slots
    hass.config_entries.async_update_entry(entry, data=data)
    return parsed


def provider_config(entry: Any, setting: SlotRealtimeConfig, hass: Any | None = None) -> ProviderConfig:
    key = api_key(entry, setting.provider, hass)
    if key is None: raise RealtimeConfigError("Realtime provider key is not configured")
    return ProviderConfig(setting.provider, setting.model, setting.voice, key, setting.instructions)
