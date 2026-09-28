"""Screen on/off.

Off switches the display off when Tacktab may (device owner or device admin),
otherwise it blacks the screen out and dims it. The `off_mode` attribute says
which. A tap or "on" wakes it.
"""

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

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        mode = self.coordinator.data.get("screen_off")
        return None if mode is None else {"off_mode": mode}
