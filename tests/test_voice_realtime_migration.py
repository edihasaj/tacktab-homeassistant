"""Retired provider persistence and runtime rejection regressions."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tacktab import async_setup_entry
from custom_components.tacktab.voice_realtime import RealtimeController
from custom_components.tacktab.voice_realtime_catalog import async_catalog
from custom_components.tacktab.voice_realtime_config import (
    KEYS, SLOTS, RealtimeConfigError, api_key, async_prepare_provider_store,
    get_slot, key_status, migrate_removed_provider, parse_slot_config, update_key,
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


def test_fresh_slot_defaults_to_assist_with_inactive_xai_settings():
    fresh = parse_slot_config({})
    assert fresh.mode == "assist"
    assert (fresh.provider, fresh.model, fresh.voice) == ("xai", "grok-voice-3.0", "eve")


@pytest.mark.parametrize("provider", ["openai", None])
def test_legacy_slot_returns_to_assist_preserving_pipeline_and_preferences(provider):
    old = {"mode": "realtime", "model": "gpt-realtime", "voice": "marin", "instructions": "Kitchen", "language": "en", "llm_api_ids": ["assist"], "follow_up_timeout_ms": 5_000, "memory_timeout_ms": 60_000}
    if provider is not None:
        old["provider"] = provider
    secondary = {"mode": "realtime", "provider": "gemini", "model": "models/custom", "voice": "Kore"}
    voice_slots = [{"slot_id": "primary", "pipeline_id": "local-pipeline", "wake_word": {"id": "okay"}, "output": "remote", "media_player_entity_id": "media_player.kitchen"}]
    entry = SimpleNamespace(data={SLOTS: {"primary": old, "secondary": secondary}, "voice_slots": voice_slots})
    entries = Entries([entry])
    hass = SimpleNamespace(config_entries=entries)

    migrate_removed_provider(hass, entry)

    migrated = entry.data[SLOTS]["primary"]
    assert migrated == {**old, "mode": "assist", "provider": "xai", "model": "grok-voice-3.0", "voice": "eve"}
    assert get_slot(entry, "primary").mode == "assist"
    assert entry.data[SLOTS]["secondary"] == secondary
    assert entry.data["voice_slots"] == voice_slots
    migrate_removed_provider(hass, entry)
    assert entries.updates == 1


async def test_entry_setup_purges_shared_and_legacy_keys_before_voice_setup(hass, monkeypatch):
    import custom_components.tacktab as integration

    bootstrap = MockConfigEntry(domain="tacktab", data={"voice_bridge_bootstrap": True, KEYS: {"openai": "retired-shared", "xai": "keep-shared"}})
    tablet = MockConfigEntry(domain="tacktab", data={"voice_install_id": "tablet", KEYS: {"openai": "retired-tablet", "gemini": "keep-tablet"}, SLOTS: {"primary": {"provider": "openai", "mode": "realtime"}}})
    bootstrap.add_to_hass(hass)
    tablet.add_to_hass(hass)
    setup = AsyncMock(return_value=True)
    monkeypatch.setattr(integration, "async_setup_voice_entry", setup)

    assert await async_setup_entry(hass, bootstrap)

    assert dict(bootstrap.data[KEYS]) == {"xai": "keep-shared"}
    assert dict(tablet.data[KEYS]) == {"gemini": "keep-tablet"}
    assert get_slot(tablet, "primary").mode == "assist"
    setup.assert_awaited_once_with(hass, bootstrap)


async def test_preparing_store_discards_retired_keys_and_migrates_supported_keys(monkeypatch):
    bootstrap = SimpleNamespace(data={"voice_bridge_bootstrap": True, KEYS: {"openai": "retired", "xai": "shared"}})
    tablet = SimpleNamespace(data={KEYS: {"openai": "retired-legacy", "xai": "legacy", "gemini": "preserved"}})
    hass = SimpleNamespace(config_entries=Entries([bootstrap, tablet]))
    monkeypatch.setattr("custom_components.tacktab.voice_setup.async_ensure_voice_bootstrap_entry", AsyncMock(return_value=bootstrap))

    assert await async_prepare_provider_store(hass, tablet) is bootstrap

    assert bootstrap.data[KEYS] == {"xai": "shared", "gemini": "preserved"}
    assert KEYS not in tablet.data
    assert key_status(tablet, "gemini", hass) == {"provider": "gemini", "key_configured": True}


@pytest.mark.parametrize("operation", [
    lambda entry: parse_slot_config({"mode": "realtime", "provider": "openai"}),
    lambda entry: key_status(entry, "openai"),
    lambda entry: api_key(entry, "openai"),
    lambda entry: update_key(SimpleNamespace(), entry, "openai", "replace", "new-key"),
])
def test_retired_provider_configuration_and_key_access_are_rejected(operation):
    entry = SimpleNamespace(data={KEYS: {"openai": "retired"}})
    original = deepcopy(entry.data)
    with pytest.raises(RealtimeConfigError, match="Invalid"):
        operation(entry)
    assert entry.data == original


@pytest.mark.parametrize("command, fields", [
    ("catalog", {"provider": "openai"}),
    ("configure_provider", {"provider": "openai", "key_action": "replace", "api_key": "must-not-echo"}),
])
def test_old_provider_commands_fail_closed(command, fields):
    with pytest.raises(RealtimeProtocolError) as error:
        parse_command({"id": 1, "type": f"tacktab/realtime/{command}", "epoch": "epoch", **fields})
    assert error.value.code == "invalid_format"
    assert error.value.message == "Invalid realtime message"


@pytest.mark.parametrize("provider", ["openai", "unknown"])
async def test_unknown_catalog_does_not_read_keys_or_call_provider(provider, monkeypatch):
    fetch = AsyncMock()
    monkeypatch.setattr("custom_components.tacktab.voice_realtime_catalog._get_json", fetch)
    with pytest.raises(RealtimeConfigError, match="Invalid realtime provider"):
        await async_catalog(object(), SimpleNamespace(data={}), provider)
    fetch.assert_not_awaited()


async def test_old_slot_command_is_rejected_without_mutation():
    import asyncio

    entry = SimpleNamespace(data={SLOTS: {"primary": {"mode": "assist", "provider": "gemini"}}})
    device = SimpleNamespace(entry=entry, config_lock=asyncio.Lock(), config_revision=3, slot=lambda slot: None)
    session = SimpleNamespace(device=device, validate_epoch=lambda msg: None)
    bridge = SimpleNamespace(session_for_connection=lambda connection: session)
    controller = RealtimeController(SimpleNamespace(), bridge)
    connection = SimpleNamespace(user=SimpleNamespace(is_admin=True))
    original = deepcopy(entry.data)
    with pytest.raises(RealtimeProtocolError) as error:
        await controller.handle(connection, {"id": 1, "type": "tacktab/realtime/configure_slot", "epoch": "epoch", "slot_id": "primary", "expected_config_revision": 3, "config": {"mode": "realtime", "provider": "openai"}})
    assert error.value.code == "invalid_format"
    assert entry.data == original
    assert device.config_revision == 3


@pytest.mark.parametrize("provider", ["openai", None])
async def test_legacy_realtime_start_never_acquires_lease_or_opens_another_provider(provider):
    raw = {"mode": "realtime"}
    if provider is not None:
        raw["provider"] = provider
    entry = SimpleNamespace(data={SLOTS: {"primary": raw}})
    device = SimpleNamespace(slot=lambda slot: None)
    session = SimpleNamespace(device=device)
    controller = RealtimeController(SimpleNamespace(), SimpleNamespace())

    with pytest.raises(RealtimeProtocolError) as error:
        await controller.start(SimpleNamespace(), session, entry, {"slot_id": "primary", "trigger": "button"})

    assert error.value.code == "slot_disabled"
    assert not controller.sessions and not controller.pending
