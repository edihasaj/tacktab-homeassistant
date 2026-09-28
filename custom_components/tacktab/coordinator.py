"""Polls a Tacktab tablet for its status."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TacktabAuthError, TacktabClient, TacktabError
from .const import DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

type TacktabConfigEntry = ConfigEntry[TacktabCoordinator]


class TacktabCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Keeps the latest status of one tablet."""

    config_entry: TacktabConfigEntry

    def __init__(self, hass: HomeAssistant, entry: TacktabConfigEntry, client: TacktabClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self.client = client

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.client.status()
        except TacktabAuthError as err:
            raise ConfigEntryAuthFailed from err
        except TacktabError as err:
            raise UpdateFailed(f"Tacktab is not reachable: {err}") from err

    async def command(self, call: Callable[[], Awaitable[dict[str, Any]]]) -> None:
        """Run a command; its answer is the new status, so no extra poll is needed."""
        try:
            self.async_set_updated_data(await call())
        except TacktabAuthError as err:
            raise ConfigEntryAuthFailed from err
        except TacktabError as err:
            raise UpdateFailed(f"Tacktab did not accept the command: {err}") from err
