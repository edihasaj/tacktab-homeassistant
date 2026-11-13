"""Per-tablet fleet profile controls: opt in, choose groups, see the revision."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .voice_bridge import VoiceDevice
from .voice_profile import GROUPS
from .voice_profile_manager import VoiceProfileManager, async_get_profile_manager


class _FleetEntity:
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, device: VoiceDevice, manager: VoiceProfileManager) -> None:
        self.entry = entry
        self.device = device
        self.manager = manager
        self._remove_listener = None

    @property
    def device_info(self) -> dict[str, Any]:
        return {"identifiers": {(DOMAIN, f"voice:{self.device.install_id}")}}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()  # type: ignore[misc]
        self._remove_listener = self.manager.async_add_listener(self.async_write_ha_state)  # type: ignore[attr-defined]

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        await super().async_will_remove_from_hass()  # type: ignore[misc]


class TacktabFleetFollowSwitch(_FleetEntity, SwitchEntity):
    """Master opt-in: this tablet follows (and may push to) the fleet profile."""

    _attr_icon = "mdi:tablet-dashboard"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, device: VoiceDevice, manager: VoiceProfileManager) -> None:
        super().__init__(entry, device, manager)
        self._attr_unique_id = f"voice:{device.install_id}:fleet_follow"
        self._attr_name = "Follow fleet voice profile"

    @property
    def is_on(self) -> bool:
        return self.manager.follow_state(self.device)[0]

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.manager.async_set_follow(self.entry, True, self.manager.follow_state(self.device)[1])

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.manager.async_set_follow(self.entry, False, self.manager.follow_state(self.device)[1])


class TacktabFleetGroupSwitch(_FleetEntity, SwitchEntity):
    """Whether this tablet follows one group of the fleet profile."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry: ConfigEntry, device: VoiceDevice, manager: VoiceProfileManager, group: str) -> None:
        super().__init__(entry, device, manager)
        self.profile_group = group
        self._attr_unique_id = f"voice:{device.install_id}:fleet_group:{group}"
        self._attr_name = f"Follow fleet {group}"

    @property
    def is_on(self) -> bool:
        return self.profile_group in self.manager.follow_state(self.device)[1]

    async def _set(self, on: bool) -> None:
        enabled, groups = self.manager.follow_state(self.device)
        wanted = {*groups, self.profile_group} if on else set(groups) - {self.profile_group}
        await self.manager.async_set_follow(self.entry, enabled, tuple(wanted))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class TacktabFleetProfileSensor(_FleetEntity, SensorEntity):
    """The fleet profile revision and who last wrote it."""

    _attr_icon = "mdi:cloud-sync-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ConfigEntry, device: VoiceDevice, manager: VoiceProfileManager) -> None:
        super().__init__(entry, device, manager)
        self._attr_unique_id = f"voice:{device.install_id}:fleet_revision"
        self._attr_name = "Fleet voice profile revision"

    @property
    def native_value(self) -> int:
        return self.manager.doc["revision"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        session = self.device.session
        enabled, groups = self.manager.follow_state(self.device)
        return {
            "updated_at": self.manager.doc["updated_at"],
            "updated_by": self.manager.doc["updated_by"],
            "following": enabled,
            "followed_groups": list(groups),
            "app_supported": bool(session is not None and not session.closed and getattr(session, "profile_sync", False)),
        }


async def async_build_fleet_entities(hass: HomeAssistant, entry: ConfigEntry) -> tuple[list[SwitchEntity], list[SensorEntity]]:
    manager = await async_get_profile_manager(hass)
    device: VoiceDevice = entry.runtime_data
    switches: list[SwitchEntity] = [TacktabFleetFollowSwitch(entry, device, manager)]
    switches += [TacktabFleetGroupSwitch(entry, device, manager, group) for group in GROUPS]
    return switches, [TacktabFleetProfileSensor(entry, device, manager)]
