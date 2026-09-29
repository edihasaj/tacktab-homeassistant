"""Base entity for Tacktab."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TacktabCoordinator


class TacktabEntity(CoordinatorEntity[TacktabCoordinator]):
    """An entity that belongs to one tablet."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: TacktabCoordinator, key: str) -> None:
        super().__init__(coordinator)
        device_id = coordinator.config_entry.unique_id or coordinator.config_entry.entry_id
        self._attr_unique_id = f"{device_id}_{key}"
        data = coordinator.data or {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=data.get("name") or coordinator.config_entry.title,
            manufacturer="Applifyer",
            model=data.get("model"),
            sw_version=data.get("version"),
            configuration_url="https://tacktab.com/help/",
        )
