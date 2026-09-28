"""Screen on/off. Off blacks the screen out and dims it; a tap or "on" wakes it."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TacktabConfigEntry
from .entity import TacktabEntity


async def async_setup_entry(hass: HomeAssistant, entry: TacktabConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    add([TacktabScreen(entry.runtime_data)])


class TacktabScreen(TacktabEntity, SwitchEntity):
    _attr_translation_key = "screen"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "screen")

    @property
    def is_on(self) -> bool | None:
        screen = self.coordinator.data.get("screen")
        return None if screen is None else screen == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.command(self.coordinator.client.wake)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.command(self.coordinator.client.sleep)
