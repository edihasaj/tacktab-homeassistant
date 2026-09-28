"""Reload the page."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TacktabConfigEntry
from .entity import TacktabEntity


async def async_setup_entry(hass: HomeAssistant, entry: TacktabConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    add([TacktabReload(entry.runtime_data)])


class TacktabReload(TacktabEntity, ButtonEntity):
    _attr_translation_key = "reload"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "reload")

    async def async_press(self) -> None:
        await self.coordinator.command(self.coordinator.client.reload)
