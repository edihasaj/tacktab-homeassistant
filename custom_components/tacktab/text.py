"""The page Tacktab shows. Setting it opens that address."""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TacktabConfigEntry
from .entity import TacktabEntity


async def async_setup_entry(hass: HomeAssistant, entry: TacktabConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    add([TacktabPage(entry.runtime_data)])


class TacktabPage(TacktabEntity, TextEntity):
    _attr_translation_key = "page"
    _attr_native_max = 2048

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "page")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.get("url")

    async def async_set_value(self, value: str) -> None:
        await self.coordinator.command(lambda: self.coordinator.client.load(value))
