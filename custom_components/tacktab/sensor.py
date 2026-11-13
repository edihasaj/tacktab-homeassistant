"""Battery level and app version."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TacktabConfigEntry
from .entity import TacktabEntity


async def async_setup_entry(hass: HomeAssistant, entry: TacktabConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    from .voice_setup import is_voice_entry

    if is_voice_entry(entry):
        # Voice entries own a separate tablet device with its own turn sensor.
        from .voice_sensor import async_setup_entry as async_setup_voice_sensors

        await async_setup_voice_sensors(hass, entry, add)
        return
    add([TacktabBattery(entry.runtime_data), TacktabVersion(entry.runtime_data)])


class TacktabBattery(TacktabEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "battery")

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.get("battery")


class TacktabVersion(TacktabEntity, SensorEntity):
    _attr_translation_key = "version"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "version")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.get("version")
