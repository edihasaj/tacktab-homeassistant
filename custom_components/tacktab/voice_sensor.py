"""Voice turn sensor: which tablet is talking right now, and with which slot."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .voice_bridge import VoiceDevice
from .voice_context import async_device_identity

STATE_IDLE = "idle"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    """Add one voice-turn sensor per tablet device."""
    device: VoiceDevice = entry.runtime_data
    add([TacktabVoiceTurnSensor(device)])


class TacktabVoiceTurnSensor(SensorEntity):
    """Report the tablet's current voice turn.

    An Assist satellite entity is idle while a realtime conversation runs, so
    automations that need to know which tablet is speaking read this sensor.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "voice_turn"
    _attr_should_poll = False
    _attr_icon = "mdi:microphone-message"

    def __init__(self, device: VoiceDevice) -> None:
        self.device = device
        self._attr_unique_id = f"voice:{device.install_id}:turn"
        self._remove_listener = None
        self._remove_registry_listener = None

    @property
    def device_info(self) -> dict[str, Any]:
        return {"identifiers": {(DOMAIN, f"voice:{self.device.install_id}")}}

    @property
    def native_value(self) -> str:
        activity = self.device.active_activity
        return STATE_IDLE if activity is None else activity["kind"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        activity = self.device.active_activity
        name, area = async_device_identity(self.hass, self.device.device_id, self.device.name)
        return {
            "device_id": self.device.device_id,
            "device_name": name,
            "area": area,
            "slot_id": None if activity is None else activity["slot_id"],
            "active": activity is not None,
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_listener = self.device.async_add_activity_listener(self._activity_changed)
        # Area and name come from the device registry; keep them current when a
        # user moves the tablet without reloading the integration.
        from homeassistant.helpers import device_registry as dr

        @callback
        def registry_updated(event) -> None:
            if event.data.get("device_id") == self.device.device_id:
                self.async_write_ha_state()

        self._remove_registry_listener = self.hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED, registry_updated
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        if self._remove_registry_listener is not None:
            self._remove_registry_listener()
            self._remove_registry_listener = None
        await super().async_will_remove_from_hass()

    @callback
    def _activity_changed(self) -> None:
        self.async_write_ha_state()
