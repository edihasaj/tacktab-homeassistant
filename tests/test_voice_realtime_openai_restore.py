"""OpenAI realtime provider is accepted again; unknown providers and Assist routes stay safe."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.tacktab.voice_realtime import RealtimeController
from custom_components.tacktab.voice_realtime_catalog import async_catalog
from custom_components.tacktab.voice_realtime_config import (
    KEYS, SLOTS, RealtimeConfigError, api_key, get_slot, key_status, parse_slot_config, update_key,
)
from custom_components.tacktab.voice_realtime_protocol import RealtimeProtocolError, parse_command


class Entries:
    def __init__(self, entries):
        self.entries = entries
        self.updates = 0

    def async_entries(self, domain):
        return self.entries

    def async_update_entry(self, entry, *, data):
        self.updates += 1
        entry.data = data


def test_openai_slot_is_accepted_with_its_defaults():
    slot = parse_slot_config({"mode": "realtime", "provider": "openai"})
    assert (slot.mode, slot.provider, slot.model, slot.voice) == ("realtime", "openai", "gpt-realtime", "marin")


@pytest.mark.parametrize("provider", ["openai", "xai", "gemini"])
def test_supported_provider_commands_parse(provider):
    parse_command({"id": 1, "type": "tacktab/realtime/catalog", "epoch": "e", "provider": provider})
    parse_command({"id": 2, "type": "tacktab/realtime/configure_provider", "epoch": "e", "provider": provider, "key_action": "replace", "api_key": "k"})


@pytest.mark.parametrize("provider", ["anthropic", "unknown", "", None])
def test_unknown_provider_commands_are_rejected(provider):
    for command, extra in (("catalog", {}), ("configure_provider", {"key_action": "replace", "api_key": "k"})):
        with pytest.raises(RealtimeProtocolError) as error:
            parse_command({"id": 1, "type": f"tacktab/realtime/{command}", "epoch": "e", "provider": provider, **extra})
        assert error.value.code == "invalid_format"


@pytest.mark.parametrize("provider", ["anthropic", "unknown"])
async def test_unknown_provider_config_key_and_catalog_access_are_rejected(provider, monkeypatch):
    entry = SimpleNamespace(data={KEYS: {"openai": "kept"}})
    with pytest.raises(RealtimeConfigError):
        parse_slot_config({"mode": "realtime", "provider": provider})
    for operation in (lambda: key_status(entry, provider), lambda: api_key(entry, provider), lambda: update_key(SimpleNamespace(), entry, provider, "replace", "k")):
        with pytest.raises(RealtimeConfigError):
            operation()
    fetch = AsyncMock()
    monkeypatch.setattr("custom_components.tacktab.voice_realtime_catalog._get_json", fetch)
    with pytest.raises(RealtimeConfigError, match="Invalid realtime provider"):
        await async_catalog(object(), entry, provider)
    fetch.assert_not_awaited()
    assert entry.data == {KEYS: {"openai": "kept"}}


def test_migrated_assist_routes_stay_assist_without_provider_conversion():
    migrated = {"mode": "assist", "provider": "xai", "model": "grok-voice-3.0", "voice": "eve", "instructions": "Kitchen"}
    plain = {"mode": "assist"}
    entry = SimpleNamespace(data={SLOTS: {"primary": migrated, "secondary": plain}})
    entries = Entries([entry])
    first, second = get_slot(entry, "primary"), get_slot(entry, "secondary")
    assert (first.mode, first.provider, first.instructions) == ("assist", "xai", "Kitchen")
    assert second.mode == "assist"
    assert entry.data[SLOTS] == {"primary": migrated, "secondary": plain}
    assert entries.updates == 0


async def test_assist_slot_never_starts_a_provider_session():
    entry = SimpleNamespace(data={SLOTS: {"primary": {"mode": "assist", "provider": "openai"}}})
    session = SimpleNamespace(device=SimpleNamespace(slot=lambda slot: None))
    controller = RealtimeController(SimpleNamespace(), SimpleNamespace())
    with pytest.raises(RealtimeProtocolError) as error:
        await controller.start(SimpleNamespace(), session, entry, {"slot_id": "primary", "trigger": "button"})
    assert error.value.code == "slot_disabled"
    assert not controller.sessions and not controller.pending


async def test_entry_setup_keeps_saved_openai_key_and_slots(hass, monkeypatch):
    import custom_components.tacktab as integration
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.tacktab import async_setup_entry

    slots = {"primary": {"mode": "assist", "provider": "xai", "model": "grok-voice-3.0", "voice": "eve"}}
    entry = MockConfigEntry(domain="tacktab", data={"voice_bridge_bootstrap": True, KEYS: {"openai": "keep"}, SLOTS: slots})
    entry.add_to_hass(hass)
    monkeypatch.setattr(integration, "async_setup_voice_entry", AsyncMock(return_value=True))
    assert await async_setup_entry(hass, entry)
    assert dict(entry.data[KEYS]) == {"openai": "keep"}
    assert dict(entry.data[SLOTS]) == slots
