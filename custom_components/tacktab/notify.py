"""Optional spoken announcements for supported Tacktab tablets."""

from __future__ import annotations

from typing import override

from homeassistant.components.notify import NotifyEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TacktabConfigEntry, TacktabCoordinator
from .entity import TacktabEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TacktabConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the notifier when the tablet supports announcements."""
    coordinator = entry.runtime_data
    added = False

    @callback
    def async_add_announcements_if_supported() -> None:
        """Add the entity once a tablet reports the capability."""
        nonlocal added
        if added or "announcements_enabled" not in (coordinator.data or {}):
            return
        added = True
        async_add_entities([TacktabAnnouncements(coordinator)])

    entry.async_on_unload(coordinator.async_add_listener(async_add_announcements_if_supported))
    async_add_announcements_if_supported()


class TacktabAnnouncements(TacktabEntity, NotifyEntity):
    """Speak messages when the user has enabled announcements in Tacktab."""

    _attr_translation_key = "announcements"

    def __init__(self, coordinator: TacktabCoordinator) -> None:
        TacktabEntity.__init__(self, coordinator, "announcements")
        NotifyEntity.__init__(self)

    @property
    def available(self) -> bool:
        """Show unavailable until the tablet owner opts in on the tablet."""
        return super().available and (self.coordinator.data or {}).get("announcements_enabled") is True

    @override
    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Speak a message through the tablet's configured voice."""
        if (self.coordinator.data or {}).get("announcements_enabled") is not True:
            raise HomeAssistantError("Announcements are disabled in Tacktab")
        await self.coordinator.command(lambda: self.coordinator.client.announce(message))
