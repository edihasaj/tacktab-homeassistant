"""Feature-gated Home Assistant setup for native Tacktab voice entries."""

from __future__ import annotations

from importlib.metadata import version as package_version
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState, SOURCE_SYSTEM
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .voice_bridge import VoiceDevice, async_get_bridge
from .voice_protocol import parse_slots

VOICE_ENTRY_KEY = "voice_install_id"
VOICE_ENTRY_PLATFORM = "voice_platform"
VOICE_ENTRY_SLOTS = "voice_slots"
VOICE_BOOTSTRAP_KEY = "voice_bridge_bootstrap"


def native_voice_supported() -> bool:
    """Return whether this Core can provide the official satellite interfaces."""
    try:
        current = tuple(int(part) for part in package_version("homeassistant").split(".")[:2])
    except Exception:
        return False
    return current >= (2026, 10)


def is_voice_entry(entry: ConfigEntry) -> bool:
    return VOICE_ENTRY_KEY in entry.data or entry.data.get(VOICE_BOOTSTRAP_KEY) is True


async def async_ensure_voice_entry(hass: HomeAssistant, msg: dict[str, Any]) -> ConfigEntry:
    """Create the free voice-only entry on first administrator pairing."""
    install_id = msg["install_id"]
    unique_id = f"voice:{install_id}"
    existing = hass.config_entries.async_entry_for_domain_unique_id("tacktab", unique_id)
    if existing is not None:
        if existing.state is not ConfigEntryState.LOADED or not hasattr(existing, "runtime_data"):
            raise RuntimeError("Voice device entry is not ready")
        return existing
    entry = ConfigEntry(
        data={
            VOICE_ENTRY_KEY: install_id,
            VOICE_ENTRY_PLATFORM: msg["platform"],
            VOICE_ENTRY_SLOTS: msg["slots"],
        },
        discovery_keys=MappingProxyType({}),
        domain="tacktab",
        minor_version=1,
        options={},
        source=SOURCE_SYSTEM,
        subentries_data=(),
        title=msg["name"],
        unique_id=unique_id,
        version=1,
    )
    await hass.config_entries.async_add(entry)
    if entry.state is not ConfigEntryState.LOADED or not hasattr(entry, "runtime_data"):
        raise RuntimeError("Voice device entry did not load")
    return entry


async def async_setup_voice_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an independent voice-only config entry and its real HA entities."""
    if not native_voice_supported():
        return False
    # Load components while Home Assistant is setting up the bridge entry, not
    # from a later websocket request after the HTTP router has frozen.
    for component in ("intent", "assist_pipeline", "assist_satellite"):
        if not await async_setup_component(hass, component, {}):
            return False
    if entry.data.get(VOICE_BOOTSTRAP_KEY) is True:
        # This entry exists solely so a fresh install loads the integration and
        # registers the authenticated websocket pairing command. Pairing later
        # creates one normal, per-tablet voice entry.
        entry.runtime_data = None
        return True
    bridge = async_get_bridge(hass)
    install_id = entry.data[VOICE_ENTRY_KEY]
    device = bridge.devices.get(install_id)
    if device is None:
        device = VoiceDevice(
            hass,
            install_id,
            entry.title,
            entry.data[VOICE_ENTRY_PLATFORM],
            parse_slots(entry.data[VOICE_ENTRY_SLOTS]),
        )
        bridge.devices[install_id] = device
    device.entry = entry  # type: ignore[attr-defined]
    entry.runtime_data = device
    from homeassistant.helpers import device_registry as dr
    from .const import DOMAIN
    from .voice_timers import VoiceTimerAdapter

    registry_entry = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"voice:{install_id}")},
        manufacturer="Applifyer",
        model="Tacktab",
        name=device.name,
    )
    adapter = VoiceTimerAdapter(hass, device, registry_entry.id)
    device.device_id = registry_entry.id
    adapter.async_start()
    device.timer_adapter = adapter  # type: ignore[attr-defined]
    entry.async_on_unload(adapter.close)
    from homeassistant.const import Platform

    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SELECT, Platform.ASSIST_SATELLITE])
    return True


async def async_unload_voice_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload only voice entities; HTTP-control entries are unrelated."""
    from homeassistant.const import Platform

    if entry.data.get(VOICE_BOOTSTRAP_KEY) is True:
        return True
    device: VoiceDevice = entry.runtime_data
    if device.session is not None:
        await device.session.close("unloaded")
    return await hass.config_entries.async_unload_platforms(entry, [Platform.SELECT, Platform.ASSIST_SATELLITE])
