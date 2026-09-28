"""Screen brightness, 0-100 %."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TacktabConfigEntry
from .entity import TacktabEntity


async def async_setup_entry(hass: HomeAssistant, entry: TacktabConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    add([TacktabBrightness(entry.runtime_data)])


class TacktabBrightness(TacktabEntity, NumberEntity):
    _attr_translation_key = "brightness"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "brightness")

    @property
    def native_value(self) -> float | None:
        # null means Tacktab follows the system brightness.
        return self.coordinator.data.get("brightness")

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.command(lambda: self.coordinator.client.brightness(round(value)))
