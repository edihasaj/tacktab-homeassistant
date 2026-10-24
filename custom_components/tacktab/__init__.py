"""The Tacktab integration: control a Tacktab kiosk tablet from Home Assistant."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import config_validation as cv

from .api import TacktabClient
from .coordinator import TacktabConfigEntry, TacktabCoordinator
from .const import DOMAIN
from .voice_setup import async_setup_voice_entry, async_unload_voice_entry, is_voice_entry, native_voice_supported

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NOTIFY,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]


async def async_setup_entry(hass: HomeAssistant, entry: TacktabConfigEntry) -> bool:
    if is_voice_entry(entry):
        return await async_setup_voice_entry(hass, entry)
    client = TacktabClient(
        async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_PASSWORD]
    )
    coordinator = TacktabCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TacktabConfigEntry) -> bool:
    if is_voice_entry(entry):
        return await async_unload_voice_entry(hass, entry)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register native voice only on the Core release that exposes its APIs."""
    if native_voice_supported():
        from .voice_ws import async_register_voice_websocket_api

        async_register_voice_websocket_api(hass)
    return True
