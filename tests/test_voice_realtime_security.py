"""Regression tests for server-only realtime provider credentials."""
from __future__ import annotations

from types import SimpleNamespace

from custom_components.tacktab.voice_realtime_config import KEYS, key_status, key_transport_allowed, update_key
from custom_components.tacktab.voice_setup import async_ensure_voice_bootstrap_entry


class Entries:
    def __init__(self, entries): self.entries = entries
    def async_entries(self, domain): return self.entries
    def async_update_entry(self, entry, *, data): entry.data = data


def test_legacy_per_tablet_key_migrates_to_one_bootstrap_store_without_returning_value():
    bootstrap = SimpleNamespace(data={"voice_bridge_bootstrap": True})
    tablet = SimpleNamespace(data={KEYS: {"openai": "legacy-secret"}})
    hass = SimpleNamespace(config_entries=Entries([bootstrap, tablet]))
    status = key_status(tablet, "openai", hass)
    assert status == {"provider": "openai", "key_configured": True}
    assert bootstrap.data[KEYS]["openai"] == "legacy-secret"
    assert KEYS not in tablet.data


def test_key_mutation_requires_verified_secure_or_explicit_ha_local_opt_in():
    bootstrap = SimpleNamespace(data={"voice_bridge_bootstrap": True})
    hass = SimpleNamespace(config_entries=Entries([bootstrap]))
    def connection(*, secure, remote):
        request = SimpleNamespace(secure=secure, remote=remote)
        class Handler:
            def __init__(self): self._request = request
            def send(self, message): return None
        return SimpleNamespace(send_message=Handler().send)
    untrusted = SimpleNamespace(data={})
    assert key_transport_allowed(hass, connection(secure=True, remote="8.8.8.8"), untrusted)
    assert not key_transport_allowed(hass, connection(secure=False, remote="8.8.8.8"), untrusted)
    bootstrap.data["realtime_trusted_local_http"] = True
    assert key_transport_allowed(hass, connection(secure=False, remote="192.168.1.9"), untrusted)
    assert not key_transport_allowed(hass, connection(secure=False, remote="8.8.8.8"), untrusted)


def test_replace_key_writes_shared_store_and_returns_only_status():
    bootstrap = SimpleNamespace(data={"voice_bridge_bootstrap": True})
    tablet = SimpleNamespace(data={})
    hass = SimpleNamespace(config_entries=Entries([bootstrap, tablet]))
    result = update_key(hass, tablet, "gemini", "replace", "server-only-key")
    assert result == {"provider": "gemini", "key_configured": True}
    assert bootstrap.data[KEYS]["gemini"] == "server-only-key"
    assert "server-only-key" not in str(result)


async def test_bootstrap_initializer_is_singleton_and_defaults_http_trust_off(hass):
    first = await async_ensure_voice_bootstrap_entry(hass)
    second = await async_ensure_voice_bootstrap_entry(hass)
    assert first is second
    assert first.data["voice_bridge_bootstrap"] is True
    assert first.options.get("realtime_trusted_local_http", False) is False
